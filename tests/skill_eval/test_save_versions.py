import pytest
from .test_drafts import service
from .test_imports import imported


def test_save_beta_updates_pointer_not_existing_snapshot(service):
    item=imported(service);draft=item['draft'];release=item['release']
    old=service.export_snapshot(item['snapshot']['id'])
    draft=service.patch_draft(draft['id'],1,[{'op':'write','path':'code.py','text':'print(1)'}])
    saved=service.save_draft(draft['id'],2,1)
    assert saved['release']['id']==release['id']
    assert saved['release']['version_label']=='0.0.1'
    assert saved['release']['revision']==2
    assert service.export_snapshot(item['snapshot']['id'])==old
    with pytest.raises(Exception):service.save_draft(draft['id'],2,1)


def test_published_source_requires_unused_new_version_on_first_save(service):
    item=imported(service)
    pub=service.publish_draft(item['draft']['id'],1,1,'1.0')
    draft=service.create_draft(pub['release']['id'])
    with pytest.raises(Exception):service.save_draft(draft['id'],1,None)
    with pytest.raises(Exception):service.save_draft(draft['id'],1,None,'1.0')
    saved=service.save_draft(draft['id'],1,None,'1.1-beta')
    assert saved['release']['status']=='unpublished'
    assert saved['release']['version_label']=='1.1-beta'
    again=service.save_draft(draft['id'],saved['draft']['revision'],saved['release']['revision'])
    assert again['release']['id']==saved['release']['id']


def test_parallel_beta_save_is_optimistic_and_old_snapshots_remain(service):
    import concurrent.futures
    from backend.skill_eval.service import EvalError
    item=imported(service)
    first=service.create_draft(item['release']['id'])
    second=service.create_draft(item['release']['id'])
    def save(draft):
        try:
            return service.save_draft(draft['id'],1,1,changes=[{'op':'write','path':'code','text':draft['id']}])['release']['revision']
        except EvalError as error:
            return error.status
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(save,[first,second]))==[2,409]
    assert service.get_release(item['release']['id'])['revision']==2
    assert service.export_snapshot(item['snapshot']['id']).startswith(b'PK')


async def test_refined_run_keeps_source_diff_after_saving_beta(tmp_path):
    from backend.config import Settings
    from backend.skill_eval.runs import RunService
    from backend.skill_eval.worker import Worker
    from .test_create_runs import prepared
    from .fakes import FakeExecutionRuntime
    cfg=Settings(octagon={'data_path':tmp_path},skill_eval={'enabled':True})
    service=RunService(tmp_path/'db',tmp_path,settings=cfg);service.repo.initialize();item,body=prepared(service)
    id_=service.create_run(body,idempotency_key='r',allowed_models=['fixture'])['run_id']
    await Worker(service,FakeExecutionRuntime(service.store)).tick()
    refined=service.create_run_draft(id_)
    saved=service.save_draft(refined['id'],1,1,changes=[{'op':'write','path':'SKILL.md','text':'---\nname: sample\n---\nnew content'}])
    assert saved['draft']['base_version_id']==service.get_run(id_)['skill_version_id']
    assert saved['draft']['version_is_saved'] is True
    assert service.read_snapshot_file(saved['draft']['base_version_id'],'SKILL.md')['text'].endswith('Read input.')
