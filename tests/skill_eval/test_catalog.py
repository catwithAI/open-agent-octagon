from pathlib import Path

import pytest

from backend.skill_eval.catalog import SkillCatalog
from backend.skill_eval.service import EvalError
from .test_drafts import service


def test_catalog_copy_is_read_only_and_fingerprint_checks(service,tmp_path):
    root=tmp_path/'catalog';package=root/'sample';package.mkdir(parents=True)
    body=package/'SKILL.md';body.write_text('---\nname: sample\n---\noriginal')
    before=body.read_bytes()
    catalog=SkillCatalog([root])
    listing=catalog.list();entry=listing['items'][0]
    assert str(root) not in str(listing)
    data=catalog.copy(entry['id'],entry['fingerprint'])
    imported=service.import_package('catalog.zip',data,source_ref=entry['id'])
    service.commit_import(imported['id'],'sample',None)
    assert body.read_bytes()==before
    body.write_text('---\nname: sample\n---\nchanged')
    with pytest.raises(EvalError,match='changed'):
        catalog.copy(entry['id'],entry['fingerprint'])
    with pytest.raises(EvalError):catalog.copy('/etc/passwd',entry['fingerprint'])


def test_catalog_rejects_symlink_without_exposing_host_paths(tmp_path):
    root=tmp_path/'catalog';root.mkdir()
    (root/'SKILL.md').write_text('---\nname: sample\n---\nx')
    (root/'link').symlink_to('/etc/passwd')
    result=SkillCatalog([root]).list()
    assert result['items'][0]['status']=='invalid'
    assert '/etc/passwd' not in str(result)
