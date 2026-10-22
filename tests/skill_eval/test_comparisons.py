import pytest
from backend.skill_eval.queries import Queries
from backend.skill_eval.worker import Worker
from backend.skill_eval.service import EvalError
from .test_create_runs import run_service,prepared
from .fakes import FakeExecutionRuntime


async def test_three_results_cross_experiments_and_fixed_collection(run_service):
    service=run_service;item,body=prepared(service)
    runtime=FakeExecutionRuntime(service.store);worker=Worker(service,runtime)
    ids=[]
    for i in range(3):
        experiment=service.create_experiment(item['skill']['id'],f'experiment-{i}')
        id_=service.create_run({**body,'experiment_id':experiment['id']},idempotency_key=str(i),allowed_models=['fixture'])['run_id']
        await worker.tick();service.finish(id_);await worker.tick();ids.append(id_)
    queries=Queries(service)
    comparison=queries.compare(ids,ids[1])
    assert len(comparison['columns'])==3
    assert comparison['baseline_run_id']==ids[1]
    assert not any(column['partial'] for column in comparison['columns'])
    original=queries.comparison(comparison['id'])
    session=service.get_run(ids[0])['external_refs']['session_id']
    runtime.files[session]['report.txt']=b'recollected new content'
    with service.repo.transaction() as c:service._job(c,ids[0],'recollect-test','collect')
    await worker.tick()
    assert queries.comparison(comparison['id'])==original
    assert queries.diff(comparison['id'],ids[0],ids[1],kind='result')['diff']
    assert all(not row['changed'] for row in queries.diff(comparison['id'],ids[0],ids[1])['files'])
    service.store.collect_unreferenced(service.repo,older_than=float('inf'))
    old_blob=original['columns'][0]['result']['artifacts'][0]['hash']
    assert service.store.read_blob(old_blob)


async def test_active_run_is_rejected_and_missing_columns_are_explicit(run_service):
    service=run_service;_,body=prepared(service);ids=[]
    worker=Worker(service,FakeExecutionRuntime(service.store))
    for i in range(3):
        id_=service.create_run(body,idempotency_key=str(i),allowed_models=['fixture'])['run_id'];ids.append(id_)
        await worker.tick()
        if i<2:service.finish(id_);await worker.tick()
    queries=Queries(service)
    with pytest.raises(EvalError):queries.compare(ids)
    result=queries.compare(ids[:2]+['missing'])
    assert result['columns'][-1]['missing'] is True
    with pytest.raises(EvalError):queries.compare([ids[0],'missing'])
