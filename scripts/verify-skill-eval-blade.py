#!/usr/bin/env python3
"""Explicit Blade protocol probe; uses only a self-contained temporary Skill.

Preflight never creates a session. Execute uses the specified catalog model and
only removes sessions it created. Redacted evidence is local, not a test fixture
for automatically contacting the service.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import sys
import tempfile
import uuid
from collections import Counter
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from typing import Any

# Allow `uv run python scripts/verify-skill-eval-blade.py` from repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.config import load_settings
from backend.wire.redaction import redact_json


def parser():
    p=argparse.ArgumentParser(description=__doc__)
    mode=p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--preflight',action='store_true',help='Read version and model catalog only')
    mode.add_argument('--execute',action='store_true',help='Create temporary sessions and execute the test Skill')
    p.add_argument('--model',help='Exact model ID from the service catalog (required for execution)')
    p.add_argument('--startup-message',help='Explicit visible system startup text for the empty-user-input probe')
    p.add_argument('--answer',help='Explicit fixture answer for the probe question; never auto-answer unexpected questions')
    p.add_argument('--output',type=Path,default=Path('/tmp/skill-eval-blade-verification.json'))
    p.add_argument('--timeout',type=float,default=180,help='Maximum seconds for each generation')
    p.add_argument('--scenario',choices=('upload','basic','empty','interaction','stop','all'),default='basic')
    return p


async def chat(client, session_id, prompt, model, evidence, *, timeout=180, stop=False, **opts):
    stream=client.chat(session_id,prompt,model=model,headless=False,mode='executing',**opts)
    events=[]
    try:
        async with asyncio.timeout(timeout):
            async for event in stream:
                row={'kind':event.kind,'raw':redact_json(event.raw)}
                events.append(row)
                if stop and event.kind=='chat:start':
                    raw=event.raw.get('payload',event.raw)
                    target=raw.get('chat_run_id')
                    if target:
                        receipt=await client.stop(session_id,chat_run_id=target)
                        evidence.setdefault('stop_receipts',[]).append(redact_json(receipt))
                        stop=False
                if event.kind=='chat:end':
                    break
    finally:
        await stream.close(send_stop=not any(e['kind']=='chat:end' for e in events))
        evidence['generations'].append({'prompt':prompt,'options':redact_json(opts),'events':events})
        print(json.dumps({'generation':len(evidence['generations']),'event_count':len(events),'kinds':dict(Counter(e['kind'] for e in events))},ensure_ascii=False),flush=True)
    return events


async def verify(args, *, client_factory=None, settings=None):
    import httpx
    from blade_agent_kit import BladeAgentClient
    cfg=settings or load_settings()
    if not cfg.blade.api_key or not cfg.blade.api_key.get_secret_value().strip():
        raise ValueError('Configure blade.api_key in octagon.yaml before verification')
    if args.execute and not args.model:
        raise ValueError('--execute requires --model')
    factory=client_factory or BladeAgentClient
    report={'timestamp':datetime.now(timezone.utc).isoformat(),'sdk_version':version('blade-agent-kit'),
            'mode':'execute' if args.execute else 'preflight','service_version':None,'sessions':[],
            'checks':{},'generations':[],'errors':[]}
    async with factory(cfg.blade.base_url,token=cfg.blade.api_key.get_secret_value(),timeout=30) as client:
        # The public version endpoint is a read-only query, independent of execution.
        async with httpx.AsyncClient(base_url=cfg.blade.base_url,timeout=20) as http:
            response=await http.get('/api/version')
            response.raise_for_status()
            data=response.json()
            report['service_version']=data.get('version')
        models=await client.list_models()
        report['models']=[{'id':m.id,'label':m.label} for m in models.models]
        report['default_model']=models.default
        report['checks']['model_catalog']='verified'
        if not args.execute:
            return report
        if args.model not in {m.id for m in models.models}:
            raise ValueError('Requested model is not in the configured service catalog')
        name='octagon/probe-'+uuid.uuid4().hex[:12]
        marker='PROBE_'+uuid.uuid4().hex
        md=f'''---
name: {name}
description: Self-contained temporary Octagon skill protocol verification.
---
Use only this session workspace. Do not access network or edit existing inputs.
When invoked, run the bundled scripts/probe.sh directly as an executable,
passing probe-result.txt as its output argument. Return its stdout verbatim.
When the user explicitly requests a question, call AskUserQuestion and wait.
'''
        script=f'#!/bin/sh\nprintf "%s\\n" "{marker}" > "$1"\nprintf "%s\\n" "{marker}"\n'
        session=None
        try:
            session=await client.create_session(intent='',model=args.model,initiator='sdk',solution_id='general_chat')
            if not session.id:
                raise RuntimeError('Server did not return a session ID')
            report['sessions'].append(session.id)
            print(json.dumps({'session_id':session.id,'phase':'created'}),flush=True)
            before=await client.get_history(session.id)
            report['checks']['empty_intent_history_nodes']=len(before.nodes)
            upload=await client.upload_session_skill(session.id,name=name,files=[
                {'path':'SKILL.md','content':md},
                {'path':'scripts/probe.sh','content':script,'executable':True},
                {'path':'assets/data.bin','content':base64.b64encode(b'\0\xffprobe').decode(),'encoding':'base64'},
            ])
            report['upload']=redact_json(upload.raw)
            report['checks']['skill_upload']='verified' if upload.file_count==3 else 'unknown'
            with tempfile.TemporaryDirectory(prefix='octagon-skill-probe-') as temp:
                material=Path(temp)/'probe-input.txt';material.write_text('MATERIAL_'+marker)
                if args.scenario!='empty':
                    receipt=await client.upload_file(session.id,material,remote_path='probe-input.txt')
                    report['input_upload']=redact_json(receipt.raw)
                if args.scenario in ('basic','all','empty'):
                    prompt=(args.startup_message or '') if args.scenario=='empty' else f'Use the installed skill {name}. Read its SKILL.md from the installed skill path, invoke its bundled scripts/probe.sh directly as an executable with output probe-result.txt, then read probe-input.txt. Only create new output files, do not modify inputs.'
                    report['system_startup_message']=args.startup_message if args.scenario=='empty' else None
                    opts={'refs':[{'provider':'skill','id':name,'label':name}]}
                    await chat(client,session.id,prompt,args.model,report,timeout=args.timeout,**opts)
                    try:
                        output=await client.download_file(session.id,'probe-result.txt')
                        report['checks']['script_output']='verified' if output.strip()==marker.encode() else 'mismatch'
                        report['checks']['output_sha256']=hashlib.sha256(output).hexdigest()
                    except Exception as error:
                        report['checks']['script_output']='unknown'
                        report['errors'].append({'phase':'download','type':type(error).__name__})
                    # Append a new attachment and follow up in the SAME session.
                    extra=Path(temp)/'followup.txt';extra.write_text('FOLLOWUP_'+marker)
                    await client.upload_file(session.id,extra,remote_path='followup.txt')
                    await chat(client,session.id,'Read followup.txt and reply with its exact content. Do not run the script again.',args.model,report,timeout=args.timeout)
                if args.scenario in ('interaction','all'):
                    events=await chat(client,session.id,'For this protocol test call AskUserQuestion now: ask which label to use, with options Alpha and Beta. Do not decide for me and do not call other tools.',args.model,report,timeout=args.timeout)
                    end=next((e['raw'] for e in reversed(events) if e['kind']=='chat:end'),{})
                    report['interaction_end']=end
                    # Never fabricate a live answer or auto-answer an unexpected question.
                    report['checks']['interaction']='pending_user' if end.get('pause_tool_data') else 'unknown'
                    pause=end.get('pause_tool_data')
                    if args.answer and isinstance(pause,dict) and pause.get('tool_call_id'):
                        answer={'tool_call_id':pause['tool_call_id'],'selections':{},'custom':{'0':args.answer}}
                        answered=await chat(client,session.id,args.answer,args.model,report,timeout=args.timeout,askuser_answer=answer)
                        report['checks']['interaction_answer_source']='explicit_probe_argument'
                        report['checks']['interaction_answer']='verified' if any(e['kind']=='chat:end' and e['raw'].get('status')=='completed' for e in answered) else 'unknown'
                if args.scenario in ('stop','all'):
                    await chat(client,session.id,'Write the integers 1 through 5000, each on a new line; use no tools.',args.model,report,timeout=args.timeout,stop=True)
                    state=await client.get_session(session.id)
                    report['after_stop']=redact_json(state.raw)
                    report['checks']['stop']='receipt_received' if report.get('stop_receipts') else 'unconfirmed'
            history=await client.get_history(session.id)
            report['history']=redact_json(history.raw)
            report['final_directory']=[redact_json(e.raw) for e in await client.list_dir(session.id)]
            report['checks']['same_session_followup']='verified' if len(report['generations'])>=2 else 'unknown'
        except Exception as error:
            detail={'phase':'execute','type':type(error).__name__,'message':redact_json(str(error))}
            response=getattr(error,'response',None)
            if response is not None:
                try: detail['response']=redact_json(response.json())
                except ValueError: detail['response']='non_json_error_response'
            report['errors'].append(detail)
            if session and session.id:
                try:
                    report['failure_session']=redact_json((await client.get_session(session.id)).raw)
                    report['failure_history']=redact_json((await client.get_history(session.id)).raw)
                except Exception as read_error:
                    report['failure_reconciliation']={'status':'unknown','type':type(read_error).__name__}
        finally:
            if session and session.id:
                try:
                    await client.delete_session(session.id,delete_workspace=True)
                    report['cleanup']='deleted_owned_session'
                except Exception as error:
                    report['cleanup']='unconfirmed'
                    report['errors'].append({'phase':'cleanup','type':type(error).__name__})
    return report


def save_report(path, report):
    path.parent.mkdir(parents=True,exist_ok=True)
    data=redact_json(report)
    with path.open('w',encoding='utf-8') as stream:
        os.chmod(path,0o600)
        json.dump(data,stream,ensure_ascii=False,indent=2)


def main():
    args=parser().parse_args()
    if args.timeout<=0:
        raise SystemExit('--timeout must be positive')
    try:
        report=asyncio.run(verify(args))
    except Exception as error:
        print(json.dumps({'error_type':type(error).__name__,'message':redact_json(str(error))}),file=sys.stderr)
        raise SystemExit(1)
    save_report(args.output,report)
    print(json.dumps({'report':str(args.output),'service_version':report['service_version'],'checks':report['checks'],'errors':report['errors']},ensure_ascii=False))
    if report['errors']:
        raise SystemExit(1)


if __name__=='__main__':
    main()
