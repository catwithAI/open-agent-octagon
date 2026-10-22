"""Bound multipart/JSON bodies before FastAPI parses or spools their contents."""
from starlette.responses import JSONResponse


class BodyTooLarge(Exception):
    pass


class BodyLimits:
    def __init__(self,app,*,package_bytes,input_bytes,json_bytes=150*1024*1024):
        self.app=app;self.package_bytes=package_bytes;self.input_bytes=input_bytes;self.json_bytes=json_bytes

    async def __call__(self,scope,receive,send):
        if scope['type']!='http' or scope.get('method') not in ('POST','PATCH','PUT') or not scope.get('path','').startswith('/api/skill-eval/'):
            return await self.app(scope,receive,send)
        path=scope['path']
        maximum=self.package_bytes+65536 if path.endswith('/skills/import') else self.input_bytes+65536 if path.endswith('/inputs') else self.json_bytes
        async def reject():
            response=JSONResponse({'error':{'code':'request_too_large','message':'Skill Eval request body limit exceeded'}},status_code=413)
            await response(scope,receive,send)
        headers=dict(scope.get('headers',[]))
        if b'content-length' in headers:
            try:length=int(headers[b'content-length'])
            except ValueError:
                return await JSONResponse({'error':{'code':'invalid_content_length','message':'Invalid Content-Length'}},status_code=400)(scope,receive,send)
            if length<0:
                return await JSONResponse({'error':{'code':'invalid_content_length','message':'Invalid Content-Length'}},status_code=400)(scope,receive,send)
            if length>maximum:return await reject()
        size=0;exceeded=False
        async def checked_receive():
            nonlocal size,exceeded
            message=await receive()
            if message['type']=='http.request':
                size+=len(message.get('body',b''))
                if size>maximum:
                    exceeded=True
                    raise BodyTooLarge()
            return message
        async def checked_send(message):
            if not exceeded:await send(message)
        try:
            await self.app(scope,checked_receive,checked_send)
        except Exception:
            if not exceeded:raise
        if exceeded:await reject()
