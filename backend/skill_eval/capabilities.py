"""Read-only Blade discovery. Method presence never means verified execution."""
from __future__ import annotations

import asyncio
import importlib.metadata
import shutil
from typing import Any

import httpx

from backend.config import Settings
from .capture import Capture

SUPPORTED_SCOPE = {
    'input':'read_only', 'results':['text','new_files'],
    'unsupported':['external_writes','in_place_input_changes'],
    'enforcement_verified':False,
}


class Capabilities:
    def __init__(self, settings: Settings, *, service=None):
        self.settings=settings
        self.service=service
        self.last_discovery: dict[str,Any] | None=None

    def local(self):
        cfg=self.settings
        try:sdk=importlib.metadata.version('blade-agent-kit')
        except importlib.metadata.PackageNotFoundError:sdk=None
        cli=cfg.blade.cli_path or shutil.which('blade')
        # A CLI is optional for the SDK channel. Never invoke it to test execution.
        enabled=bool(cfg.skill_eval.enabled and not cfg.octagon.display_only)
        unavailable=self.service is not None and getattr(self.service,'execution_available',None) is False
        return {
            'enabled':enabled,'display_only':cfg.octagon.display_only,
            'agents':[{'id':'blade-agent','default':True}],
            'sdk':{'version':sdk,'status':'installed' if sdk else 'unavailable'},
            'cli':{'status':'installed' if cli else 'unavailable','version':None},
            'server':self.last_discovery['server'] if self.last_discovery else {'status':'unknown','version':None},
            'execution':{'status':'unverified' if sdk and enabled and not unavailable else 'unavailable',
                         'reason':'runtime_unavailable' if unavailable else 'live_acceptance_required' if sdk and enabled else 'disabled_or_missing_sdk'},
            'operations':{name:{'status':'unknown','reason':'requires_live_acceptance'} for name in ['skill_upload','attachment_upload','public_events','history','interaction','stop']},
            'limits':{**cfg.skill_eval.limits.model_dump(),'input_max_bytes':cfg.skill_eval.input_max_bytes,'text_edit_max_bytes':cfg.skill_eval.text_edit_max_bytes,'context_max_bytes':cfg.skill_eval.context_max_bytes},
            'scope':SUPPORTED_SCOPE,
            'capture':Capture(server_max=cfg.octagon.wire_capture_max_policy,source_capability='full' if cfg.octagon.wire_capture_enabled else 'off',max_bytes=cfg.skill_eval.event_max_bytes).manifest(),
        }

    async def discover(self):
        cfg=self.settings
        if cfg.octagon.display_only:
            return {'status':'unavailable','reason':'display_only','models':[],'default':None}
        if not cfg.blade.api_key:
            return {'status':'unavailable','reason':'missing_credentials','models':[],'default':None}
        try:
            # Same model catalog protocol as backend.api._blade_models, retaining
            # only known display fields; no client-supplied URL or gateway secrets.
            async with httpx.AsyncClient(base_url=cfg.blade.base_url,timeout=cfg.blade.request_timeout_seconds,
                headers={'Authorization':'Bearer '+cfg.blade.api_key.get_secret_value()},follow_redirects=False) as client:
                version_response, models_response=await asyncio.gather(client.get('/api/version'),client.get('/api/config/models'))
                models_response.raise_for_status()
                payload=models_response.json()
                if not isinstance(payload,dict) or not isinstance(payload.get('models'),list):
                    raise ValueError('invalid model catalog')
                models=[{'id':m['id'],'label':m.get('label') or m['id']} for m in payload['models'] if isinstance(m,dict) and isinstance(m.get('id'),str) and m['id']]
                # A malformed or failed version response never becomes a guessed version.
                server={'status':'unknown','version':None}
                if version_response.is_success:
                    remote_version=version_response.json()
                    if isinstance(remote_version,dict) and isinstance(remote_version.get('version'),str):
                        server={'status':'reachable','version':remote_version['version']}
                result={'status':'available','default':payload.get('default'),'models':models,'server':server}
                self.last_discovery=result
                return result
        except httpx.HTTPStatusError as error:
            return {'status':'unavailable','reason':'authentication_failed' if error.response.status_code in (401,403) else 'catalog_http_error','models':[],'default':None}
        except (httpx.HTTPError,ValueError,TypeError):
            return {'status':'unavailable','reason':'catalog_unreachable_or_invalid','models':[],'default':None}

    async def validate_model(self, model):
        catalog=await self.discover()
        return catalog['status']=='available' and any(m['id']==model for m in catalog['models'])
