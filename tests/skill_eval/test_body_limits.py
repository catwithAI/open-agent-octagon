import asyncio
import json
from backend.skill_eval.body_limits import BodyLimits


async def test_chunked_body_is_rejected_before_application_can_accept_it():
    chunks=iter([{'type':'http.request','body':b'1234','more_body':True},{'type':'http.request','body':b'5678','more_body':False}]);sent=[];accepted=[]
    async def receive():return next(chunks)
    async def send(message):sent.append(message)
    async def application(scope,receive,send):
        try:
            await receive();await receive();accepted.append(True)
        except Exception:
            # A framework parser can turn a read failure into 400. The outer
            # boundary must preserve the actual 413 and send one response only.
            await send({'type':'http.response.start','status':400,'headers':[]})
            await send({'type':'http.response.body','body':b'bad request'})
    await BodyLimits(application,package_bytes=1,input_bytes=1,json_bytes=5)({'type':'http','method':'POST','path':'/api/skill-eval/runs','headers':[]},receive,send)
    assert accepted==[]
    assert [message['status'] for message in sent if message['type']=='http.response.start']==[413]
    assert json.loads(sent[-1]['body'])['error']['code']=='request_too_large'


async def test_content_length_rejection_does_not_read_or_parse_body():
    async def never(*args):raise AssertionError('must not parse oversized body')
    sent=[]
    async def send(message):sent.append(message)
    await BodyLimits(never,package_bytes=1,input_bytes=1,json_bytes=5)({'type':'http','method':'POST','path':'/api/skill-eval/runs','headers':[(b'content-length',b'6')]},never,send)
    assert sent[0]['status']==413
