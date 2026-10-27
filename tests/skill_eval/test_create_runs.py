import pytest
from backend.config import Settings
from backend.skill_eval.runs import RunService
from backend.skill_eval.service import EvalError
from .test_imports import imported


@pytest.fixture
def run_service(tmp_path):
    cfg=Settings(octagon={'data_path':tmp_path},skill_eval={'enabled':True})
    service=RunService(tmp_path/'db',tmp_path,settings=cfg);service.repo.initialize()
    return service


def prepared(service):
    item=imported(service)
    experiment=service.create_experiment(item['skill']['id'],'experiment')
    return item,{'experiment_id':experiment['id'],'snapshot_id':item['snapshot']['id'],'release_id':item['release']['id'],'model':{'id':'fixture'},'prompt':'','input_ids':[]}


def test_queued_run_freezes_empty_inputs_and_is_idempotent(run_service):
    service=run_service;item,body=prepared(service)
    result=service.create_run(body,idempotency_key='one',allowed_models=['fixture'])
    run=service.get_run(result['run_id'])
    assert run['run_status']=='queued'
    assert run['input_snapshot']['prompt']==''
    assert run['input_snapshot']['system_startup_message']
    assert service.create_run(body,idempotency_key='one',allowed_models=['fixture'])['run_id']==run['id']
    with pytest.raises(EvalError):service.create_run({**body,'prompt':'new'},idempotency_key='one',allowed_models=['fixture'])
    assert service.create_run(body,idempotency_key='two',allowed_models=['fixture'])['run_id']!=run['id']
    service.save_draft(item['draft']['id'],1,1,changes=[{'op':'write','path':'new.py','text':'new'}])
    assert service.get_run(run['id'])['skill_version_id']==item['snapshot']['id']
    assert service.get_run(run['id'])['source_release_revision']==1


def test_creation_rejects_invalid_agent_model_and_experiment(run_service):
    service=run_service;_,body=prepared(service)
    with pytest.raises(ValueError):service.create_run({**body,'agent':'codex'},idempotency_key='one',allowed_models=['fixture'])
    with pytest.raises(EvalError):service.create_run(body,idempotency_key='one',allowed_models=[])
    other=imported(service,name='other')
    with pytest.raises(EvalError):service.create_run({**body,'snapshot_id':other['snapshot']['id']},idempotency_key='one',allowed_models=['fixture'])
    with service.repo.connection() as c:
        assert c.execute('SELECT count(*) FROM skill_eval_runs').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM skill_eval_jobs').fetchone()[0]==0


def test_finish_closes_messages_and_notes_stay_optional(run_service):
    service=run_service;_,body=prepared(service)
    id_=service.create_run(body,idempotency_key='one',allowed_models=['fixture'])['run_id']
    with service.repo.transaction() as c:
        c.execute("UPDATE skill_eval_runs SET run_status='waiting_continue' WHERE id=?",(id_,))
    message=service.send_message(id_,{'text':'followup'},idempotency_key='m1')
    assert service.send_message(id_,{'text':'followup'},idempotency_key='m1')['id']==message['id']
    service.finish(id_)
    with pytest.raises(EvalError):service.send_message(id_,{'text':'late'},idempotency_key='m2')
    assert service.get_run(id_)['note']['text']==''
    assert service.save_note(id_,'optional',0)['revision']==1
    assert service.save_note(id_,'',1)['text']==''
    assert service.finish(id_)['run_status']=='finishing'


def test_db_failure_rolls_back_run_messages_jobs_and_idempotency_receipt(run_service,monkeypatch):
    service=run_service;_,body=prepared(service)
    def fail(*args,**kwargs):raise OSError('simulated database failure')
    monkeypatch.setattr(service,'_job',fail)
    with pytest.raises(OSError):service.create_run(body,idempotency_key='r',allowed_models=['fixture'])
    with service.repo.connection() as c:
        for table in ('runs','messages','jobs','requests'):
            assert c.execute('SELECT count(*) FROM skill_eval_'+table).fetchone()[0]==0
