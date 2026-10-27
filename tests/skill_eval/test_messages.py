import pytest
from backend.skill_eval.worker import Worker
from .test_create_runs import run_service,prepared
from .fakes import FakeExecutionRuntime


async def test_multiple_followups_and_attachment_only_keep_one_session_and_initial_snapshot(run_service):
    service=run_service;_,body=prepared(service);body['prompt']='initial'
    id_=service.create_run(body,idempotency_key='r',allowed_models=['fixture'])['run_id']
    runtime=FakeExecutionRuntime(service.store);worker=Worker(service,runtime);await worker.tick()
    initial=service.get_run(id_)['input_snapshot']
    with pytest.raises(ValueError):service.send_message(id_,{'text':''},idempotency_key='empty')
    attachment=service.upload_input('input.txt',b'new attachment')
    body={'text':'','input_ids':[attachment['id']]}
    sent=service.send_message(id_,body,idempotency_key='attachment')
    assert service.send_message(id_,body,idempotency_key='attachment')['id']==sent['id']
    await worker.tick()
    service.send_message(id_,{'text':'third message'},idempotency_key='third');await worker.tick()
    assert runtime.count('create')==1 and runtime.count('chat')==3
    assert service.get_run(id_)['input_snapshot']==initial
    assert service.get_run(id_)['run_status']=='waiting_continue'
    assert service.messages(id_)[1]['attachment_refs'][0]['hash']==attachment['hash']
