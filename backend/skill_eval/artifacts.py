"""Final-file collection with explicit gaps and immutable original inputs."""
from __future__ import annotations

import json
import mimetypes

from .service import new_id
from .storage import digest,safe_path


class ArtifactCollector:
    def __init__(self,service,runtime,*,max_file=50*1024*1024,max_total=200*1024*1024,max_files=1000):
        self.service,self.runtime=service,runtime
        self.max_file,self.max_total,self.max_files=max_file,max_total,max_files

    async def collect(self,run):
        session_id=run['external_refs']['session_id']
        rows=[];gaps=[];observations=[];total=0
        inputs={item['remote_path']:item for message in self.service.messages(run['id']) for item in message['attachment_refs']}
        try:files=await self.runtime.list_files(session_id,max_entries=self.max_files)
        except Exception as error:return {'status':'partial','gaps':[{'reason':'directory_unavailable','error_type':type(error).__name__}],'artifacts':[],'observations':[]}
        listed={entry['path'] for entry in files}
        for path in sorted(set(inputs)-listed):
            observations.append({'kind':'input_not_listed','path':path,'scope':'unsupported_input_changes'})
        for entry in files:
            path=safe_path(entry['path'])
            if entry.get('state')=='skipped':
                gaps.append({'path':path,'reason':entry.get('reason','unsupported_entry')});continue
            # Skill source files are outside the normal working directory. Do not
            # accidentally collect a skills cache even if exposed by a deployment.
            if path.startswith(('.agents/','.blade/','.git/')):continue
            max_bytes=self.service.input_max_bytes if path in inputs else min(self.max_file,self.max_total-total)
            row={'id':new_id(),'run_id':run['id'],'relative_path':path,'hash':None,'size':None,
                 'media_type':mimetypes.guess_type(path)[0] or 'application/octet-stream','state':'missing','error':None}
            if max_bytes<=0:
                row['error']='artifact_total_limit';rows.append(row);gaps.append({'path':path,'reason':row['error']});continue
            try:
                data=await self.runtime.download(session_id,path,max_bytes=max_bytes)
                # A second read detects common concurrent changes. This is not a
                # claim of a cross-file atomic filesystem snapshot.
                second=await self.runtime.download(session_id,path,max_bytes=max_bytes)
                if digest(data)!=digest(second):raise ValueError('file_changed_during_collection')
                hash_=digest(data)
                if path in inputs:
                    if hash_!=inputs[path]['hash']:
                        observations.append({'kind':'input_modified','path':path,'scope':'unsupported_input_changes'})
                    continue
                with self.service.store.guard(),self.service.repo.transaction() as c:
                    self.service.store.put_blob(data)
                    c.execute('INSERT OR IGNORE INTO skill_eval_blobs(hash,size,storage_ref,created_at) VALUES(?,?,?,?)',(hash_,len(data),hash_,self.service._time()))
                    row.update(hash=hash_,size=len(data),state='complete')
                    # Upsert only the final index. No per-generation file versions.
                    c.execute('''INSERT INTO skill_eval_artifacts(id,run_id,relative_path,hash,size,media_type,state,error,provenance_json)
                        VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id,relative_path) DO UPDATE SET
                        hash=excluded.hash,size=excluded.size,media_type=excluded.media_type,state=excluded.state,error=excluded.error,provenance_json=excluded.provenance_json''',
                        (row['id'],run['id'],path,hash_,len(data),row['media_type'],'complete',None,json.dumps({'source':'final_remote_workspace','consistency':'two_matching_reads'})))
                    actual=c.execute('SELECT id FROM skill_eval_artifacts WHERE run_id=? AND relative_path=?',(run['id'],path)).fetchone()
                    row['id']=actual['id']
                total+=len(data)
            except Exception as error:
                row['state']='missing';row['error']=type(error).__name__
                gaps.append({'path':path,'reason':row['error']})
                if path in inputs:
                    observations.append({'kind':'input_verification_unavailable','path':path,'scope':'unsupported_input_changes'})
                    continue
            rows.append(row)
        with self.service.repo.transaction() as c:
            current={row['relative_path'] for row in rows}
            for previous in c.execute('SELECT id,relative_path FROM skill_eval_artifacts WHERE run_id=?',(run['id'],)).fetchall():
                if previous['relative_path'] not in current:
                    c.execute("UPDATE skill_eval_artifacts SET state='missing',error='not_in_final_listing' WHERE id=?",(previous['id'],))
            for row in rows:
                if row['state']=='complete':continue
                c.execute('''INSERT INTO skill_eval_artifacts(id,run_id,relative_path,hash,size,media_type,state,error,provenance_json)
                    VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id,relative_path) DO UPDATE SET
                    hash=NULL,size=NULL,state=excluded.state,error=excluded.error''',
                    (row['id'],run['id'],row['relative_path'],None,None,row['media_type'],row['state'],row['error'],'{}'))
                row['id']=c.execute('SELECT id FROM skill_eval_artifacts WHERE run_id=? AND relative_path=?',(run['id'],row['relative_path'])).fetchone()['id']
        return {'status':'partial' if gaps else 'complete','artifacts':rows,'gaps':gaps,'observations':observations,
                'consistency':'per_file_rechecked_not_atomic','background_effects':'not_verified'}
