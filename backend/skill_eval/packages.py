"""Validate complete Skill packages; never execute their scripts."""
from __future__ import annotations

import io
import json
import stat
import uuid
import zipfile
import zlib
from dataclasses import dataclass, field
from typing import Any

import yaml

from .repository import key
from .storage import Entry, Store, canonical, content_hash, digest, safe_path, validate_tree


@dataclass(frozen=True)
class Limits:
    # Configurable implementation defaults, not promises of remote capacity.
    max_upload: int = 20 * 1024 * 1024
    max_total: int = 100 * 1024 * 1024
    max_file: int = 20 * 1024 * 1024
    max_files: int = 1000
    max_ratio: int = 200

    def __post_init__(self):
        if any(value <= 0 for value in self.__dict__.values()):
            raise ValueError('limits must be positive')


@dataclass
class Package:
    name: str
    version: str | None
    entries: list[Entry]
    manifest_ref: str
    content_hash: str
    version_candidates: list[str] = field(default_factory=list)
    suggested_metadata: dict[str, Any] = field(default_factory=dict)


class _UniqueLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader, node):
    out = {}
    for k_node, v_node in node.value:
        k = loader.construct_object(k_node)
        if not isinstance(k, str) or k in out:
            raise ValueError('frontmatter keys must be unique strings')
        out[k] = loader.construct_object(v_node)
    return out


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def metadata(body: bytes) -> tuple[str, str | None, list[str]]:
    try:
        text = body.decode('utf-8')
    except UnicodeDecodeError as error:
        raise ValueError('SKILL.md must be UTF-8') from error
    lines = text.splitlines()
    if not lines or lines[0] != '---':
        raise ValueError('SKILL.md requires frontmatter')
    try:
        end = lines.index('---', 1)
    except ValueError as error:
        raise ValueError('SKILL.md frontmatter is not closed') from error
    header = '\n'.join(lines[1:end])
    if len(header.encode('utf-8')) > 64 * 1024:
        raise ValueError('frontmatter size limit')
    try:
        count = 0
        for event in yaml.parse(header):
            count += 1
            if count > 4096 or isinstance(event, yaml.events.AliasEvent):
                raise ValueError('frontmatter aliases or complexity limit')
        parsed = yaml.load(header, Loader=_UniqueLoader)
    except (yaml.YAMLError, TypeError, RecursionError) as error:
        raise ValueError('invalid SKILL.md frontmatter') from error
    if not isinstance(parsed, dict) or not isinstance(parsed.get('name'), str):
        raise ValueError('frontmatter requires a string name')
    name = key(parsed['name'])
    meta = parsed.get('metadata', {})
    if not isinstance(meta, dict):
        raise ValueError('metadata must be a mapping')
    versions = []
    for v in (meta.get('version'), parsed.get('version')):
        if v is not None:
            if isinstance(v, (bool, list, dict)):
                raise ValueError('version must be text or a number')
            v = key(str(v))
            if v not in versions:
                versions.append(v)
    return name, versions[0] if len(versions) == 1 else None, versions


def _check_size(value: int, maximum: int, label: str) -> None:
    if value > maximum:
        raise ValueError(f'{label} limit exceeded')


def parse_package(filename: str, data: bytes, store: Store, limits: Limits | None = None) -> Package:
    limits = limits or Limits()
    _check_size(len(data), limits.max_upload, 'upload')
    raw: list[tuple[str, bytes, int]] = []
    suggestion = {}
    if filename == 'SKILL.md':
        _check_size(len(data), limits.max_file, 'SKILL.md file')
        _check_size(len(data), limits.max_total, 'total')
        raw = [('SKILL.md', data, 0o644)]
    elif filename.lower().endswith('.zip'):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                infos = archive.infolist()
                _check_size(len(infos), limits.max_files, 'entry count')
                total = 0
                seen = set()
                directories = set()
                for info in infos:
                    path = safe_path(info.filename[:-1] if info.is_dir() else info.filename)
                    if path in seen:
                        raise ValueError(f'duplicate path: {path}')
                    seen.add(path)
                    mode = info.external_attr >> 16
                    kind = stat.S_IFMT(mode)
                    if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or (kind == stat.S_IFDIR) != info.is_dir() and kind != 0:
                        raise ValueError(f'unsupported file type: {path}')
                    if info.flag_bits & 1:
                        raise ValueError(f'encrypted entry: {path}')
                    _check_size(info.file_size, limits.max_file, path+' file')
                    total += info.file_size
                    _check_size(total, limits.max_total, 'total')
                    if info.file_size > max(info.compress_size, 1) * limits.max_ratio:
                        raise ValueError(f'compression ratio limit: {path}')
                    if info.is_dir():
                        directories.add(path)
                        continue
                    with archive.open(info) as stream:
                        value = stream.read(limits.max_file + 1)
                    _check_size(len(value), limits.max_file, path+' file')
                    if len(value) != info.file_size:
                        raise ValueError(f'entry size mismatch: {path}')
                    raw.append((path, value, 0o755 if mode & 0o111 else 0o644))
                file_paths = {p for p, _, _ in raw}
                if any('/'.join(d.split('/')[:i]) in file_paths for d in directories for i in range(1,len(d.split('/'))+1)):
                    raise ValueError('ZIP directory conflicts with file')
                if archive.comment:
                    try:
                        comment = json.loads(archive.comment)
                        suggestion = comment.get('octagon.skill', {}) if isinstance(comment, dict) else {}
                    except (ValueError, UnicodeDecodeError):
                        suggestion = {}
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError, EOFError, zlib.error) as error:
            raise ValueError('invalid ZIP package') from error
    else:
        raise ValueError('upload SKILL.md or a ZIP package')
    roots = [path.removesuffix('SKILL.md') for path, _, _ in raw if path.split('/')[-1] == 'SKILL.md']
    if len(roots) != 1:
        raise ValueError('package must contain one unambiguous SKILL.md root')
    root = roots[0]
    # One optional outer directory is allowed. Files alongside it are ambiguous.
    if root and (root.count('/') != 1 or any(not path.startswith(root) for path, _, _ in raw)):
        raise ValueError('package has multiple or ambiguous roots')
    raw = [(path[len(root):], value, mode) for path, value, mode in raw]
    body = next(value for path, value, _ in raw if path == 'SKILL.md')
    name, version, candidates = metadata(body)
    entries = []
    # Validate all entry paths before publishing any bytes.
    for path, value, mode in raw:
        path = safe_path(path)
        try:
            value.decode('utf-8')
            encoding = 'utf-8' if b'\0' not in value else None
        except UnicodeDecodeError:
            encoding = None
        entries.append(Entry(uuid.uuid4().hex, path, digest(value), len(value), mode, encoding))
    validate_tree(entries)
    for _, value, _ in raw:
        store.put_blob(value)
    hash_ = content_hash(entries)
    # ZIP comments are untrusted hints and are only used with a matching fingerprint.
    if not isinstance(suggestion, dict) or suggestion.get('content_hash') != hash_:
        suggestion = {}
    if suggestion.get('name') is not None:key(suggestion['name'])
    if suggestion.get('version') is not None and not isinstance(suggestion['version'],str):raise ValueError('export version hint must be a string')
    return Package(name, version, entries, store.put_manifest(entries), hash_, candidates, suggestion)


def validate_skill(entries: list[Entry], store: Store) -> None:
    validate_tree(entries)
    body = next((e for e in entries if e.path == 'SKILL.md'), None)
    if body is None:
        raise ValueError('SKILL.md is required')
    metadata(store.read_blob(body.hash))


def export_package(entries: list[Entry], store: Store, platform_metadata: dict[str, Any]) -> bytes:
    validate_skill(entries, store)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as archive:
        for entry in sorted(entries, key=lambda e: e.path):
            info = zipfile.ZipInfo(entry.path)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | entry.mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, store.read_blob(entry.hash))
        archive.comment = canonical({'octagon.skill': {**platform_metadata, 'content_hash': content_hash(entries)}})
    return buf.getvalue()
