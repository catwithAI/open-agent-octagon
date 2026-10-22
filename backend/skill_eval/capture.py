"""Policy and redaction boundary for every persisted/public Skill Eval event."""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from backend.wire.policy import resolve_effective_policy
from backend.wire.redaction import redact_json, safe_redact_payload


class Capture:
    def __init__(self, *, requested='full', server_max=None, source_capability='full', max_bytes=1024*1024):
        self.policy=resolve_effective_policy(run_requested=requested,server_max=server_max,source_capability=source_capability)
        self.max_bytes=max_bytes

    def manifest(self):
        body=self.policy.effective in ('parsed','full')
        return {'policy':asdict(self.policy),'coverage':{
            'public_reasoning':{'status':'available_if_emitted' if body else 'unavailable','reason':None if body else 'capture_policy'},
            'tool_arguments':{'status':'available_if_emitted' if body else 'unavailable','reason':None if body else 'capture_policy'},
            'tool_returns':{'status':'available_if_emitted' if body else 'unavailable','reason':None if body else 'capture_policy'},
            'private_reasoning':{'status':'unavailable','reason':'not_exposed_by_source'},
            'actual_model_request':{'status':'unavailable','reason':'session_events_are_not_provider_requests'},
        }}

    def encode(self, kind: str, raw: dict[str,Any]):
        # Metadata IDs are deliberately extracted, not a filtered copy of the body.
        payload=raw.get('payload',raw)
        if not isinstance(payload,dict):payload={}
        metadata={'kind':kind,'capture':self.manifest(),'body':None,'redaction':'skipped'}
        for name in ('turn_id','tool_call_id','chat_run_id','loop_name'):
            value=payload.get(name)
            if isinstance(value,str) and len(value)<=300:
                metadata[name]=redact_json(value)
        cleaned=safe_redact_payload(raw,policy=self.policy.effective)
        metadata['redaction']=cleaned.status
        if cleaned.payload is not None:
            encoded=json.dumps(cleaned.payload,ensure_ascii=False).encode()
            if len(encoded)>self.max_bytes:
                # Do not truncate JSON into invalid syntax or retain a hidden raw copy.
                metadata['body_omitted']='event_size_limit'
                metadata['body_bytes']=len(encoded)
            else:
                metadata['body']=cleaned.payload
        if cleaned.status=='failed':
            metadata['body_omitted']='redaction_failed'
        return metadata
