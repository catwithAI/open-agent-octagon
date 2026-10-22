"""Deterministic remote boundary with explicit gates, not wall-clock sleeps."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Clock:
    now: float = 1_792_627_200.0

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("clock cannot go backwards")
        self.now += seconds

    def __call__(self) -> float:
        return self.now


@dataclass
class Gate:
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    proceed: asyncio.Event = field(default_factory=asyncio.Event)

    async def wait(self) -> None:
        self.entered.set()
        await self.proceed.wait()


class FakeBladeRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.gates: dict[str, Gate] = {}
        self.failures: dict[str, Exception] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.events: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.files: dict[str, dict[str, bytes]] = defaultdict(dict)

    async def invoke(self, operation: str, **payload: Any) -> dict[str, Any]:
        self.calls.append((operation, payload))
        if operation in self.gates:
            await self.gates[operation].wait()
        if operation in self.failures:
            raise self.failures[operation]
        if operation == "create":
            session_id = f"session-{len(self.sessions) + 1}"
            self.sessions[session_id] = payload
            return {"session_id": session_id}
        return {"accepted": True, "operation_id": str(len(self.calls))}

    def count(self, operation: str) -> int:
        return sum(name == operation for name, _ in self.calls)


class FakeExecutionRuntime(FakeBladeRuntime):
    """Worker-facing protocol fake. It does not mock SDK encoding tests."""
    def __init__(self,store):
        super().__init__();self.store=store;self.serial=0;self.status={};self.history_nodes=defaultdict(list)

    async def open(self):pass
    async def close(self):pass

    async def create(self,run):
        result=await self.invoke('create',run_id=run['id'])
        self.status[result['session_id']]='idle'
        return result

    async def upload_skill(self,run,session_id,entries):
        await self.invoke('skill_upload',session_id=session_id,files=[e.path for e in entries])
        return {'name':'octagon/eval-'+run['id'],'file_count':len(entries),'overwritten':False}

    async def upload_input(self,session_id,input_):
        await self.invoke('input_upload',session_id=session_id,path=input_['remote_path'])
        self.files[session_id][input_['remote_path']]=self.store.read_blob(input_['hash'])
        return {'path':input_['remote_path']}

    async def chat(self,run,session_id,message,*,skill_name=None,answer=None):
        self.serial+=1;chat_id=f'chat-{self.serial}'
        await self.invoke('answer' if answer else 'chat',session_id=session_id,message=message,answer=answer,skill_name=skill_name)
        self.status[session_id]='running'
        yield {'kind':'chat:start','raw':{'type':'chat:start','payload':{'chat_run_id':chat_id}}}
        if 'generation' in self.gates:await self.gates['generation'].wait()
        paused='ask me' in message and not answer
        output='chosen '+str(answer.get('custom',{}).get('0') or ('Alpha' if answer.get('selections',{}).get('0')==[0] else 'Beta')) if answer else 'result '+str(self.serial)
        if self.status[session_id]=='interrupted':
            paused=False;output=''
        elif paused:self.status[session_id]='paused'
        else:
            self.status[session_id]='completed'
            self.files[session_id]['report.txt']=output.encode()
        turn={'id':f'turn-{self.serial}','loop_id':'root','role':'assistant','kind':'message','status':'completed','blocks':[{'type':'text','content':output}]}
        self.history_nodes[session_id].append(turn)
        yield {'kind':'turn:snapshot','raw':{'type':'turn:snapshot','payload':{'turn':turn}}}
        pause={'tool_call_id':'question-'+chat_id,'name':'builtin:AskUserQuestion','arguments':{'questions':[{'question':'Which label?','options':[{'label':'Alpha'},{'label':'Beta'}]}]}} if paused else None
        yield {'kind':'chat:end','raw':{'chat_run_id':chat_id,'status':'paused' if paused else 'interrupted' if self.status[session_id]=='interrupted' else 'completed','finish_reason':'paused' if paused else 'user_stop' if self.status[session_id]=='interrupted' else 'completed','output':output,'pause_tool_data':pause}}

    async def stop(self,session_id,chat_run_id):
        await self.invoke('stop',session_id=session_id,chat_run_id=chat_run_id)
        self.status[session_id]='interrupted'
        if 'generation' in self.gates:self.gates['generation'].proceed.set()
        return {'chat_run_id':chat_run_id,'status':'stopping'}

    async def history(self,session_id):
        await self.invoke('history',session_id=session_id)
        return {'nodes':self.history_nodes[session_id]}

    async def session(self,session_id):
        return {'id':session_id,'status':self.status[session_id]}

    async def list_files(self,session_id,*,max_entries=1000):
        await self.invoke('list_files',session_id=session_id)
        return [{'path':path,'state':'listed','size':len(data)} for path,data in self.files[session_id].items()]

    async def download(self,session_id,path,*,max_bytes):
        await self.invoke('download',session_id=session_id,path=path)
        data=self.files[session_id][path]
        if len(data)>max_bytes:raise ValueError('Artifact file size limit')
        return data
