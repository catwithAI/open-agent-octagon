import pytest
from backend.skill_eval.reruns import Reruns
from backend.skill_eval.service import EvalError
from .test_create_runs import run_service,prepared
from .fakes import FakeExecutionRuntime
from backend.skill_eval.worker import Worker


async def test_reuse_all_requires_confirmation_and_sends_once_in_new_session(run_service):
    service=run_service;_,body=prepared(service);body['prompt']='first user instruction'
    source=service.create_run(body,idempotency_key='r1',allowed_models=['fixture'])['run_id']
    runtime=FakeExecutionRuntime(service.store);worker=Worker(service,runtime);await worker.tick()
    file=service.upload_input('later.txt',b'later')
    service.send_message(source,{'text':'later followup','input_ids':[file['id']]},idempotency_key='m1');await worker.tick()
    service.save_note(source,'DO_NOT_CARRY_NOTE',0)
    reruns=Reruns(service);plan=reruns.create(source,'reuse_all')
    derived={**body,'prompt':'','input_ids':[],'source_run_id':source,'rerun_plan_id':plan['id']}
    with pytest.raises(EvalError):service.create_run(derived,idempotency_key='r2',allowed_models=['fixture'])
    confirmed=reruns.confirm(plan['id'],plan['hash'])
    new=service.create_run(derived,idempotency_key='r2',allowed_models=['fixture'])['run_id']
    await worker.tick()
    assert runtime.count('create')==2
    assert runtime.count('chat')==3  # No mechanical replay of the two old messages.
    run=service.get_run(new)
    assert 'first user instruction' in run['input_snapshot']['prompt']
    assert 'later followup' in run['input_snapshot']['prompt']
    assert 'DO_NOT_CARRY_NOTE' not in str(run['input_snapshot'])
    assert run['input_snapshot']['inputs'][0]['hash']==file['hash']
    assert run['note']['text']==''
    assert service.create_run(derived,idempotency_key='r2',allowed_models=[])['run_id']==new
    with pytest.raises(EvalError,match='new plan'):service.create_run(derived,idempotency_key='r3',allowed_models=['fixture'])


async def test_source_append_invalidates_confirmation_and_replace_can_be_empty(run_service):
    service=run_service;_,body=prepared(service)
    source=service.create_run(body,idempotency_key='r1',allowed_models=['fixture'])['run_id']
    worker=Worker(service,FakeExecutionRuntime(service.store));await worker.tick()
    reruns=Reruns(service);plan=reruns.create(source,'reuse_all')
    service.send_message(source,{'text':'new answer'},idempotency_key='m1')
    with pytest.raises(EvalError,match='changed'):reruns.confirm(plan['id'],plan['hash'])
    plan=reruns.create(source,'replace');confirmed=reruns.confirm(plan['id'],plan['hash'])
    new=service.create_run({**body,'source_run_id':source,'rerun_plan_id':plan['id']},idempotency_key='r2',allowed_models=['fixture'])['run_id']
    assert service.get_run(new)['input_snapshot']['prompt']==''
    assert service.get_run(new)['input_snapshot']['inputs']==[]


async def test_oversized_context_is_rejected_without_truncation_or_confirmation(run_service):
    service=run_service;_,body=prepared(service);body['prompt']='full original user input'
    source=service.create_run(body,idempotency_key='r',allowed_models=['fixture'])['run_id']
    reruns=Reruns(service);plan=reruns.create(source,'reuse_all')
    service.settings.skill_eval.context_max_bytes=10
    with pytest.raises(EvalError,match='context exceeds'):
        reruns.confirm(plan['id'],plan['hash'])
    assert reruns.get(plan['id'])['state']=='pending'
    assert service.get_run(source)['input_snapshot']['prompt']=='full original user input'
