"""Cross-module failures must preserve evidence without repeating remote writes."""
import asyncio
import pytest
from backend.skill_eval.artifacts import ArtifactCollector
from backend.skill_eval.queries import Queries
from backend.skill_eval.service import EvalError
from backend.skill_eval.worker import Worker
from .fakes import FakeExecutionRuntime,Gate
from .test_create_runs import run_service,prepared


async def test_attachment_failure_preserves_original_and_does_not_send(run_service):
    service=run_service;_,body=prepared(service);input_=service.upload_input('file.txt',b'original')
    id_=service.create_run({**body,'input_ids':[input_['id']]},idempotency_key='r',allowed_models=['fixture'])['run_id']
    runtime=FakeExecutionRuntime(service.store);runtime.failures['input_upload']=RuntimeError('upload failed')
    await Worker(service,runtime).tick()
    assert runtime.count('create')==1 and runtime.count('chat')==0
    assert service.read_input(input_['id'])==b'original'
    assert service.get_run(id_)['run_status']=='failed'
    assert not await Worker(service,runtime).tick()


async def test_accepted_stream_failure_never_replays_and_can_collect_partial(run_service):
    class BrokenStream(FakeExecutionRuntime):
        async def chat(self,*args,**kwargs):
            async for event in super().chat(*args,**kwargs):
                yield event
                if event['kind']=='chat:start':raise ConnectionError('stream disconnected')
    service=run_service;_,body=prepared(service)
    id_=service.create_run(body,idempotency_key='r',allowed_models=['fixture'])['run_id']
    runtime=BrokenStream(service.store);worker=Worker(service,runtime);await worker.tick()
    assert service.get_run(id_)['error_code']=='acceptance_unknown'
    assert runtime.count('chat')==1 and runtime.count('create')==1
    assert not await Worker(service,runtime).tick()
    # Explicit read-only recovery targets the accepted generation, never chats.
    with service.repo.transaction() as c:service._job(c,id_,'explicit-collection','collect')
    await worker.tick()
    assert runtime.count('chat')==1 and runtime.count('stop')==1
    assert service.get_run(id_)['final_result']['final_reply'] is None


async def test_finish_closes_a_queued_followup_without_sending_it(run_service):
    service=run_service;_,body=prepared(service)
    id_=service.create_run(body,idempotency_key='r',allowed_models=['fixture'])['run_id']
    runtime=FakeExecutionRuntime(service.store);worker=Worker(service,runtime);await worker.tick()
    service.send_message(id_,{'text':'queued before finish'},idempotency_key='m')
    service.finish(id_)
    with pytest.raises(EvalError):service.send_message(id_,{'text':'late'},idempotency_key='late')
    await worker.tick();await worker.tick()
    assert runtime.count('chat')==1
    assert service.get_run(id_)['run_status']=='completed'
    assert service.messages(id_)[1]['delivery_state']=='cancelled'


async def test_final_collection_retry_does_not_change_frozen_comparison(run_service):
    service=run_service;_,body=prepared(service);runtime=FakeExecutionRuntime(service.store);worker=Worker(service,runtime);ids=[]
    for i in range(2):
        id_=service.create_run(body,idempotency_key=str(i),allowed_models=['fixture'])['run_id'];ids.append(id_)
        await worker.tick();service.finish(id_)
        if i==0:runtime.failures['download']=OSError('temporary')
        await worker.tick();runtime.failures.clear()
    query=Queries(service);comparison=query.compare(ids)
    assert comparison['columns'][0]['partial']
    old=service.get_run(ids[0])['final_result']['artifacts'][0]
    with service.repo.transaction() as c:service._job(c,ids[0],'retry-collect','collect')
    await worker.tick()
    new=service.get_run(ids[0])['final_result']['artifacts'][0]
    assert new['state']=='complete' and new['id']==old['id']
    assert query.comparison(comparison['id'])==comparison
    assert query.diff(comparison['id'],*ids,kind='artifact')['files']
    with pytest.raises(EvalError):query.diff(comparison['id'],*ids,kind='artifact',path='report.txt')


async def test_input_verification_does_not_consume_artifact_budget(run_service):
    service=run_service;_,body=prepared(service);input_=service.upload_input('big.txt',b'x'*100)
    id_=service.create_run({**body,'input_ids':[input_['id']]},idempotency_key='r',allowed_models=['fixture'])['run_id']
    runtime=FakeExecutionRuntime(service.store);await Worker(service,runtime).tick()
    result=await ArtifactCollector(service,runtime,max_file=20,max_total=20).collect(service.get_run(id_))
    assert result['status']=='complete'
    assert [a['relative_path'] for a in result['artifacts']]==['report.txt']
    assert not result['observations']
