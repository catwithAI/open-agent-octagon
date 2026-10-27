"""Reconcile persisted receipts before allowing any resumed remote write."""
from __future__ import annotations

import json

from .capture import Capture
from .events import EventStore
from .service import new_id


class Recovery:
    def __init__(self,service,runtime):self.service,self.runtime=service,runtime;self.repo=service.repo

    async def reconcile(self):
        if self.service.settings.octagon.display_only or not self.service.settings.skill_eval.enabled:return []
        with self.repo.connection() as c:
            candidates=[dict(row) for row in c.execute("SELECT j.* FROM skill_eval_jobs j JOIN skill_eval_runs r ON r.id=j.run_id JOIN skill_eval_skills s ON s.id=r.skill_id WHERE j.state='running' AND (j.lease_until IS NULL OR j.lease_until<=?) AND s.scope_id=?",(self.service._time(),self.service.scope))]
        results=[]
        for job in candidates:
            try:
                result=await self._one(job);results.append(result)
            except Exception as error:
                with self.repo.transaction() as c:
                    changed=c.execute("UPDATE skill_eval_jobs SET state='acceptance_unknown',last_error='reconciliation_failed' WHERE id=? AND (state='reconciling' OR (state='running' AND (lease_until IS NULL OR lease_until<=?)))",(job['id'],self.service._time())).rowcount
                    if changed:c.execute("UPDATE skill_eval_runs SET run_status='failed',error_code='reconciliation_failed',capture_status='partial',ended_at=? WHERE id=?",(self.service._time(),job['run_id']))
                results.append({'job_id':job['id'],'status':'unknown','error_type':type(error).__name__})
        return results

    async def _one(self,job):
        run=self.service.get_run(job['run_id']);checkpoint=json.loads(job['checkpoint_json'])
        session_id=run['external_refs'].get('session_id')
        remote_started=checkpoint.get('remote_started')
        # Claim the expired record for reconciliation with a CAS. Another worker
        # can neither resume nor reconcile the same expired lease concurrently.
        with self.repo.transaction() as c:
            changed=c.execute("UPDATE skill_eval_jobs SET state='reconciling',owner=NULL,lease_until=NULL WHERE id=? AND state='running' AND (lease_until IS NULL OR lease_until<=?)",(job['id'],self.service._time())).rowcount
            if not changed:return {'job_id':job['id'],'status':'already_claimed'}
        if not session_id and remote_started:
            with self.repo.transaction() as c:
                c.execute("UPDATE skill_eval_jobs SET state='acceptance_unknown',last_error='create_session_acceptance_unknown' WHERE id=?",(job['id'],))
                c.execute("UPDATE skill_eval_runs SET run_status='failed',error_code='create_session_acceptance_unknown',capture_status='partial',ended_at=? WHERE id=?",(self.service._time(),run['id']))
            return {'job_id':job['id'],'status':'acceptance_unknown'}
        if job['phase'] in ('finish','collect'):
            with self.repo.transaction() as c:c.execute("UPDATE skill_eval_jobs SET state='pending' WHERE id=?",(job['id'],))
            return {'job_id':job['id'],'status':'read_only_collection_resumed'}
        message_id=json.loads(job['payload_json']).get('message_id')
        with self.repo.connection() as c:
            message=c.execute('SELECT * FROM skill_eval_messages WHERE id=?',(message_id,)).fetchone() if message_id else None
        upload_only=session_id and checkpoint.get('phase') in ('create','skill_upload','input_upload')
        if (not remote_started or upload_only) and (not message or message['delivery_state']=='pending'):
            # Known safe phase: no chat has started. A bound session is reused;
            # immutable uploads can be revalidated, never a new session substitute.
            with self.repo.transaction() as c:c.execute("UPDATE skill_eval_jobs SET state='pending' WHERE id=?",(job['id'],))
            return {'job_id':job['id'],'status':'safe_phase_resumed'}
        generation_id=checkpoint.get('generation_id') or json.loads(job['payload_json']).get('generation_id')
        if session_id:
            try:
                state=await self.runtime.session(session_id)
                history=await self.runtime.history(session_id)
                EventStore(self.repo,Capture(requested=run['input_snapshot']['capture']['policy']['requested'],server_max=self.service.settings.octagon.wire_capture_max_policy,source_capability='full' if self.service.settings.octagon.wire_capture_enabled else 'off',max_bytes=self.service.settings.skill_eval.event_max_bytes)).append(run['id'],'session:history',history,source='history',source_id='recovery:'+job['id'])
            except Exception:
                with self.repo.transaction() as c:
                    c.execute("UPDATE skill_eval_jobs SET state='acceptance_unknown',last_error='remote_reconciliation_unavailable' WHERE id=?",(job['id'],))
                    c.execute("UPDATE skill_eval_runs SET run_status='failed',error_code='remote_reconciliation_unavailable',capture_status='partial',ended_at=? WHERE id=?",(self.service._time(),run['id']))
                return {'job_id':job['id'],'status':'acceptance_unknown'}
            accepted=bool(message and message['delivery_state']=='sent')
            if accepted and state.get('status')=='running' and generation_id and run['external_refs'].get('chat_run_id'):
                with self.repo.transaction() as c:
                    c.execute("UPDATE skill_eval_jobs SET state='completed',last_error='read_only_subscription_resumed' WHERE id=?",(job['id'],))
                    self.service._job(c,run['id'],'observe:'+job['id'],'observe',{'message_id':message_id,'generation_id':generation_id})
                return {'job_id':job['id'],'status':'read_only_subscription_resumed'}

            remote_status=state.get('status')
            if accepted and remote_status in ('completed','interrupted','idle','paused'):
                pending=False
                if remote_status=='paused':
                    with self.repo.connection() as c:
                        pending=bool(c.execute("SELECT 1 FROM skill_eval_interactions WHERE run_id=? AND state='pending'",(run['id'],)).fetchone())
                    if not pending:
                        for node in reversed(history.get('nodes',[])):
                            for call in node.get('tool_calls') or []:
                                function=call.get('function') or {}
                                if str(function.get('name','')).endswith('AskUserQuestion') and call.get('id'):
                                    arguments=function.get('arguments') or {}
                                    if isinstance(arguments,str):arguments=json.loads(arguments)
                                    from backend.wire.redaction import redact_json
                                    with self.repo.transaction() as c:
                                        c.execute("INSERT OR IGNORE INTO skill_eval_interactions(id,run_id,remote_tool_call_id,question_json,state,created_at) VALUES(?,?,?,?,'pending',?)",(new_id(),run['id'],call['id'],json.dumps(redact_json({'arguments':arguments,'name':function['name'],'tool_call_id':call['id']})),self.service._time()))
                                    pending=True;break
                            if pending:break
                    if not pending:
                        # Preserve the pause fact without fabricating a question or
                        # inventing an answer. Explicitly mark the capture gap.
                        with self.repo.transaction() as c:
                            c.execute("UPDATE skill_eval_runs SET capture_status='partial',error_code='paused_question_not_recovered' WHERE id=?",(run['id'],))
                with self.repo.transaction() as c:
                    c.execute("UPDATE skill_eval_jobs SET state='completed',last_error='reconciled_from_history' WHERE id=?",(job['id'],))
                    c.execute("UPDATE skill_eval_runs SET run_status=CASE WHEN finish_requested_at IS NOT NULL THEN 'finishing' ELSE ? END,generation_status=?,revision=revision+1 WHERE id=?",('waiting_input' if remote_status=='paused' else 'waiting_continue','paused' if remote_status=='paused' else 'completed',run['id']))
                return {'job_id':job['id'],'status':'session_restored_without_send'}
        # A live or ambiguous command is never re-issued. Keep remote IDs and all
        # evidence, and let finish/collection inspect or stop that exact generation.
        with self.repo.transaction() as c:
            c.execute("UPDATE skill_eval_jobs SET state='acceptance_unknown',last_error='delivery_not_replayed' WHERE id=?",(job['id'],))
            c.execute("UPDATE skill_eval_runs SET run_status='failed',generation_status='failed',error_code='acceptance_unknown',capture_status='partial',ended_at=? WHERE id=?",(self.service._time(),run['id']))
        return {'job_id':job['id'],'status':'acceptance_unknown'}
