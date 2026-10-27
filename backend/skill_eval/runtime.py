"""Blade protocol boundary. No scoring callback, CLI fallback, or auto-answer."""
from __future__ import annotations

import base64
from importlib.metadata import version
from urllib.parse import quote

import httpx

from .storage import safe_path


class BladeSkillRuntime:
    def __init__(self,settings,store):
        self.settings,self.store=settings,store
        self.client=None
        self.server_version=None

    async def open(self):
        from blade_agent_kit import BladeAgentClient
        cfg=self.settings.blade
        if not cfg.api_key:raise ValueError('Blade credentials are not configured')
        self.client=BladeAgentClient(cfg.base_url,token=cfg.api_key.get_secret_value(),timeout=cfg.request_timeout_seconds,
                                    reconnect_grace_seconds=cfg.reconnect_timeout_seconds)
        await self.client.__aenter__()
        try:
            async with httpx.AsyncClient(base_url=cfg.base_url,timeout=cfg.request_timeout_seconds) as http:
                response=await http.get('/api/version')
                if response.is_success:
                    value=response.json().get('version')
                    if isinstance(value,str):self.server_version=value
        except (httpx.HTTPError,ValueError,AttributeError):
            pass

    async def close(self):
        if self.client:await self.client.close()
        self.client=None

    async def create(self,run):
        # beta.4 rejects the legacy `initiator` field with extra_forbidden.
        # The SDK defaults to the supported initiator on the server side.
        session=await self.client.create_session(intent='',model=run['requested_model'],solution_id='general_chat')
        if not session.id:raise RuntimeError('Blade did not return a session ID')
        return {'session_id':session.id,'sdk_version':version('blade-agent-kit'),'server_version':session.raw.get('ba_version') or self.server_version,'session_model':session.raw.get('model'),'transport':'sdk'}

    async def upload_skill(self,run,session_id,entries):
        name='octagon/eval-'+run['id']
        files=[]
        for entry in entries:
            data=self.store.read_blob(entry.hash)
            files.append({'path':entry.path,'content':data.decode('utf-8') if entry.encoding else base64.b64encode(data).decode('ascii'),
                          'encoding':'utf-8' if entry.encoding else 'base64','executable':entry.mode==0o755})
        result=await self.client.upload_session_skill(session_id,name=name,files=files)
        if result.file_count!=len(entries):raise RuntimeError('Skill upload file count mismatch')
        # skill_dir in the receipt can be a HOST path, not a sandbox path.
        # Activate by skill reference; never inject that absolute path into chat.
        return {'name':name,'file_count':result.file_count,'overwritten':result.overwritten,'receipt':result.raw}

    async def upload_input(self,session_id,input_):
        receipt=await self.client.upload_file(session_id,self.store.blob_path(input_['hash']),remote_path=input_['remote_path'])
        return receipt.raw

    async def chat(self,run,session_id,message,*,skill_name=None,answer=None):
        opts={'model':run['requested_model'],'headless':False,'mode':'executing'}
        if skill_name:opts['refs']=[{'provider':'skill','id':skill_name,'label':skill_name}]
        if answer:opts['askuser_answer']=answer
        stream=self.client.chat(session_id,message,**opts)
        ended=False
        try:
            async for event in stream:
                if event.kind=='chat:end':ended=True
                yield {'kind':event.kind,'raw':event.raw}
        finally:
            await stream.close(send_stop=not ended)

    async def subscribe(self,session_id):
        stream=self.client.subscribe(session_id)
        try:
            async for event in stream:
                yield {'kind':event.kind,'raw':event.raw}
                if event.kind=='chat:end':break
        finally:
            await stream.close(send_stop=False)

    async def stop(self,session_id,chat_run_id):
        if not chat_run_id:raise ValueError('Stopping requires the actual accepted generation ID')
        return await self.client.stop(session_id,chat_run_id=chat_run_id)

    async def history(self,session_id):
        return (await self.client.get_history(session_id)).raw

    async def session(self,session_id):
        return (await self.client.get_session(session_id)).raw

    async def list_files(self,session_id,*,max_entries=1000):
        files=[];pending=['.'];seen=set()
        while pending:
            directory=pending.pop()
            if directory in seen:raise ValueError('Remote directory cycle')
            seen.add(directory)
            entries=await self.client.list_dir(session_id,directory)
            if len(files)+len(entries)+len(seen)>max_entries:raise ValueError('Remote entry count limit')
            for entry in entries:
                # Construct a relative path from the requested directory and a
                # checked basename; do not trust server-supplied absolute paths.
                name=safe_path(entry.name)
                if '/' in name:raise ValueError('Invalid remote basename')
                path=name if directory=='.' else safe_path(directory+'/'+name)
                if entry.raw.get('is_symlink') or entry.raw.get('type')=='symlink':
                    files.append({'path':path,'state':'skipped','reason':'symlink'})
                elif entry.is_dir:pending.append(path)
                else:files.append({'path':path,'size':entry.raw.get('size'),'state':'listed'})
        return files

    async def download(self,session_id,path,*,max_bytes):
        path=safe_path(path)
        cfg=self.settings.blade
        url=f'{cfg.base_url.rstrip("/")}/api/sessions/{quote(session_id,safe="")}/files/{quote(path,safe="")}'
        async with httpx.AsyncClient(timeout=cfg.request_timeout_seconds,headers={'Authorization':'Bearer '+cfg.api_key.get_secret_value()},follow_redirects=False) as client:
            async with client.stream('GET',url) as response:
                response.raise_for_status()
                if int(response.headers.get('content-length','0'))>max_bytes:raise ValueError('Artifact file size limit')
                parts=[];size=0
                async for chunk in response.aiter_bytes():
                    size+=len(chunk)
                    if size>max_bytes:raise ValueError('Artifact file size limit')
                    parts.append(chunk)
                return b''.join(parts)
