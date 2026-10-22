import httpx
from fastapi import FastAPI
from backend.skill_eval.api import register_routes as assets
from backend.skill_eval.run_api import register_routes as runs
from backend.skill_eval.runs import RunService
from backend.skill_eval.worker import Worker
from backend.skill_eval.queries import Queries
from .test_create_runs import run_service,prepared
from .fakes import FakeExecutionRuntime


class Catalog:
    def local(self):return {'enabled':True,'display_only':False}
    async def discover(self):return {'models':[{'id':'fixture'}]}


async def test_all_resource_gets_and_writes_enforce_scope(run_service):
    owner=run_service;item,body=prepared(owner);ids=[]
    runtime=FakeExecutionRuntime(owner.store);worker=Worker(owner,runtime)
    for i in range(2):
        id_=owner.create_run(body,idempotency_key=str(i),allowed_models=['fixture'])['run_id'];ids.append(id_)
        await worker.tick();owner.finish(id_);await worker.tick()
    artifact=owner.get_run(ids[0])['final_result']['artifacts'][0]['id']
    comparison=Queries(owner).compare(ids)
    outsider=RunService(owner.repo.db_path,owner.store.root.parent,scope='other',settings=owner.settings)
    # Use the same immutable store; authorization must fail before touching it.
    outsider.store=owner.store
    app=FastAPI();assets(app,outsider);runs(app,outsider,Catalog())
    paths=[f"/skills/{item['skill']['id']}",f"/releases/{item['release']['id']}",f"/releases/{item['release']['id']}/export",f"/drafts/{item['draft']['id']}",f"/versions/{item['snapshot']['id']}",f"/versions/{item['snapshot']['id']}/files?path=SKILL.md",f"/versions/{item['snapshot']['id']}/export",f'/artifacts/{artifact}',f'/artifacts/{artifact}/preview',f"/comparisons/{comparison['id']}"]
    paths.extend(f'/runs/{ids[0]}'+suffix for suffix in ('','/messages','/interactions','/process','/events','/artifacts','/skill/export'))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://local') as client:
        for path in paths:
            result=await client.get('/api/skill-eval'+path)
            assert result.status_code==404,(path,result.text)
        for suffix in ('/stop-generation','/finish'):
            assert (await client.post(f'/api/skill-eval/runs/{ids[0]}'+suffix)).status_code==404
        assert (await client.get('/api/skill-eval/runs')).json()['items']==[]
        assert (await client.get('/api/skill-eval/record-groups')).json()['items']==[]


async def test_run_capture_request_defaults_full_and_respects_explicit_policy(run_service):
    service=run_service;_,body=prepared(service)
    service.settings.octagon.wire_capture_enabled=True
    service.settings.octagon.wire_capture_max_policy='full'
    app=FastAPI();assets(app,service);runs(app,service,Catalog())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://local') as client:
        for policy in (None,'off','metadata','parsed','full'):
            request=dict(body)
            if policy is not None:request['capture_policy']=policy
            response=await client.post('/api/skill-eval/runs',json=request,headers={'Idempotency-Key':str(policy)})
            assert response.status_code==202,response.text
            run=service.get_run(response.json()['run_id'])
            assert run['input_snapshot']['capture']['policy']['requested']==(policy or 'full')
            assert run['input_snapshot']['capture']['policy']['effective']==(policy or 'full')
        invalid=await client.post('/api/skill-eval/runs',json={**body,'capture_policy':'invented'},headers={'Idempotency-Key':'bad'})
        assert invalid.status_code==422
