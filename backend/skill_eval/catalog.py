"""Read-only copies from server-configured roots; opaque IDs never expose paths."""
from __future__ import annotations

import io
import os
import stat
import uuid
import zipfile
from pathlib import Path

from .packages import Limits, metadata
from .service import EvalError
from .storage import Entry, content_hash, digest, safe_path


class SkillCatalog:
    def __init__(self, roots: list[Path] | None = None, limits: Limits | None = None):
        self.roots = [Path(p).resolve() for p in roots or []]
        self.limits = limits or Limits()

    def _packages(self):
        for index, root in enumerate(self.roots):
            if not root.is_dir():
                continue
            count = 0
            for dirpath, dirs, files in os.walk(root, followlinks=False):
                dirs[:] = sorted(d for d in dirs if not (Path(dirpath)/d).is_symlink())
                count += len(dirs) + len(files)
                if count > self.limits.max_files * 10:
                    raise EvalError('catalog_limit','Catalog directory scan limit exceeded',413)
                if 'SKILL.md' in files:
                    directory = Path(dirpath)
                    yield digest(f'{index}:{directory.relative_to(root).as_posix()}'.encode()), directory
                    # Nested skill roots belong to this package and will be rejected as ambiguous.
                    dirs[:] = []

    def _read(self, directory):
        entries, raw, total = [], [], 0
        for dirpath, dirs, files in os.walk(directory, followlinks=False):
            if any((Path(dirpath)/d).is_symlink() for d in dirs):
                raise ValueError('catalog symlink directory is unsupported')
            for name in sorted(files):
                path = Path(dirpath)/name
                mode = path.lstat().st_mode
                if not stat.S_ISREG(mode):
                    raise ValueError('catalog entry must be a regular file')
                if path.stat().st_size > self.limits.max_file:
                    raise ValueError('catalog file size limit')
                data = path.read_bytes()
                total += len(data)
                if total > self.limits.max_total or len(raw) >= self.limits.max_files:
                    raise ValueError('catalog package limit')
                relative = safe_path(path.relative_to(directory).as_posix())
                normalized_mode = 0o755 if mode & 0o111 else 0o644
                try:
                    data.decode('utf-8'); encoding = 'utf-8' if b'\0' not in data else None
                except UnicodeDecodeError:
                    encoding = None
                entries.append(Entry(uuid.uuid4().hex,relative,digest(data),len(data),normalized_mode,encoding))
                raw.append((relative,data,normalized_mode))
        name, version, candidates = metadata(next(data for path,data,_ in raw if path=='SKILL.md'))
        return name, version, content_hash(entries), raw

    def list(self):
        if not self.roots:
            return {'status':'unconfigured','items':[]}
        items = []
        for id_, directory in self._packages():
            try:
                name,version,fingerprint,raw=self._read(directory)
                items.append({'id':id_,'name':name,'version':version,'fingerprint':fingerprint,'file_count':len(raw)})
            except (ValueError, OSError, StopIteration):
                items.append({'id':id_,'status':'invalid','message':'Package is invalid or cannot be read'})
        return {'status':'configured','items':items}

    def copy(self, id_, expected_fingerprint):
        directory=next((directory for candidate,directory in self._packages() if candidate==id_),None)
        if directory is None:
            raise EvalError('not_found','Catalog item not found',404)
        try:
            _,_,fingerprint,raw=self._read(directory)
        except (ValueError, OSError, StopIteration) as error:
            raise EvalError('catalog_unreadable','Catalog package cannot be copied',422) from error
        if fingerprint != expected_fingerprint:
            raise EvalError('catalog_changed','Catalog changed; refresh before copying')
        output=io.BytesIO()
        with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
            for path,data,mode in raw:
                info=zipfile.ZipInfo(path);info.create_system=3;info.compress_type=zipfile.ZIP_DEFLATED
                info.external_attr=(stat.S_IFREG|mode)<<16
                archive.writestr(info,data)
        return output.getvalue()
