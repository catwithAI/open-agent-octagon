#!/usr/bin/env python3
"""Explicit live acceptance of Octagon HTTP -> queue -> Blade -> result storage.

Uses temporary SQLite/files, synthetic inputs, and only deletes its own sessions.
The production feature switch and configured data directory are never modified.
"""
from __future__ import annotations
import argparse
import asyncio
import hashlib
import io
import json
import sys
import tempfile
import uuid
import zipfile
from datetime import datetime,timezone
from importlib.metadata import version
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import httpx
from fastapi import FastAPI
from backend.config import load_settings
from backend.skill_eval.api import register_routes as assets
from backend.skill_eval.run_api import register_routes as runs
from backend.skill_eval.capabilities import Capabilities
from backend.skill_eval.runs import RunService
from backend.skill_eval.runtime import BladeSkillRuntime
from backend.skill_eval.worker import Worker
from backend.wire.redaction import redact_json


def save(path,report):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w') as stream:
        path.chmod(0o600);json.dump(redact_json(report),stream,ensure_ascii=False,indent=2)


async def verify(args):
    cfg=load_settings().model_copy(deep=True)
    cfg.skill_eval.enabled=True;cfg.octagon.display_only=False
    report={'timestamp':datetime.now(timezone.utc).isoformat(),'sdk_version':version('blade-agent-kit'),'model':args.model,'stages':[],'runs':[],'errors':[],'cleanup':[]}
    async with httpx.AsyncClient(base_url=cfg.blade.base_url,timeout=30) as http:
        report['server_version']=(await http.get('/api/version')).json().get('version')
    with tempfile.TemporaryDirectory(prefix='octagon-live-workflow-') as directory:
        root=Path(directory);cfg.octagon.data_path=root
        service=RunService(root/'db',root,settings=cfg);service.repo.initialize()
        runtime=BladeSkillRuntime(cfg,service.store);await runtime.open()
        app=FastAPI();cap=Capabilities(cfg);assets(app,service);runs(app,service,cap);worker=Worker(service,runtime)
        def stage(name,**values):
            report['stages'].append({'name':name,**values});save(args.output,report)
            print(json.dumps({'stage':name,**values},ensure_ascii=False),flush=True)
        marker='LIVE_'+uuid.uuid4().hex
        binary=b'\x00\xff\x80octagon-resource\n'
        md=f'''---\nname: workflow-probe\ndescription: Synthetic read-only-input acceptance skill.\n---\nUse only this session workspace; do not access any network or change existing inputs.\nOn task execution, invoke bundled scripts/probe.sh DIRECTLY as an executable with output filename report.txt. It emits {marker}. Also copy the bundled assets/data.bin to a NEW workspace output named binary-output.bin without altering the resource. Return the marker.\nWhen user requests a question, use AskUserQuestion with options Alpha and Beta, then wait. Never answer on the user's behalf.\n'''
        package=io.BytesIO()
        with zipfile.ZipFile(package,'w',zipfile.ZIP_DEFLATED) as z:
            z.writestr('SKILL.md',md)
            info=zipfile.ZipInfo('scripts/probe.sh');info.external_attr=0o100755<<16;info.compress_type=zipfile.ZIP_DEFLATED
            z.writestr(info,f'#!/bin/sh\nprintf "%s\\n" "{marker}" > "$1"\nprintf "%s\\n" "{marker}"\n')
            z.writestr('assets/data.bin',binary)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://local') as client:
                async def post(path,body=None,key=None):
                    response=await client.post('/api/skill-eval'+path,json=body or {},headers={'Idempotency-Key':key} if key else {})
                    if response.status_code>=300:raise RuntimeError(f'{path}: HTTP {response.status_code}: {response.text[:400]}')
                    return response.json()
                async def get(path):
                    response=await client.get('/api/skill-eval'+path);response.raise_for_status();return response.json()
                async def tick(id_,label):
                    await worker.tick();run=service.get_run(id_)
                    stage(label,run_id=id_,status=run['run_status'],generation=run['generation_status'],error_code=run['error_code'])
                    if run['run_status']=='failed':raise RuntimeError(f'Worker failed at {label}: {run["error_code"]}')
                    return run
                upload=await client.post('/api/skill-eval/skills/import',files={'file':('probe.zip',package.getvalue())});upload.raise_for_status()
                item=await post(f"/imports/{upload.json()['id']}/commit",{'name':'workflow-probe'})
                draft=item['draft'];release=item['release'];experiment=await post('/experiments',{'skill_id':item['skill']['id'],'name':'Live acceptance'})
                if args.controls_only:
                    body={'experiment_id':experiment['id'],'snapshot_id':item['snapshot']['id'],'release_id':release['id'],'agent':'blade-agent','model':{'id':args.model},'prompt':'For a stop protocol test, print the integers 1 to 10000, one per line, without tools.'}
                    id_=(await post('/runs',body,'controls'))['run_id'];report['runs'].append(id_)
                    async def accepted(previous=None):
                        async with asyncio.timeout(60):
                            while True:
                                current=service.get_run(id_)
                                target=current['external_refs'].get('chat_run_id')
                                if target and target!=previous:return target
                                if current['run_status']=='failed':raise RuntimeError('Generation failed before acceptance')
                                await asyncio.sleep(0.05)
                    generating=asyncio.create_task(worker.tick())
                    try:
                        first_chat=await accepted()
                        await post(f'/runs/{id_}/stop-generation')
                        await generating
                    finally:
                        if not generating.done():generating.cancel();await asyncio.gather(generating,return_exceptions=True)
                    current=service.get_run(id_);assert current['generation_status']=='stopped',current['generation_status']
                    stage('targeted_stop_verified',run_id=id_,generation_status=current['generation_status'])
                    await post(f'/runs/{id_}/messages',{'text':'Again print integers 1 to 10000, one per line, without tools.'},'continue-after-stop')
                    generating=asyncio.create_task(worker.tick())
                    try:
                        await accepted(first_chat)
                        await post(f'/runs/{id_}/finish')
                        await generating
                    finally:
                        if not generating.done():generating.cancel();await asyncio.gather(generating,return_exceptions=True)
                    current=await tick(id_,'finish_during_generation_collected')
                    assert current['run_status']=='completed' and current['final_result']['complete']
                    late=await client.post(f'/api/skill-eval/runs/{id_}/messages',json={'text':'late'},headers={'Idempotency-Key':'late'})
                    assert late.status_code==409
                    report['checks']={'targeted_stop':'verified','continue_after_stop':'verified','finish_during_generation':'verified','late_input_rejected':'verified'}
                    return report
                material=await client.post('/api/skill-eval/inputs',files={'file':('input.txt',b'SYNTHETIC_MATERIAL')});material.raise_for_status()
                snapshot=await post(f"/drafts/{draft['id']}/snapshots",{'expected_revision':1,'changes':[{'op':'write','path':'references/extra.txt','text':'unsaved resource'}]})
                body={'experiment_id':experiment['id'],'snapshot_id':snapshot['id'],'draft_id':draft['id'],'release_id':release['id'],'agent':'blade-agent','model':{'id':args.model},'prompt':'Execute the selected Skill exactly. Run the script directly as executable, read the uploaded input, and create report.txt plus binary-output.bin. Preserve input bytes.','input_ids':[material.json()['id']]}
                first=(await post('/runs',body,'live-1'))['run_id'];report['runs'].append(first)
                await tick(first,'initial_execution')
                await post(f'/runs/{first}/messages',{'text':'Now call AskUserQuestion: which label, Alpha or Beta? Wait for my answer.'},'question')
                run=await tick(first,'question_pause')
                questions=(await get(f'/runs/{first}/interactions'))['items'];pending=[q for q in questions if q['state']=='pending']
                if run['run_status']!='waiting_input' or len(pending)!=1:raise RuntimeError('Expected a real pending question')
                extra=await client.post('/api/skill-eval/inputs',files={'file':('answer.txt',b'ANSWER_MATERIAL')});extra.raise_for_status()
                await post(f"/runs/{first}/interactions/{pending[0]['id']}/answer",{'answer':{'selections':{'0':[0]},'custom':{}},'input_ids':[extra.json()['id']],'expected_revision':pending[0]['revision']},'explicit-fixture-answer')
                await tick(first,'explicit_answer_with_attachment')
                await post(f'/runs/{first}/finish');run=await tick(first,'first_final_collection')
                final=run['final_result'];assert final and final['complete'],final
                assert run['resolved_model']==args.model.split('::')[-1]
                assert run['resolved_provider']==args.model.split('::')[0]
                outputs={a['relative_path']:a for a in final['artifacts'] if a['state']=='complete'}
                assert service.store.read_blob(outputs['report.txt']['hash']).strip()==marker.encode()
                assert service.store.read_blob(outputs['binary-output.bin']['hash'])==binary
                assert service.read_input(material.json()['id'])==b'SYNTHETIC_MATERIAL'
                stage('final_bytes_verified',binary_sha256=hashlib.sha256(binary).hexdigest(),artifact_count=len(outputs))
                note=await client.put(f'/api/skill-eval/runs/{first}/note',json={'text':'LIVE_NOTE_CANARY','expected_revision':0});note.raise_for_status()
                refined=await post(f'/runs/{first}/drafts')
                changed=await post(f"/drafts/{refined['id']}/snapshots",{'expected_revision':1,'changes':[{'op':'write','path':'references/extra.txt','text':'refined resource'}]})
                plan=await post('/rerun-plans',{'source_run_id':first,'mode':'reuse_all'});await post(f"/rerun-plans/{plan['id']}/confirm",{'expected_hash':plan['hash']})
                second=(await post('/runs',{**body,'snapshot_id':changed['id'],'draft_id':refined['id'],'source_run_id':first,'rerun_plan_id':plan['id'],'prompt':'','input_ids':[]},'live-2'))['run_id'];report['runs'].append(second)
                await tick(second,'refined_rerun')
                # A model may legitimately ask again from the carried context. Do
                # not auto-answer: preserve the pause and explicitly finish it.
                await post(f'/runs/{second}/finish');await tick(second,'second_final_collection')
                replacement=await post('/rerun-plans',{'source_run_id':second,'mode':'replace'});await post(f"/rerun-plans/{replacement['id']}/confirm",{'expected_hash':replacement['hash'],'prompt':'','input_ids':[]})
                third=(await post('/runs',{**body,'source_run_id':second,'rerun_plan_id':replacement['id'],'prompt':'','input_ids':[]},'live-3'))['run_id'];report['runs'].append(third)
                empty=await tick(third,'empty_user_input_execution');assert empty['input_snapshot']['prompt']=='' and not empty['input_snapshot']['inputs']
                await post(f'/runs/{third}/finish');await tick(third,'third_final_collection')
                comparison=await post('/comparisons/query',{'run_ids':[first,second,third],'baseline_run_id':first});assert len(comparison['columns'])==3
                assert 'LIVE_NOTE_CANARY' not in str(service.get_run(second)['input_snapshot'])
                published=await post(f"/drafts/{refined['id']}/publish",{'expected_revision':1,'expected_release_revision':1,'version':'1.0','changes':[{'op':'write','path':'references/extra.txt','text':'refined resource'}]})
                exported=await client.get(f"/api/skill-eval/versions/{published['snapshot']['id']}/export");exported.raise_for_status()
                with zipfile.ZipFile(io.BytesIO(exported.content)) as z:
                    assert z.read('assets/data.bin')==binary
                    assert (z.getinfo('scripts/probe.sh').external_attr>>16)&0o111
                reimport=await client.post('/api/skill-eval/skills/import',files={'file':('export.zip',exported.content)});reimport.raise_for_status()
                await post(f"/imports/{reimport.json()['id']}/commit",{'name':'workflow-roundtrip','version':'1.0'})
                report['checks']={'three_run_comparison':'verified','unsaved_execution':'verified','question_and_attachment':'verified','binary_roundtrip':'verified','empty_inputs':'verified','publish_export_reimport':'verified'}
                report['evidence']=[{'run_id':id_,'session_id':service.get_run(id_)['external_refs'].get('session_id'),'projected_turn_count':len((await get(f'/runs/{id_}/process'))['turns']),'resolved_model':service.get_run(id_)['resolved_model'],'resolved_provider':service.get_run(id_)['resolved_provider'],'sdk_version':service.get_run(id_)['external_refs'].get('sdk_version'),'server_version':service.get_run(id_)['external_refs'].get('server_version'),'result':service.get_run(id_)['final_result']} for id_ in report['runs']]
                stage('workflow_complete')
        except Exception as error:
            report['errors'].append({'type':type(error).__name__,'message':str(error)[:1000]})
        finally:
            for id_ in report['runs']:
                sid=service.get_run(id_)['external_refs'].get('session_id')
                if sid:
                    try:await runtime.client.delete_session(sid,delete_workspace=True);report['cleanup'].append({'session_id':sid,'status':'deleted_owned_session'})
                    except Exception as error:report['cleanup'].append({'session_id':sid,'status':'unknown','type':type(error).__name__})
            await runtime.close();save(args.output,report)
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--execute',action='store_true',required=True);parser.add_argument('--controls-only',action='store_true');parser.add_argument('--model',required=True);parser.add_argument('--output',type=Path,default=Path('/tmp/skill-eval-live-workflow.json'))
    args=parser.parse_args();result=asyncio.run(verify(args));print(json.dumps({'checks':result.get('checks',{}),'errors':result['errors'],'cleanup':result['cleanup']},ensure_ascii=False));sys.exit(1 if result['errors'] else 0)
