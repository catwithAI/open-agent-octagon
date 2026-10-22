"""Project public Blade facts; never reconstruct hidden reasoning or requests."""
from __future__ import annotations

import json
from collections import defaultdict


def display(value):
    if value is None:return ''
    return value if isinstance(value,str) else json.dumps(value,ensure_ascii=False)


def project(events):
    turns={};loops={};deltas=defaultdict(lambda:{'text':[],'thinking':[],'reasoning_summary':[]})
    result_first={};gaps=set();last_cursor=0
    for event in events:
        last_cursor=max(last_cursor,event['seq'])
        envelope=event['payload'];raw=envelope.get('body')
        if not raw:
            gaps.add(envelope.get('body_omitted') or 'body_not_captured')
            continue
        kind=event['type'];payload=raw.get('payload',raw)
        if not isinstance(payload,dict):continue
        if kind=='loop:turn' and isinstance(payload.get('turn_id'),str):
            loops[payload['turn_id']]=payload.get('loop_name') or 'unknown'
        if kind in ('llm:content:delta','llm:thinking:delta','llm:thinking:summary'):
            id_=payload.get('turn_id')
            if isinstance(id_,str):
                category='reasoning_summary' if kind=='llm:thinking:summary' else 'thinking' if kind=='llm:thinking:delta' else 'text'
                deltas[id_][category].append(str(payload.get('content','')))
        candidates=[]
        if kind=='turn:snapshot':candidates=[payload.get('turn',{})]
        elif kind=='turn:end':candidates=[payload]
        elif event['source']=='history':
            candidates=payload.get('nodes',[]) if isinstance(payload.get('nodes'),list) else [payload]
        for candidate in candidates:
            if not isinstance(candidate,dict):continue
            id_=candidate.get('turn_id') or candidate.get('id')
            if not isinstance(id_,str) or not id_ or len(id_)>300:
                gaps.add('missing_turn_identity');continue
            loop=candidate.get('loop_id') or candidate.get('loop_name') or loops.get(id_,'unknown')
            if not isinstance(loop,str) or not loop or len(loop)>300:loop='unknown'
            # Prefer a confirmed loop for a previously unlabelled snapshot.
            known=[value for (known_loop,known_id),value in turns.items() if known_id==id_]
            if loop=='unknown' and len(known)==1:loop=known[0]['loop']
            identity=(loop,id_)
            if identity not in turns and loop!='unknown' and ('unknown',id_) in turns:
                turns[identity]=turns.pop(('unknown',id_))
            blocks=candidate.get('blocks')
            if not isinstance(blocks,list):
                blocks=[]
                if candidate.get('content'):
                    blocks.append({'type':'tool_result' if candidate.get('role')=='tool' else 'text',
                                   'content':candidate['content'],'tool_call_id':candidate.get('tool_call_id')})
                for call in candidate.get('tool_calls') or []:
                    if not isinstance(call,dict):
                        gaps.add('invalid_tool_call');continue
                    function=call.get('function',{})
                    if not isinstance(function,dict):
                        gaps.add('invalid_tool_call');continue
                    blocks.append({'type':'tool_use','tool_call_id':call.get('id'),'tool_name':function.get('name'),'content':function.get('arguments')})
            if any(not isinstance(block,dict) for block in blocks):gaps.add('invalid_block')
            blocks=[{**block,'tool_call_id':block.get('tool_call_id') if isinstance(block.get('tool_call_id'),str) else None} for block in blocks if isinstance(block,dict)]
            old=turns.get(identity)
            # Sparse history is a supplement, not a replacement for richer snapshots.
            if old and event['source']=='history' and old['source']!='history':
                previous={(block.get('type'),block.get('tool_call_id')) for block in old['blocks']}
                for block in blocks:
                    signature=(block.get('type'),block.get('tool_call_id'))
                    if block.get('type') in ('tool_use','tool_result') and signature not in previous:
                        old['blocks'].append({**block,'_event_id':event['event_id'],'_source':'history'})
                        if block.get('type')=='tool_result' and block.get('tool_call_id'):
                            result_first.setdefault((loop,block['tool_call_id']),event['seq'])
                continue
            turn={'id':id_,'loop':loop,'role':candidate.get('role') if isinstance(candidate.get('role'),str) else 'unknown','kind':candidate.get('kind') if isinstance(candidate.get('kind'),str) else 'message',
                  'status':candidate.get('status','unknown'),'first_seq':old['first_seq'] if old else event['seq'],
                  'remote_sequence':candidate.get('sequence'),'source':event['source'],
                  'event_id':event['event_id'],'blocks':blocks,'text':'','thinking':'','reasoning_summary':'',
                  'context':display(candidate.get('context_content')) or None,'context_kind':candidate.get('context_kind'),
                  'provenance':{'source':event['source'],'event_id':event['event_id'],'request_context_verified':False}}
            turn['text']='\n'.join(display(b.get('content')) for b in blocks if b.get('type') in ('text','content'))
            turn['thinking']='\n'.join(display(b.get('content')) for b in blocks if b.get('type') in ('thinking','reasoning'))
            turn['reasoning_summary']='\n'.join(display(b.get('content')) for b in blocks if b.get('type')=='reasoning_summary')
            for block in blocks:
                if block.get('type')=='tool_result' and block.get('tool_call_id'):
                    result_first.setdefault((loop,block['tool_call_id']),event['seq'])
            turns[identity]=turn
    identities=defaultdict(int)
    for _,id_ in turns:identities[id_]+=1
    for (_,id_),turn in turns.items():
        turn['remote_turn_id']=id_
        if identities[id_]>1:
            turn['id']=turn['loop']+':'+id_
            gaps.add('turn_id_reused_across_loops')
        if turn['loop']=='unknown' and id_ in loops:turn['loop']=loops[id_]
        if turn['status']!='completed' and identities[id_]==1:
            if not turn['text']:turn['text']=''.join(deltas[id_]['text'])
            if not turn['thinking']:turn['thinking']=''.join(deltas[id_]['thinking'])
        if identities[id_]==1 and not turn['reasoning_summary']:turn['reasoning_summary']=''.join(deltas[id_]['reasoning_summary'])
    ordered=sorted(turns.values(),key=lambda t:(t['first_seq'],t['id']))
    actions=[];by_call=defaultdict(list);pending=[]
    for turn in ordered:
        for index,block in enumerate(turn['blocks']):
            kind=block.get('type')
            id_=block.get('tool_call_id')
            if kind=='tool_use':
                action={'id':f"{turn['id']}:{id_ or index}",'turn_id':turn['id'],'loop':turn['loop'],
                        'tool_call_id':id_,'name':block.get('tool_name') if isinstance(block.get('tool_name'),str) else None,'arguments':display(block.get('content')) or None,
                        'result':None,'result_event_id':None,'react_turn_id':None,
                        'association':'unconfirmed','provenance':turn['provenance']}
                actions.append(action)
                if id_:by_call[(turn['loop'],id_)].append(action)
                else:gaps.add('missing_tool_call_identity')
            elif kind=='tool_result':
                pending.append((turn,block))
    for turn,block in pending:
        id_=block.get('tool_call_id')
        options=by_call[(turn['loop'],id_)] if id_ else []
        own=[action for action in options if action['turn_id']==turn['id']]
        match=own[0] if len(own)==1 else options[0] if len(options)==1 else None
        if not match:
            gaps.add('unmatched_or_ambiguous_tool_result');continue
        match['result']=display(block.get('content'))
        match['result_event_id']=block.get('_event_id',turn['event_id'])
        match['association']='explicit_tool_call_id'
        first_result=result_first.get((turn['loop'],id_),turn['first_seq'])
        next_turn=next((other for other in ordered if other['role']=='assistant' and other['kind']!='context'
                       and other['loop']==turn['loop'] and other['first_seq']>first_result and other['id']!=match['turn_id']),None)
        if next_turn:
            match['react_turn_id']=next_turn['id']
            match['react_relation']='next_observed_model_turn_same_loop'
    return {'schema_version':1,'cursor':last_cursor,'turns':ordered,'actions':actions,
            'gaps':sorted(gaps),'actual_model_request':{'status':'unavailable','reason':'public_events_not_provider_requests'}}
