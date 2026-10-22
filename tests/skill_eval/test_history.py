from backend.skill_eval.queries import Queries
from .test_create_runs import run_service,prepared


def test_history_keyset_and_unverified_groups_do_not_merge(run_service):
    service=run_service;_,body=prepared(service)
    ids=[]
    for i in range(3):ids.append(service.create_run(body,idempotency_key=str(i),allowed_models=['fixture'])['run_id'])
    query=Queries(service);first=query.history(limit=1)
    second=query.history(limit=1,cursor=first['next_cursor'])
    assert first['items'][0]['id']!=second['items'][0]['id']
    groups=query.groups()
    assert groups['items'][0]['run_count']==3
    assert groups['items'][0]['group_key'].startswith('requested/unverified:')
    assert 'input_snapshot_json' not in str(first)


def test_model_groups_require_root_observation_and_confirmed_provider(run_service):
    from backend.skill_eval.worker import Worker
    from .fakes import FakeExecutionRuntime
    service=run_service;_,body=prepared(service);body['model']={'id':'provider-a::canonical'}
    ids=[service.create_run(body,idempotency_key=str(i),allowed_models=['provider-a::canonical'])['run_id'] for i in range(3)]
    worker=Worker(service,FakeExecutionRuntime(service.store))
    for id_ in ids[:2]:worker.refs(id_,session_model='provider-a::canonical')
    worker.observe_model(ids[0],'loop:turn',{'payload':{'loop_name':'child','model':'child-model'}},'child-event')
    assert service.get_run(ids[0])['resolved_model'] is None
    for id_ in ids[:2]:worker.observe_model(id_,'loop:turn',{'payload':{'loop_name':'root','model':'canonical'}},'root-event')
    groups=Queries(service).groups()['items']
    assert len(groups)==2
    assert sorted(group['run_count'] for group in groups)==[1,2]
    resolved=next(group for group in groups if group['group_key'].startswith('resolved:'))
    assert resolved['resolved_provider']=='provider-a' and resolved['resolved_model']=='canonical'
    worker.observe_model(ids[0],'loop:turn',{'payload':{'loop_name':'root','model':'fallback-model'}},'fallback-event')
    assert service.get_run(ids[0])['group_key'].startswith('requested/unverified:')
    assert service.get_run(ids[0])['resolved_model'] is None


def test_history_model_time_and_invalid_cursor_filters(run_service):
    import pytest
    service=run_service;_,body=prepared(service)
    run=service.create_run(body,idempotency_key='r',allowed_models=['fixture'])['run_id']
    query=Queries(service)
    assert query.history(model='fixture',created_after='2000-01-01T00:00:00Z')['items'][0]['id']==run
    assert query.history(model='other')['items']==[]
    assert query.history(created_before='2000-01-01T00:00:00Z')['items']==[]
    for kwargs in ({'cursor':'broken'},{'created_after':'2020-01-01'}):
        with pytest.raises(ValueError):query.history(**kwargs)
