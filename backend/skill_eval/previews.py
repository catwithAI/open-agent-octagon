"""Reuse the existing artifact scanner and bounded Office renderer by blob ID."""
import mimetypes
from pathlib import Path

from backend.artifact_preview import (
    PREVIEW_CONTRACT_VERSION,
    SCANNER_NAME,
    SCANNER_VERSION,
    scheduled_preview_descriptor,
)
from .storage import safe_path


def _readonly_unavailable(filename,data,hash_,artifact_ref):
    media_type=mimetypes.guess_type(filename)[0] or 'application/octet-stream'
    return {
        'version':PREVIEW_CONTRACT_VERSION,
        'artifact':{'ref':artifact_ref,'name':filename,'size':len(data),'media_type':media_type,'type':'binary'},
        'status':'unsupported',
        'counts':{},
        'renderer':{'name':SCANNER_NAME,'version':SCANNER_VERSION},
        'error':{'code':'display_only_preview_unavailable','message':'Preview generation is disabled in display-only mode'},
        'cache_key':f'sha256:{hash_}',
        'poll_after_ms':None,
        'security':{},
        'capability_gaps':['display_only'],
        'content':None,
        'text':None,
    }


def descriptor(store,hash_,filename,artifact_ref,*,text_limit=2*1024*1024,read_only=False):
    filename=safe_path(Path(filename).name)
    data=store.read_blob(hash_)
    source=store.root/'previews'/hash_/filename
    if read_only:
        if not source.is_file() or source.is_symlink():
            return _readonly_unavailable(filename,data,hash_,artifact_ref)
        store._read_checked(source,hash_)
        result=scheduled_preview_descriptor(source,artifact_ref,None)
    else:
        # Preserve the checked suffix for the shared scanner. The client supplies
        # neither a host path nor a renderer command; bytes come from a scoped index.
        with store.guard():
            store._publish(source,data,hash_)
        result=scheduled_preview_descriptor(source,artifact_ref,source.parent/'rendered')
    result['text']=None
    if result['artifact']['type'] in ('text','html') and result['status']=='ready':
        if len(data)>text_limit:
            result['status']='unsupported'
            result['capability_gaps'].append('text_preview_limit')
        else:
            try:result['text']=data.decode('utf-8')
            except UnicodeDecodeError:
                result['status']='unsupported';result['capability_gaps'].append('invalid_utf8_text')
    return result
