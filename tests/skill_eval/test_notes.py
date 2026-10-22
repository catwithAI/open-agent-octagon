import pytest
from backend.skill_eval.worker import Worker
from backend.skill_eval.service import EvalError
from .test_create_runs import run_service,prepared
from .fakes import FakeExecutionRuntime


async def test_optional_note_is_editable_after_end_and_never_enters_model_or_skill(run_service):
    service=run_service;item,body=prepared(service)
    id_=service.create_run(body,idempotency_key='r',allowed_models=['fixture'])['run_id']
    runtime=FakeExecutionRuntime(service.store);worker=Worker(service,runtime);await worker.tick();service.finish(id_);await worker.tick()
    assert service.get_run(id_)['note']['text']==''
    service.save_note(id_,'NOTE_CANARY',0)
    with pytest.raises(EvalError):service.save_note(id_,'conflicting',0)
    assert 'NOTE_CANARY' not in str(runtime.calls) and 'NOTE_CANARY' not in str(service.get_run(id_)['input_snapshot'])
    assert b'NOTE_CANARY' not in service.export_run_skill(id_)
    service.save_note(id_,'changed',1);service.save_note(id_,'',2)
    assert service.get_run(id_)['note']['text']==''
    with pytest.raises(ValueError):service.save_note(id_,'x'*(service.settings.skill_eval.note_max_chars+1),3)
