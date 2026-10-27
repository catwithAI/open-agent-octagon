import importlib.util
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx

from backend.config import Settings

spec=importlib.util.spec_from_file_location('skill_probe',Path('scripts/verify-skill-eval-blade.py'))
probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)


class ReadClient:
    calls=[]
    def __init__(self,*args,**kwargs):pass
    async def __aenter__(self):return self
    async def __aexit__(self,*args):pass
    async def list_models(self):
        self.calls.append('models')
        return SimpleNamespace(models=[SimpleNamespace(id='fixture',label='Fixture')],default='fixture')
    async def create_session(self,*args,**kwargs):
        raise AssertionError('preflight must not create a session')


async def test_preflight_only_reads_and_never_executes():
    args=probe.parser().parse_args(['--preflight'])
    ReadClient.calls=[]
    with respx.mock as router:
        router.get('https://blade.test/api/version').respond(200,json={'version':'test'})
        report=await probe.verify(args,client_factory=ReadClient,settings=Settings(blade={'base_url':'https://blade.test','api_key':'fixture-secret'}))
    assert report['sessions']==[]
    assert report['generations']==[]
    assert report['service_version']=='test'
    assert ReadClient.calls==['models']
    assert 'fixture-secret' not in str(report)


async def test_execute_requires_an_explicit_model_before_any_network():
    args=probe.parser().parse_args(['--execute'])
    with pytest.raises(ValueError,match='requires --model'):
        await probe.verify(args,client_factory=ReadClient,settings=Settings(blade={'api_key':'fixture'}))
    with pytest.raises(SystemExit):probe.parser().parse_args([])
    with pytest.raises(SystemExit):probe.parser().parse_args(['--preflight','--execute'])


def test_report_scrubs_secrets(tmp_path):
    path=tmp_path/'report.json'
    probe.save_report(path,{'authorization':'Bearer do-not-leak','nested':{'api_key':'canary'}})
    assert 'canary' not in path.read_text()
    assert 'do-not-leak' not in path.read_text()
    assert path.stat().st_mode&0o777==0o600
