from backend.skill_eval.events import EventStore
from backend.skill_eval.worker import Worker
from .fakes import FakeExecutionRuntime
from .test_create_runs import run_service,prepared


def stored_response(text,loop='root'):
    return {'loop_name':loop,'body':{'type':'llm:response:done','payload':{'loop_name':loop,'role':'assistant','content':text,'finish_reason':'stop'}}}


def stored_end(chat_run_id='c1',output=None):
    body={'status':'completed','chat_run_id':chat_run_id}
    if output is not None:body['output']=output
    return {'chat_run_id':chat_run_id,'body':body}


def raw_response(text,loop='root'):
    return {'type':'llm:response:done','payload':{'loop_name':loop,'role':'assistant','content':text,'finish_reason':'stop'}}


def raw_end(chat_run_id='c1'):
    return {'status':'completed','chat_run_id':chat_run_id}


def test_final_reply_falls_back_to_the_sdk_response_event():
    events=[
        {'event_id':'e1','type':'llm:response:done','payload':stored_response('draft')},
        {'event_id':'e2','type':'chat:end','payload':stored_end()},
        {'event_id':'e3','type':'llm:response:done','payload':stored_response('final answer')},
        {'event_id':'e4','type':'chat:end','payload':stored_end('c2')},
    ]
    assert Worker.latest_reply(events)=={'event_id':'e3','text':'final answer'}


def test_chat_end_output_still_wins_when_present():
    events=[{'event_id':'e1','type':'llm:response:done','payload':stored_response('response')},
            {'event_id':'e2','type':'chat:end','payload':stored_end(output='explicit output')}]
    assert Worker.latest_reply(events)=={'event_id':'e2','text':'explicit output'}


def test_run_chat_replies_use_response_content_when_chat_end_has_no_output(run_service):
    service=run_service;item,body=prepared(service)
    run_id=service.create_run(body,idempotency_key='reply',allowed_models=['fixture'])['run_id']
    run=service.get_run(run_id)
    store=EventStore(service.repo,Worker(service,FakeExecutionRuntime(service.store)).capture(run))
    store.append(run_id,'llm:response:done',raw_response('real sdk answer'))
    store.append(run_id,'chat:end',raw_end('chat-1'))
    replies=service.get_run(run_id)['chat_replies']
    assert [reply['text'] for reply in replies]==['real sdk answer']
