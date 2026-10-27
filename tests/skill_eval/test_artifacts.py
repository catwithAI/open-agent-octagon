from backend.skill_eval.artifacts import ArtifactCollector
from backend.skill_eval.worker import Worker
from .test_create_runs import run_service,prepared
from .fakes import FakeExecutionRuntime


async def test_inputs_changed_or_deleted_are_observations_not_output_versions(run_service):
    service=run_service;_,body=prepared(service);a=service.upload_input('a.txt',b'original');b=service.upload_input('b.txt',b'original-b')
    id_=service.create_run({**body,'input_ids':[a['id'],b['id']]},idempotency_key='r',allowed_models=['fixture'])['run_id']
    runtime=FakeExecutionRuntime(service.store);await Worker(service,runtime).tick();run=service.get_run(id_);files=runtime.files[run['external_refs']['session_id']]
    files[a['remote_path']]=b'modified in place';del files[b['remote_path']]
    result=await ArtifactCollector(service,runtime).collect(run)
    assert {item['kind'] for item in result['observations']}=={'input_modified','input_not_listed'}
    assert [item['relative_path'] for item in result['artifacts']]==['report.txt']
    assert service.read_input(a['id'])==b'original' and service.read_input(b['id'])==b'original-b'


async def test_unavailable_listing_is_partial_and_changed_file_is_not_published(run_service):
    service=run_service;_,body=prepared(service)
    id_=service.create_run(body,idempotency_key='r',allowed_models=['fixture'])['run_id']
    runtime=FakeExecutionRuntime(service.store);await Worker(service,runtime).tick();run=service.get_run(id_)
    runtime.failures['list_files']=OSError('unavailable')
    result=await ArtifactCollector(service,runtime).collect(run)
    assert result['status']=='partial' and result['gaps'][0]['reason']=='directory_unavailable'
    runtime.failures.clear();original=runtime.download;reads=0
    async def changing(*args,**kwargs):
        nonlocal reads
        reads+=1;value=await original(*args,**kwargs)
        return value if reads%2 else b'changed'
    runtime.download=changing
    result=await ArtifactCollector(service,runtime).collect(run)
    assert result['status']=='partial' and result['artifacts'][0]['hash'] is None
