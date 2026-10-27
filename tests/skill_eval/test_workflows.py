"""Local HTTP workflow, with real SQLite/files and a fake remote boundary."""
import io
import zipfile

import httpx
from fastapi import FastAPI

from backend.skill_eval.api import register_routes as assets
from backend.skill_eval.run_api import register_routes as runs
from backend.skill_eval.worker import Worker
from .test_create_runs import run_service
from .fakes import FakeExecutionRuntime


class Catalog:
    async def discover(self):return {'status':'available','models':[{'id':'fixture','label':'Fixture'}],'default':'fixture'}
    def local(self):return {'enabled':True,'display_only':False}


async def test_complete_http_workflow_preserves_versions_inputs_and_results(run_service):
    service=run_service;app=FastAPI();assets(app,service);runs(app,service,Catalog())
    runtime=FakeExecutionRuntime(service.store);worker=Worker(service,runtime)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://local') as client:
        async def post(path,body=None,key=None):
            result=await client.post('/api/skill-eval'+path,json=body or {},headers={'Idempotency-Key':key} if key else {})
            assert result.status_code<300,result.text
            return result.json()
        raw=b'---\nname: report\n---\nRead inputs and create a new report.'
        upload=await client.post('/api/skill-eval/skills/import',files={'file':('SKILL.md',raw)})
        initial=await post(f"/imports/{upload.json()['id']}/commit",{'name':'report'})
        skill=initial['skill']['id'];draft=initial['draft'];release=initial['release']
        experiment=await post('/experiments',{'skill_id':skill,'name':'first experiment'})
        snapshot=await post(f"/drafts/{draft['id']}/snapshots",{'expected_revision':1,'changes':[{'op':'write','path':'scripts/task.py','text':'print(1)'}]})
        body={'experiment_id':experiment['id'],'snapshot_id':snapshot['id'],'draft_id':draft['id'],'release_id':release['id'],'agent':'blade-agent','model':{'id':'fixture'},'prompt':'ask me'}
        first=await post('/runs',body,'r1');id1=first['run_id'];await worker.tick()
        current=(await client.get(f'/api/skill-eval/runs/{id1}')).json()
        assert current['run_status']=='waiting_input' and current['ended_at'] is None
        question=(await client.get(f'/api/skill-eval/runs/{id1}/interactions')).json()['items'][0]
        material=await client.post('/api/skill-eval/inputs',files={'file':('details.txt',b'original bytes')})
        answer=await post(f"/runs/{id1}/interactions/{question['id']}/answer",{'answer':{'selections':{'0':[0]},'custom':{}},'input_ids':[material.json()['id']],'expected_revision':1},'a1')
        await worker.tick();assert runtime.count('answer')==1
        await post(f'/runs/{id1}/messages',{'text':'Keep the source unchanged'},'m1');await worker.tick()
        await post(f'/runs/{id1}/finish');await worker.tick()
        assert (await client.get(f'/api/skill-eval/runs/{id1}')).json()['run_status']=='completed'
        note=await client.put(f'/api/skill-eval/runs/{id1}/note',json={'text':'NOTE_NOT_MODEL_INPUT','expected_revision':0});assert note.status_code==200
        refined=await post(f'/runs/{id1}/drafts')
        new_snapshot=await post(f"/drafts/{refined['id']}/snapshots",{'expected_revision':1,'changes':[{'op':'write','path':'scripts/task.py','text':'print(2)'}]})
        plan=await post('/rerun-plans',{'source_run_id':id1,'mode':'reuse_all'})
        confirmed=await post(f"/rerun-plans/{plan['id']}/confirm",{'expected_hash':plan['hash']})
        body2={**body,'snapshot_id':new_snapshot['id'],'draft_id':refined['id'],'prompt':'','source_run_id':id1,'rerun_plan_id':plan['id']}
        second=await post('/runs',body2,'r2');id2=second['run_id'];await worker.tick()
        question=(await client.get(f'/api/skill-eval/runs/{id2}/interactions')).json()['items'][0]
        await post(f"/runs/{id2}/interactions/{question['id']}/answer",{'text':'Alpha','expected_revision':1},'a2');await worker.tick()
        await post(f'/runs/{id2}/finish');await worker.tick()
        replacement=await post('/rerun-plans',{'source_run_id':id2,'mode':'replace'})
        await post(f"/rerun-plans/{replacement['id']}/confirm",{'expected_hash':replacement['hash'],'prompt':'','input_ids':[]})
        third=await post('/runs',{**body2,'source_run_id':id2,'rerun_plan_id':replacement['id']},'r3');id3=third['run_id'];await worker.tick();await post(f'/runs/{id3}/finish');await worker.tick()
        comparison=await post('/comparisons/query',{'run_ids':[id1,id2,id3],'baseline_run_id':id1})
        assert len(comparison['columns'])==3
        final=(await client.get(f'/api/skill-eval/runs/{id2}')).json()
        assert 'NOTE_NOT_MODEL_INPUT' not in str(final['input_snapshot'])
        assert service.read_input(material.json()['id'])==b'original bytes'
        assert runtime.count('create')==3
        assert runtime.count('chat')==4 and runtime.count('answer')==2
        published=await post(f"/drafts/{refined['id']}/publish",{'expected_revision':1,'expected_release_revision':1,'version':'1.0','changes':[{'op':'write','path':'scripts/task.py','text':'print(2)'}]})
        assert published['release']['status']=='published'
        exported=await client.get(f"/api/skill-eval/versions/{published['snapshot']['id']}/export")
        with zipfile.ZipFile(io.BytesIO(exported.content)) as z:
            assert z.read('scripts/task.py')==b'print(2)'
            assert b'NOTE_NOT_MODEL_INPUT' not in exported.content
        groups=(await client.get('/api/skill-eval/record-groups')).json()
        assert groups['items'][0]['run_count']==3
