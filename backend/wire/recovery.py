"""wire manifest 启动恢复。

后端崩溃/被杀后，已 prepare 但没走到 finalize 的 attempt 会留下
status=in-progress 的 wire-manifest 和 `.partial` spool。lifespan 启动时扫描：

- attempt 在 DB 已终态 → 读取完整 spool 行重新 finalize，status 标
  ``recovered``；finalize 自身失败则写 ``failed``——绝不长期伪装 in-progress；
- attempt 仍 running/queued（可能由 recovery.py 接管续跑）→ 不动；
- 已 finalize 的 manifest（complete/partial/...）不重复处理。

与既有 `backend/recovery.py`（attempt 级 blade 续跑）职责正交：那边恢复
attempt 本身，这边只收敛 wire 观测的落盘状态。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from backend.wire import finalize, paths, writer
from backend.wire.policy import EffectivePolicy

logger = logging.getLogger(__name__)

# 显式终态集合（与 run_dispatch._refresh_run_status 对齐）。用「不在非终态里」
# 推断终态是错的：starting_blade_session 等中间态会被当成终态提前 finalize，
# 之后 blade 异步恢复成功时 manifest 已不再 in-progress、无法补收敛。
# DB 查不到 attempt（None）同样不是终态——宁可留到下次扫描。
_TERMINAL = {
    "completed",
    "gave_up",
    "scoring_failed",
    "timeout",
    # 用户主动停止。漏掉它会让被 Stop 的 attempt 的 in-progress
    # manifest 在每次重启时都被判为「还没跑完」而跳过，永远收敛不了。
    "cancelled",
    "chat_failed",
    "auth_failed",
    "session_create_failed",
    "blade_service_unavailable",
    "cli_not_found",
    "cli_error",
    "interrupted",
    "capture_infrastructure_failed",
    "model_integrity_failed",
}


def _attempt_status(db_path: Path, attempt_id: str) -> str | None:
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT status FROM attempts WHERE id=?", (attempt_id,)
            ).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def _policy_from_manifest(manifest: dict) -> EffectivePolicy:
    p = manifest.get("policy") or {}
    return EffectivePolicy(
        requested=p.get("requested", "metadata"),
        effective=p.get("effective", "metadata"),
        downgrade_reason=p.get("downgrade_reason"),
    )


def _oversized_spools(
    data_path: Path, attempt_id: str, limit: int
) -> list[tuple[str, int]]:
    """返回超过 ``limit`` 的 spool 文件 (名字, 字节数)。``limit<=0`` 表示不限。"""
    if limit <= 0:
        return []
    src_dir = paths.sources_dir(data_path, attempt_id)
    if not src_dir.is_dir():
        return []
    oversized = []
    for file in sorted(src_dir.iterdir()):
        try:
            if not file.is_file():
                continue
            size = file.stat().st_size
        except OSError:
            continue
        if size > limit:
            oversized.append((file.name, size))
    return oversized


def _write_failed_manifest(
    data_path: Path,
    db_path: Path,
    attempt_id: str,
    manifest: dict,
    *,
    gaps: list[dict] | None = None,
) -> None:
    """把 manifest 收敛成 failed 并同步 DB 摘要。绝不长期伪装 in-progress。"""
    failed_manifest = {
        **manifest,
        "status": "failed",
        "generation": int(manifest.get("generation", 0)) + 1,
    }
    if gaps:
        failed_manifest["gaps"] = list(manifest.get("gaps") or []) + gaps
    try:
        writer.atomic_write_json(paths.manifest_file(data_path, attempt_id), failed_manifest)
        # DB 摘要必须与磁盘事实一致
        finalize.update_db_summary(db_path, attempt_id, failed_manifest)
    except Exception:
        logger.exception("wire recovery 写 failed manifest 失败 attempt=%s", attempt_id)


def recover_wire_manifests(
    data_path: Path,
    db_path: Path,
    *,
    attempt_id: str | None = None,
    max_spool_bytes: int = 0,
) -> int:
    """扫描并收敛 in-progress wire manifest，返回处理数。异常不外抛。

    ``attempt_id`` 限定单 attempt：attempt 级 recovery（blade 续跑）是异步
    task，启动扫描时它还没终态；其收尾处应再次调用本函数补收敛，否则该
    manifest 会一直 in-progress 到下次重启。
    """
    attempts_dir = Path(data_path) / "attempts"
    if not attempts_dir.is_dir():
        return 0
    if attempt_id is not None:
        candidates = [attempts_dir / attempt_id / "wire-manifest.json"]
    else:
        candidates = sorted(attempts_dir.glob("*/wire-manifest.json"))
    handled = 0
    for manifest_path in candidates:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if manifest.get("status") != "in-progress":
            continue  # 已 finalize 的 attempt 不重复处理
        aid = manifest_path.parent.name
        status = _attempt_status(db_path, aid)
        if status not in _TERMINAL:
            continue  # 未终态/未知：不动，等 attempt 级 recovery 收尾或下次扫描
        # 体积闸：超大 spool 直接隔离成 failed，不进 finalize。原始 spool
        # 留在磁盘上供离线分析，但绝不在启动路径上把它读进内存。
        oversized = _oversized_spools(data_path, aid, max_spool_bytes)
        if oversized:
            detail = ", ".join(f"{name}={size}B" for name, size in oversized)
            logger.error(
                "wire recovery: attempt=%s spool 超过 %d 字节上限（%s）→ 隔离为 failed",
                aid, max_spool_bytes, detail,
            )
            _write_failed_manifest(
                data_path, db_path, aid, manifest,
                gaps=[{
                    "field": "lifecycle",
                    "reason": "recovery_skipped_oversized_spool",
                    "detail": detail,
                    "limit_bytes": max_spool_bytes,
                }],
            )
            handled += 1
            continue
        policy = _policy_from_manifest(manifest)
        # in-progress manifest 里持久化的 prepare 快照：没有它，
        # 「source 在建 spool 前失败」会被误判 not-applicable 而不是 failed。
        declared = manifest.get("declared_sources") or []
        gaps = list(manifest.get("gaps") or [])
        gaps.append({"field": "lifecycle", "reason": "recovered_after_restart"})
        try:
            recovered = finalize.finalize_attempt(
                data_path=data_path,
                attempt_id=aid,
                policy=policy,
                strict=bool(manifest.get("strict")),
                declared_sources=declared,
                gaps=gaps,
                phase_attribution=manifest.get("phase_attribution", "explicit"),
                started_at=manifest.get("started_at"),
                finished_at=None,
                recovered=True,
            )
            finalize.update_db_summary(db_path, aid, recovered)
            logger.warning("wire recovery: attempt=%s → recovered", aid)
        except Exception:
            logger.exception("wire recovery finalize 失败 attempt=%s", aid)
            _write_failed_manifest(data_path, db_path, aid, manifest)
        handled += 1
    return handled
