import pytest
from backend.skill_eval.service import SkillService, EvalError
from .test_imports import imported


@pytest.fixture
def service(db_path,tmp_path):
    svc=SkillService(db_path,tmp_path);svc.repo.initialize();return svc


def test_atomic_whole_tree_edit_and_revision(service):
    item=imported(service);draft=item['draft']
    changes=[{'op':'write','path':'SKILL.md','text':'---\nname: sample\n---\nchanged'}, {'op':'write','path':'a.py','text':'one'}, {'op':'write','path':'b.sh','text':'two','mode':0o755}]
    updated=service.patch_draft(draft['id'],1,changes)
    assert updated['revision']==2
    assert {e['path'] for e in updated['entries']}=={'SKILL.md','a.py','b.sh'}
    with pytest.raises(EvalError):service.patch_draft(draft['id'],1,changes)
    with pytest.raises(ValueError):service.patch_draft(draft['id'],2,[{'op':'delete','path':'SKILL.md'}])
    assert service.get_draft(draft['id'])==updated
    assert service.read_draft_file(draft['id'],'a.py')['text']=='one'
    release=service.get_release(item['release']['id'])
    assert release['current_version_id']==item['snapshot']['id']


def test_rename_swap_applies_to_final_tree_and_binary_replacement(service):
    draft=imported(service)['draft']
    draft=service.patch_draft(draft['id'],1,[{'op':'write','path':'a','text':'A'}, {'op':'write','path':'b','content_base64':'AP8='}])
    draft=service.patch_draft(draft['id'],2,[{'op':'rename','path':'a','to':'b'}, {'op':'rename','path':'b','to':'a'}])
    assert service.read_draft_file(draft['id'],'b')['text']=='A'
    assert service.read_draft_file(draft['id'],'a')['content_base64']=='AP8='
    with pytest.raises(ValueError):service.patch_draft(draft['id'],3,[{'op':'write','path':'a','text':'lossy'}])
