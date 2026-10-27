#!/usr/bin/env python3
"""Explicit offline demo for browser acceptance. Never uses octagon.yaml/key."""
import argparse
import asyncio
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from backend.config import Settings
from backend.skill_eval.api import register_routes as register_assets
from backend.skill_eval.run_api import register_routes as register_runs
from backend.skill_eval.capabilities import Capabilities
from backend.skill_eval.runs import RunService
from backend.skill_eval.worker import Worker
from tests.skill_eval.fakes import FakeExecutionRuntime


def create_demo(data_path):
    cfg=Settings(octagon={'data_path':data_path},skill_eval={'enabled':True})
    service=RunService(data_path/'octagon.db',data_path,settings=cfg)
    service.repo.initialize()
    runtime=FakeExecutionRuntime(service.store)
    class DemoCapabilities(Capabilities):
        async def discover(self):return {'status':'available','default':'demo-model','models':[{'id':'demo-model','label':'模拟模型（离线演示）'}]}
        def local(self):return {**super().local(),'demo':True,'execution':{'status':'available','reason':'offline_demo'}}
    caps=DemoCapabilities(cfg)
    worker=Worker(service,runtime)
    @asynccontextmanager
    async def lifespan(app):
        pending=service.import_package('SKILL.md',b'---\nname: report-skill\nmetadata:\n  version: 0.0.1\n---\nRead input data and produce a new report. Preserve original inputs.\n')
        imported=service.commit_import(pending['id'],'report-skill','0.0.1')
        draft=imported['draft']
        service.patch_draft(draft['id'],1,[{'op':'write','path':'scripts/analyze.py','text':'from pathlib import Path\n\ndef analyze(text: str) -> str:\n    return "Report: " + text.strip()\n'},{'op':'write','path':'references/format.md','text':'# Report format\nSummary, evidence, limitations.\n'}])
        service.save_draft(draft['id'],2,1)
        experiment=service.create_experiment(imported['skill']['id'],'报告结构与证据可读性')
        current=service.get_draft(draft['id'])
        snapshot=service.snapshot_draft(current['id'],current['revision'])
        for index in range(3):
            result=service.create_run({'experiment_id':experiment['id'],'snapshot_id':snapshot['id'],'draft_id':draft['id'],'release_id':imported['release']['id'],'model':{'id':'demo-model'},'prompt':f'生成简洁报告，方案 {index+1}。'},idempotency_key=f'demo-{index}',allowed_models=['demo-model'])
            await worker.tick();service.finish(result['run_id']);await worker.tick()
            service.save_note(result['run_id'],['初始方案，结论较简洁。','调整了结构，下一步检查工具证据。','保留核心结论，作为当前参考。'][index],0)
        task=asyncio.create_task(worker.run_forever())
        try:yield
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True);await worker.close()
    app=FastAPI(lifespan=lifespan)
    register_assets(app,service,capabilities=caps);register_runs(app,service,caps)
    @app.get('/api/capabilities')
    def shell_capabilities():return {'features':{'experiments':False},'display_only':False}
    return app


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=18100)
    parser.add_argument('--data-path',type=Path)
    args=parser.parse_args()
    import uvicorn
    with tempfile.TemporaryDirectory(prefix='octagon-skill-eval-demo-') as temporary:
        uvicorn.run(create_demo(args.data_path or Path(temporary)),host='127.0.0.1',port=args.port)


if __name__=='__main__':main()
