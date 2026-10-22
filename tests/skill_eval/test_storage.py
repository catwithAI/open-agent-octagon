import dataclasses

import pytest

from backend.skill_eval.storage import Entry, Store, content_hash, safe_path


@pytest.mark.parametrize('path', ['../a','a/../b','/a','C:/a','a\\b','a//b','a/./b','a/','\x00x'])
def test_rejects_ambiguous_or_escaping_paths(path):
    with pytest.raises(ValueError):
        safe_path(path)


def test_blob_manifest_hash_and_immutable_bytes(tmp_path):
    store = Store(tmp_path)
    blob = store.put_blob(b'old')
    assert store.put_blob(b'old') == blob
    old = Entry('a', 'SKILL.md', blob, 3, 0o644, 'utf-8')
    renamed_id = dataclasses.replace(old, file_id='b')
    assert content_hash([old]) == content_hash([renamed_id])
    assert content_hash([old]) != content_hash([dataclasses.replace(old, mode=0o755)])
    first = store.put_manifest([old])
    second = store.put_manifest([renamed_id])
    assert first != second
    assert store.get_manifest(first) == [old]
    store.put_blob(b'new')
    assert store.read_blob(blob) == b'old'
    with pytest.raises(ValueError):
        store.read_blob('../../a')


def test_interrupted_publication_does_not_publish_partial_object(tmp_path, monkeypatch):
    import os
    store = Store(tmp_path)
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(OSError):
        store.put_blob(b'test')
    assert not list(store.root.rglob('*.tmp'))
    assert not [p for p in store.root.rglob('*') if p.is_file() and p.name != '.store.lock']


def test_corrupt_blob_or_manifest_is_not_silently_trusted(tmp_path):
    store = Store(tmp_path)
    blob = store.put_blob(b'abc')
    path = store.blob_path(blob)
    path.chmod(0o644)
    path.write_bytes(b'xyz')
    with pytest.raises(ValueError, match='integrity'):
        store.read_blob(blob)
    with pytest.raises(ValueError, match='integrity'):
        store.put_blob(b'abc')


def test_gc_retains_all_references_and_cannot_race_commit(tmp_path):
    import concurrent.futures
    import threading
    from backend.skill_eval.service import SkillService
    from .test_imports import imported
    service=SkillService(tmp_path/'db',tmp_path);service.repo.initialize()
    orphan=service.store.put_blob(b'orphan')
    entered=threading.Event();release=threading.Event();gc_started=threading.Event()
    def writer():
        with service.store.guard():
            item=imported(service)
            entered.set()
            assert release.wait(10)
            return item
    def collector():
        gc_started.set()
        return Store(tmp_path).collect_unreferenced(service.repo,older_than=float('inf'))
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        writing=pool.submit(writer)
        assert entered.wait(10)
        collecting=pool.submit(collector)
        assert gc_started.wait(10)
        try:
            assert not collecting.done()
        finally:
            release.set()
        item=writing.result(10);removed=collecting.result(10)
    assert removed['blobs']==1
    assert not service.store.blob_path(orphan).exists()
    assert service.export_snapshot(item['snapshot']['id']).startswith(b'PK')


def test_gc_aborts_if_any_reference_is_corrupt(tmp_path):
    from backend.skill_eval.service import SkillService
    from .test_imports import imported
    service=SkillService(tmp_path/'db',tmp_path);service.repo.initialize()
    item=imported(service)
    orphan=service.store.put_blob(b'keep if uncertain')
    manifest=service.store.root/'manifests'/f"{item['snapshot']['manifest_ref']}.json"
    manifest.chmod(0o644);manifest.write_bytes(b'corrupt')
    with pytest.raises(ValueError,match='integrity'):
        service.store.collect_unreferenced(service.repo,older_than=float('inf'))
    assert service.store.read_blob(orphan)==b'keep if uncertain'
