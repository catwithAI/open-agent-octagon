import asyncio
from backend.skill_eval.worker import Worker
from .test_create_runs import run_service,prepared
from .fakes import FakeExecutionRuntime,Gate


async def test_failed_stop_is_explicit_and_manual_retry_targets_same_generation(run_service):
    class RetryRuntime(FakeExecutionRuntime):
        failed=asyncio.Event()
        async def stop(self,*args):
            try:return await super().stop(*args)
            except OSError:self.failed.set();raise
    service=run_service;_,body=prepared(service)
    id_=service.create_run(body,idempotency_key='r',allowed_models=['fixture'])['run_id']
    runtime=RetryRuntime(service.store);runtime.failures['stop']=OSError('network')
    gate=runtime.gates['generation']=Gate();worker=Worker(service,runtime);task=asyncio.create_task(worker.tick())
    try:
        await gate.entered.wait();service.stop_generation(id_);await runtime.failed.wait()
        assert service.get_run(id_)['generation_status']=='stopping'
        assert service.get_run(id_)['external_refs']['last_stop_error']['status']=='unconfirmed'
        runtime.failures.clear();service.stop_generation(id_);await asyncio.wait_for(task,2)
    finally:
        if not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
    stops=[payload['chat_run_id'] for op,payload in runtime.calls if op=='stop']
    assert stops==['chat-1','chat-1']
    assert service.get_run(id_)['generation_status']=='stopped'
    assert service.get_run(id_)['external_refs']['last_stop_error'] is None


async def test_paused_question_has_no_running_generation_to_stop(run_service):
    service=run_service;_,body=prepared(service);body['prompt']='ask me'
    id_=service.create_run(body,idempotency_key='r',allowed_models=['fixture'])['run_id']
    runtime=FakeExecutionRuntime(service.store);await Worker(service,runtime).tick()
    stopped=service.stop_generation(id_)
    assert stopped['run_status']=='waiting_input' and stopped['generation_status']=='paused'
    assert runtime.count('stop')==0


async def test_stop_cancels_queued_initial_without_remote_actions(run_service):
    service=run_service;_,body=prepared(service)
    id_=service.create_run(body,idempotency_key='r',allowed_models=['fixture'])['run_id']
    service.stop_generation(id_)
    runtime=FakeExecutionRuntime(service.store)
    assert not await Worker(service,runtime).tick()
    result=service.get_run(id_)
    assert result['run_status']=='cancelled' and result['final_result']['execution_started'] is False
    assert runtime.calls==[]


async def test_stop_cancels_queued_followup_and_keeps_existing_session(run_service):
    service=run_service;_,body=prepared(service)
    id_=service.create_run(body,idempotency_key='r',allowed_models=['fixture'])['run_id']
    runtime=FakeExecutionRuntime(service.store);worker=Worker(service,runtime);await worker.tick()
    service.send_message(id_,{'text':'will not send'},idempotency_key='cancelled')
    service.stop_generation(id_);assert not await worker.tick()
    assert service.get_run(id_)['run_status']=='waiting_continue'
    assert service.messages(id_)[-1]['delivery_state']=='cancelled'
    service.send_message(id_,{'text':'continue'},idempotency_key='continue');await worker.tick()
    assert runtime.count('create')==1 and runtime.count('chat')==2


async def test_generation_deadline_stops_actual_remote_id_without_wall_clock_wait(run_service):
    class ControlledDeadline:
        def __call__(self,seconds):return self
        async def __aenter__(self):self.task=asyncio.current_task();return self
        def expire(self):self.expired=True;self.task.cancel()
        async def __aexit__(self,type_,error,traceback):
            if type_ is asyncio.CancelledError and self.expired:
                self.task.uncancel();raise TimeoutError('controlled generation deadline')
    service=run_service;_,body=prepared(service)
    id_=service.create_run(body,idempotency_key='r',allowed_models=['fixture'])['run_id']
    runtime=FakeExecutionRuntime(service.store);gate=runtime.gates['generation']=Gate();deadline=ControlledDeadline()
    worker=Worker(service,runtime,timeout_factory=deadline);task=asyncio.create_task(worker.tick())
    try:await gate.entered.wait();deadline.expire();await task
    finally:
        if not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
    assert [p['chat_run_id'] for name,p in runtime.calls if name=='stop']==['chat-1']
    assert runtime.count('chat')==1
    assert service.get_run(id_)['run_status']=='failed'
    assert not await worker.tick()
