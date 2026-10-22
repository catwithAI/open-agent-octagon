import json
from backend.skill_eval.recovery import Recovery
from backend.skill_eval.worker import Worker
from .test_create_runs import run_service,prepared
from .fakes import FakeExecutionRuntime


async def test_create_acceptance_unknown_never_creates_another_session(run_service):
    service=run_service;_,body=prepared(service)
    id_=service.create_run(body,idempotency_key='r',allowed_models=['fixture'])['run_id']
    runtime=FakeExecutionRuntime(service.store);worker=Worker(service,runtime);job=worker.claim()
    worker.checkpoint(job,phase='create',remote_started=True)
    await runtime.create(service.get_run(id_))  # Remote accepted; local binding lost.
    with service.repo.transaction() as c:c.execute("UPDATE skill_eval_jobs SET lease_until='2000-01-01' WHERE id=?",(job['id'],))
    result=await Recovery(service,runtime).reconcile()
    assert result[0]['status']=='acceptance_unknown'
    assert runtime.count('create')==1
    assert not await Worker(service,runtime).tick()


async def test_bound_session_and_completed_chat_restore_without_resend(run_service):
    service=run_service;_,body=prepared(service)
    id_=service.create_run(body,idempotency_key='r',allowed_models=['fixture'])['run_id']
    runtime=FakeExecutionRuntime(service.store);worker=Worker(service,runtime);await worker.tick()
    before=runtime.count('chat')
    with service.repo.transaction() as c:
        row=c.execute('SELECT id FROM skill_eval_jobs WHERE run_id=?',(id_,)).fetchone()
        c.execute("UPDATE skill_eval_jobs SET state='running',owner='dead-process',lease_until='2000-01-01',checkpoint_json=? WHERE id=?",(json.dumps({'remote_started':True,'phase':'chat_send'}),row['id']))
        c.execute("UPDATE skill_eval_runs SET run_status='active' WHERE id=?",(id_,))
    result=await Recovery(service,runtime).reconcile()
    assert result[0]['status']=='session_restored_without_send'
    assert service.get_run(id_)['run_status']=='waiting_continue'
    assert runtime.count('chat')==before
    assert runtime.count('create')==1


async def test_display_only_does_not_reconcile(run_service):
    runtime=FakeExecutionRuntime(run_service.store)
    run_service.settings.octagon.display_only=True
    assert await Recovery(run_service,runtime).reconcile()==[]
    assert runtime.calls==[]


async def test_bad_record_is_isolated_and_releases_reconciling_state(run_service):
    service=run_service;_,body=prepared(service)
    first=service.create_run(body,idempotency_key='first',allowed_models=['fixture'])['run_id']
    second=service.create_run(body,idempotency_key='second',allowed_models=['fixture'])['run_id']
    runtime=FakeExecutionRuntime(service.store);worker=Worker(service,runtime)
    a=worker.claim();b=worker.claim()
    with service.repo.transaction() as c:
        c.execute("UPDATE skill_eval_jobs SET lease_until='2000-01-01'")
        c.execute("UPDATE skill_eval_jobs SET payload_json='invalid' WHERE id=?",(a['id'],))
    result=await Recovery(service,runtime).reconcile()
    assert {row['status'] for row in result}=={'unknown','safe_phase_resumed'}
    with service.repo.connection() as c:
        assert c.execute("SELECT count(*) FROM skill_eval_jobs WHERE state='reconciling'").fetchone()[0]==0
    assert service.get_run(first)['run_status']=='failed'
    await Worker(service,runtime).tick()
    assert service.get_run(second)['run_status']=='waiting_continue'
    assert runtime.count('create')==1


async def test_bound_prechat_upload_can_resume_without_creating_or_chatting_twice(run_service):
    service=run_service;_,body=prepared(service)
    id_=service.create_run(body,idempotency_key='r',allowed_models=['fixture'])['run_id']
    runtime=FakeExecutionRuntime(service.store);worker=Worker(service,runtime);job=worker.claim()
    receipt=await runtime.create(service.get_run(id_));worker.refs(id_,**receipt)
    worker.checkpoint(job,phase='skill_upload',remote_started=True)
    with service.repo.transaction() as c:c.execute("UPDATE skill_eval_jobs SET lease_until='2000-01-01' WHERE id=?",(job['id'],))
    assert (await Recovery(service,runtime).reconcile())[0]['status']=='safe_phase_resumed'
    await Worker(service,runtime).tick()
    assert runtime.count('create')==1 and runtime.count('chat')==1
    assert service.get_run(id_)['run_status']=='waiting_continue'


async def test_worker_and_recovery_never_consume_other_scopes(run_service):
    from backend.skill_eval.runs import RunService
    service=run_service;_,body=prepared(service)
    service.create_run(body,idempotency_key='r',allowed_models=['fixture'])
    other=RunService(service.repo.db_path,service.store.root.parent,scope='other',settings=service.settings)
    runtime=FakeExecutionRuntime(service.store)
    assert Worker(other,runtime).claim() is None
    claimed=Worker(service,runtime).claim()
    with service.repo.transaction() as c:c.execute("UPDATE skill_eval_jobs SET lease_until='2000-01-01' WHERE id=?",(claimed['id'],))
    assert await Recovery(other,runtime).reconcile()==[]
    assert runtime.calls==[]


async def test_reconciling_job_holds_the_run_writer_lock(run_service):
    service=run_service;_,body=prepared(service)
    id_=service.create_run(body,idempotency_key='r',allowed_models=['fixture'])['run_id']
    runtime=FakeExecutionRuntime(service.store);worker=Worker(service,runtime);job=worker.claim()
    with service.repo.transaction() as c:
        c.execute("UPDATE skill_eval_jobs SET state='reconciling' WHERE id=?",(job['id'],))
        service._job(c,id_,'finish','finish')
    assert Worker(service,runtime).claim() is None
