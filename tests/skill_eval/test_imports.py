import asyncio
import concurrent.futures

import httpx
import pytest

from backend.skill_eval.service import SkillService, EvalError
from backend.skill_eval.api import create_app
from backend.skill_eval.packages import Limits


@pytest.fixture
def service(db_path,tmp_path):
    svc=SkillService(db_path,tmp_path)
    svc.repo.initialize()
    return svc


def imported(service,body=None,name=None,version=None):
    body=body or b'---\nname: sample\n---\nRead input.'
    pending=service.import_package('SKILL.md',body)
    return service.commit_import(pending['id'],name or 'sample',version)


def test_import_matrix_conflicts_keep_pending_and_commit_idempotent(service):
    first=imported(service)
    assert first['release']['version_label']=='0.0.1'
    assert service.commit_import(first['import_id'],'ignored','9')==first
    pending=service.import_package('SKILL.md',b'---\nname: sample\n---\nx')
    for label in (None,'0.0.1'):
        with pytest.raises(EvalError) as error:
            service.commit_import(pending['id'],'sample',label)
        assert error.value.status==409
        assert service.get_import(pending['id'])['state']=='pending'
    next_=service.commit_import(pending['id'],'sample','beta')
    assert next_['skill']['id']==first['skill']['id']
    with pytest.raises(EvalError):
        imported(service,version='beta')
    renamed=imported(service,name='new')
    assert renamed['skill']['id']!=first['skill']['id']
    assert renamed['release']['version_label']=='0.0.1'


def test_same_version_concurrent_commit_only_one_wins(service):
    packages=[service.import_package('SKILL.md',b'---\nname: sample\n---\nx') for _ in range(2)]
    def commit(p):
        try:return service.commit_import(p['id'],'sample','1')['release']['id']
        except EvalError as error:return error.status
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        results=list(pool.map(commit,packages))
    assert results.count(409)==1
    assert len(service.list_skills())==1


async def test_real_multipart_http_and_unconfigured_catalog(service):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(service)),base_url='http://local') as client:
        response=await client.post('/api/skill-eval/skills/import',files={'file':('SKILL.md',b'---\nname: sample\n---\nx','text/markdown')})
        assert response.status_code==201
        pending=response.json()
        result=await client.post(f"/api/skill-eval/imports/{pending['id']}/commit",json={'name':'sample'})
        assert result.status_code==201
        assert (await client.get('/api/skill-eval/catalog')).json()=={'status':'unconfigured','items':[]}
        assert len((await client.get('/api/skill-eval/skills')).json()['items'])==1


def test_scope_and_expired_import(service):
    from datetime import datetime,timezone,timedelta
    pending=service.import_package('SKILL.md',b'---\nname: sample\n---\nx')
    other=SkillService(service.repo.db_path,service.store.root.parent,scope='other')
    with pytest.raises(EvalError) as error:other.get_import(pending['id'])
    assert error.value.status==404
    service.clock=lambda: datetime.now(timezone.utc)+timedelta(days=2)
    with pytest.raises(EvalError) as error:service.commit_import(pending['id'],'sample',None)
    assert error.value.status==410


def test_detected_version_is_honored_and_commit_receipt_does_not_drift(service):
    item=imported(service,body=b'---\nname: sample\nmetadata:\n  version: 2.3-beta\n---\nx')
    assert item['release']['version_label']=='2.3-beta'
    service.save_draft(item['draft']['id'],1,1,changes=[{'op':'write','path':'new.py','text':'changed'}])
    assert service.commit_import(item['import_id'],'ignored','ignored')==item
