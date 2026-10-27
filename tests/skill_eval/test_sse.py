from fastapi import FastAPI
from backend.skill_eval.api import register_routes as assets
from backend.skill_eval.run_api import register_routes as runs
from backend.skill_eval.events import EventStore
from backend.skill_eval.capture import Capture
from .test_create_runs import run_service,prepared
from .test_api_contract import Catalog


async def test_sse_backfills_committed_ids_and_resets_an_ahead_cursor(run_service):
    service=run_service;_,body=prepared(service)
    id_=service.create_run(body,idempotency_key='r',allowed_models=['fixture'])['run_id']
    store=EventStore(service.repo,Capture());store.append(id_,'public:event',{'content':'BODY_NOT_IN_SSE'})
    app=FastAPI();assets(app,service);runs(app,service,Catalog())
    endpoint=next(route.endpoint for route in app.routes if getattr(route,'path','')=='/api/skill-eval/runs/{id_}/events/stream')
    class Connected:
        async def is_disconnected(self):return False
    response=await endpoint(id_,Connected(),after=999,last_event_id=None)
    iterator=response.body_iterator
    try:
        reset=await anext(iterator);assert 'event: reset' in reset and 'cursor_ahead_of_stream' in reset
        event=await anext(iterator);assert 'id: 1' in event and 'public:event' in event and 'BODY_NOT_IN_SSE' not in event
    finally:await iterator.aclose()
    resumed=await endpoint(id_,Connected(),after=0,last_event_id='0')
    try:assert 'id: 1' in await anext(resumed.body_iterator)
    finally:await resumed.body_iterator.aclose()
