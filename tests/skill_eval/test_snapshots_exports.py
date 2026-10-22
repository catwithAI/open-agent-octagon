import io
import zipfile
from .test_drafts import service
from .test_imports import imported


def test_unsaved_snapshot_and_export_include_all_edits_without_save(service):
    item=imported(service)
    snap=service.snapshot_draft(item['draft']['id'],1,[{'op':'write','path':'SKILL.md','text':'---\nname: sample\n---\nunsaved'}, {'op':'write','path':'scripts/run.sh','text':'echo new','mode':0o755}])
    assert service.get_release(item['release']['id'])==item['release']
    assert service.get_draft(item['draft']['id'])['revision']==1
    zipped=service.export_snapshot(snap['id'])
    with zipfile.ZipFile(io.BytesIO(zipped)) as z:
        assert b'unsaved' in z.read('SKILL.md')
        assert z.read('scripts/run.sh')==b'echo new'
        assert set(z.namelist())=={'SKILL.md','scripts/run.sh'}
    pending=service.import_package('roundtrip.zip',zipped)
    assert pending['metadata']['suggested_metadata']['content_hash']==snap['content_hash']


def test_platform_name_and_published_version_survive_export_without_rewriting_skill_md(service):
    item=imported(service,body=b'---\nname: original\nmetadata:\n  version: 0.1\n---\nx',name='platform-name',version='beta')
    published=service.publish_draft(item['draft']['id'],1,1,'1.0')
    # The exact same bytes can have multiple labels; supply the explicit frozen
    # label when exporting a user-selected release or historical run.
    zipped=service.export_snapshot(published['snapshot']['id'],platform_metadata={'name':'platform-name','version':'1.0','status':'published'})
    pending=service.import_package('export.zip',zipped)
    assert pending['detected_name']=='platform-name'
    assert pending['detected_version']=='1.0'
    with zipfile.ZipFile(io.BytesIO(zipped)) as z:
        assert b'version: 0.1' in z.read('SKILL.md')
    from backend.skill_eval.service import EvalError
    import pytest
    with pytest.raises(EvalError):service.commit_import(pending['id'],'platform-name',None)
    imported_new=service.commit_import(pending['id'],'another-skill','1.0')
    assert imported_new['release']['status']=='unpublished'


def test_selected_release_export_has_version_but_historical_snapshot_stays_stable(service):
    import json
    item=imported(service)
    original=service.export_snapshot(item['snapshot']['id'])
    published=service.publish_draft(item['draft']['id'],1,1,'1.0')
    exported=service.export_release(published['release']['id'])
    with zipfile.ZipFile(io.BytesIO(exported)) as archive:
        metadata=json.loads(archive.comment)['octagon.skill']
        assert metadata['version']=='1.0' and metadata['status']=='published'
    assert service.export_snapshot(item['snapshot']['id'])==original
