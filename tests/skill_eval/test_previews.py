from backend.skill_eval.previews import descriptor
from backend.skill_eval.storage import Store


def test_preview_uses_immutable_bytes_and_escapes_no_host_paths(tmp_path):
    store=Store(tmp_path/'objects')
    data=b'final report\n'
    hash_=store.put_blob(data)
    result=descriptor(store,hash_,'report.txt','run/report.txt')
    assert result['status']=='ready'
    assert result['text']=='final report\n'
    assert result['artifact']['ref']=='run/report.txt'
    assert store.read_blob(hash_)==data


def test_large_text_is_explicitly_unsupported_not_truncated(tmp_path):
    store=Store(tmp_path/'objects');hash_=store.put_blob(b'abcd')
    result=descriptor(store,hash_,'report.txt','report.txt',text_limit=3)
    assert result['status']=='unsupported'
    assert result['text'] is None
    assert 'text_preview_limit' in result['capability_gaps']
