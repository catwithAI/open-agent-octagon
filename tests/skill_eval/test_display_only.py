import httpx
from fastapi import FastAPI
from backend.skill_eval.api import register_routes as assets,create_app
from backend.skill_eval.run_api import register_routes as runs
from backend.skill_eval.worker import Worker
from .test_create_runs import run_service,prepared
from .test_api_contract import Catalog
from .fakes import FakeExecutionRuntime


async def test_all_write_endpoints_are_blocked_but_history_is_readable(run_service):
    service=run_service;item,body=prepared(service)
    id_=service.create_run(body,idempotency_key='r',allowed_models=['fixture'])['run_id']
    await Worker(service,FakeExecutionRuntime(service.store)).tick()
    service.settings.octagon.display_only=True
    app=create_app(service,display_only=True);runs(app,service,Catalog())
    draft=item['draft']['id'];rel=item['release']['id']
    writes=[('POST','/skills/import',{}),('POST','/skills/from-catalog',{}),('POST',f"/imports/{item['skill']['id']}/commit",{}),('POST','/drafts',{'release_id':rel}),('PATCH',f'/drafts/{draft}',{'expected_revision':1,'changes':[]}),('POST',f'/drafts/{draft}/save',{}),('POST',f'/drafts/{draft}/publish',{}),('POST',f'/drafts/{draft}/snapshots',{}),('POST','/inputs',{}),('POST','/experiments',{}),('PATCH',f"/experiments/{body['experiment_id']}",{}),('POST','/runs',body),('POST',f'/runs/{id_}/messages',{'text':'blocked'}),('POST',f'/runs/{id_}/interactions/unknown/answer',{}),('POST',f'/runs/{id_}/stop-generation',{}),('POST',f'/runs/{id_}/finish',{}),('PUT',f'/runs/{id_}/note',{'text':'blocked','expected_revision':0}),('POST',f'/runs/{id_}/drafts',{}),('POST',f'/runs/{id_}/collect',{}),('POST','/rerun-plans',{}),('POST','/rerun-plans/unknown/confirm',{})]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://local') as client:
        for method,path,body in writes:
            result=await client.request(method,'/api/skill-eval'+path,json=body,headers={'Idempotency-Key':'blocked'})
            assert result.status_code==403,(path,result.text)
        assert (await client.get(f'/api/skill-eval/runs/{id_}')).status_code==200
        assert (await client.get('/api/skill-eval/runs')).json()['items']
        assert service.get_run(id_)['note']['text']==''
