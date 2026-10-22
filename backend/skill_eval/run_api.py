"""Execution, evidence and control HTTP contracts for Skill Eval sessions."""
from __future__ import annotations

import asyncio
import json
from typing import Any,Literal
from urllib.parse import quote

from fastapi import APIRouter,FastAPI,Header,HTTPException,Request
from fastapi.responses import Response,StreamingResponse
from pydantic import Field
from starlette.concurrency import run_in_threadpool

from .api import RequestBody
from .capture import Capture
from .events import EventStore
from .process import project
from .service import EvalError


class ModelSelection(RequestBody):
    id:str=Field(min_length=1,max_length=300)
    provider:str|None=None


class CreateRun(RequestBody):
    experiment_id:str
    snapshot_id:str
    draft_id:str|None=None
    release_id:str|None=None
    agent:str='blade-agent'
    capture_policy:Literal['off','metadata','parsed','full']='full'
    model:ModelSelection
    prompt:str=Field(default='',max_length=1000000)
    input_ids:list[str]=Field(default_factory=list,max_length=1000)
    source_run_id:str|None=None
    rerun_plan_id:str|None=None


class StructuredAnswer(RequestBody):
    selections:dict[str,list[int]]=Field(default_factory=dict)
    custom:dict[str,str]=Field(default_factory=dict)


class Message(RequestBody):
    answer:StructuredAnswer|None=None
    text:str=Field(default='',max_length=1000000)
    input_ids:list[str]=Field(default_factory=list,max_length=1000)
    expected_revision:int|None=Field(default=None,ge=1)


class Note(RequestBody):
    text:str
    expected_revision:int=Field(ge=0)


class RerunPlan(RequestBody):
    source_run_id:str
    mode:str


class ConfirmPlan(RequestBody):
    expected_hash:str
    prompt:str=Field(default='',max_length=1000000)
    input_ids:list[str]=Field(default_factory=list,max_length=1000)


class Compare(RequestBody):
    run_ids:list[str]=Field(min_length=2,max_length=100)
    baseline_run_id:str|None=None


def register_routes(app:FastAPI,service,capabilities):
    router=APIRouter(prefix='/api/skill-eval',tags=['skill-eval'])
    store=EventStore(service.repo,Capture())

    def call(function,*args,**kwargs):
        try:return function(*args,**kwargs)
        except ValueError as error:raise HTTPException(422,detail={'code':'invalid_run_input','message':str(error)}) from error

    @router.post('/runs',status_code=202)
    async def create_run(body:CreateRun,idempotency_key:str=Header(alias='Idempotency-Key')):
        service._writable(execute=True)
        models=await capabilities.discover()
        return await run_in_threadpool(call,service.create_run,body.model_dump(exclude_none=True),idempotency_key=idempotency_key,allowed_models=[model['id'] for model in models['models']])

    @router.get('/runs/{id_}')
    def run(id_:str):return call(service.get_run,id_)

    @router.get('/runs/{id_}/messages')
    def messages(id_:str):return {'items':call(service.messages,id_)}

    @router.post('/runs/{id_}/messages',status_code=202)
    def send(id_:str,body:Message,idempotency_key:str=Header(alias='Idempotency-Key')):
        return call(service.send_message,id_,body.model_dump(exclude_none=True),idempotency_key=idempotency_key)

    @router.get('/runs/{id_}/interactions')
    def interactions(id_:str):return {'items':call(service.interactions,id_)}

    @router.post('/runs/{id_}/interactions/{interaction_id}/answer',status_code=202)
    def answer(id_:str,interaction_id:str,body:Message,idempotency_key:str=Header(alias='Idempotency-Key')):
        return call(service.send_message,id_,body.model_dump(exclude_none=True),idempotency_key=idempotency_key,interaction_id=interaction_id)

    @router.post('/runs/{id_}/stop-generation',status_code=202)
    def stop(id_:str):return call(service.stop_generation,id_)

    @router.post('/runs/{id_}/finish',status_code=202)
    def finish(id_:str):return call(service.finish,id_)

    @router.put('/runs/{id_}/note')
    def note(id_:str,body:Note):return call(service.save_note,id_,body.text,body.expected_revision)

    @router.get('/runs/{id_}/events')
    def events(id_:str,after:int=0,limit:int=100):
        call(service.get_run,id_)
        return call(store.read,id_,after=after,limit=limit)

    @router.get('/runs/{id_}/evidence/{event_id}')
    def evidence(id_:str,event_id:str):
        call(service.get_run,id_)
        item=store.get(id_,event_id)
        if item is None:raise EvalError('not_found','Evidence not found',404)
        return item

    def all_events(id_,through=None):
        cursor=0
        while True:
            page=store.read(id_,after=cursor,limit=500)
            for item in page['items']:
                if through is not None and item['seq']>through:return
                yield item
            cursor=page['next_cursor']
            if not page['has_more']:break

    @router.get('/runs/{id_}/process')
    def process(id_:str,limit:int=100,cursor:str|None=None,search:str=''):
        call(service.get_run,id_)
        if not 1<=limit<=500 or len(search)>200:raise HTTPException(422,'Invalid process limit/search')
        from .queries import cursor_decode,cursor_encode
        head=store.head(id_);offset=0
        if cursor:
            token=call(cursor_decode,cursor)
            if token.get('schema')!=1:raise EvalError('reset_required','Projection schema changed; reopen the process',409)
            if token.get('run_id')!=id_ or token.get('search')!=search or not isinstance(token.get('head'),int) or not isinstance(token.get('offset'),int) or token['offset']<0:raise HTTPException(422,'Invalid process cursor')
            if token['head']>head or token['head']<0:raise EvalError('snapshot_required','Process cursor is unavailable; reopen the process',409)
            head=token['head'];offset=token['offset']
        value=project(all_events(id_,head));turns=value['turns']
        if search:
            matched={turn['id'] for turn in turns if search.casefold() in json.dumps(turn,ensure_ascii=False).casefold()}
            matched.update(action['turn_id'] for action in value['actions'] if search.casefold() in json.dumps(action,ensure_ascii=False).casefold())
            turns=[turn for turn in turns if turn['id'] in matched]
        total=len(turns);selected=turns[offset:offset+limit];identities={turn['id'] for turn in selected}
        return {**value,'turns':selected,'actions':[action for action in value['actions'] if action['turn_id'] in identities],
                'cursor':head,'projection_generation':f'1:{head}','total_turns':total,
                'next_cursor':cursor_encode({'schema':1,'run_id':id_,'head':head,'offset':offset+len(selected),'search':search}) if offset+len(selected)<total else None}

    @router.get('/runs/{id_}/events/stream')
    async def stream(id_:str,request:Request,after:int=0,last_event_id:str|None=Header(default=None)):
        await run_in_threadpool(service.get_run,id_)
        try:cursor=int(last_event_id) if last_event_id else after
        except ValueError:raise HTTPException(422,'Invalid event cursor')
        if cursor<0:raise HTTPException(422,'Invalid event cursor')
        async def generate():
            nonlocal cursor
            heartbeat=0
            head=await run_in_threadpool(store.head,id_)
            if cursor>head:
                cursor=0
                yield 'id: 0\nevent: reset\ndata: '+json.dumps({'seq':0,'reason':'cursor_ahead_of_stream'})+'\n\n'
            while not await request.is_disconnected():
                page=await run_in_threadpool(store.read,id_,after=cursor,limit=100)
                for event in page['items']:
                    cursor=event['seq']
                    # Small notifications; clients fetch policy-filtered evidence
                    # by ID rather than embedding large bodies in every SSE frame.
                    yield f'id: {cursor}\nevent: event\ndata: '+json.dumps({'seq':cursor,'event_id':event['event_id'],'type':event['type']})+'\n\n'
                if not page['has_more']:
                    heartbeat+=1
                    if heartbeat%10==0:yield ': heartbeat\n\n'
                    await asyncio.sleep(0.5)
        return StreamingResponse(generate(),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

    @router.get('/runs/{id_}/artifacts')
    def artifacts(id_:str):
        run=call(service.get_run,id_)
        return {'status':run['artifact_status'],'items':run['final_result']['artifacts'] if run['final_result'] else []}

    @router.get('/artifacts/{artifact_id}/preview')
    def artifact_preview(artifact_id:str):
        with service.repo.connection() as c:
            row=service._row(c,'SELECT a.* FROM skill_eval_artifacts a JOIN skill_eval_runs r ON r.id=a.run_id JOIN skill_eval_skills s ON s.id=r.skill_id WHERE a.id=? AND s.scope_id=?',(artifact_id,service.scope))
        if row['state']!='complete' or not row['hash']:raise EvalError('artifact_unavailable','Artifact unavailable',410)
        from .previews import descriptor
        return descriptor(service.store,row['hash'],row['relative_path'],artifact_id)

    @router.get('/artifacts/{artifact_id}')
    def artifact(artifact_id:str):
        with service.repo.connection() as c:
            row=service._row(c,'SELECT a.* FROM skill_eval_artifacts a JOIN skill_eval_runs r ON r.id=a.run_id JOIN skill_eval_skills s ON s.id=r.skill_id WHERE a.id=? AND s.scope_id=?',(artifact_id,service.scope))
        if row['state']!='complete' or not row['hash']:raise EvalError('artifact_unavailable','Artifact is unavailable',410)
        data=service.store.read_blob(row['hash'])
        filename=quote(row['relative_path'].split('/')[-1],safe='')
        return Response(data,media_type='application/octet-stream',headers={'Content-Disposition':f"attachment; filename*=UTF-8''{filename}",'X-Content-Type-Options':'nosniff'})

    from .queries import Queries
    from .reruns import Reruns
    query=Queries(service);reruns=Reruns(service)

    @router.get('/runs')
    def history(skill_id:str|None=None,experiment_id:str|None=None,release_id:str|None=None,group_key:str|None=None,status:str|None=None,model:str|None=None,created_after:str|None=None,created_before:str|None=None,limit:int=50,cursor:str|None=None):
        return call(query.history,skill_id=skill_id,experiment_id=experiment_id,release_id=release_id,group_key=group_key,status=status,model=model,created_after=created_after,created_before=created_before,limit=limit,cursor=cursor)

    @router.get('/record-groups')
    def groups(limit:int=50,cursor:str|None=None):return call(query.groups,limit=limit,cursor=cursor)

    @router.post('/runs/{id_}/drafts',status_code=201)
    def refine(id_:str):return call(service.create_run_draft,id_)

    @router.get('/runs/{id_}/skill/export')
    def export_run(id_:str):
        return Response(call(service.export_run_skill,id_),media_type='application/zip',headers={'Content-Disposition':'attachment; filename="skill.zip"'})

    @router.post('/rerun-plans',status_code=201)
    def plan(body:RerunPlan):return call(reruns.create,body.source_run_id,body.mode)

    @router.get('/rerun-plans/{id_}')
    def get_plan(id_:str):return call(reruns.get,id_)

    @router.post('/rerun-plans/{id_}/confirm')
    def confirm(id_:str,body:ConfirmPlan):return call(reruns.confirm,id_,body.expected_hash,prompt=body.prompt,input_ids=body.input_ids)

    @router.post('/comparisons/query')
    def compare(body:Compare):return call(query.compare,body.run_ids,body.baseline_run_id)

    @router.get('/comparisons/{id_}')
    def comparison(id_:str):return call(query.comparison,id_)

    @router.get('/comparisons/{id_}/diff')
    def diff(id_:str,left:str,right:str,kind:str='skill',path:str|None=None):return call(query.diff,id_,left,right,kind=kind,path=path)

    @router.get('/comparisons/{id_}/artifact')
    def fixed_artifact(id_:str,run_id:str,path:str):
        manifest=call(query.comparison,id_)
        column=next((column for column in manifest['columns'] if column['run_id']==run_id and not column.get('missing')),None)
        artifact=next((item for item in ((column.get('result') or {}).get('artifacts',[]) if column else []) if item['relative_path']==path),None)
        if not artifact or not artifact.get('hash'):raise EvalError('artifact_unavailable','Frozen comparison artifact unavailable',410)
        return Response(service.store.read_blob(artifact['hash']),media_type='application/octet-stream',headers={'Content-Disposition':f"attachment; filename*=UTF-8''{quote(path.split('/')[-1],safe='')}",'X-Content-Type-Options':'nosniff'})

    @router.get('/comparisons/{id_}/artifact-preview')
    def fixed_artifact_preview(id_:str,run_id:str,path:str):
        manifest=call(query.comparison,id_)
        column=next((column for column in manifest['columns'] if column['run_id']==run_id and not column.get('missing')),None)
        artifact=next((item for item in ((column.get('result') or {}).get('artifacts',[]) if column else []) if item['relative_path']==path),None)
        if not artifact or not artifact.get('hash'):raise EvalError('artifact_unavailable','Comparison artifact unavailable',410)
        from .previews import descriptor
        return descriptor(service.store,artifact['hash'],path,id_+':'+run_id+':'+path)

    @router.post('/runs/{id_}/collect',status_code=202)
    def recollect(id_:str,idempotency_key:str=Header(alias='Idempotency-Key')):
        service._writable()
        with service.repo.transaction() as c:
            run=service._run(c,id_)
            if run['run_status'] not in ('completed','failed','cancelled'):raise EvalError('run_not_ended','Finish the run before recollecting')
            previous,hash_=service._idempotent(c,'collect:'+id_,idempotency_key,{})
            if not previous:
                service._job(c,id_,'collect:'+id_+':'+idempotency_key,'collect')
                service._receipt(c,'collect:'+id_,idempotency_key,hash_,id_)
        return {'run_id':id_,'status':'queued'}

    app.include_router(router)
