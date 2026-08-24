"""Immutable, content-addressed inputs for deferred scoring jobs."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .artifact_scope import ARTIFACT_SKIP_DIRS
from .experiments.hashing import canonical_hash

SNAPSHOT_SCHEMA_VERSION = "octagon-scoring-snapshot-v1"


class ScoringInputMismatchError(RuntimeError):
    """The registered scoring input no longer matches its immutable snapshot."""


@dataclass(frozen=True)
class ScoringSnapshot:
    input_hash: str
    relative_path: str


def _ignore_non_deliverables(directory: str, names: list[str]) -> set[str]:
    """`copytree` ignore hook: never copy dependency/build trees into a snapshot.

    Filtering after the copy would be too late — the cost being avoided is the
    copy itself. One measured attempt carried 190MB of `node_modules` whose 61
    actual deliverables totalled 384KB, and that tree was written twice (once
    here, once by `materialize_scoring_input`).

    Scoring reads the frozen copy, so anything dropped here is invisible to the
    judge. That is the intent: judging is about what the agent delivered, and a
    reinstallable dependency tree is not part of it.
    """
    return {name for name in names if name in ARTIFACT_SKIP_DIRS}


def _tree_manifest(root: Path) -> list[dict[str, Any]]:
    """Return a deterministic manifest without following symlinks."""
    if not root.is_dir():
        raise FileNotFoundError(f"scoring input directory missing: {root}")
    resolved_root = root.resolve()
    entries: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            entries.append({"path": relative, "type": "dir"})
        elif stat.S_ISREG(info.st_mode):
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            entries.append(
                {
                    "path": relative,
                    "type": "file",
                    "size": info.st_size,
                    "hash": f"sha256:{digest.hexdigest()}",
                }
            )
        elif stat.S_ISLNK(info.st_mode):
            target = os.readlink(path)
            try:
                (path.parent / target).resolve(strict=False).relative_to(resolved_root)
            except ValueError:
                # 指向 attempt 之外的链接**不是攻击信号**：agent 建 venv
                # （`backend/venv/bin/python` → 系统 Python）、node_modules 的
                # bin 链接都会产生它们。实测 2026-07-28 codex 因此整个 attempt
                # 崩成 dispatch_crashed——一个正常的开发动作让评分链路失败，
                # 代价远大于收益。
                #
                # 快照的目的是冻结「判分依据的内容」。外指链接的目标本就不在
                # attempt 内、不属于产物，跳过它即可；标记 external 让判分侧
                # 知道这里有个不可用的链接，而不是静默当作不存在。
                entries.append(
                    {
                        "path": relative,
                        "type": "symlink",
                        "target": target,
                        "external": True,
                    }
                )
                continue
            entries.append(
                {
                    "path": relative,
                    "type": "symlink",
                    "target": target,
                }
            )
        else:
            raise ValueError(f"unsupported scoring input file type: {path}")
    return entries


def _snapshot_hash(attempt_root: Path) -> tuple[str, list[dict[str, Any]]]:
    manifest = _tree_manifest(attempt_root)
    return (
        canonical_hash(
            {
                "schema_version": SNAPSHOT_SCHEMA_VERSION,
                "entries": manifest,
            }
        ),
        manifest,
    )


def _make_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_symlink():
            continue
        path.chmod(0o555 if path.is_dir() else 0o444)
    root.chmod(0o555)


def _remove_tree(root: Path) -> None:
    """Remove a private temporary/work tree even when it was made read-only."""
    if not root.exists():
        return
    for path in [root, *root.rglob("*")]:
        if not path.is_symlink():
            try:
                path.chmod(0o755 if path.is_dir() else 0o644)
            except OSError:
                pass
    shutil.rmtree(root)


def _stabilize_env_db(source_attempt: Path, copied_attempt: Path) -> None:
    """Replace a file-level SQLite copy with a transactionally consistent backup."""
    source_db = source_attempt / "env.db"
    if not source_db.is_file() or source_db.is_symlink():
        return
    copied_db = copied_attempt / "env.db"
    copied_db.unlink(missing_ok=True)
    source_uri = source_db.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source_conn:
        with sqlite3.connect(copied_db) as copied_conn:
            source_conn.backup(copied_conn)
    for suffix in ("-wal", "-shm", "-journal"):
        (copied_attempt / f"env.db{suffix}").unlink(missing_ok=True)


def create_scoring_snapshot(*, data_path: Path, attempt_id: str) -> ScoringSnapshot:
    """Copy the completed attempt, hash the copy, then publish it atomically."""
    data_path = Path(data_path)
    source = data_path / "attempts" / attempt_id
    snapshots = data_path / "scoring-snapshots"
    snapshots.mkdir(parents=True, exist_ok=True)
    temporary = snapshots / f".tmp-{attempt_id}-{uuid.uuid4().hex}"
    copied_attempt = temporary / "attempt"
    try:
        shutil.copytree(source, copied_attempt, symlinks=True, ignore=_ignore_non_deliverables)
        _stabilize_env_db(source, copied_attempt)
        input_hash, manifest = _snapshot_hash(copied_attempt)
        destination = snapshots / input_hash.removeprefix("sha256:")
        metadata = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "input_hash": input_hash,
            "entries": manifest,
        }
        (temporary / "snapshot.json").write_text(
            json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        _make_read_only(temporary)
        try:
            os.replace(temporary, destination)
        except OSError:
            # A concurrent/retried enqueue may have published identical content.
            if not destination.is_dir():
                raise
            _remove_tree(temporary)
        relative = destination.relative_to(data_path).as_posix()
        verify_scoring_snapshot(
            data_path=data_path,
            snapshot_ref=relative,
            attempt_id=attempt_id,
            expected_hash=input_hash,
        )
        return ScoringSnapshot(input_hash=input_hash, relative_path=relative)
    finally:
        if temporary.exists():
            _remove_tree(temporary)


def verify_scoring_snapshot(
    *,
    data_path: Path,
    snapshot_ref: str,
    attempt_id: str,
    expected_hash: str,
) -> Path:
    """Verify a registered snapshot and return its frozen attempt directory."""
    data_path = Path(data_path).resolve()
    snapshot_root = (data_path / snapshot_ref).resolve()
    try:
        snapshot_root.relative_to(data_path / "scoring-snapshots")
    except ValueError as exc:
        raise ScoringInputMismatchError("snapshot reference escapes snapshot root") from exc
    attempt_root = snapshot_root / "attempt"
    try:
        actual_hash, _ = _snapshot_hash(attempt_root)
    except (OSError, ValueError) as exc:
        raise ScoringInputMismatchError(f"cannot verify scoring snapshot: {exc}") from exc
    if actual_hash != expected_hash:
        raise ScoringInputMismatchError(
            f"scoring snapshot hash mismatch: expected {expected_hash}, got {actual_hash}"
        )
    return attempt_root


def materialize_scoring_input(
    *,
    data_path: Path,
    snapshot_ref: str,
    attempt_id: str,
    expected_hash: str,
    job_id: str,
) -> Path:
    """Create a private writable view for legacy scorers and return its data root."""
    frozen_attempt = verify_scoring_snapshot(
        data_path=data_path,
        snapshot_ref=snapshot_ref,
        attempt_id=attempt_id,
        expected_hash=expected_hash,
    )
    work_root = Path(data_path) / "scoring-work" / job_id
    destination = work_root / "attempts" / attempt_id
    if work_root.exists():
        _remove_tree(work_root)
    destination.parent.mkdir(parents=True)
    shutil.copytree(frozen_attempt, destination, symlinks=True)
    for path in [destination, *destination.rglob("*")]:
        if not path.is_symlink():
            path.chmod(0o755 if path.is_dir() else 0o644)
    return work_root
