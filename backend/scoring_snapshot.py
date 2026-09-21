"""Immutable, content-addressed inputs for deferred scoring jobs."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import threading
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
    link_mode: str = "link"


# `copytree` 的 `copy_function` 没有返回值，也没有地方挂状态，而回落是否发生
# 必须记进 manifest（需求 2.2：不得静默）。用线程局部的计数器承接。
# 今天 `create_scoring_snapshot` 在事件循环线程上同步调用、本就串行，用
# threading.local 是为了将来它被挪进 `asyncio.to_thread` 时不必再回来改。
_link_stats = threading.local()


def _link_or_copy(src: str, dst: str) -> None:
    """优先硬链接；跨设备或不支持硬链接时回落复制。

    快照的语义是「判分期间产物不可再变」，而不是「产物有独立的第二份字节」。
    硬链接同样满足：判分前容器已被 kill（sandbox spec D-11），没有写入方，
    且 `_make_read_only` 把快照置为只读。源文件此后被删除也不影响快照——
    硬链接持有 inode 引用。省下的是整整一份 attempt 树的磁盘（实测 32G）。
    """
    stats = _link_stats.__dict__
    try:
        os.link(src, dst)
    except OSError as exc:
        # EXDEV：跨文件系统。EMLINK：inode 链接数到顶。EPERM/EOPNOTSUPP：
        # 文件系统不支持硬链接（部分 overlayfs / 网络盘）。这些都是环境限制，
        # 回落复制即可；其它 errno（如 ENOSPC）是真错误，必须抛出去。
        if exc.errno not in (errno.EXDEV, errno.EMLINK, errno.EPERM, errno.EOPNOTSUPP):
            raise
        shutil.copy2(src, dst)
        stats["copied"] = stats.get("copied", 0) + 1
    else:
        stats["linked"] = stats.get("linked", 0) + 1


def _link_mode(stats: dict[str, int]) -> str:
    linked = stats.get("linked", 0)
    copied = stats.get("copied", 0)
    if linked and copied:
        return "mixed"
    if copied:
        return "copy"
    return "link"


# attempt 根目录下的 agent 私有运行时目录。它们不是交付物：`sandbox_home` 是
# 容器家目录（LibreOffice 运行时、apt 缓存、字体），`sandbox_ro` 是只读物料。
#
# 只在 attempt 根一层排除，不进 `ARTIFACT_SKIP_DIRS`——那份清单是按**任意层级
# 的目录名**匹配的，且与 API 产物扫描、scorer 共用；把这些名字加进去会波及
# 那些链路，而它们只在 attempt 根下才有这个含义。
_ATTEMPT_ROOT_RUNTIME_DIRS = frozenset({
    "sandbox_home",
    "sandbox_ro",
    ".opencode-iso-home",
})


def _ignore_factory(attempt_root: Path):
    """Build the `copytree` ignore hook for one attempt root."""
    resolved_root = Path(attempt_root).resolve()

    def _is_attempt_root(directory: str) -> bool:
        try:
            return Path(directory).resolve() == resolved_root
        except OSError:
            return False

    return lambda directory, names: _ignore_non_deliverables(
        directory, names, is_attempt_root=_is_attempt_root(directory)
    )


def _ignore_non_deliverables(
    directory: str, names: list[str], *, is_attempt_root: bool = False
) -> set[str]:
    """`copytree` ignore hook: never copy dependency/build trees into a snapshot.

    Filtering after the copy would be too late — the cost being avoided is the
    copy itself. One measured attempt carried 190MB of `node_modules` whose 61
    actual deliverables totalled 384KB, and that tree was written twice (once
    here, once by `materialize_scoring_input`).

    Scoring reads the frozen copy, so anything dropped here is invisible to the
    judge. That is the intent: judging is about what the agent delivered, and a
    reinstallable dependency tree is not part of it.

    同理排除 attempt 根下的沙盒运行时目录。改硬链接后这一条从「省一次复制」
    升级成**归档能否真正回收空间**的前提：快照一旦链住 `sandbox_home` 的
    inode，归档删掉 attempt 侧的目录项也不会释放任何字节（实测：声称回收
    4.8MB，`du` 纹丝不动）。
    """
    ignored = {name for name in names if name in ARTIFACT_SKIP_DIRS}
    # 只有正在遍历 attempt 根时才排除运行时目录——workspace 深处若有同名目录，
    # 那是 agent 自己建的，属于产物。
    if is_attempt_root:
        ignored |= {name for name in names if name in _ATTEMPT_ROOT_RUNTIME_DIRS}
    return ignored


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
    """冻结快照树。

    快照用硬链接后，文件的 chmod 落在**共享 inode** 上，attempt 目录里的同一
    文件也会变成 0444。这是可接受的、甚至是想要的：快照建立时 attempt 已执行
    结束（`enqueue_scoring_job` 在此刻把状态推进到 scoring），不应再有写入方，
    只读正好把这一点落到文件系统上。

    删除不受影响——`unlink` 看的是父目录的写权限，而目录不共享 inode，
    所以归档仍然可以删 attempt 目录里的文件。
    """
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
    _link_stats.__dict__.clear()
    try:
        shutil.copytree(
            source,
            copied_attempt,
            symlinks=True,
            ignore=_ignore_factory(source),
            copy_function=_link_or_copy,
        )
        # env.db 必须先解链再重建：`_stabilize_env_db` 写的是一份事务一致的
        # backup，若沿用硬链接就会写穿到 attempt 目录里的活动数据库。
        _stabilize_env_db(source, copied_attempt)
        link_mode = _link_mode(dict(_link_stats.__dict__))
        input_hash, manifest = _snapshot_hash(copied_attempt)
        destination = snapshots / input_hash.removeprefix("sha256:")
        metadata = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "input_hash": input_hash,
            "link_mode": link_mode,
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
        return ScoringSnapshot(
            input_hash=input_hash, relative_path=relative, link_mode=link_mode
        )
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
    # 这里**不能**用硬链接：下面要把工作副本 chmod 成可写，供 legacy scorer
    # 原地改动。硬链接会让那次 chmod 写穿到只读快照的 inode 上，快照就不再
    # 冻结了。materialize 的语义本来就是「一份可改的私有副本」，必须真复制。
    shutil.copytree(frozen_attempt, destination, symlinks=True)
    for path in [destination, *destination.rglob("*")]:
        if not path.is_symlink():
            path.chmod(0o755 if path.is_dir() else 0o644)
    return work_root
