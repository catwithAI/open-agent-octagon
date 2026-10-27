import httpx
import pytest

from backend.skill_eval.api import create_app
from backend.config import Settings
from backend.main import create_app as platform_app
from .test_drafts import service


async def test_full_asset_http_flow_and_display_only(service):
    transport=httpx.ASGITransport(app=create_app(service))
    async with httpx.AsyncClient(transport=transport,base_url='http://local') as client:
        pending=(await client.post('/api/skill-eval/skills/import',files={'file':('SKILL.md',b'---\nname: sample\n---\nx')})).json()
        item=(await client.post(f"/api/skill-eval/imports/{pending['id']}/commit",json={'name':'sample'})).json()
        draft=item['draft']['id']
        changes={'expected_revision':1,'changes':[{'op':'write','path':'script.py','text':'print(1)'}]}
        export=await client.post(f'/api/skill-eval/drafts/{draft}/export',json=changes)
        assert export.status_code==200 and export.content.startswith(b'PK')
        release=item['release']['id']
        assert (await client.get(f'/api/skill-eval/releases/{release}')).json()['revision']==1
        saved=await client.post(f'/api/skill-eval/drafts/{draft}/save',json={**changes,'expected_release_revision':1})
        assert saved.status_code==200
        assert saved.json()['draft']['revision']==2
        text=await client.get(f'/api/skill-eval/drafts/{draft}/files',params={'path':'script.py'})
        assert text.json()['text']=='print(1)'
        published=await client.post(f'/api/skill-eval/drafts/{draft}/publish',json={'expected_revision':2,'expected_release_revision':2,'version':'1.0'})
        assert published.status_code==201
        exp=await client.post('/api/skill-eval/experiments',json={'skill_id':item['skill']['id'],'name':'experiment'})
        assert exp.status_code==201
        eid=exp.json()['id']
        assert (await client.patch(f'/api/skill-eval/experiments/{eid}',json={'name':'new name'})).json()['name']=='new name'
        response=await client.post('/api/skill-eval/inputs',files={'file':('a.txt',b'original')})
        iid=response.json()['id']
        assert (await client.get(f'/api/skill-eval/inputs/{iid}/download')).content==b'original'
        assert (await client.get('/api/skill-eval/drafts/unknown')).status_code==404
        assert (await client.patch(f'/api/skill-eval/drafts/{draft}',json={'expected_revision':2,'changes':[{'op':'delete','path':'SKILL.md'}]})).status_code==422
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(service,display_only=True)),base_url='http://local') as client:
        assert (await client.get('/api/skill-eval/skills')).status_code==200
        for method,path,payload in [('post','/api/skill-eval/drafts',{'release_id':release}),('patch',f'/api/skill-eval/experiments/{eid}',{'name':'blocked'})]:
            assert (await client.request(method,path,json=payload)).status_code==403


def test_real_application_registers_asset_routes_without_executing(tmp_path):
    cfg=Settings(octagon={'data_path':tmp_path,'display_only':True},skill_eval={'catalog_roots':[str(tmp_path/'catalog')]})
    app=platform_app(cfg)
    paths={route.path for route in app.routes if hasattr(route,'path')}
    assert '/api/skill-eval/skills/import' in paths
    assert '/api/skill-eval/drafts/{id_}/publish' in paths
    assert cfg.skill_eval.enabled is False
    with pytest.raises(ValueError):Settings(skill_eval={'limits':{'max_upload':-1}})
