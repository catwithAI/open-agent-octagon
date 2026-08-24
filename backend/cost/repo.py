"""`run_cost_audits` 的读写层。

**所有状态迁移都走带 `WHERE status IN (...)` 的条件 UPDATE**，由 SQLite 保证：

- `final` 不可迁出——条件里不含 `final`，改不动；
- 并发/重复调用幂等——第二次 UPDATE 的 rowcount 为 0，调用方据此
  知道自己不是赢家，不做重复副作用。

用 `_open_sync` 短连接 + WAL，与 `backend/db.py` 既有写路径一致，
不在 lifespan 长连接上序列化写。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..db import _open_sync
from .models import RunCostAudit, allowed_targets

_COLUMNS = (
    "run_id",
    "provider",
    "api_key_hash",
    "api_key_name",
    "attribution_mode",
    "status",
    "usage_start",
    "usage_after_execution",
    "usage_final",
    "execution_cost_usd",
    "scoring_cost_usd",
    "total_cost_usd",
    "key_limit_usd",
    "activity_json",
    "key_disabled",
    "settle_attempts",
    "last_polled_at",
    "started_at",
    "finalized_at",
    "error_code",
    "error_message",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_audit(row: sqlite3.Row | tuple[Any, ...]) -> RunCostAudit:
    data = dict(zip(_COLUMNS, row))
    activity_raw = data.pop("activity_json", None)
    activity: list[dict[str, Any]] | None = None
    if activity_raw:
        try:
            parsed = json.loads(activity_raw)
            activity = parsed if isinstance(parsed, list) else None
        except json.JSONDecodeError:
            activity = None
    disabled = data.pop("key_disabled", None)
    return RunCostAudit(
        activity=activity,
        key_disabled=None if disabled is None else bool(disabled),
        **data,
    )


def insert_audit(db_path: Path, audit: RunCostAudit) -> bool:
    """插入审计记录。已存在时返回 False（不覆盖）。

    `run_id` 是主键，一对一由 DB 保证。用 `INSERT OR IGNORE` 而非先
    查再插：后者在并发下有 TOCTOU 窗口，两个 run 恢复任务可能同时插入。
    """
    with _open_sync(db_path) as conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO run_cost_audits("
            "run_id,provider,api_key_hash,api_key_name,attribution_mode,status,"
            "usage_start,key_limit_usd,started_at,settle_attempts,"
            "error_code,error_message"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                audit.run_id,
                audit.provider,
                audit.api_key_hash,
                audit.api_key_name,
                audit.attribution_mode,
                audit.status,
                audit.usage_start,
                audit.key_limit_usd,
                audit.started_at or _now_iso(),
                audit.settle_attempts,
                # 起点采样失败的原因必须落库：否则"usage_start 为 NULL"看起来
                # 像没采过，实际是采了但上游不可用，两者的后续处理不同。
                audit.error_code,
                audit.error_message,
            ),
        )
        conn.commit()
        return cursor.rowcount > 0


def get_audit(db_path: Path, run_id: str) -> RunCostAudit | None:
    with _open_sync(db_path) as conn:
        row = conn.execute(
            f"SELECT {','.join(_COLUMNS)} FROM run_cost_audits WHERE run_id=?",
            (run_id,),
        ).fetchone()
    return _row_to_audit(row) if row else None


def list_active(db_path: Path, *, limit: int = 100) -> list[RunCostAudit]:
    """列出需要 settler 继续推进的记录（重启恢复用）。"""
    with _open_sync(db_path) as conn:
        rows = conn.execute(
            f"SELECT {','.join(_COLUMNS)} FROM run_cost_audits "
            "WHERE status IN ('pending','settling') ORDER BY started_at LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row_to_audit(r) for r in rows]


def list_awaiting_activity(db_path: Path, *, limit: int = 50) -> list[RunCostAudit]:
    """已结算、有 key hash、但还没补 Activity 明细的记录。

    只取终态：`pending`/`settling` 的 run 还在花钱，此时拉明细是半截数据。
    `failed` 也排除——那条 run 的资金口径本身就不可信，补明细没有意义。
    """
    with _open_sync(db_path) as conn:
        rows = conn.execute(
            f"SELECT {','.join(_COLUMNS)} FROM run_cost_audits "
            "WHERE status IN ('final','upper_bound') AND api_key_hash IS NOT NULL "
            "AND (activity_json IS NULL OR activity_json='') "
            "ORDER BY finalized_at LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row_to_audit(r) for r in rows]


def transition(
    db_path: Path,
    run_id: str,
    *,
    target: str,
    expected: str | tuple[str, ...],
    **fields: Any,
) -> bool:
    """条件状态迁移。赢得迁移返回 True，否则 False。

    `expected` 是允许的源状态。**调用方必须处理 False**——那表示别的执行者
    已经推进过（重启恢复、并发 checkpoint），此时不能重复做副作用
    （比如重复禁用 key、重复计费）。

    非法迁移（如 `final → settling`）直接抛 ValueError：那是代码 bug，
    不是并发竞争，不能静默吞掉。
    """
    sources = (expected,) if isinstance(expected, str) else tuple(expected)
    for src in sources:
        if target not in allowed_targets(src):
            raise ValueError(f"illegal transition: {src} -> {target}")

    assignments = ["status=?"]
    params: list[Any] = [target]
    for name, value in fields.items():
        if name not in _COLUMNS:
            raise ValueError(f"unknown column: {name}")
        assignments.append(f"{name}=?")
        params.append(value)
    params.append(run_id)
    placeholders = ",".join("?" for _ in sources)
    params.extend(sources)

    with _open_sync(db_path) as conn:
        cursor = conn.execute(
            f"UPDATE run_cost_audits SET {','.join(assignments)} "
            f"WHERE run_id=? AND status IN ({placeholders})",
            params,
        )
        conn.commit()
        return cursor.rowcount > 0


def update_fields(db_path: Path, run_id: str, **fields: Any) -> None:
    """更新非状态字段（轮询计数、activity 明细等）。不改 status。"""
    if not fields:
        return
    assignments = []
    params: list[Any] = []
    for name, value in fields.items():
        if name not in _COLUMNS or name == "status":
            raise ValueError(f"unknown or protected column: {name}")
        assignments.append(f"{name}=?")
        params.append(value)
    params.append(run_id)
    with _open_sync(db_path) as conn:
        conn.execute(
            f"UPDATE run_cost_audits SET {','.join(assignments)} WHERE run_id=?",
            params,
        )
        conn.commit()


def bump_settle_attempt(db_path: Path, run_id: str) -> None:
    with _open_sync(db_path) as conn:
        conn.execute(
            "UPDATE run_cost_audits SET settle_attempts=settle_attempts+1,"
            "last_polled_at=? WHERE run_id=?",
            (_now_iso(), run_id),
        )
        conn.commit()


def set_activity(db_path: Path, run_id: str, rows: list[dict[str, Any]]) -> None:
    update_fields(
        db_path,
        run_id,
        activity_json=json.dumps(rows, ensure_ascii=False, sort_keys=True),
    )
