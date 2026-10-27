"""Content-addressed storage with explicit, serialized orphan collection.

Only complete, fsynced objects are published. A failed DB commit can leave an
unreferenced object; keeping it is safer than racing a concurrent reference.
Historical assets are never deleted; only proven, aged orphans are eligible.
"""
from __future__ import annotations

import fcntl
import threading
from contextlib import contextmanager
from functools import wraps

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_GUARDS: dict[str, threading.RLock] = {}
_GUARDS_LOCK = threading.Lock()
_DEPTH = threading.local()


def guarded(method):
    @wraps(method)
    def invoke(self, *args, **kwargs):
        store = self.store if hasattr(self, 'store') else self
        with store.guard():
            return method(self, *args, **kwargs)
    return invoke


_DIGEST = re.compile(r'^[a-f0-9]{64}$')


def safe_path(value: str) -> str:
    path = unicodedata.normalize('NFC', value)
    parts = path.split('/')
    if (not path or len(path.encode('utf-8')) > 1024 or '\\' in path or ':' in path
            or any(part in {'', '.', '..'} for part in parts)
            or any(ord(c) < 32 or ord(c) == 127 for c in path)):
        raise ValueError(f'invalid relative path: {value!r}')
    return path


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')


@dataclass(frozen=True)
class Entry:
    file_id: str
    path: str
    hash: str
    size: int
    mode: int = 0o644
    encoding: str | None = None

    def __post_init__(self):
        if (safe_path(self.path) != self.path or not self.file_id or not _DIGEST.fullmatch(self.hash)
                or self.size < 0 or self.mode not in (0o644, 0o755) or self.encoding not in (None, 'utf-8')):
            raise ValueError('invalid manifest entry')


def validate_tree(entries: list[Entry]) -> None:
    paths = set()
    ids = set()
    for entry in entries:
        if entry.path in paths or entry.file_id in ids:
            raise ValueError(f'duplicate path or file identity: {entry.path}')
        paths.add(entry.path)
        ids.add(entry.file_id)
    for path in paths:
        parts = path.split('/')
        if any('/'.join(parts[:i]) in paths for i in range(1, len(parts))):
            raise ValueError(f'file/directory conflict: {path}')


def content_hash(entries: list[Entry]) -> str:
    validate_tree(entries)
    return digest(canonical([
        {k: v for k, v in asdict(entry).items() if k != 'file_id'}
        for entry in sorted(entries, key=lambda e: e.path)
    ]))


class Store:
    def __init__(self, data_path: Path):
        self.root = Path(data_path) / 'skill-eval'

    @contextmanager
    def guard(self):
        """Serialize storage publication/GC through the following DB commit.

        Reentrant across Store instances in the same thread. flock also protects
        independent workers/processes; every reference-writing service uses it.
        Always acquire this before a repository transaction, never the reverse.
        """
        identity = str(self.root.resolve())
        with _GUARDS_LOCK:
            lock = _GUARDS.setdefault(identity, threading.RLock())
        with lock:
            depths = getattr(_DEPTH, 'stores', None)
            if depths is None:
                depths = _DEPTH.stores = {}
            if identity in depths:
                depths[identity] += 1
                try:
                    yield
                finally:
                    depths[identity] -= 1
                return
            self.root.mkdir(parents=True, exist_ok=True)
            with (self.root / '.store.lock').open('a+b') as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                depths[identity] = 1
                try:
                    yield
                finally:
                    del depths[identity]
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    @guarded
    def collect_unreferenced(self, repository, *, older_than: float):
        """Explicit delayed orphan collection, preserving ALL historical refs.

        Pending, expired and committed imports retain their manifest. Staging
        retention/deletion is a separate policy. If any referenced manifest is
        missing/corrupt, abort before deleting anything (fail closed).
        """
        with repository.transaction() as conn:
            manifests = set()
            for table, column in [('versions','manifest_ref'),('drafts','manifest_hash'),('imports','staging_ref')]:
                manifests.update(row[0] for row in conn.execute(f'SELECT {column} FROM skill_eval_{table}'))
            blobs = set()
            for ref in manifests:
                blobs.update(e.hash for e in self.get_manifest(ref))
            for table in ('inputs','artifacts'):
                blobs.update(row[0] for row in conn.execute(f'SELECT hash FROM skill_eval_{table} WHERE hash IS NOT NULL'))
            for row in conn.execute('SELECT result_json FROM skill_eval_result_collections'):
                result = json.loads(row[0])
                blobs.update(item['hash'] for item in result.get('artifacts',[]) if item.get('hash'))
            for row in conn.execute('SELECT manifest_json FROM skill_eval_comparisons'):
                manifest = json.loads(row[0])
                for column in manifest.get('columns',[]):
                    blobs.update(item['hash'] for item in (column.get('result') or {}).get('artifacts',[]) if item.get('hash'))
            removed = {'blobs':0,'manifests':0,'temporary':0}
            candidates = []
            for path in (self.root / 'blobs' / 'sha256').glob('*/*'):
                if _DIGEST.fullmatch(path.name) and path.name not in blobs:
                    candidates.append((path,'blobs',path.name))
            for path in (self.root / 'manifests').glob('*.json'):
                if _DIGEST.fullmatch(path.stem) and path.stem not in manifests:
                    candidates.append((path,'manifests',path.stem))
            candidates.extend((p,'temporary',None) for p in self.root.rglob('*.tmp'))
            for path,kind,hash_ in candidates:
                if path.is_symlink() or not path.is_file() or path.stat().st_mtime >= older_than:
                    continue
                path.unlink()
                removed[kind] += 1
                if kind=='blobs':
                    conn.execute('DELETE FROM skill_eval_blobs WHERE hash=?',(hash_,))
            return removed

    def blob_path(self, hash_: str) -> Path:
        self._check_hash(hash_)
        return self.root / 'blobs' / 'sha256' / hash_[:2] / hash_

    @staticmethod
    def _check_hash(hash_: str) -> None:
        if not _DIGEST.fullmatch(hash_):
            raise ValueError('invalid content hash')

    @staticmethod
    def _read_checked(path: Path, expected: str) -> bytes:
        if path.is_symlink():
            raise ValueError('storage integrity: symlink')
        data = path.read_bytes()
        if digest(data) != expected:
            raise ValueError('storage integrity: hash mismatch')
        return data

    def _publish(self, path: Path, data: bytes, expected: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            self._read_checked(path, expected)
            return
        fd, temporary = tempfile.mkstemp(suffix='.tmp', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
                os.fchmod(stream.fileno(), 0o444)
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            Path(temporary).unlink(missing_ok=True)

    @guarded
    def put_blob(self, data: bytes) -> str:
        hash_ = digest(data)
        self._publish(self.blob_path(hash_), data, hash_)
        return hash_

    def read_blob(self, hash_: str) -> bytes:
        return self._read_checked(self.blob_path(hash_), hash_)

    @guarded
    def put_manifest(self, entries: list[Entry]) -> str:
        validate_tree(entries)
        for entry in entries:
            if len(self.read_blob(entry.hash)) != entry.size:
                raise ValueError('manifest size mismatch')
        data = canonical([asdict(e) for e in sorted(entries, key=lambda e: e.path)])
        ref = digest(data)
        self._publish(self.root / 'manifests' / f'{ref}.json', data, ref)
        return ref

    def get_manifest(self, ref: str) -> list[Entry]:
        self._check_hash(ref)
        data = self._read_checked(self.root / 'manifests' / f'{ref}.json', ref)
        entries = [Entry(**row) for row in json.loads(data)]
        validate_tree(entries)
        return entries
