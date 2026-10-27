import httpx
import pytest
import respx
from backend.config import Settings
from backend.skill_eval.capabilities import Capabilities


async def test_model_catalog_is_not_execution_verification(monkeypatch):
    cfg=Settings(blade={'base_url':'https://blade.test','api_key':'canary'},skill_eval={'enabled':True})
    capability=Capabilities(cfg)
    monkeypatch.setattr('backend.skill_eval.capabilities.shutil.which',lambda _:None)
    with respx.mock as router:
        router.get('https://blade.test/api/version').respond(200,json={'version':'beta.42'})
        router.get('https://blade.test/api/config/models').respond(200,json={'default':'m','models':[{'id':'m','label':'Model','api_key':'do-not-expose'}]})
        assert await capability.validate_model('m')
        assert not await capability.validate_model('unknown')
    report=capability.local()
    assert report['server']['version']=='beta.42'
    assert report['cli']['status']=='unavailable'
    assert report['execution']['status']=='unverified'
    assert all(op['status']=='unknown' for op in report['operations'].values())
    assert 'canary' not in str(report)
    assert 'do-not-expose' not in str(capability.last_discovery)


@pytest.mark.parametrize('status',[401,403,500])
async def test_catalog_failure_has_no_secret_or_raw_exception(status):
    capability=Capabilities(Settings(blade={'base_url':'https://blade.test','api_key':'canary'}))
    with respx.mock as router:
        router.get('https://blade.test/api/version').respond(200,json={'version':'x'})
        router.get('https://blade.test/api/config/models').respond(status,json={'error':'canary'})
        result=await capability.discover()
    assert result['status']=='unavailable'
    assert 'canary' not in str(result)


async def test_display_only_and_missing_credentials_do_not_probe():
    for cfg in [Settings(octagon={'display_only':True},blade={'api_key':'key'}),Settings()]:
        result=await Capabilities(cfg).discover()
        assert result['status']=='unavailable'
        assert result['models']==[]
        assert Capabilities(cfg).local()['enabled'] is False


def test_runtime_startup_failure_is_visible_in_capabilities():
    class Unavailable:
        execution_available=False
    report=Capabilities(Settings(skill_eval={'enabled':True}),service=Unavailable()).local()
    assert report['enabled'] is True
    assert report['execution']=={'status':'unavailable','reason':'runtime_unavailable'}
