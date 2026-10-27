import json
import pytest
from backend.skill_eval.capture import Capture
from backend.skill_eval.events import EventStore
from backend.skill_eval.repository import Repository
from .test_schema_runs import seed


@pytest.mark.parametrize('requested,maximum,effective', [('full',None,'full'),('full','parsed','parsed'),('full','metadata','metadata'),('parsed','off','off'),('metadata',None,'metadata')])
def test_policy_combinations(requested,maximum,effective):
    capture=Capture(requested=requested,server_max=maximum)
    assert capture.policy.effective==effective
    event=capture.encode('llm:thinking:delta',{'payload':{'content':'body-canary','authorization':'Bearer test-secret','api_key':'canary'}})
    if effective in ('off','metadata'):
        assert 'body-canary' not in json.dumps(event)
    else:
        assert 'body-canary' in json.dumps(event)
        assert 'test-secret' not in json.dumps(event)
        assert event['body']['payload']['api_key']=='[REDACTED]'
    assert capture.manifest()['coverage']['actual_model_request']['status']=='unavailable'


@pytest.mark.parametrize('policy',['off','metadata'])
def test_body_canary_never_reaches_any_disk_or_notification(db_path,tmp_path,policy):
    repo=Repository(db_path);repo.initialize();seed(repo)
    notifications=[]
    store=EventStore(repo,Capture(requested=policy),notify=notifications.append)
    store.append('a','model',{'payload':{'content':'BODY_SHOULD_NOT_PERSIST'}})
    for path in tmp_path.rglob('*'):
        if path.is_file():assert b'BODY_SHOULD_NOT_PERSIST' not in path.read_bytes()
    assert 'BODY_SHOULD_NOT_PERSIST' not in str(notifications)


def test_redaction_failure_and_size_limits_drop_body(monkeypatch):
    capture=Capture(max_bytes=10)
    event=capture.encode('model',{'content':'a'*100})
    assert event['body'] is None
    assert event['body_omitted']=='event_size_limit'
    def fail(*args,**kwargs):raise ValueError('bad redactor')
    monkeypatch.setattr('backend.wire.redaction.redact_json',fail)
    event=Capture().encode('model',{'content':'body-canary'})
    assert event['body'] is None
    assert event['body_omitted']=='redaction_failed'
