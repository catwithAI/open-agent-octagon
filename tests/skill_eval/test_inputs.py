import pytest
from backend.skill_eval.service import SkillService,EvalError
from .test_drafts import service


def test_inputs_are_independent_immutable_and_scoped(service,tmp_path):
    a=service.upload_input('input.bin',b'\0\xff','application/octet-stream')
    b=service.upload_input('input.bin',b'new','application/octet-stream')
    assert a['id']!=b['id']
    assert a['hash']!=b['hash']
    assert a['remote_path']!=b['remote_path']
    assert service.read_input(a['id'])==b'\0\xff'
    assert service.read_input(b['id'])==b'new'
    empty=service.upload_input('empty',b'','text/plain')
    assert empty['size']==0
    other=SkillService(service.repo.db_path,service.store.root.parent,scope='other')
    with pytest.raises(EvalError):other.read_input(a['id'])
    with pytest.raises(ValueError):service.upload_input('../escape',b'bad','text/plain')
