"""Persisted queue and bounded generation workers; waiting sessions use no slot."""
from __future__ import annotations

import asyncio
import json
import mimetypes
import uuid
from datetime import timedelta

from backend.wire.redaction import redact_json,scrub_text
from .capture import Capture
from .events import EventStore
from .runs import TERMINAL
from .service import new_id
from .storage import digest,guarded


class Worker:
    def __init__(self,service,runtime,*,timeout_factory=asyncio.timeout):
        self.service,self.runtime=service,runtime
        self.timeout=timeout_factory
        self.repo=service.repo;self.owner=uuid.uuid4().hex
        self.active={};self.closing=False;self.signals={}
        self.service._control_notifier=self.signal

    def capture(self,run):
        cfg=self.service.settings
        return Capture(requested=run['input_snapshot']['capture']['policy']['requested'],
                       server_max=cfg.octagon.wire_capture_max_policy,
                       source_capability='full' if cfg.octagon.wire_capture_enabled else 'off',
                       max_bytes=cfg.skill_eval.event_max_bytes)

    async def renew_lease(self,job):
        while True:
            await asyncio.sleep(max(0.1,self.service.settings.skill_eval.lease_seconds/3))
            with self.repo.transaction() as c:
                lease=(self.service.clock()+timedelta(seconds=self.service.settings.skill_eval.lease_seconds)).isoformat()
                changed=c.execute("UPDATE skill_eval_jobs SET lease_until=? WHERE id=? AND owner=? AND state='running'",(lease,job['id'],self.owner)).rowcount
                if changed!=1:return

    def signal(self,run_id):
        self.signals.setdefault(run_id,asyncio.Event()).set()

    def claim(self):
        self.service._writable(execute=True)
        timestamp=self.service._time()
        with self.repo.transaction() as c:
            row=c.execute("""SELECT j.* FROM skill_eval_jobs j WHERE j.state='pending'
                AND (j.next_retry_at IS NULL OR j.next_retry_at<=?)
                AND EXISTS (SELECT 1 FROM skill_eval_runs r JOIN skill_eval_skills s ON s.id=r.skill_id WHERE r.id=j.run_id AND s.scope_id=?)
                AND NOT EXISTS (SELECT 1 FROM skill_eval_jobs active WHERE active.run_id=j.run_id AND active.state IN ('running','reconciling'))
                ORDER BY j.rowid LIMIT 1""",(timestamp,self.service.scope)).fetchone()
            if not row:return None
            lease=(self.service.clock()+timedelta(seconds=self.service.settings.skill_eval.lease_seconds)).isoformat()
            c.execute("UPDATE skill_eval_jobs SET state='running',owner=?,lease_until=? WHERE id=? AND state='pending'",(self.owner,lease,row['id']))
            return dict(c.execute('SELECT * FROM skill_eval_jobs WHERE id=?',(row['id'],)).fetchone())

    def checkpoint(self,job,**fields):
        checkpoint=json.loads(job.get('checkpoint_json') or '{}');checkpoint.update(fields)
        with self.repo.transaction() as c:
            changed=c.execute('UPDATE skill_eval_jobs SET checkpoint_json=? WHERE id=? AND owner=?',(json.dumps(checkpoint),job['id'],self.owner)).rowcount
            if changed!=1:raise RuntimeError('Worker lease ownership was lost')
        job['checkpoint_json']=json.dumps(checkpoint)

    def refs(self,id_,**changes):
        with self.repo.transaction() as c:
            row=self.service._run(c,id_)
            refs=json.loads(row['external_refs_json']);refs.update(changes)
            c.execute('UPDATE skill_eval_runs SET external_refs_json=?,revision=revision+1 WHERE id=?',(json.dumps(refs),id_))

    def observe_model(self,run_id,kind,raw,event_id):
        payload=raw.get('payload',raw)
        if kind!='loop:turn' or not isinstance(payload,dict) or payload.get('loop_name')!='root':return
        model=payload.get('model')
        if not isinstance(model,str) or not model or len(model)>300 or any(ord(ch)<32 for ch in model):return
        with self.repo.transaction() as c:
            run=self.service._run(c,run_id);refs=json.loads(run['external_refs_json'])
            observed=dict(refs.get('observed_root_models',{}));observed.setdefault(model,event_id);refs['observed_root_models']=observed
            provider=None;canonical=model;verified=False
            if len(observed)==1:
                if '::' in model:provider,canonical=model.split('::',1);verified=bool(provider and canonical)
                else:
                    session_model=refs.get('session_model') or ''
                    if '::' in session_model:
                        provider,configured=session_model.split('::',1);verified=configured==model
                        if not verified:provider=None
            else:canonical=None
            group=('resolved:' if verified else 'requested/unverified:')+json.dumps(['blade-agent',provider or run['requested_provider'],canonical or run['requested_model'],sorted(observed) if not verified else []],separators=(',',':'))
            c.execute('UPDATE skill_eval_runs SET resolved_model=?,resolved_provider=?,model_resolution_source=?,group_key=?,external_refs_json=? WHERE id=?',
                (canonical,provider,'loop:turn.root+session.model' if verified else 'loop:turn.root_provider_unconfirmed',group,json.dumps(refs),run_id))

    async def tick(self):
        if self.closing:return False
        job=self.claim()
        if not job:return False
        await self.execute(job)
        return True

    async def execute(self,job):
        id_=job['run_id']
        renewal=asyncio.create_task(self.renew_lease(job))
        try:
            run=self.service.get_run(id_)
            if job['phase'] in ('prepare','message') and (run['finish_requested_at'] or run['run_status'] in TERMINAL):
                self._cancel_unsent(job)
            elif job['phase']=='prepare':
                await self.prepare(job,run)
            elif job['phase']=='message':
                await self.deliver(job,run)
            elif job['phase']=='observe':
                await self.observe(job,run)
            elif job['phase'] in ('finish','collect'):
                await self.collect(job,run)
            else:raise ValueError('Unknown job phase')
            with self.repo.transaction() as c:
                c.execute("UPDATE skill_eval_jobs SET state='completed',owner=NULL,lease_until=NULL WHERE id=? AND owner=?",(job['id'],self.owner))
        except asyncio.CancelledError:
            self._uncertain(job,'worker_shutdown')
            raise
        except Exception as error:
            # Error text can contain a remote response body. Retain only a
            # bounded scrubbed message, never a traceback containing inputs.
            self._uncertain(job,type(error).__name__,'Remote operation failed; inspect the persisted phase and acceptance state')
        finally:
            renewal.cancel()
            await asyncio.gather(renewal,return_exceptions=True)

    def _cancel_unsent(self,job):
        message_id=json.loads(job['payload_json']).get('message_id')
        if message_id:
            with self.repo.transaction() as c:
                c.execute("UPDATE skill_eval_messages SET delivery_state='cancelled' WHERE id=? AND delivery_state='pending'",(message_id,))

    def _uncertain(self,job,code,message=''):
        checkpoint=json.loads(job.get('checkpoint_json') or '{}')
        accepted=checkpoint.get('remote_started',False)
        with self.repo.transaction() as c:
            changed=c.execute("UPDATE skill_eval_jobs SET state=?,last_error=?,owner=NULL,lease_until=NULL WHERE id=? AND owner=?",('acceptance_unknown' if accepted else 'failed',code,job['id'],self.owner)).rowcount
            if changed!=1:return
            c.execute("UPDATE skill_eval_runs SET run_status='failed',generation_status='failed',error_code=?,error_message=?,ended_at=?,capture_status='partial',revision=revision+1 WHERE id=?",
                      ('acceptance_unknown' if accepted else code,message,self.service._time(),job['run_id']))
            message_id=json.loads(job['payload_json']).get('message_id')
            if message_id:c.execute("UPDATE skill_eval_messages SET delivery_state=? WHERE id=? AND delivery_state!='sent'",('unknown' if accepted else 'failed',message_id))

    async def prepare(self,job,run):
        with self.repo.transaction() as c:
            c.execute("UPDATE skill_eval_runs SET run_status='preparing',started_at=coalesce(started_at,?),revision=revision+1 WHERE id=?",(self.service._time(),run['id']))
        refs=run['external_refs']
        if not refs.get('session_id'):
            self.checkpoint(job,phase='create',remote_started=True)
            created=await self.runtime.create(run)
            self.refs(run['id'],**redact_json(created))
            if created.get('session_model') and created['session_model']!=run['requested_model']:
                raise RuntimeError('Remote session model differs from requested model')
            self.checkpoint(job,phase='session_bound',remote_started=False)
            refs['session_id']=created['session_id']
        with self.repo.connection() as c:
            snapshot=self.service._version(c,run['skill_version_id'])
        entries=self.service.store.get_manifest(snapshot['manifest_ref'])
        if not refs.get('skill_upload'):
            self.checkpoint(job,phase='skill_upload',remote_started=True)
            uploaded=await self.runtime.upload_skill(run,refs['session_id'],entries)
            refs['skill_upload']=uploaded;self.refs(run['id'],skill_upload=redact_json(uploaded))
            self.checkpoint(job,phase='skill_uploaded',remote_started=False)
        await self.deliver(job,self.service.get_run(run['id']),initial=True)

    async def deliver(self,job,run,initial=False):
        message_id=json.loads(job['payload_json'])['message_id']
        with self.repo.connection() as c:
            message=self.service._message_result(self.service._row(c,'SELECT * FROM skill_eval_messages WHERE id=? AND run_id=?',(message_id,run['id'])))
        if message['delivery_state'] in ('sent','unknown'):
            raise RuntimeError('Do not replay an accepted or uncertain message')
        session_id=run['external_refs']['session_id']
        uploads=dict(run['external_refs'].get('input_uploads',{}))
        for input_ in message['attachment_refs']:
            if input_['id'] in uploads:continue
            self.checkpoint(job,phase='input_upload',remote_started=True)
            receipt=await self.runtime.upload_input(session_id,input_)
            uploads[input_['id']]={'hash':input_['hash'],'path':input_['remote_path'],'receipt':redact_json(receipt)}
            self.refs(run['id'],input_uploads=uploads)
            self.checkpoint(job,phase='input_uploaded',remote_started=False)
        text=message['text']
        if initial and not text.strip():text=run['input_snapshot']['system_startup_message']
        if message['attachment_refs']:
            text+='\n\n附件路径：\n'+'\n'.join(input_['remote_path'] for input_ in message['attachment_refs'])
        answer=None
        if message['interaction_id']:
            with self.repo.connection() as c:
                interaction=self.service._row(c,'SELECT * FROM skill_eval_interactions WHERE id=?',(message['interaction_id'],))
            answer={'tool_call_id':interaction['remote_tool_call_id'],'selections':message['answer'].get('selections',{}),'custom':message['answer'].get('custom',{})}
            if not any(answer['selections'].values()) and not any(answer['custom'].values()):answer['custom']={'0':text}
        generation_id=new_id()
        with self.repo.transaction() as c:
            current=self.service._run(c,run['id'])
            if current['stop_requested_at'] and not current['finish_requested_at']:
                c.execute("UPDATE skill_eval_messages SET delivery_state='cancelled' WHERE id=?",(message_id,))
                c.execute("UPDATE skill_eval_interactions SET state='pending',answer_message_id=NULL,revision=revision+1 WHERE answer_message_id=? AND state='answering'",(message_id,))
                pending=c.execute("SELECT 1 FROM skill_eval_interactions WHERE run_id=? AND state='pending'",(run['id'],)).fetchone()
                c.execute("UPDATE skill_eval_runs SET run_status=?,generation_status='stopped',stop_requested_at=NULL,revision=revision+1 WHERE id=?",('waiting_input' if pending else 'waiting_continue',run['id']))
                return
            index=c.execute('SELECT coalesce(max(generation_index),0)+1 FROM skill_eval_generations WHERE run_id=?',(run['id'],)).fetchone()[0]
            c.execute("INSERT INTO skill_eval_generations(id,run_id,generation_index,triggering_message_id,status,started_at) VALUES(?,?,?,?,'running',?)",(generation_id,run['id'],index,message_id,self.service._time()))
            c.execute("UPDATE skill_eval_messages SET delivery_state='sending' WHERE id=?",(message_id,))
            c.execute("UPDATE skill_eval_runs SET run_status=CASE WHEN finish_requested_at IS NULL THEN 'active' ELSE 'finishing' END,generation_status='running',capture_status='live',stop_requested_at=NULL,revision=revision+1 WHERE id=?",(run['id'],))
        capture=self.capture(run)
        event_store=EventStore(self.repo,capture)
        self.checkpoint(job,phase='chat_send',remote_started=True,generation_id=generation_id)
        ended=None;remote_id=None;stop_sent=False;last_stop_request=None
        wake=self.signals.setdefault(run['id'],asyncio.Event())
        async def controls():
            nonlocal stop_sent,last_stop_request
            while True:
                wake.clear()
                current=self.service.get_run(run['id'])
                token=(current['finish_requested_at'],current['stop_requested_at'])
                if remote_id and not stop_sent and token!=last_stop_request and any(token):
                    last_stop_request=token
                    try:
                        receipt=await self.runtime.stop(session_id,remote_id)
                        stop_sent=True
                        self.refs(run['id'],last_stop_receipt=redact_json(receipt),last_stop_error=None)
                    except Exception as error:
                        self.refs(run['id'],last_stop_error={'status':'unconfirmed','error_type':type(error).__name__})
                with self.repo.transaction() as c:
                    lease=(self.service.clock()+timedelta(seconds=self.service.settings.skill_eval.lease_seconds)).isoformat()
                    c.execute('UPDATE skill_eval_jobs SET lease_until=? WHERE id=? AND owner=?',(lease,job['id'],self.owner))
                try:await asyncio.wait_for(wake.wait(),timeout=0.2)
                except TimeoutError:pass
        control=asyncio.create_task(controls())
        try:
            async with self.timeout(run['input_snapshot']['generation_timeout_seconds']):
                async for event in self.runtime.chat(run,session_id,text,skill_name=run['external_refs'].get('skill_upload',{}).get('name') if initial or index==1 else None,answer=answer):
                    kind,raw=event['kind'],event['raw']
                    payload=raw.get('payload',raw)
                    stored=event_store.append(run['id'],kind,raw,stream_id=generation_id,source_sequence=raw.get('stream_sequence'),source_id=raw.get('event_id'))
                    self.observe_model(run['id'],kind,raw,stored['event_id'])
                    if kind=='chat:start' and isinstance(payload,dict) and payload.get('chat_run_id'):
                        remote_id=payload['chat_run_id']
                        wake.set()
                        self.refs(run['id'],chat_run_id=remote_id)
                        with self.repo.transaction() as c:
                            c.execute('UPDATE skill_eval_generations SET chat_run_id=? WHERE id=?',(remote_id,generation_id))
                            c.execute("UPDATE skill_eval_messages SET delivery_state='sent',remote_receipt_json=? WHERE id=?",(json.dumps({'chat_run_id':remote_id}),message_id))
                    if kind=='chat:end':ended=raw;break
                if ended is None:raise RuntimeError('Generation ended without a terminal receipt')
        except TimeoutError:
            if remote_id and not stop_sent:
                receipt=await self.runtime.stop(session_id,remote_id)
                self.refs(run['id'],last_stop_receipt=redact_json(receipt))
            raise
        finally:
            control.cancel();await asyncio.gather(control,return_exceptions=True)
        self.checkpoint(job,phase='chat_completed',remote_started=False)
        await self._generation_end(run,generation_id,message,ended,event_store)

    async def _generation_end(self,run,generation_id,message,end,event_store):
        paused=end.get('status')=='paused' or end.get('finish_reason')=='paused'
        pause=end.get('pause_tool_data')
        with self.repo.transaction() as c:
            if paused and isinstance(pause,dict):
                c.execute("INSERT OR IGNORE INTO skill_eval_interactions(id,run_id,remote_tool_call_id,question_json,state,created_at) VALUES(?,?,?,?,'pending',?)",(new_id(),run['id'],pause.get('tool_call_id'),json.dumps(redact_json(pause)),self.service._time()))
            c.execute("UPDATE skill_eval_messages SET delivery_state='sent' WHERE id=?",(message['id'],))
            if message['interaction_id']:
                c.execute("UPDATE skill_eval_interactions SET state='answered' WHERE id=?",(message['interaction_id'],))
            status='paused' if paused else 'stopped' if end.get('finish_reason')=='user_stop' else 'completed'
            c.execute('UPDATE skill_eval_generations SET status=?,ended_at=? WHERE id=?',(status,self.service._time(),generation_id))
            c.execute("UPDATE skill_eval_runs SET run_status=CASE WHEN finish_requested_at IS NOT NULL THEN 'finishing' ELSE ? END,generation_status=?,capture_status='complete',stop_requested_at=NULL,revision=revision+1 WHERE id=?",('waiting_input' if paused else 'waiting_continue',status,run['id']))
        # History is a second evidence source. A failure must not cause chat replay.
        try:
            history=await self.runtime.history(run['external_refs']['session_id'])
            event_store.append(run['id'],'session:history',history,source='history',source_id=generation_id)
        except Exception:
            with self.repo.transaction() as c:c.execute("UPDATE skill_eval_runs SET capture_status='partial' WHERE id=?",(run['id'],))

    async def observe(self,job,run):
        payload=json.loads(job['payload_json'])
        with self.repo.connection() as c:
            message=self.service._message_result(self.service._row(c,'SELECT * FROM skill_eval_messages WHERE id=?',(payload['message_id'],)))
        session_id=run['external_refs']['session_id'];target=run['external_refs'].get('chat_run_id')
        self.checkpoint(job,phase='observe',remote_started=True,generation_id=payload['generation_id'])
        events=EventStore(self.repo,self.capture(run))
        wake=self.signals.setdefault(run['id'],asyncio.Event());stop_sent=False;last_stop_request=None
        async def controls():
            nonlocal stop_sent,last_stop_request
            while True:
                wake.clear();current=self.service.get_run(run['id'])
                token=(current['finish_requested_at'],current['stop_requested_at'])
                if target and not stop_sent and token!=last_stop_request and any(token):
                    last_stop_request=token
                    try:
                        receipt=await self.runtime.stop(session_id,target)
                        stop_sent=True
                        self.refs(run['id'],last_stop_receipt=redact_json(receipt),last_stop_error=None)
                    except Exception as error:
                        self.refs(run['id'],last_stop_error={'status':'unconfirmed','error_type':type(error).__name__})
                try:await asyncio.wait_for(wake.wait(),timeout=0.2)
                except TimeoutError:pass
        control=asyncio.create_task(controls())
        try:
            async with self.timeout(self.service.settings.skill_eval.generation_timeout_seconds):
                async for event in self.runtime.subscribe(session_id):
                    kind,raw=event['kind'],event['raw']
                    stored=events.append(run['id'],kind,raw,source='recovery_socket',stream_id=job['id'],source_sequence=raw.get('stream_sequence'))
                    self.observe_model(run['id'],kind,raw,stored['event_id'])
                    if kind=='chat:end' and (not target or raw.get('chat_run_id')==target):
                        await self._generation_end(run,payload['generation_id'],message,raw,events)
                        return
            raise RuntimeError('Recovery subscription ended without the expected receipt')
        finally:
            control.cancel();await asyncio.gather(control,return_exceptions=True)

    async def collect(self,job,run):
        from .artifacts import ArtifactCollector
        id_=run['id'];refs=run['external_refs'];gaps=[];history=None;quiescent=False;never_started=False
        store=EventStore(self.repo,self.capture(run))
        try:
            if refs.get('session_id'):
                state=await self.runtime.session(refs['session_id'])
                if state.get('status') not in ('completed','interrupted','paused','idle','created','ready','failed') and refs.get('chat_run_id'):
                    receipt=await self.runtime.stop(refs['session_id'],refs['chat_run_id'])
                    self.refs(id_,last_stop_receipt=redact_json(receipt))
                    for _ in range(50):
                        state=await self.runtime.session(refs['session_id'])
                        if state.get('status') in ('completed','interrupted','paused','idle','failed'):break
                        await asyncio.sleep(0.2)
                quiescent=state.get('status') in ('completed','interrupted','paused','idle','created','ready','failed')
                self.refs(id_,final_remote_status=state.get('status'))
                history=await self.runtime.history(refs['session_id'])
                store.append(id_,'session:history',history,source='history',source_id='finish:'+job['id'])
        except Exception as error:
            gaps.append({'reason':'remote_state_or_history_unavailable','type':type(error).__name__})
        if not refs.get('session_id'):
            with self.repo.connection() as c:
                checkpoints=[json.loads(row['checkpoint_json']) for row in c.execute('SELECT checkpoint_json FROM skill_eval_jobs WHERE run_id=?',(id_,))]
            never_started=not any(item.get('remote_started') for item in checkpoints)
            quiescent=never_started
        if not quiescent:
            gaps.append({'reason':'remote_quiescence_unconfirmed'})
            artifacts={'status':'partial','artifacts':[],'gaps':gaps,'observations':[]}
        elif never_started:
            artifacts={'status':'complete','artifacts':[],'gaps':[],'observations':[]}
        else:
            limits=self.service.settings.skill_eval
            artifacts=await ArtifactCollector(self.service,self.runtime,max_file=limits.artifact_max_file_bytes,max_total=limits.artifact_max_total_bytes,max_files=limits.artifact_max_files).collect(run)
            gaps.extend(artifacts['gaps'])
        # Read only the public final reply; do not produce a new model summary.
        events=store.read(id_,limit=500)['items'];cursor=events[-1]['seq'] if events else 0
        while events and cursor and len(events)%500==0:
            page=store.read(id_,after=cursor,limit=500)['items']
            if not page:break
            events.extend(page);cursor=page[-1]['seq']
            if len(page)<500:break
        last_reply=None
        for event in events:
            if event['type']=='chat:end':
                body=event['payload'].get('body') or {}
                if body.get('output'):
                    last_reply={'event_id':event['event_id'],'text':body['output']}
        messages=self.service.messages(id_)
        with self.repo.transaction() as c:
            owner=c.execute('SELECT owner FROM skill_eval_jobs WHERE id=?',(job['id'],)).fetchone()
            if owner is None or owner['owner']!=self.owner:raise RuntimeError('Worker lease ownership was lost')
            current=self.service._run(c,id_)
            previous=json.loads(current['finalized_result_ref']) if current['finalized_result_ref'] else None
            result={'run_id':id_,'execution_started':False if never_started else True if refs.get('session_id') else None,'skill_snapshot_id':run['skill_version_id'],'through_user_seq':max((m['seq'] for m in messages),default=0),
                    'final_reply':last_reply,'artifacts':artifacts['artifacts'],'observations':artifacts['observations'],
                    'gaps':gaps,'complete':not gaps,'remote_quiescence_verified':quiescent,
                    'collection_generation':(previous.get('collection_generation',0) if previous else 0)+1,
                    'fixed_at':self.service._time(),'event_cursor':cursor}
            c.execute('INSERT INTO skill_eval_result_collections(run_id,generation,result_json,created_at) VALUES(?,?,?,?)',(id_,result['collection_generation'],json.dumps(result,ensure_ascii=False),self.service._time()))
            status=current['run_status'] if current['run_status'] in ('failed','cancelled') else 'completed' if quiescent else 'failed'
            c.execute("UPDATE skill_eval_runs SET run_status=?,artifact_status=?,capture_status=?,finalized_result_ref=?,ended_at=coalesce(ended_at,?),revision=revision+1 WHERE id=?",(status,artifacts['status'],'partial' if gaps else 'complete',json.dumps(result,ensure_ascii=False),self.service._time(),id_))
            c.execute("UPDATE skill_eval_interactions SET state='closed' WHERE run_id=? AND state IN ('pending','answering')",(id_,))


    async def run_forever(self):
        last_recovery=0.0
        try:
            while not self.closing:
                tick=asyncio.get_running_loop().time()
                if tick-last_recovery>=self.service.settings.skill_eval.recovery_poll_seconds:
                    from .recovery import Recovery
                    await Recovery(self.service,self.runtime).reconcile()
                    last_recovery=tick
                done=[id_ for id_,task in self.active.items() if task.done()]
                for id_ in done:
                    task=self.active.pop(id_)
                    await asyncio.gather(task,return_exceptions=True)
                if len(self.active)<self.service.settings.skill_eval.max_active_runs:
                    job=self.claim()
                    if job:self.active[job['id']]=asyncio.create_task(self.execute(job))
                await asyncio.sleep(0.1)
        finally:
            await self.close()

    async def close(self):
        self.closing=True
        tasks=list(self.active.values())
        for task in tasks:task.cancel()
        if tasks:await asyncio.gather(*tasks,return_exceptions=True)
        self.active.clear()
