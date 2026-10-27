import concurrent.futures
from backend.skill_eval.worker import Worker
from backend.skill_eval.service import EvalError
from .test_create_runs import run_service,prepared
from .fakes import FakeExecutionRuntime


async def test_two_human_answers_compete_without_double_remote_delivery(run_service):
    service=run_service;_,body=prepared(service);body['prompt']='ask me'
    id_=service.create_run(body,idempotency_key='r',allowed_models=['fixture'])['run_id']
    runtime=FakeExecutionRuntime(service.store);worker=Worker(service,runtime);await worker.tick()
    question=service.interactions(id_)[0]
    assert runtime.count('answer')==0
    def answer(label):
        try:return service.send_message(id_,{'text':label,'expected_revision':1},idempotency_key=label,interaction_id=question['id'])['id']
        except EvalError as error:return error.status
    with concurrent.futures.ThreadPoolExecutor(2) as pool:results=list(pool.map(answer,['Alpha','Beta']))
    assert results.count(409)==1
    await worker.tick()
    assert runtime.count('answer')==1 and runtime.count('create')==1
    assert service.interactions(id_)[0]['state']=='answered'
