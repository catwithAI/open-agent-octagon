"""Scoped keyset history and stable comparison inputs; no model calls."""
from __future__ import annotations

import base64
import difflib
import json
from datetime import datetime,timezone

from .service import EvalError,new_id
from .runs import TERMINAL
from .storage import guarded


def cursor_encode(value):
    return base64.urlsafe_b64encode(json.dumps(value,separators=(',',':')).encode()).decode().rstrip('=')


def cursor_decode(value):
    try:
        result=json.loads(base64.urlsafe_b64decode(value+'='*(-len(value)%4)))
        if not isinstance(result,dict):raise ValueError('Invalid cursor')
        return result
    except Exception as error:raise ValueError('Invalid cursor') from error


class Queries:
    def __init__(self,service):self.service=service;self.repo=service.repo;self.store=service.store

    def history(self,*,skill_id=None,experiment_id=None,release_id=None,group_key=None,status=None,model=None,created_after=None,created_before=None,limit=50,cursor=None):
        if not 1<=limit<=100:raise ValueError('History limit must be 1–100')
        where=['s.scope_id=?'];params=[self.service.scope]
        for column,value in [('r.skill_id',skill_id),('r.experiment_id',experiment_id),('r.source_release_id',release_id),('r.group_key',group_key),('r.run_status',status)]:
            if value is not None:where.append(column+'=?');params.append(value)
        if model:
            where.append('(r.resolved_model=? OR r.requested_model=?)');params.extend([model,model])
        for column,value,operator in [('r.created_at',created_after,'>='),('r.created_at',created_before,'<=')]:
            if value:
                try:
                    instant=datetime.fromisoformat(value.replace('Z','+00:00'))
                    if instant.tzinfo is None:raise ValueError('Timezone required')
                except (ValueError,AttributeError):raise ValueError('Invalid history timestamp; use ISO 8601 with timezone')
                where.append(column+operator+'?');params.append(instant.astimezone(timezone.utc).isoformat())
        if cursor:
            data=cursor_decode(cursor)
            if not isinstance(data.get('time'),str) or not isinstance(data.get('id'),str):raise ValueError('Invalid history cursor')
            where.append('(r.created_at,r.id)<(?,?)');params.extend([data['time'],data['id']])
        sql='''SELECT r.id,r.created_at,r.run_status,r.generation_status,r.agent,r.requested_model,r.requested_provider,
            r.resolved_model,r.resolved_provider,r.group_key,r.experiment_id,r.skill_id,r.skill_version_id,
            r.source_release_label,r.source_release_revision,e.name AS experiment_name,s.name AS skill_name,
            substr(coalesce(n.text,''),1,160) AS note_summary FROM skill_eval_runs r
            JOIN skill_eval_skills s ON s.id=r.skill_id JOIN skill_eval_experiments e ON e.id=r.experiment_id
            LEFT JOIN skill_eval_notes n ON n.run_id=r.id WHERE '''+' AND '.join(where)+' ORDER BY r.created_at DESC,r.id DESC LIMIT ?'
        with self.repo.connection() as c:rows=[dict(row) for row in c.execute(sql,(*params,limit+1))]
        items=rows[:limit];last=items[-1] if items else None
        return {'items':items,'next_cursor':cursor_encode({'time':last['created_at'],'id':last['id']}) if len(rows)>limit and last else None}

    def groups(self,*,limit=50,cursor=None):
        if not 1<=limit<=100:raise ValueError('Group limit must be 1–100')
        previous=cursor_decode(cursor) if cursor else None
        with self.repo.connection() as c:
            boundary=previous.get('boundary') if previous else c.execute('SELECT coalesce(max(rowid),0) FROM skill_eval_runs').fetchone()[0]
            if not isinstance(boundary,int):raise ValueError('Invalid group cursor')
            params=[self.service.scope,boundary]
            having=''
            if previous:
                if not isinstance(previous.get('time'),str) or not isinstance(previous.get('key'),str):raise ValueError('Invalid group cursor')
                having=' HAVING (max(r.created_at),r.group_key)<(?,?)';params.extend([previous['time'],previous['key']])
            sql='''SELECT r.group_key,r.agent,r.requested_model,r.requested_provider,r.resolved_model,r.resolved_provider,
                count(*) AS run_count,max(r.created_at) AS last_run_at FROM skill_eval_runs r JOIN skill_eval_skills s ON s.id=r.skill_id
                WHERE s.scope_id=? AND r.rowid<=? GROUP BY r.group_key'''+having+' ORDER BY last_run_at DESC,r.group_key DESC LIMIT ?'
            rows=[dict(row) for row in c.execute(sql,(*params,limit+1))]
        items=rows[:limit];last=items[-1] if items else None
        return {'items':items,'next_cursor':cursor_encode({'boundary':boundary,'time':last['last_run_at'],'key':last['group_key']}) if len(rows)>limit and last else None}

    @guarded
    def compare(self,run_ids,baseline_run_id=None):
        if not 2<=len(run_ids)<=100 or len(set(run_ids))!=len(run_ids):raise ValueError('Select 2–100 distinct runs')
        baseline=baseline_run_id or run_ids[0]
        if baseline not in run_ids:raise ValueError('Baseline must be selected')
        columns=[];accessible=0
        for id_ in run_ids:
            try:run=self.service.get_run(id_)
            except EvalError as error:
                if error.status!=404:raise
                columns.append({'run_id':id_,'missing':True,'reason':'unavailable'});continue
            if run['run_status'] not in TERMINAL:raise EvalError('run_not_ended','Finish each run before comparing final results')
            accessible+=1
            columns.append({'run_id':id_,'skill_id':run['skill_id'],'skill_name':run['skill']['name'],'experiment_name':run['experiment']['name'],
                'snapshot_id':run['skill_version_id'],'status':run['run_status'],'requested_model':run['requested_model'],
                'resolved_model':run['resolved_model'],'result':run['final_result'],'note':run['note'],
                'partial':run['run_status']!='completed' or not run['final_result'] or not run['final_result']['complete']})
        if accessible<2:raise EvalError('insufficient_runs','At least two accessible final results are required',422)
        manifest={'id':new_id(),'baseline_run_id':baseline,'columns':columns,'created_at':self.service._time()}
        with self.repo.transaction() as c:
            c.execute('INSERT INTO skill_eval_comparisons(id,scope_id,baseline_run_id,manifest_json,created_at) VALUES(?,?,?,?,?)',(manifest['id'],self.service.scope,baseline,json.dumps(manifest,ensure_ascii=False),manifest['created_at']))
        return manifest

    def comparison(self,id_):
        with self.repo.connection() as c:
            row=self.service._row(c,'SELECT * FROM skill_eval_comparisons WHERE id=? AND scope_id=?',(id_,self.service.scope))
        return json.loads(row['manifest_json'])

    def diff(self,id_,left,right,*,kind='skill',path=None):
        comparison=self.comparison(id_)
        columns={c['run_id']:c for c in comparison['columns'] if not c.get('missing')}
        if left not in columns or right not in columns:raise EvalError('not_found','Comparison column unavailable',404)
        a,b=columns[left],columns[right]
        if kind=='result':
            texts=[((column.get('result') or {}).get('final_reply') or {}).get('text','') for column in (a,b)]
        elif kind=='artifact':
            trees=[{entry['relative_path']:entry for entry in (column.get('result') or {}).get('artifacts',[])} for column in (a,b)]
            if path is None:
                return {'files':[{'path':name,'changed':(trees[0].get(name) or {}).get('hash')!=(trees[1].get(name) or {}).get('hash')} for name in sorted(set(trees[0])|set(trees[1]))]}
            entries=[tree.get(path) for tree in trees]
            if any(entry and entry.get('state')!='complete' for entry in entries):raise EvalError('artifact_unavailable','Final artifact unavailable',410)
            if any(entry and (entry.get('size') or 0)>1024*1024 for entry in entries):raise EvalError('diff_too_large','Artifact diff exceeds the limit',413)
            try:texts=[self.service.store.read_blob(entry['hash']).decode('utf-8') if entry else '' for entry in entries]
            except UnicodeDecodeError:return {'binary':True,'diff':None}
            if any('\x00' in text for text in texts):return {'binary':True,'diff':None}
        elif kind=='skill':
            with self.repo.connection() as c:
                versions=[self.service._version(c,column['snapshot_id']) for column in (a,b)]
            trees=[{entry.path:entry for entry in self.service.store.get_manifest(version['manifest_ref'])} for version in versions]
            if path is None:
                names=sorted(set(trees[0])|set(trees[1]))
                return {'files':[{'path':name,'left_hash':trees[0].get(name).hash if name in trees[0] else None,'right_hash':trees[1].get(name).hash if name in trees[1] else None,
                                  'changed':((trees[0][name].hash,trees[0][name].mode) if name in trees[0] else None)!=((trees[1][name].hash,trees[1][name].mode) if name in trees[1] else None)} for name in names]}
            entries=[tree.get(path) for tree in trees]
            if any(entry and not entry.encoding for entry in entries):return {'binary':True,'diff':None}
            if any(entry and entry.size>1024*1024 for entry in entries):raise EvalError('diff_too_large','Text diff exceeds the editor limit',413)
            texts=[self.service.store.read_blob(entry.hash).decode('utf-8') if entry else '' for entry in entries]
        else:raise ValueError('Unknown comparison diff kind')
        if any(len(text.encode())>1024*1024 for text in texts):raise EvalError('diff_too_large','Text diff size limit',413)
        return {'binary':False,'diff':'\n'.join(difflib.unified_diff(texts[0].splitlines(),texts[1].splitlines(),fromfile=left,tofile=right))}
