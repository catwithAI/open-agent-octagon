import asyncio

import pytest

from backend.skill_eval.worker import Worker
from .fakes import FakeExecutionRuntime,Gate
from .test_create_runs import run_service,prepared


async def test_uploads_precede_chat_and_reply_does_not_end_run(run_service):
    service=run_service;_,body=prepared(service)
    attachment=service.upload_input('input.txt',b'original')
    body['input_ids']=[attachment['id']]
    id_=service.create_run(body,idempotency_key='run',allowed_models=['fixture'])['run_id']
    runtime=FakeExecutionRuntime(service.store);worker=Worker(service,runtime)
    assert await worker.tick()
    assert service.get_run(id_)['run_status']=='waiting_continue'
    assert service.get_run(id_)['ended_at'] is None
    calls=[op for op,_ in runtime.calls]
    assert calls[:4]==['create','skill_upload','input_upload','chat']
    assert runtime.count('create')==1
    service.send_message(id_,{'text':'followup'},idempotency_key='followup')
    assert await worker.tick()
    assert runtime.count('create')==1 and runtime.count('chat')==2
    assert service.get_run(id_)['run_status']=='waiting_continue'
    service.finish(id_)
    assert await worker.tick()
    run=service.get_run(id_)
    assert run['run_status']=='completed'
    result=__import__('json').loads(run['finalized_result_ref'])
    assert len(result['artifacts'])==1
    assert result['artifacts'][0]['relative_path']=='report.txt'
    assert service.read_input(attachment['id'])==b'original'
    assert not await worker.tick()


async def test_real_question_waits_for_explicit_answer_and_more_files(run_service):
    service=run_service;_,body=prepared(service);body['prompt']='ask me'
    id_=service.create_run(body,idempotency_key='run',allowed_models=['fixture'])['run_id']
    runtime=FakeExecutionRuntime(service.store);worker=Worker(service,runtime)
    await worker.tick()
    assert runtime.count('answer')==0
    assert service.get_run(id_)['run_status']=='waiting_input'
    question=service.interactions(id_)[0]
    input_=service.upload_input('answer.txt',b'details')
    service.send_message(id_,{'text':'Alpha','input_ids':[input_['id']],'expected_revision':1},idempotency_key='answer',interaction_id=question['id'])
    await worker.tick()
    assert runtime.count('answer')==1
    answer=next(payload for op,payload in runtime.calls if op=='answer')
    assert answer['answer']['custom']=={'0':'Alpha'}
    assert answer['answer']['tool_call_id']==question['remote_tool_call_id']
    assert service.get_run(id_)['run_status']=='waiting_continue'
    assert service.interactions(id_)[0]['state']=='answered'


async def test_upload_failure_never_sends_prompt(run_service):
    service=run_service;_,body=prepared(service)
    id_=service.create_run(body,idempotency_key='run',allowed_models=['fixture'])['run_id']
    runtime=FakeExecutionRuntime(service.store);runtime.failures['skill_upload']=RuntimeError('broken')
    await Worker(service,runtime).tick()
    assert runtime.count('chat')==0
    assert service.get_run(id_)['run_status']=='failed'


async def test_two_workers_cannot_claim_same_job(run_service):
    _,body=prepared(run_service)
    run_service.create_run(body,idempotency_key='run',allowed_models=['fixture'])
    runtime=FakeExecutionRuntime(run_service.store)
    a,b=Worker(run_service,runtime),Worker(run_service,runtime)
    assert a.claim() is not None
    assert b.claim() is None


async def test_stop_is_targeted_and_does_not_finish_the_run(run_service):
    service=run_service;_,body=prepared(service)
    id_=service.create_run(body,idempotency_key='run',allowed_models=['fixture'])['run_id']
    runtime=FakeExecutionRuntime(service.store);gate=runtime.gates['generation']=Gate()
    worker=Worker(service,runtime)
    task=asyncio.create_task(worker.tick())
    try:
        await gate.entered.wait()
        service.stop_generation(id_)
        await asyncio.wait_for(task,2)
    finally:
        if not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
    assert runtime.count('stop')==1
    assert service.get_run(id_)['run_status']=='waiting_continue'
    assert service.get_run(id_)['generation_status']=='stopped'
    stop=next(payload for op,payload in runtime.calls if op=='stop')
    assert stop['chat_run_id']=='chat-1'


async def test_finish_during_generation_stops_then_freezes_one_final_result(run_service):
    service=run_service;_,body=prepared(service)
    id_=service.create_run(body,idempotency_key='run',allowed_models=['fixture'])['run_id']
    runtime=FakeExecutionRuntime(service.store);gate=runtime.gates['generation']=Gate()
    worker=Worker(service,runtime);task=asyncio.create_task(worker.tick())
    try:
        await gate.entered.wait()
        service.finish(id_)
        await asyncio.wait_for(task,2)
    finally:
        if not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
    assert service.get_run(id_)['run_status']=='finishing'
    await worker.tick()
    run=service.get_run(id_)
    assert run['run_status']=='completed'
    assert run['final_result']['remote_quiescence_verified'] is True
    assert run['final_result']['artifacts']==[]
    assert runtime.count('stop')==1
