import httpx
from fastapi import FastAPI
from backend.skill_eval.api import register_routes as assets
from backend.skill_eval.run_api import register_routes as runs
from backend.skill_eval.events import EventStore
from backend.skill_eval.capture import Capture
from .test_create_runs import run_service,prepared
from .test_api_contract import Catalog


async def test_process_pages_freeze_source_head_and_reject_foreign_or_stale_cursors(run_service):
    service=run_service;_,body=prepared(service);ids=[service.create_run(body,idempotency_key=str(i),allowed_models=['fixture'])['run_id'] for i in range(2)]
    store=EventStore(service.repo,Capture())
    for i in range(3):store.append(ids[0],'turn:snapshot',{'payload':{'turn':{'id':str(i),'loop_id':'root','role':'assistant','blocks':[{'type':'text','content':f'message-{i}'}]}}})
    app=FastAPI();assets(app,service);runs(app,service,Catalog())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://local') as client:
        path='/api/skill-eval/runs/'+ids[0]+'/process'
        first=(await client.get(path,params={'limit':2})).json();assert len(first['turns'])==2
        store.append(ids[0],'turn:snapshot',{'payload':{'turn':{'id':'new','loop_id':'root','role':'assistant','blocks':[]}}})
        second=(await client.get(path,params={'limit':2,'cursor':first['next_cursor']})).json()
        assert [turn['id'] for turn in second['turns']]==['2']
        assert first['projection_generation']==second['projection_generation']
        assert len((await client.get(path)).json()['turns'])==4
        assert (await client.get(path,params={'search':'message-1'})).json()['turns'][0]['id']=='1'
        foreign=await client.get('/api/skill-eval/runs/'+ids[1]+'/process',params={'cursor':first['next_cursor']});assert foreign.status_code==422
        from backend.skill_eval.queries import cursor_encode,cursor_decode
        token=cursor_decode(first['next_cursor']);token['schema']=0
        assert (await client.get(path,params={'cursor':cursor_encode(token)})).json()['error']['code']=='reset_required'
        token['schema']=1;token['head']=999
        assert (await client.get(path,params={'cursor':cursor_encode(token)})).json()['error']['code']=='snapshot_required'
