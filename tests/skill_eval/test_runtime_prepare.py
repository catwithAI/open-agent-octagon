import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
import httpx
import respx
from backend.skill_eval.runtime import BladeSkillRuntime
from .test_create_runs import run_service,prepared


async def test_runtime_upload_preserves_binary_permissions_and_activates_by_ref(run_service):
    service=run_service;item,body=prepared(service)
    draft=service.patch_draft(item['draft']['id'],1,[{'op':'write','path':'scripts/run.sh','text':'#!/bin/sh\necho yes','mode':0o755},{'op':'write','path':'assets/data.bin','content_base64':'AP+A'}])
    entries=service.store.get_manifest(draft['manifest_hash'])
    runtime=BladeSkillRuntime(service.settings,service.store);runtime.client=SimpleNamespace(upload_session_skill=AsyncMock(return_value=SimpleNamespace(file_count=3,overwritten=False,raw={'skill_dir':'/host/path'})))
    result=await runtime.upload_skill({'id':'run'},'session',entries)
    arguments=runtime.client.upload_session_skill.call_args.kwargs
    assert arguments['name']=='octagon/eval-run'
    assert next(file for file in arguments['files'] if file['path'].endswith('.sh'))['executable'] is True
    binary=next(file for file in arguments['files'] if file['path'].endswith('.bin'))
    assert binary['encoding']=='base64' and base64.b64decode(binary['content'])==b'\0\xff\x80'
    assert result['name']=='octagon/eval-run' and '/host/path' not in result['name']


async def test_runtime_download_stops_when_stream_exceeds_limit(run_service):
    cfg=run_service.settings;cfg.blade.base_url='https://blade.test';cfg.blade.api_key='not-used-by-fixture'
    runtime=BladeSkillRuntime(cfg,run_service.store)
    # SecretStr assignment follows the normal Settings construction path.
    from pydantic import SecretStr
    cfg.blade.api_key=SecretStr('fixture-secret')
    with respx.mock as router:
        route=router.get('https://blade.test/api/sessions/s/files/out.txt').respond(200,content=b'12345')
        with pytest.raises(ValueError,match='size limit'):await runtime.download('s','out.txt',max_bytes=4)
        assert route.call_count==1
    with pytest.raises(ValueError):await runtime.download('s','../escape',max_bytes=10)
