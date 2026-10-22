"""Reuse the existing artifact scanner and bounded Office renderer by blob ID."""
from pathlib import Path

from backend.artifact_preview import scheduled_preview_descriptor
from .storage import safe_path


def descriptor(store,hash_,filename,artifact_ref,*,text_limit=2*1024*1024):
    filename=safe_path(Path(filename).name)
    data=store.read_blob(hash_)
    # Preserve the checked suffix for the shared scanner. The client supplies
    # neither a host path nor a renderer command; bytes come from a scoped index.
    with store.guard():
        source=store.root/'previews'/hash_/filename
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
