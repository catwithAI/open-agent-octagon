"""Atomic package/version operations. No agent or remote effects in this layer."""
from __future__ import annotations

import base64
import binascii
import json
import sqlite3
import uuid
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .packages import Limits, export_package, parse_package, validate_skill
from .repository import Repository, key
from .storage import Entry, Store, content_hash, safe_path, guarded


class EvalError(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def new_id() -> str:
    return uuid.uuid4().hex


def now() -> datetime:
    return datetime.now(timezone.utc)


class SkillService:
    def __init__(self, db_path: Path, data_path: Path, *, scope: str = 'deployment', limits: Limits | None = None, clock: Callable[[], datetime] = now, input_max_bytes: int = 100 * 1024 * 1024, text_edit_max_bytes: int = 1024 * 1024):
        self.repo, self.store = Repository(db_path), Store(data_path)
        self.scope, self.limits, self.clock = scope, limits or Limits(), clock
        self.input_max_bytes = input_max_bytes
        self.text_edit_max_bytes = text_edit_max_bytes

    def _time(self) -> str:
        return self.clock().isoformat()

    @staticmethod
    def _row(c, sql, params=()):
        row = c.execute(sql, params).fetchone()
        if row is None:
            raise EvalError('not_found', 'Resource not found', 404)
        return dict(row)

    def _skill(self, c, id_):
        return self._row(c, 'SELECT * FROM skill_eval_skills WHERE id=? AND scope_id=?', (id_, self.scope))

    def _release(self, c, id_):
        return self._row(c, 'SELECT r.* FROM skill_eval_releases r JOIN skill_eval_skills s ON s.id=r.skill_id WHERE r.id=? AND s.scope_id=?', (id_, self.scope))

    def _draft(self, c, id_):
        return self._row(c, 'SELECT d.* FROM skill_eval_drafts d JOIN skill_eval_skills s ON s.id=d.skill_id WHERE d.id=? AND s.scope_id=?', (id_, self.scope))

    def _version(self, c, id_):
        return self._row(c, 'SELECT v.* FROM skill_eval_versions v JOIN skill_eval_skills s ON s.id=v.skill_id WHERE v.id=? AND s.scope_id=?', (id_, self.scope))

    @staticmethod
    def _expect(actual, expected, field='revision'):
        if actual != expected:
            raise EvalError('revision_conflict', f'{field} changed; reload before retrying')

    def list_skills(self):
        with self.repo.connection() as c:
            return [dict(r) for r in c.execute('SELECT * FROM skill_eval_skills WHERE scope_id=? ORDER BY created_at,id', (self.scope,))]

    def get_skill(self, id_):
        with self.repo.connection() as c:
            skill = self._skill(c, id_)
            skill['releases'] = [dict(r) for r in c.execute('SELECT * FROM skill_eval_releases WHERE skill_id=? ORDER BY created_at,id', (id_,))]
            return skill

    def get_release(self, id_):
        with self.repo.connection() as c:
            return self._release(c, id_)

    @guarded
    def import_package(self, filename: str, data: bytes, *, source_ref: str | None = None):
        package = parse_package(filename, data, self.store, self.limits)
        id_ = new_id()
        info = {'version_candidates': package.version_candidates, 'suggested_metadata': package.suggested_metadata, 'source_ref': source_ref}
        with self.repo.transaction() as c:
            self._register_blobs(c, package.entries)
            c.execute('INSERT INTO skill_eval_imports(id,scope_id,staging_ref,detected_name,detected_version,metadata_json,expires_at,created_at) VALUES(?,?,?,?,?,?,?,?)',
                      (id_, self.scope, package.manifest_ref, key(package.suggested_metadata.get('name') or package.name), key(str(package.suggested_metadata['version'])) if package.suggested_metadata.get('version') else package.version, json.dumps(info), (self.clock()+timedelta(days=1)).isoformat(), self._time()))
        return self.get_import(id_)

    def get_import(self, id_):
        with self.repo.connection() as c:
            result = self._row(c, 'SELECT * FROM skill_eval_imports WHERE id=? AND scope_id=?', (id_, self.scope))
        result['metadata'] = json.loads(result.pop('metadata_json'))
        result['entries'] = [asdict(e) for e in self.store.get_manifest(result['staging_ref'])]
        return result

    def _register_blobs(self, c, entries):
        for e in entries:
            c.execute('INSERT OR IGNORE INTO skill_eval_blobs(hash,size,storage_ref,created_at) VALUES(?,?,?,?)', (e.hash, e.size, e.hash, self._time()))

    def _snapshot(self, c, skill_id, entries):
        self._register_blobs(c, entries)
        hash_ = content_hash(entries)
        row = c.execute('SELECT * FROM skill_eval_versions WHERE skill_id=? AND content_hash=?', (skill_id, hash_)).fetchone()
        if row:
            return dict(row)
        id_ = new_id()
        ref = self.store.put_manifest(entries)
        c.execute('INSERT INTO skill_eval_versions(id,skill_id,content_hash,manifest_ref,created_at) VALUES(?,?,?,?,?)', (id_, skill_id, hash_, ref, self._time()))
        return self._version(c, id_)

    def _new_release(self, c, skill_id, label, snapshot, status='unpublished', source=None):
        label = key(label)
        existing = c.execute('SELECT 1 FROM skill_eval_releases WHERE skill_id=? AND version_key=?', (skill_id, label)).fetchone()
        if existing:
            raise EvalError('version_conflict', 'Version already exists; rename the skill or choose an unused version')
        id_, timestamp = new_id(), self._time()
        c.execute('INSERT INTO skill_eval_releases(id,skill_id,version_label,version_key,status,current_version_id,source_release_id,published_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                  (id_, skill_id, label, label, status, snapshot['id'], source, timestamp if status=='published' else None, timestamp, timestamp))
        return self._release(c, id_)

    def _new_draft(self, c, release):
        snapshot = self._version(c, release['current_version_id'])
        id_, timestamp = new_id(), self._time()
        c.execute('INSERT INTO skill_eval_drafts(id,skill_id,base_version_id,source_release_id,target_release_id,manifest_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                  (id_, release['skill_id'], snapshot['id'], release['id'], release['id'] if release['status']=='unpublished' else None, snapshot['manifest_ref'], timestamp, timestamp))
        return self._draft_result(self._draft(c, id_),c)

    @guarded
    def commit_import(self, id_: str, name: str, version: str | None):
        with self.repo.transaction() as c:
            pending = self._row(c, 'SELECT * FROM skill_eval_imports WHERE id=? AND scope_id=?', (id_, self.scope))
            if pending['state']=='committed' and pending['committed_result_json']:
                return json.loads(pending['committed_result_json'])
            if pending['state']=='committed':
                release = self._release(c, pending['committed_release_id'])
                snapshot = self._version(c, release['current_version_id'])
                # Existing import returns its original draft, not an additional copy.
                draft = self._row(c, 'SELECT * FROM skill_eval_drafts WHERE source_release_id=? ORDER BY created_at,id LIMIT 1', (release['id'],))
                return {'import_id': id_, 'skill': self._skill(c, release['skill_id']), 'release': release, 'snapshot': snapshot, 'draft': self._draft_result(draft,c)}
            if pending['state']=='expired' or pending['expires_at'] <= self._time():
                raise EvalError('import_expired', 'Upload staging expired; upload again', 410)
            normalized = key(name)
            if version is None:
                version = pending['detected_version']
            row = c.execute('SELECT * FROM skill_eval_skills WHERE scope_id=? AND name_key=?', (self.scope, normalized)).fetchone()
            if row:
                skill = dict(row)
                if not version or not version.strip():
                    raise EvalError('version_required', 'Existing skill requires an unused version or a new name')
            else:
                id_skill, timestamp = new_id(), self._time()
                info = json.loads(pending['metadata_json'])
                c.execute('INSERT INTO skill_eval_skills(id,scope_id,name,name_key,source_kind,source_ref,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                          (id_skill, self.scope, normalized, normalized, 'catalog' if info.get('source_ref') else 'upload', info.get('source_ref'), timestamp, timestamp))
                skill = self._skill(c, id_skill)
            if not version and len(json.loads(pending['metadata_json'])['version_candidates']) > 1:
                raise EvalError('version_ambiguous', 'Package versions disagree; explicitly choose a version')
            entries = self.store.get_manifest(pending['staging_ref'])
            snapshot = self._snapshot(c, skill['id'], entries)
            release = self._new_release(c, skill['id'], version or '0.0.1', snapshot)
            draft = self._new_draft(c, release)
            result = {'import_id': id_, 'skill': skill, 'release': release, 'snapshot': snapshot, 'draft': draft}
            c.execute("UPDATE skill_eval_imports SET state='committed',committed_release_id=?,committed_result_json=? WHERE id=?", (release['id'], json.dumps(result), id_))
            return result

    def _draft_result(self, draft, c=None):
        if c is None:
            with self.repo.connection() as connection:
                return self._draft_result(draft,connection)
        entries=self.store.get_manifest(draft['manifest_hash'])
        base=self._version(c,draft['base_version_id']) if draft['base_version_id'] else None
        saved=False
        if draft['target_release_id']:
            release=self._release(c,draft['target_release_id'])
            saved=self._version(c,release['current_version_id'])['content_hash']==content_hash(entries)
        return {**draft,'entries':[asdict(e) for e in entries],
                'base_entries':[asdict(e) for e in self.store.get_manifest(base['manifest_ref'])] if base else [],
                'version_is_saved':saved}


    def get_draft(self, id_):
        with self.repo.connection() as c:
            return self._draft_result(self._draft(c, id_),c)

    @guarded
    def create_draft(self, release_id):
        with self.repo.transaction() as c:
            return self._new_draft(c, self._release(c, release_id))

    def read_draft_file(self, id_, path):
        path = safe_path(path)
        draft = self.get_draft(id_)
        row = next((row for row in draft['entries'] if row['path']==path), None)
        if row is None:
            raise EvalError('not_found', 'File not found', 404)
        if row['encoding'] and row['size']>self.text_edit_max_bytes:return {**row,'editable':False,'text_omitted_reason':'editor_limit'}
        data = self.store.read_blob(row['hash'])
        return {**row, **({'text': data.decode('utf-8')} if row['encoding'] else {'content_base64': base64.b64encode(data).decode('ascii')})}

    def _edited(self, draft, changes):
        entries = self.store.get_manifest(draft['manifest_hash'])
        original = {e.path:e for e in entries}
        result = {e.file_id:e for e in entries}
        touched = set()
        for change in changes:
            op, path = change.get('op'), safe_path(change.get('path', ''))
            file_id=change.get('file_id')
            if file_id:
                entry=result.get(file_id)
                if entry is None:raise ValueError('file_id does not belong to this draft')
            else:entry=original.get(path)
            identity=(entry.file_id if entry else path,op)
            if identity in touched:
                raise ValueError(f'duplicate operation on file: {path}')
            touched.add(identity)
            if op in ('delete', 'rename', 'mode'):
                if entry is None:
                    raise ValueError(f'file not found: {path}')
                if op=='delete':
                    del result[entry.file_id]
                elif op=='rename':
                    result[entry.file_id] = replace(entry, path=safe_path(change['to']))
                else:
                    result[entry.file_id] = replace(entry, mode=change['mode'])
            elif op=='write':
                if ('text' in change)==('content_base64' in change):
                    raise ValueError('provide text or content_base64, exclusively')
                if 'text' in change:
                    if entry and entry.encoding is None:
                        raise ValueError('binary file requires explicit base64 replacement')
                    if not isinstance(change['text'], str):
                        raise ValueError('text must be a string')
                    data, encoding = change['text'].encode('utf-8'), 'utf-8'
                    if len(data)>self.text_edit_max_bytes:raise EvalError('editor_limit','Text edit limit exceeded; upload a replacement file instead',413)
                else:
                    try:
                        data = base64.b64decode(change['content_base64'], validate=True)
                    except (binascii.Error, TypeError, ValueError) as error:
                        raise ValueError('invalid base64') from error
                    try:
                        data.decode('utf-8');encoding='utf-8' if b'\0' not in data else None
                    except UnicodeDecodeError:encoding=None
                if len(data)>self.limits.max_file:
                    raise ValueError(f'{path} file size limit')
                hash_ = self.store.put_blob(data)
                file_id = entry.file_id if entry else new_id()
                result[file_id] = Entry(file_id, path, hash_, len(data), change.get('mode', entry.mode if entry else 0o644), encoding)
            else:
                raise ValueError('unknown changeset operation')
        edited = sorted(result.values(), key=lambda e:e.path)
        if len(edited)>self.limits.max_files or sum(e.size for e in edited)>self.limits.max_total:
            raise ValueError('package size/count limit')
        validate_skill(edited, self.store)
        return edited

    @guarded
    def patch_draft(self, id_, expected_revision, changes):
        with self.repo.transaction() as c:
            draft = self._draft(c, id_)
            self._expect(draft['revision'], expected_revision)
            entries = self._edited(draft, changes)
            self._register_blobs(c, entries)
            ref = self.store.put_manifest(entries)
            c.execute('UPDATE skill_eval_drafts SET manifest_hash=?,revision=revision+1,updated_at=? WHERE id=?', (ref, self._time(), id_))
            return self._draft_result(self._draft(c, id_),c)

    @guarded
    def snapshot_draft(self, id_, expected_revision, changes=None):
        with self.repo.transaction() as c:
            draft = self._draft(c, id_)
            self._expect(draft['revision'], expected_revision)
            return self._snapshot(c, draft['skill_id'], self._edited(draft, changes or []))

    @guarded
    def save_draft(self, id_, expected_revision, expected_release_revision, version=None, changes=None):
        with self.repo.transaction() as c:
            draft = self._draft(c, id_)
            self._expect(draft['revision'], expected_revision)
            entries = self._edited(draft, changes or [])
            snapshot = self._snapshot(c, draft['skill_id'], entries)
            if draft['target_release_id']:
                release = self._release(c, draft['target_release_id'])
                if release['status']!='unpublished':
                    raise EvalError('published_immutable', 'Published release cannot be overwritten')
                if version is not None and key(version)!=release['version_key']:
                    raise EvalError('version_fixed', 'Save keeps the existing unpublished version')
                self._expect(release['revision'], expected_release_revision, 'release revision')
                c.execute('UPDATE skill_eval_releases SET current_version_id=?,revision=revision+1,updated_at=? WHERE id=?', (snapshot['id'], self._time(), release['id']))
                release = self._release(c, release['id'])
            else:
                if not version:
                    raise EvalError('new_version_required', 'First save from a published source requires an unused version')
                release = self._new_release(c, draft['skill_id'], version, snapshot, source=draft['source_release_id'])
            c.execute('UPDATE skill_eval_drafts SET target_release_id=?,base_version_id=?,manifest_hash=?,revision=revision+1,updated_at=? WHERE id=?',
                      (release['id'], draft['base_version_id'] if draft['source_run_id'] else snapshot['id'], self.store.put_manifest(entries), self._time(), id_))
            return {'release':release, 'snapshot':snapshot, 'draft':self._draft_result(self._draft(c,id_),c)}

    @guarded
    def publish_draft(self, id_, expected_revision, expected_release_revision, version, changes=None):
        with self.repo.transaction() as c:
            draft = self._draft(c, id_)
            self._expect(draft['revision'], expected_revision)
            source_id=draft['target_release_id'] or draft['source_release_id']
            source=self._release(c,source_id) if source_id else None
            if source:self._expect(source['revision'], expected_release_revision, 'source release revision')
            snapshot = self._snapshot(c, draft['skill_id'], self._edited(draft, changes or []))
            release = self._new_release(c, draft['skill_id'], version, snapshot, 'published', source['id'] if source else None)
            return {'release':release,'snapshot':snapshot}

    def export_snapshot(self, id_, *, platform_metadata=None):
        with self.repo.connection() as c:
            snapshot = self._version(c, id_)
            skill = self._skill(c, snapshot['skill_id'])
        metadata={'name':skill['name'],'snapshot_id':id_}
        if platform_metadata is not None:metadata.update(platform_metadata)
        return export_package(self.store.get_manifest(snapshot['manifest_ref']),self.store,metadata)

    def export_release(self,id_):
        with self.repo.connection() as c:
            release=self._release(c,id_)
        return self.export_snapshot(release['current_version_id'],platform_metadata={
            'version':release['version_label'],'status':release['status'],
            'release_revision':release['revision'],'modified':False})

    @guarded
    def export_draft(self,id_,expected_revision,changes=None):
        snapshot=self.snapshot_draft(id_,expected_revision,changes)
        draft=self.get_draft(id_)
        release_id=draft['target_release_id'] or draft['source_release_id']
        metadata={'modified':True}
        if release_id:
            release=self.get_release(release_id)
            modified=release['current_version_id']!=snapshot['id']
            metadata.update(version=release['version_label'],source_status=release['status'],status='working_copy' if modified else release['status'],
                            source_release_id=release_id,source_release_revision=release['revision'],modified=modified)
        return self.export_snapshot(snapshot['id'],platform_metadata=metadata)


    def read_snapshot_file(self,id_,path):
        path=safe_path(path)
        with self.repo.connection() as c:snapshot=self._version(c,id_)
        entry=next((e for e in self.store.get_manifest(snapshot['manifest_ref']) if e.path==path),None)
        if not entry:raise EvalError('not_found','Snapshot file not found',404)
        if entry.encoding and entry.size>self.text_edit_max_bytes:return {**asdict(entry),'editable':False,'text_omitted_reason':'editor_limit'}
        data=self.store.read_blob(entry.hash)
        return {**asdict(entry),**({'text':data.decode('utf-8')} if entry.encoding else {'content_base64':base64.b64encode(data).decode()})}

    @guarded
    def upload_input(self, filename, data, media_type='application/octet-stream'):
        filename=safe_path(filename)
        if '/' in filename:
            raise ValueError('input filename must be a basename')
        if len(data)>self.input_max_bytes:
            raise ValueError('input size limit')
        id_,hash_=new_id(),self.store.put_blob(data)
        with self.repo.transaction() as c:
            c.execute('INSERT OR IGNORE INTO skill_eval_blobs(hash,size,storage_ref,created_at) VALUES(?,?,?,?)',(hash_,len(data),hash_,self._time()))
            c.execute('INSERT INTO skill_eval_inputs(id,scope_id,hash,original_name,media_type,size,created_at) VALUES(?,?,?,?,?,?,?)',(id_,self.scope,hash_,filename,media_type,len(data),self._time()))
        return self.get_input(id_)

    def get_input(self, id_):
        with self.repo.connection() as c:
            row=self._row(c,"SELECT * FROM skill_eval_inputs WHERE id=? AND scope_id=? AND state='complete'",(id_,self.scope))
        row['remote_path']=f"inputs/{id_}/{row['original_name']}"
        return row

    def read_input(self, id_):
        return self.store.read_blob(self.get_input(id_)['hash'])

    def create_experiment(self, skill_id, name):
        with self.repo.transaction() as c:
            self._skill(c, skill_id)
            id_, timestamp = new_id(), self._time()
            c.execute('INSERT INTO skill_eval_experiments(id,skill_id,name,created_at,updated_at) VALUES(?,?,?,?,?)', (id_,skill_id,key(name),timestamp,timestamp))
            return dict(c.execute('SELECT * FROM skill_eval_experiments WHERE id=?',(id_,)).fetchone())

    def get_experiment(self, id_):
        with self.repo.connection() as c:
            return self._row(c,'SELECT e.* FROM skill_eval_experiments e JOIN skill_eval_skills s ON s.id=e.skill_id WHERE e.id=? AND s.scope_id=?',(id_,self.scope))

    def list_experiments(self, skill_id):
        with self.repo.connection() as c:
            self._skill(c,skill_id)
            return [dict(r) for r in c.execute('SELECT * FROM skill_eval_experiments WHERE skill_id=? ORDER BY created_at,id',(skill_id,))]

    def rename_experiment(self, id_, name):
        with self.repo.transaction() as c:
            owned=self._row(c,'SELECT e.* FROM skill_eval_experiments e JOIN skill_eval_skills s ON s.id=e.skill_id WHERE e.id=? AND s.scope_id=?',(id_,self.scope))
            c.execute('UPDATE skill_eval_experiments SET name=?,updated_at=? WHERE id=?',(key(name),self._time(),owned['id']))
            return dict(c.execute('SELECT * FROM skill_eval_experiments WHERE id=?',(id_,)).fetchone())
