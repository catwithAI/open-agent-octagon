"""Durable Skill Eval session commands, separate from remote delivery."""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta

from .capture import Capture
from .service import SkillService,EvalError,new_id
from .storage import canonical,guarded

TERMINAL={'completed','failed','cancelled'}
STARTUP_MESSAGE='请执行本次选择的 Skill。'


class RunService(SkillService):
    def __init__(self,*args,settings,**kwargs):
        super().__init__(*args,**kwargs)
        self.settings=settings

    def _writable(self,*,execute=False):
        if self.settings.octagon.display_only:
            raise EvalError('display_only','This deployment is read only',403)
        if execute and getattr(self,'execution_available',None) is False:
            raise EvalError('runtime_unavailable','Skill Eval runtime is unavailable',503)
        if execute and not self.settings.skill_eval.enabled:
            raise EvalError('execution_disabled','Skill Eval execution is disabled',403)

    def _run(self,c,id_):
        return self._row(c,'SELECT r.* FROM skill_eval_runs r JOIN skill_eval_skills s ON s.id=r.skill_id WHERE r.id=? AND s.scope_id=?',(id_,self.scope))

    def _idempotent(self,c,operation,key,payload):
        if not isinstance(key,str) or not key.strip() or len(key)>200:
            raise ValueError('Idempotency-Key must contain 1–200 characters')
        hash_=hashlib.sha256(canonical(payload)).hexdigest()
        previous=c.execute('SELECT * FROM skill_eval_requests WHERE scope=? AND operation=? AND idempotency_key=?',(self.scope,operation,key)).fetchone()
        if previous:
            if previous['request_hash']!=hash_:raise EvalError('idempotency_conflict','Idempotency key was used for a different request')
            return previous['resource_id'],hash_
        return None,hash_

    def _receipt(self,c,operation,key,hash_,id_):
        c.execute('INSERT INTO skill_eval_requests(scope,operation,idempotency_key,request_hash,resource_id,created_at) VALUES(?,?,?,?,?,?)',(self.scope,operation,key,hash_,id_,self._time()))

    def _job(self,c,run_id,operation,phase,payload=None):
        c.execute("INSERT INTO skill_eval_jobs(id,run_id,operation_id,phase,state,payload_json) VALUES(?,?,?,?,'pending',?)",(new_id(),run_id,operation,phase,json.dumps(payload or {})))

    def get_run(self,id_):
        with self.repo.connection() as c:
            run=self._run(c,id_)
            for field in ('input_snapshot_json','external_refs_json'):
                run[field.removesuffix('_json')]=json.loads(run.pop(field))
            note=c.execute('SELECT text,revision,updated_at FROM skill_eval_notes WHERE run_id=?',(id_,)).fetchone()
            run['note']=dict(note) if note else {'text':'','revision':0,'updated_at':None}
            run['skill']=self._skill(c,run['skill_id'])
            run['experiment']=self._row(c,'SELECT * FROM skill_eval_experiments WHERE id=?',(run['experiment_id'],))
            run['chat_replies']=[dict(row) for row in c.execute('''SELECT e.event_id,e.seq,
                json_extract(e.payload_json,'$.body.output') AS text,g.triggering_message_id AS message_id
                FROM skill_eval_events e LEFT JOIN skill_eval_generations g
                ON g.run_id=e.run_id AND g.chat_run_id=json_extract(e.payload_json,'$.body.chat_run_id')
                WHERE e.run_id=? AND e.type='chat:end' AND json_extract(e.payload_json,'$.body.output') IS NOT NULL
                AND json_extract(e.payload_json,'$.body.output')!='' ORDER BY e.seq''',(id_,))]
            run['final_result']=json.loads(run['finalized_result_ref']) if run['finalized_result_ref'] else None
            return run

    def export_run_skill(self,id_):
        run=self.get_run(id_)
        metadata={'modified':run['input_snapshot'].get('skill_modified_from_release',False)}
        if run['source_release_label']:
            metadata.update(version=run['source_release_label'],source_release_revision=run['source_release_revision'],
                            status='working_copy' if metadata['modified'] else run['input_snapshot'].get('source_release_status','unknown'),source_status=run['input_snapshot'].get('source_release_status','unknown'))
        return self.export_snapshot(run['skill_version_id'],platform_metadata=metadata)

    @guarded
    def create_run_draft(self,id_):
        with self.repo.transaction() as c:
            run=self._run(c,id_)
            snapshot=self._version(c,run['skill_version_id'])
            source=self._release(c,run['source_release_id']) if run['source_release_id'] else None
            draft_id=new_id();timestamp=self._time()
            c.execute('INSERT INTO skill_eval_drafts(id,skill_id,source_run_id,base_version_id,source_release_id,target_release_id,manifest_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                (draft_id,run['skill_id'],id_,snapshot['id'],source['id'] if source else None,source['id'] if source and source['status']=='unpublished' else None,snapshot['manifest_ref'],timestamp,timestamp))
            return self._draft_result(self._draft(c,draft_id),c)

    def messages(self,id_):
        with self.repo.connection() as c:
            self._run(c,id_)
            return [self._message_result(row) for row in c.execute('SELECT * FROM skill_eval_messages WHERE run_id=? ORDER BY seq',(id_,))]

    @staticmethod
    def _message_result(row):
        message=dict(row)
        for name in ('attachment_refs_json','remote_receipt_json','source_message_refs_json','answer_json'):
            message[name.removesuffix('_json')]=json.loads(message.pop(name))
        return message

    @guarded
    def create_run(self,body,*,idempotency_key,allowed_models):
        self._writable(execute=True)
        if body.get('agent','blade-agent')!='blade-agent':raise ValueError('Only blade-agent is supported')
        model=body['model']['id']
        with self.repo.transaction() as c:
            previous,hash_=self._idempotent(c,'create_run',idempotency_key,body)
            if previous:return {'run_id':previous,'replayed':True}
            if model not in allowed_models:raise EvalError('model_unavailable','Selected model is not in the current service catalog',422)
            experiment=self._row(c,'SELECT e.* FROM skill_eval_experiments e JOIN skill_eval_skills s ON s.id=e.skill_id WHERE e.id=? AND s.scope_id=?',(body['experiment_id'],self.scope))
            snapshot=self._version(c,body['snapshot_id'])
            if snapshot['skill_id']!=experiment['skill_id']:raise EvalError('skill_mismatch','Experiment and snapshot must belong to the same skill')
            draft_id=body.get('draft_id')
            if draft_id and self._draft(c,draft_id)['skill_id']!=experiment['skill_id']:raise EvalError('skill_mismatch','Draft belongs to another skill')
            plan=None
            source=body.get('source_run_id')
            if body.get('rerun_plan_id') and not source:raise ValueError('A rerun plan requires its source run')
            if source:plan=self._validate_rerun(c,source,body)
            inputs=plan['inputs'] if plan else [self.get_input(id_) for id_ in body.get('input_ids',[])]
            if len({i['id'] for i in inputs})!=len(inputs):raise ValueError('Duplicate input references')
            if sum(i['size'] for i in inputs)>self.input_max_bytes:raise ValueError('Input total size limit')
            capture=Capture(requested=body.get('capture_policy','full'),server_max=self.settings.octagon.wire_capture_max_policy,
                source_capability='full' if self.settings.octagon.wire_capture_enabled else 'off')
            prompt=plan['content']['actual_message'] if plan else body.get('prompt','')
            if len(prompt.encode('utf-8'))>self.settings.skill_eval.context_max_bytes:raise EvalError('context_limit','Prompt exceeds context limit',413)
            frozen={'skill_version_id':snapshot['id'],'prompt':prompt,'system_startup_message':STARTUP_MESSAGE if not prompt.strip() else None,
                    'inputs':inputs,'model':body['model'],'capture':capture.manifest(),
                    'generation_timeout_seconds':self.settings.skill_eval.generation_timeout_seconds}
            label=None;release_revision=None;release_id=body.get('release_id')
            if release_id:
                release=self._release(c,release_id)
                if release['skill_id']!=snapshot['skill_id']:raise EvalError('skill_mismatch','Release belongs to another skill')
                label,release_revision=release['version_label'],release['revision']
                frozen['source_release_status']=release['status']
                frozen['skill_modified_from_release']=release['current_version_id']!=snapshot['id']
            id_=new_id();timestamp=self._time()
            provider=body['model'].get('provider') or ''
            group='requested/unverified:'+json.dumps(['blade-agent',provider,model],separators=(',',':'))
            c.execute('''INSERT INTO skill_eval_runs(id,experiment_id,skill_id,draft_id,skill_version_id,
                source_release_id,source_release_label,source_release_revision,source_run_id,
                input_snapshot_json,input_hash,requested_provider,requested_model,group_key,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (id_,experiment['id'],snapshot['skill_id'],draft_id,snapshot['id'],release_id,label,release_revision,source,
                 json.dumps(frozen,ensure_ascii=False),hashlib.sha256(canonical(frozen)).hexdigest(),provider,model,group,timestamp))
            message_id=new_id()
            c.execute('INSERT INTO skill_eval_messages(id,run_id,seq,kind,text,attachment_refs_json,source_message_refs_json,created_at) VALUES(?,?,1,?,?,?,?,?)',
                      (message_id,id_,'context_reuse' if plan and plan['mode']=='reuse_all' else 'initial',prompt,json.dumps(inputs),json.dumps(plan['content']['messages']) if plan else '[]',timestamp))
            self._job(c,id_,'prepare:'+id_,'prepare',{'message_id':message_id})
            if plan:c.execute('UPDATE skill_eval_rerun_plans SET consumed_run_id=? WHERE id=? AND consumed_run_id IS NULL',(id_,plan['id']))
            self._receipt(c,'create_run',idempotency_key,hash_,id_)
            return {'run_id':id_,'run_status':'queued','skill_version_id':snapshot['id'],'detail_url':f'/skill-eval/runs/{id_}'}

    def _validate_rerun(self,c,source,body):
        from .reruns import Reruns
        return Reruns(self).validate(c,source,body)

    def send_message(self,id_,body,*,idempotency_key,interaction_id=None):
        self._writable(execute=True)
        operation=f'answer:{id_}:{interaction_id}' if interaction_id else f'message:{id_}'
        with self.repo.transaction() as c:
            run=self._run(c,id_)
            previous,hash_=self._idempotent(c,operation,idempotency_key,body)
            if previous:return self._message_result(c.execute('SELECT * FROM skill_eval_messages WHERE id=?',(previous,)).fetchone())
            if run['finish_requested_at'] or run['run_status'] in TERMINAL:raise EvalError('run_finishing','Run no longer accepts messages')
            if run['run_status'] in ('queued','preparing'):raise EvalError('run_not_ready','Wait for session preparation')
            if run['run_status']=='waiting_input' and not interaction_id:raise EvalError('answer_required','Answer the pending question first')
            interaction=None
            if interaction_id:
                interaction=self._row(c,'SELECT * FROM skill_eval_interactions WHERE id=? AND run_id=?',(interaction_id,id_))
                self._expect(interaction['revision'],body.get('expected_revision'),'interaction revision')
                if interaction['state']!='pending':raise EvalError('interaction_closed','Question was already answered or closed')
            text=body.get('text','')
            structured={}
            if interaction:
                structured=body.get('answer') or {'selections':{},'custom':{'0':text}}
                question=json.loads(interaction['question_json']).get('arguments',{}).get('questions',[])
                summaries=[]
                for index,values in structured.get('selections',{}).items():
                    if not index.isdigit() or int(index)>=len(question):raise ValueError('Invalid question index')
                    options=question[int(index)].get('options',[])
                    if not isinstance(values,list) or any(type(v) is not int or not 0<=v<len(options) for v in values):raise ValueError('Invalid option index')
                    if len(set(values))!=len(values) or (not question[int(index)].get('multiSelect') and len(values)>1):raise ValueError('Invalid multiple selection')
                    summaries.append('; '.join(str(options[v].get('label','')) for v in values))
                for index,value in structured.get('custom',{}).items():
                    if not index.isdigit() or (question and int(index)>=len(question)) or not isinstance(value,str) or len(value)>100000:raise ValueError('Invalid custom answer')
                    if value:summaries.append(value)
                summary='\n'.join(summaries)
                if summary and summary!=text:text=(text+'\n' if text else '')+summary
            if len(text.encode('utf-8'))>self.settings.skill_eval.context_max_bytes:raise EvalError('context_limit','Message exceeds context limit',413)
            inputs=[self.get_input(input_id) for input_id in body.get('input_ids',[])]
            if not text.strip() and not inputs:raise ValueError('Provide text or an attachment')
            if len({i['id'] for i in inputs})!=len(inputs) or sum(i['size'] for i in inputs)>self.input_max_bytes:raise ValueError('Input references exceed limits or repeat')
            message_id=new_id();seq=self.repo.next_sequence(c,'messages',id_)
            c.execute('INSERT INTO skill_eval_messages(id,run_id,seq,kind,text,attachment_refs_json,interaction_id,created_at) VALUES(?,?,?,?,?,?,?,?)',
                      (message_id,id_,seq,'answer' if interaction else 'followup',text,json.dumps(inputs),interaction_id,self._time()))
            if interaction:c.execute('UPDATE skill_eval_messages SET answer_json=? WHERE id=?',(json.dumps(structured,ensure_ascii=False),message_id))
            if interaction:
                c.execute("UPDATE skill_eval_interactions SET state='answering',revision=revision+1,answer_message_id=? WHERE id=?",(message_id,interaction_id))
            c.execute("UPDATE skill_eval_runs SET generation_status='queued',revision=revision+1 WHERE id=?",(id_,))
            self._job(c,id_,'message:'+message_id,'message',{'message_id':message_id})
            self._receipt(c,operation,idempotency_key,hash_,message_id)
            return self._message_result(c.execute('SELECT * FROM skill_eval_messages WHERE id=?',(message_id,)).fetchone())

    def interactions(self,id_):
        with self.repo.connection() as c:
            self._run(c,id_)
            rows=[]
            for row in c.execute('SELECT * FROM skill_eval_interactions WHERE run_id=? ORDER BY created_at,id',(id_,)):
                result=dict(row);result['question']=json.loads(result.pop('question_json'));rows.append(result)
            return rows

    def stop_generation(self,id_):
        self._writable()
        with self.repo.transaction() as c:
            run=self._run(c,id_)
            if run['run_status'] in TERMINAL:raise EvalError('run_ended','Run has ended')
            if run['generation_status']=='queued' or run['run_status'] in ('queued','preparing'):
                active=c.execute("SELECT 1 FROM skill_eval_jobs WHERE run_id=? AND state IN ('running','reconciling')",(id_,)).fetchone()
                if not active:
                    c.execute("UPDATE skill_eval_messages SET delivery_state='cancelled' WHERE run_id=? AND delivery_state='pending'",(id_,))
                    c.execute("UPDATE skill_eval_jobs SET state='completed' WHERE run_id=? AND state='pending' AND phase IN ('prepare','message')",(id_,))
                    c.execute("UPDATE skill_eval_interactions SET state='pending',answer_message_id=NULL,revision=revision+1 WHERE run_id=? AND state='answering'",(id_,))
                    if run['run_status']=='queued':
                        result={'run_id':id_,'execution_started':False,'complete':True,'artifacts':[],'observations':[],'gaps':[],'final_reply':None,'collection_generation':0,'fixed_at':self._time()}
                        c.execute("UPDATE skill_eval_runs SET run_status='cancelled',generation_status='stopped',capture_status='complete',artifact_status='complete',ended_at=?,finalized_result_ref=?,revision=revision+1 WHERE id=?",(self._time(),json.dumps(result),id_))
                    else:
                        pending=c.execute("SELECT 1 FROM skill_eval_interactions WHERE run_id=? AND state='pending'",(id_,)).fetchone()
                        c.execute("UPDATE skill_eval_runs SET run_status=?,generation_status='stopped',revision=revision+1 WHERE id=?",('waiting_input' if pending else 'waiting_continue',id_))
                else:
                    c.execute("UPDATE skill_eval_runs SET stop_requested_at=?,generation_status='stopping',revision=revision+1 WHERE id=?",(self._time(),id_))
            elif run['generation_status'] in ('running','stopping'):
                c.execute("UPDATE skill_eval_runs SET stop_requested_at=?,generation_status='stopping',revision=revision+1 WHERE id=?",(self._time(),id_))
        if getattr(self,'_control_notifier',None):self._control_notifier(id_)
        return self.get_run(id_)

    def finish(self,id_):
        self._writable()
        with self.repo.transaction() as c:
            run=self._run(c,id_)
            if run['run_status'] in TERMINAL or run['finish_requested_at']:return {'run_id':id_,'run_status':run['run_status']}
            timestamp=self._time()
            c.execute("UPDATE skill_eval_runs SET run_status='finishing',finish_requested_at=?,revision=revision+1 WHERE id=?",(timestamp,id_))
            self._job(c,id_,'finish:'+id_,'finish')
        if getattr(self,'_control_notifier',None):self._control_notifier(id_)
        return {'run_id':id_,'run_status':'finishing'}

    def save_note(self,id_,text,expected_revision):
        self._writable()
        if len(text)>self.settings.skill_eval.note_max_chars:raise ValueError('Note length limit exceeded')
        with self.repo.transaction() as c:
            self._run(c,id_)
            previous=c.execute('SELECT * FROM skill_eval_notes WHERE run_id=?',(id_,)).fetchone()
            self._expect(previous['revision'] if previous else 0,expected_revision,'note revision')
            revision=expected_revision+1
            c.execute('INSERT INTO skill_eval_notes(run_id,text,revision,updated_at) VALUES(?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET text=excluded.text,revision=excluded.revision,updated_at=excluded.updated_at',(id_,text,revision,self._time()))
        return {'text':text,'revision':revision}
