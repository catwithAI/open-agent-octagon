"""Submission 工作区冻结、哈希和只读快照校验。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows compatibility
    fcntl = None  # type: ignore[assignment]

from .writer import atomic_write_json, now_iso


DEFAULT_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".octagon",
        "node_modules",
        "dist",
        "build",
        "__pycache__",
    }
)
SNAPSHOT_SCHEMA_VERSION = "octagon-submission-snapshot-v1"

# venv 的解释器运行时软链：允许指向系统解释器的软链（venv/bin/python ->
# /usr/bin/python3 等），快照复制这些软链本身，评审时解析到系统解释器即可复现。
# 保守只覆盖标准 bin 目录下的 python* 工具，其它逃逸宿主路径的软链仍拒绝。
_RUNTIME_INTERPRETER_RE = re.compile(r"^/(?:usr(?:/local)?/)?bin/python[\w.\-]*$")


def _is_runtime_interpreter_symlink(resolved: Path) -> bool:
    return bool(_RUNTIME_INTERPRETER_RE.match(resolved.as_posix()))


class SubmissionSnapshotError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SnapshotFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class SubmissionSnapshot:
    attempt_id: str
    submission_id: str
    round_index: int
    snapshot_path: Path
    manifest_path: Path
    manifest_hash: str
    files: tuple[SnapshotFile, ...]
    total_size: int
    created_at: str


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _symlink_snapshot_meta(destination: Path) -> tuple[int, str]:
    """软链以自身元数据入账（不跟随，避免链式软链中间跳未复制时 stat 崩溃）。

    size=lstat 自身大小；sha256 用目标字符串（readlink）哈希，确定且不依赖
    当前是否已复制到中间跳。评审时软链按完整快照解析仍可执行。
    """
    link = os.readlink(destination)
    st = os.lstat(destination)
    digest = hashlib.sha256(link.encode("utf-8")).hexdigest()
    return st.st_size, digest


def _manifest_hash(files: Iterable[SnapshotFile]) -> str:
    payload = [asdict(item) for item in files]
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _make_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_symlink():
            continue
        path.chmod(0o555 if path.is_dir() else 0o444)
    root.chmod(0o555)


def _remove_tree(root: Path) -> None:
    if not root.exists():
        return
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            continue
        try:
            path.chmod(0o755 if path.is_dir() else 0o644)
        except OSError:
            pass
    shutil.rmtree(root, ignore_errors=True)


def _load_existing(
    submission_dir: Path,
    *,
    attempt_id: str,
    submission_id: str,
    round_index: int,
) -> SubmissionSnapshot | None:
    manifest_path = submission_dir / "snapshot-manifest.json"
    snapshot_path = submission_dir / "candidate-snapshot"
    if not manifest_path.exists() and not snapshot_path.exists():
        return None
    if not manifest_path.is_file() or not snapshot_path.is_dir():
        raise SubmissionSnapshotError("Submission 快照处于不完整状态")
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise SubmissionSnapshotError("已有快照 manifest 无法读取") from exc
    if (
        raw.get("attempt_id") != attempt_id
        or raw.get("submission_id") != submission_id
        or raw.get("round_index") != round_index
    ):
        raise SubmissionSnapshotError("已有快照身份与当前 Submission 不一致")
    files = tuple(
        SnapshotFile(
            path=str(item["path"]),
            size=int(item["size"]),
            sha256=str(item["sha256"]),
        )
        for item in raw.get("files", [])
    )
    if _manifest_hash(files) != raw.get("manifest_hash"):
        raise SubmissionSnapshotError("已有快照 manifest hash 无效")
    for item in files:
        path = snapshot_path / item.path
        if not path.exists():
            raise SubmissionSnapshotError(f"已有快照文件缺失: {item.path}")
        if path.is_symlink():
            size, sha = _symlink_snapshot_meta(path)
            if size != item.size or sha != item.sha256:
                raise SubmissionSnapshotError(f"已有快照文件完整性失败: {item.path}")
        else:
            if path.stat().st_size != item.size or _sha256_file(path) != item.sha256:
                raise SubmissionSnapshotError(f"已有快照文件完整性失败: {item.path}")
            if path.stat().st_mode & stat.S_IWUSR:
                raise SubmissionSnapshotError(f"已有快照文件不是只读: {item.path}")
    return SubmissionSnapshot(
        attempt_id=attempt_id,
        submission_id=submission_id,
        round_index=round_index,
        snapshot_path=snapshot_path,
        manifest_path=manifest_path,
        manifest_hash=str(raw["manifest_hash"]),
        files=files,
        total_size=int(raw.get("total_size", sum(item.size for item in files))),
        created_at=str(raw.get("created_at") or ""),
    )


@contextmanager
def _submission_snapshot_lock(submission_dir: Path) -> Iterator[None]:
    """Serialize creation of one Submission snapshot across processes."""
    submission_dir.mkdir(parents=True, exist_ok=True)
    lock_path = submission_dir / ".snapshot.lock"
    with lock_path.open("a+b") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _freeze_submission_snapshot_unlocked(
    *,
    workspace: Path,
    submission_dir: Path,
    attempt_id: str,
    submission_id: str,
    round_index: int,
    excluded_dirs: Iterable[str] = (),
) -> SubmissionSnapshot:
    workspace = Path(workspace)
    submission_dir = Path(submission_dir)
    if not workspace.is_dir():
        raise SubmissionSnapshotError(f"候选工作区不存在: {workspace}")

    existing = _load_existing(
        submission_dir,
        attempt_id=attempt_id,
        submission_id=submission_id,
        round_index=round_index,
    )
    if existing is not None:
        return existing

    exclusions = DEFAULT_EXCLUDED_DIRS | frozenset(excluded_dirs)
    workspace_resolved = workspace.resolve()
    temporary = submission_dir / f".candidate-snapshot.{uuid.uuid4().hex}.tmp"
    snapshot_path = submission_dir / "candidate-snapshot"
    manifest_path = submission_dir / "snapshot-manifest.json"
    temporary.mkdir(parents=True, exist_ok=False)
    files: list[SnapshotFile] = []
    snapshot_created = False

    try:
        for source in sorted(workspace.rglob("*")):
            relative = source.relative_to(workspace)
            if any(part in exclusions for part in relative.parts):
                continue
            if source.is_symlink():
                resolved = source.resolve()
                # 软链处理：工作区内软链（如 venv/lib64 -> venv/lib）直接放行；
                # 逃逸工作区的软链仅放行指向系统解释器二进制的运行时软链
                # （venv/bin/python -> /usr/bin/python3），其余仍拒绝，维持
                # 快照安全边界（防任意宿主路径引用）。
                if resolved != workspace_resolved and workspace_resolved not in resolved.parents:
                    if not _is_runtime_interpreter_symlink(resolved):
                        raise SubmissionSnapshotError(
                            f"候选路径逃逸工作区（非运行时符号链接）: "
                            f"{relative.as_posix()} -> {resolved}"
                        )
                # 放行的软链以软链本身形式复制（follow_symlinks=False），
                # 评审时按相对/绝对目标解析，快照自保持可执行。
            else:
                resolved = source.resolve()
                if resolved != workspace_resolved and workspace_resolved not in resolved.parents:
                    raise SubmissionSnapshotError(
                        f"候选路径逃逸工作区: {relative.as_posix()}"
                    )
            if source.is_dir():
                continue
            if not source.is_file():
                raise SubmissionSnapshotError(
                    f"候选工作区包含不支持的文件类型: {relative.as_posix()}"
                )
            destination = temporary / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination, follow_symlinks=False)
            if destination.is_symlink():
                size, sha = _symlink_snapshot_meta(destination)
            else:
                size = destination.stat().st_size
                sha = _sha256_file(destination)
            files.append(
                SnapshotFile(
                    path=relative.as_posix(),
                    size=size,
                    sha256=sha,
                )
            )

        if not files:
            raise SubmissionSnapshotError("候选工作区没有可评审文件")
        files_tuple = tuple(files)
        manifest_hash = _manifest_hash(files_tuple)
        submission_dir.mkdir(parents=True, exist_ok=True)
        _make_read_only(temporary)
        os.replace(temporary, snapshot_path)
        snapshot_created = True
        created_at = now_iso()
        total_size = sum(item.size for item in files_tuple)
        atomic_write_json(
            manifest_path,
            {
                "schema_version": SNAPSHOT_SCHEMA_VERSION,
                "attempt_id": attempt_id,
                "submission_id": submission_id,
                "round_index": round_index,
                "created_at": created_at,
                "files": [asdict(item) for item in files_tuple],
                "file_count": len(files_tuple),
                "total_size": total_size,
                "manifest_hash": manifest_hash,
            },
        )
        return SubmissionSnapshot(
            attempt_id=attempt_id,
            submission_id=submission_id,
            round_index=round_index,
            snapshot_path=snapshot_path,
            manifest_path=manifest_path,
            manifest_hash=manifest_hash,
            files=files_tuple,
            total_size=total_size,
            created_at=created_at,
        )
    except Exception:
        _remove_tree(temporary)
        if snapshot_created and not manifest_path.is_file():
            _remove_tree(snapshot_path)
        raise


def freeze_submission_snapshot(
    *,
    workspace: Path,
    submission_dir: Path,
    attempt_id: str,
    submission_id: str,
    round_index: int,
    excluded_dirs: Iterable[str] = (),
) -> SubmissionSnapshot:
    """Freeze a Submission idempotently under a per-Submission process lock."""
    with _submission_snapshot_lock(Path(submission_dir)):
        return _freeze_submission_snapshot_unlocked(
            workspace=workspace,
            submission_dir=submission_dir,
            attempt_id=attempt_id,
            submission_id=submission_id,
            round_index=round_index,
            excluded_dirs=excluded_dirs,
        )
