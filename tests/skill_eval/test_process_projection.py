import json
from pathlib import Path
from backend.skill_eval.capture import Capture
from backend.skill_eval.process import project


def event(seq,kind,payload,source='socket'):
    return {'seq':seq,'event_id':f'e{seq}','type':kind,'source':source,'payload':Capture().encode(kind,{'payload':payload})}


def test_real_snapshot_fixture_associates_results_and_react_without_double_counting():
    fixture=json.loads(Path('tests/fixtures/skill_eval/blade_beta42_events.json').read_text())
    events=[{'seq':i+1,'event_id':f'e{i}','type':row['kind'],'source':'socket','payload':Capture().encode(row['kind'],row['raw'])} for i,row in enumerate(fixture)]
    result=project(events)
    assert len(result['actions'])==5
    assert all(action['result'] is not None for action in result['actions'])
    assert all(action['react_turn_id'] for action in result['actions'])
    assert any(turn['thinking'] for turn in result['turns'])
    assert result==project(events)
    assert len({turn['id'] for turn in result['turns']})==len(result['turns'])
    assert result['actual_model_request']['status']=='unavailable'


def test_reverse_results_same_names_cross_loop_and_no_invented_react():
    turns=[
        {'id':'t1','loop_id':'root','role':'assistant','status':'completed','blocks':[{'type':'tool_use','tool_call_id':'a','tool_name':'read','content':'A'},{'type':'tool_use','tool_call_id':'b','tool_name':'read','content':'B'}]},
        {'id':'t2','loop_id':'child','role':'assistant','status':'completed','blocks':[{'type':'tool_result','tool_call_id':'a','content':'wrong loop'}]},
        {'id':'t3','loop_id':'root','role':'tool','status':'completed','blocks':[{'type':'tool_result','tool_call_id':'b','content':'result B'},{'type':'tool_result','tool_call_id':'a','content':'result A'}]},
        {'id':'t4','loop_id':'root','role':'assistant','status':'completed','blocks':[{'type':'tool_use','tool_call_id':'c','tool_name':'read','content':'next action only'}]},
    ]
    result=project([event(i+1,'turn:snapshot',{'turn':t}) for i,t in enumerate(turns)])
    a,b,c=result['actions']
    assert a['result']=='result A' and b['result']=='result B'
    assert a['react_turn_id']==b['react_turn_id']=='t4'
    assert c['result'] is None and c['react_turn_id'] is None
    assert 'unmatched_or_ambiguous_tool_result' in result['gaps']


def test_missing_capture_never_fills_reasoning_or_request_context():
    item=event(1,'turn:snapshot',{'turn':{'id':'x','role':'assistant'}})
    item['payload']=Capture(server_max='metadata').encode('turn:snapshot',{'payload':{'turn':{'id':'x','blocks':[{'type':'thinking','content':'CANARY'}]}}})
    result=project([item])
    assert result['turns']==[]
    assert 'CANARY' not in str(result)
    assert result['gaps']==['body_not_captured']



def test_reused_turn_id_across_loops_never_merges_tools_or_returns():
    events=[]
    for i,loop in enumerate(('root','child')):
        events.append(event(i+1,'turn:snapshot',{'turn':{'id':'same','loop_id':loop,'role':'assistant','status':'completed','blocks':[{'type':'tool_use','tool_call_id':'same-call','tool_name':'read','content':{'path':loop}},{'type':'tool_result','tool_call_id':'same-call','content':loop}]}}))
    result=project(events)
    assert {turn['id'] for turn in result['turns']}=={'root:same','child:same'}
    assert len(result['actions'])==2
    assert {action['result'] for action in result['actions']}=={'root','child'}
    assert all(isinstance(action['arguments'],str) for action in result['actions'])


def test_history_supplements_a_missing_return_without_replacing_public_thinking():
    first=event(1,'turn:snapshot',{'turn':{'id':'t','loop_id':'root','role':'assistant','status':'completed','blocks':[{'type':'thinking','content':'public reasoning'},{'type':'tool_use','tool_call_id':'a','tool_name':'read','content':'{}'}]}})
    supplement=event(2,'session:history',{'nodes':[{'id':'t','loop_id':'root','role':'assistant','blocks':[{'type':'tool_result','tool_call_id':'a','content':'history result'}]}]},source='history')
    result=project([first,supplement])
    assert result['turns'][0]['thinking']=='public reasoning'
    assert result['actions'][0]['result']=='history result'
    assert result['actions'][0]['result_event_id']=='e2'


def test_reasoning_summary_stays_separate_from_message_and_reasoning_body():
    result=project([event(1,'llm:thinking:summary',{'turn_id':'t','content':'public summary'}),event(2,'turn:snapshot',{'turn':{'id':'t','loop_id':'root','role':'assistant','status':'completed','blocks':[{'type':'text','content':'assistant reply'}]}})])
    assert result['turns'][0]['reasoning_summary']=='public summary'
    assert result['turns'][0]['text']=='assistant reply'
    assert result['turns'][0]['thinking']==''


def test_malformed_public_fields_are_reported_as_gaps_not_guessed():
    result=project([event(1,'turn:snapshot',{'turn':{'id':{'invalid':True},'blocks':[]}}),event(2,'turn:snapshot',{'turn':{'id':'valid','role':'assistant','blocks':[None,{'type':'tool_use','tool_call_id':{},'tool_name':{'bad':'name'},'content':{'safe':'argument'}}]}})])
    assert result['gaps']==['invalid_block','missing_tool_call_identity','missing_turn_identity']
    assert result['actions'][0]['name'] is None and result['actions'][0]['tool_call_id'] is None
