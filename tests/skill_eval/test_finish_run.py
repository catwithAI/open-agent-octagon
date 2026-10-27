import concurrent.futures
import threading
from backend.skill_eval.worker import Worker
from backend.skill_eval.service import EvalError
from .test_create_runs import run_service,prepared
from .fakes import FakeExecutionRuntime


async def test_finish_waiting_question_closes_it_without_an_automatic_answer(run_service):
    service=run_service;_,body=prepared(service);body['prompt']='ask me'
    id_=service.create_run(body,idempotency_key='r',allowed_models=['fixture'])['run_id']
    runtime=FakeExecutionRuntime(service.store);worker=Worker(service,runtime);await worker.tick()
    service.finish(id_);service.finish(id_);await worker.tick()
    assert service.get_run(id_)['run_status']=='completed'
    assert service.interactions(id_)[0]['state']=='closed' and runtime.count('answer')==0
    assert not await worker.tick()


async def test_finish_and_message_race_serializes_without_post_finish_send(run_service):
    service=run_service;_,body=prepared(service)
    id_=service.create_run(body,idempotency_key='r',allowed_models=['fixture'])['run_id']
    runtime=FakeExecutionRuntime(service.store);worker=Worker(service,runtime);await worker.tick();barrier=threading.Barrier(2)
    def send():
        barrier.wait()
        try:return service.send_message(id_,{'text':'racing'},idempotency_key='m')['id']
        except EvalError as error:return error.status
    def finish():barrier.wait();return service.finish(id_)
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        a=pool.submit(send);b=pool.submit(finish);a.result();b.result()
    while await worker.tick():pass
    assert service.get_run(id_)['run_status']=='completed'
    assert runtime.count('chat')==1


async def test_finish_before_execution_creates_no_remote_session(run_service):
    service=run_service;_,body=prepared(service)
    id_=service.create_run(body,idempotency_key='r',allowed_models=['fixture'])['run_id']
    runtime=FakeExecutionRuntime(service.store);worker=Worker(service,runtime);service.finish(id_)
    await worker.tick();await worker.tick()
    result=service.get_run(id_)
    assert result['run_status']=='completed' and result['final_result']['execution_started'] is False
    assert result['artifact_status']=='complete' and runtime.count('create')==0
