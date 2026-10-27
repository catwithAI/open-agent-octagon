"""Skill Eval package HTTP boundary; execution routes are added separately."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from .catalog import SkillCatalog
from .capabilities import Capabilities
from .service import EvalError, SkillService


class RequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ImportCommit(RequestBody):
    name: str = Field(min_length=1,max_length=200)
    version: str | None = Field(default=None,max_length=200)


class CatalogCopy(RequestBody):
    catalog_id: str
    expected_fingerprint: str


class DraftCreate(RequestBody):
    release_id: str


class Changes(RequestBody):
    expected_revision: int = Field(ge=1)
    changes: list[dict[str,Any]] = Field(default_factory=list,max_length=1000)


class Save(Changes):
    expected_release_revision: int | None = Field(default=None,ge=1)
    version: str | None = Field(default=None,max_length=200)


class Publish(Save):
    version: str = Field(min_length=1,max_length=200)


class ExperimentCreate(RequestBody):
    skill_id: str
    name: str = Field(min_length=1,max_length=200)


class ExperimentRename(RequestBody):
    name: str = Field(min_length=1,max_length=200)


def register_routes(app: FastAPI, service: SkillService, catalog: SkillCatalog | None = None, capabilities: Capabilities | None = None):
    catalog = catalog or SkillCatalog()
    if not getattr(app.state,'skill_eval_body_limits_installed',False):
        from .body_limits import BodyLimits
        settings=getattr(service,'settings',None)
        app.add_middleware(BodyLimits,package_bytes=service.limits.max_upload,input_bytes=service.input_max_bytes,
            json_bytes=settings.skill_eval.json_max_bytes if settings else 150*1024*1024)
        app.state.skill_eval_body_limits_installed=True
    router=APIRouter(prefix='/api/skill-eval',tags=['skill-eval'])

    @app.exception_handler(EvalError)
    async def eval_error(request, error):
        return JSONResponse(status_code=error.status,content={'error':{'code':error.code,'message':error.message}})

    # All client changes pass through this wrapper, avoiding a global ValueError
    # handler that could change legacy APIs or expose server-side paths.
    def call(function, *args, **kwargs):
        try:
            return function(*args,**kwargs)
        except ValueError as error:
            raise HTTPException(422,detail={'code':'invalid_skill_input','message':str(error)}) from error
        except FileNotFoundError as error:
            raise EvalError('content_unavailable','Stored content is unavailable',410) from error

    if capabilities is not None:
        @router.get('/capabilities')
        def capability_report():return capabilities.local()

        @router.get('/models')
        async def models():return await capabilities.discover()

    @router.get('/skills')
    def skills():return {'items':call(service.list_skills)}

    @router.get('/skills/{id_}')
    def skill(id_:str):return call(service.get_skill,id_)

    @router.get('/releases/{id_}')
    def release(id_:str):return call(service.get_release,id_)

    @router.get('/releases/{id_}/export')
    def export_release(id_:str):
        return Response(call(service.export_release,id_),media_type='application/zip',headers={'Content-Disposition':'attachment; filename="skill.zip"'})

    @router.get('/catalog')
    def list_catalog():return call(catalog.list)

    @router.post('/skills/from-catalog',status_code=201)
    def from_catalog(body:CatalogCopy):
        data=call(catalog.copy,body.catalog_id,body.expected_fingerprint)
        return call(service.import_package,'catalog.zip',data,source_ref=body.catalog_id)

    @router.post('/skills/import',status_code=201)
    async def import_package(file:UploadFile=File(...)):
        chunks=[];total=0
        try:
            while chunk:=await file.read(64*1024):
                total+=len(chunk)
                if total>service.limits.max_upload:
                    raise EvalError('upload_too_large','Package upload limit exceeded',413)
                chunks.append(chunk)
            return await run_in_threadpool(call,service.import_package,file.filename or '',b''.join(chunks))
        finally:
            await file.close()

    @router.get('/imports/{id_}')
    def get_import(id_:str):return call(service.get_import,id_)

    @router.post('/imports/{id_}/commit',status_code=201)
    def commit(id_:str,body:ImportCommit):return call(service.commit_import,id_,body.name,body.version)

    @router.post('/drafts',status_code=201)
    def draft(body:DraftCreate):return call(service.create_draft,body.release_id)

    @router.get('/drafts/{id_}')
    def get_draft(id_:str):return call(service.get_draft,id_)

    @router.get('/drafts/{id_}/files')
    def read_file(id_:str,path:str):return call(service.read_draft_file,id_,path)

    @router.get('/drafts/{id_}/download')
    def download_draft_file(id_:str,path:str):
        from .storage import safe_path
        draft=call(service.get_draft,id_)
        entry=next((entry for entry in draft['entries'] if entry['path']==call(safe_path,path)),None)
        if entry is None:raise EvalError('not_found','Draft file not found',404)
        return Response(service.store.read_blob(entry['hash']),media_type='application/octet-stream',headers={'Content-Disposition':'attachment; filename="resource"','X-Content-Type-Options':'nosniff'})

    @router.patch('/drafts/{id_}')
    def patch(id_:str,body:Changes):return call(service.patch_draft,id_,body.expected_revision,body.changes)

    @router.post('/drafts/{id_}/snapshots',status_code=201)
    def snapshot(id_:str,body:Changes):return call(service.snapshot_draft,id_,body.expected_revision,body.changes)

    @router.post('/drafts/{id_}/save')
    def save(id_:str,body:Save):return call(service.save_draft,id_,body.expected_revision,body.expected_release_revision,body.version,body.changes)

    @router.post('/drafts/{id_}/publish',status_code=201)
    def publish(id_:str,body:Publish):return call(service.publish_draft,id_,body.expected_revision,body.expected_release_revision,body.version,body.changes)

    @router.post('/drafts/{id_}/export')
    def export_draft(id_:str,body:Changes):
        return Response(call(service.export_draft,id_,body.expected_revision,body.changes),media_type='application/zip',headers={'Content-Disposition':'attachment; filename="skill.zip"'})

    @router.get('/versions/{id_}')
    def version_metadata(id_:str):
        with service.repo.connection() as c:
            snapshot=service._version(c,id_)
        from dataclasses import asdict
        return {**snapshot,'entries':[asdict(entry) for entry in service.store.get_manifest(snapshot['manifest_ref'])]}

    @router.get('/versions/{id_}/files')
    def snapshot_file(id_:str,path:str):return call(service.read_snapshot_file,id_,path)

    @router.get('/versions/{id_}/export')
    def export(id_:str):return Response(call(service.export_snapshot,id_),media_type='application/zip',headers={'Content-Disposition':'attachment; filename="skill.zip"'})

    @router.post('/inputs',status_code=201)
    async def upload_input(file:UploadFile=File(...)):
        chunks=[];total=0
        try:
            while chunk:=await file.read(64*1024):
                total+=len(chunk)
                if total>service.input_max_bytes:
                    raise EvalError('upload_too_large','Input upload limit exceeded',413)
                chunks.append(chunk)
            return await run_in_threadpool(call,service.upload_input,file.filename or '',b''.join(chunks),file.content_type or 'application/octet-stream')
        finally:
            await file.close()

    @router.get('/inputs/{id_}')
    def input_info(id_:str):return call(service.get_input,id_)

    @router.get('/inputs/{id_}/download')
    def input_download(id_:str):
        return Response(call(service.read_input,id_),media_type='application/octet-stream',headers={'Content-Disposition':'attachment; filename="input.bin"','X-Content-Type-Options':'nosniff'})

    @router.post('/experiments',status_code=201)
    def experiment(body:ExperimentCreate):return call(service.create_experiment,body.skill_id,body.name)

    @router.get('/experiments')
    def experiments(skill_id:str):return {'items':call(service.list_experiments,skill_id)}

    @router.get('/experiments/{id_}')
    def get_experiment(id_:str):return call(service.get_experiment,id_)

    @router.patch('/experiments/{id_}')
    def rename_experiment(id_:str,body:ExperimentRename):return call(service.rename_experiment,id_,body.name)

    app.include_router(router)


def create_app(service: SkillService, catalog: SkillCatalog | None = None, *, display_only: bool = False) -> FastAPI:
    """Small ASGI test harness, using the production router and read-only guard."""
    from backend.display_only import install
    app=FastAPI()
    register_routes(app,service,catalog)
    install(app,display_only)
    return app
