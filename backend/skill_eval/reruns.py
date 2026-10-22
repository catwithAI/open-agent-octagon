"""Manual input confirmation, preserving all user content without action replay."""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta

from .service import EvalError,new_id
from .storage import canonical


class Reruns:
    def __init__(self,service):self.service=service;self.repo=service.repo

    def source(self,id_):
        run=self.service.get_run(id_)
        messages=[]
        for message in self.service.messages(id_):
            if message['kind']=='context_reuse' and message['source_message_refs']:
                messages.extend(message['source_message_refs'])
            else:
                messages.append({'id':message['id'],'source_run_id':id_,'kind':message['kind'],'text':message['text'],
                                 'attachment_refs':message['attachment_refs'],'delivery_state':message['delivery_state']})
        snapshot={'run_id':id_,'through_seq':max((m['seq'] for m in self.service.messages(id_)),default=0),'messages':messages}
        hashed={**snapshot,'messages':[{key:value for key,value in message.items() if key!='delivery_state'} for message in messages]}
        return snapshot,hashlib.sha256(canonical(hashed)).hexdigest()

    def create(self,source_run_id,mode):
        self.service._writable()
        if mode not in ('reuse_all','replace'):raise ValueError('Choose reuse_all or replace')
        source,source_hash=self.source(source_run_id)
        id_=new_id();expires=(self.service.clock()+timedelta(days=1)).isoformat()
        hash_=hashlib.sha256(canonical({'source_hash':source_hash,'mode':mode})).hexdigest()
        with self.repo.transaction() as c:
            c.execute('INSERT INTO skill_eval_rerun_plans(id,source_run_id,through_user_seq,mode,confirmed_message_refs_json,hash,expires_at) VALUES(?,?,?,?,?,?,?)',
                      (id_,source_run_id,source['through_seq'],mode,json.dumps({'source_hash':source_hash,'source':source}),hash_,expires))
        return self.get(id_)

    def get(self,id_):
        with self.repo.connection() as c:
            row=self.service._row(c,'SELECT * FROM skill_eval_rerun_plans WHERE id=?',(id_,))
            self.service._run(c,row['source_run_id'])
        row['content']=json.loads(row.pop('confirmed_message_refs_json'))
        row['inputs']=json.loads(row.pop('confirmed_input_refs_json'))
        return row

    def confirm(self,id_,expected_hash,*,prompt='',input_ids=None):
        self.service._writable()
        plan=self.get(id_)
        if plan['expires_at']<=self.service._time():raise EvalError('plan_expired','Rerun plan expired',410)
        source,current_hash=self.source(plan['source_run_id'])
        if current_hash!=plan['content']['source_hash']:raise EvalError('source_changed','Source inputs changed; create and review a new plan')
        if plan['mode']=='reuse_all' and (prompt or input_ids):raise ValueError('reuse_all preserves all inputs; choose replace to provide new inputs')
        if plan['state']=='confirmed':
            if plan['mode']=='replace' and (prompt!=plan['content']['actual_message'] or (input_ids or [])!=[item['id'] for item in plan['inputs']]):raise EvalError('plan_changed','Plan was confirmed with different inputs')
            if expected_hash not in (plan['hash'],plan['content'].get('confirmation_hash')):raise EvalError('plan_changed','Plan already confirmed with different input')
            return plan
        if expected_hash!=plan['hash']:raise EvalError('plan_changed','Rerun plan changed')
        if plan['mode']=='reuse_all':
            if prompt or input_ids:raise ValueError('reuse_all preserves all inputs; choose replace to provide new inputs')
            messages=source['messages']
            inputs=list({item['id']:item for message in messages for item in message['attachment_refs']}.values())
            actual='以下是用户确认从上次运行带入的完整输入，仅作为本次上下文；请结合当前 Skill 执行本次任务，不要逐条重放旧会话操作。\n\n'
            for i,message in enumerate(messages):
                actual+=f"用户输入 {i+1}（{message['kind']}）：\n{message['text']}\n"
                if message['attachment_refs']:actual+='附件：'+', '.join(item['remote_path'] for item in message['attachment_refs'])+'\n'
            if not any(message['text'].strip() or message['attachment_refs'] for message in messages):actual=''
        else:
            messages=[];inputs=[self.service.get_input(id_) for id_ in input_ids or []];actual=prompt
        if len(actual.encode('utf-8'))>self.service.settings.skill_eval.context_max_bytes:raise EvalError('context_limit','Confirmed context exceeds limits; choose replace or reduce inputs',413)
        if sum(item['size'] for item in inputs)>self.service.input_max_bytes:raise EvalError('input_limit','Confirmed inputs exceed limits; choose replace or reduce inputs',413)
        content={'source_hash':current_hash,'source':source,'messages':messages,'actual_message':actual,'confirmation_hash':expected_hash}
        new_hash=hashlib.sha256(canonical({'content':content,'inputs':inputs,'mode':plan['mode']})).hexdigest()
        with self.repo.transaction() as c:
            # Source append and confirmation serialize through the same SQLite
            # transaction. Recheck the cutoff after acquiring the writer lock.
            latest=c.execute('SELECT coalesce(max(seq),0) FROM skill_eval_messages WHERE run_id=?',(plan['source_run_id'],)).fetchone()[0]
            if latest!=source['through_seq']:raise EvalError('source_changed','Source inputs changed while confirming')
            changed=c.execute("UPDATE skill_eval_rerun_plans SET state='confirmed',confirmed_at=?,confirmed_message_refs_json=?,confirmed_input_refs_json=?,hash=? WHERE id=? AND state='pending' AND hash=?",
                             (self.service._time(),json.dumps(content,ensure_ascii=False),json.dumps(inputs),new_hash,id_,expected_hash)).rowcount
            if changed!=1:raise EvalError('plan_changed','Rerun plan changed while confirming')
        return self.get(id_)

    def validate(self,c,source,body):
        plan_id=body.get('rerun_plan_id')
        if not plan_id:raise EvalError('rerun_confirmation_required','Review and confirm all inputs before rerunning')
        plan=self.get(plan_id)
        if plan.get('consumed_run_id'):raise EvalError('plan_consumed','Confirm a new plan for each rerun')
        if plan['state']!='confirmed' or plan['source_run_id']!=source:raise EvalError('rerun_confirmation_required','Rerun plan is not confirmed for this source')
        if plan['expires_at']<=self.service._time():raise EvalError('plan_expired','Rerun plan expired',410)
        source_data,current_hash=self.source(source)
        if current_hash!=plan['content']['source_hash']:raise EvalError('source_changed','Source inputs changed; confirm a new plan')
        source_run=self.service._run(c,source)
        snapshot=self.service._version(c,body['snapshot_id'])
        if source_run['skill_id']!=snapshot['skill_id']:raise EvalError('skill_mismatch','Rerun snapshot must belong to the source skill')
        if body.get('prompt','') or body.get('input_ids'):raise ValueError('Rerun inputs are taken from the confirmed plan, not editable request fields')
        return plan
