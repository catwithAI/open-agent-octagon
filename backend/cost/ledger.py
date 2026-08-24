"""逐 key 成本账的读写（revision-per-attempt-key）。

**一把 key 一行**。成本直接取自上游 `GET /keys/{hash}` 的 `usage`，
不经过 `compute_cost`——本地 token 估算实测高估 26×，且偏差方向不一致
（详见 `docs/specs/revision-per-attempt-key.md`）。

与 `repo.py`（run 级 `s`）的关系：那张表是修订前的口径，
保留只为读历史记录；新链路一律走这里。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from ..db import _open_sync
from .models import PARTIAL_MODES, allowed_targets, is_auditable

#: `attempt` = 单个 agent 执行；`judge` = 整个 run 的评分共用一把。
LedgerScope = Literal["attempt", "judge"]

_COLUMNS = (
    "id",
    "run_id",
    "scope",
    "scope_id",
    "agent_name",
    "provider",
    "api_key_hash",
    "api_key_name",
    "attribution_mode",
    "status",
    "usage_start",
    "usage_final",
    "cost_usd",
    "key_limit_usd",
    "key_disabled",
    "settle_attempts",
    "last_polled_at",
    "activity_json",
    "started_at",
    "finalized_at",
    "error_code",
    "error_message",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class CostKeyLedger:
    """一把 key 的成本账。

    `cost_usd` 是**上游实扣**（`usage_final - usage_start`），不是估算。
    起点通常为 0（新建 key），但仍实读一次——不假设上游初值。
    """

    run_id: str
    scope: LedgerScope
    scope_id: str
    attribution_mode: str
    status: str
    started_at: str
    id: str = ""
    agent_name: str | None = None
    provider: str = "openrouter"
    api_key_hash: str | None = None
    api_key_name: str | None = None
    usage_start: float | None = None
    usage_final: float | None = None
    cost_usd: float | None = None
    key_limit_usd: float | None = None
    key_disabled: bool | None = None
    settle_attempts: int = 0
    last_polled_at: str | None = None
    #: 上游 Activity 明细（三维 token）。日结后才有。
    activity: list[dict[str, Any]] | None = None
    finalized_at: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    extra: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def auditable(self) -> bool:
        return is_auditable(self.status, self.attribution_mode)

    @property
    def budget_consumed_ratio(self) -> float | None:
        if self.cost_usd is None or not self.key_limit_usd:
            return None
        return self.cost_usd / self.key_limit_usd

    def to_dict(self) -> dict[str, Any]:
        """序列化。不含任何 key 明文——本对象根本不持有明文。"""
        return {
            "id": self.id,
            "run_id": self.run_id,
            "scope": self.scope,
            "scope_id": self.scope_id,
            "agent_name": self.agent_name,
            "provider": self.provider,
            "api_key_hash": self.api_key_hash,
            "attribution_mode": self.attribution_mode,
            "status": self.status,
            "auditable": self.auditable,
            "usage_start": self.usage_start,
            "usage_final": self.usage_final,
            "cost_usd": self.cost_usd,
            "key_limit_usd": self.key_limit_usd,
            "budget_consumed_ratio": self.budget_consumed_ratio,
            "key_disabled": self.key_disabled,
            "settle_attempts": self.settle_attempts,
            "started_at": self.started_at,
            "finalized_at": self.finalized_at,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


def _row_to_ledger(row: tuple[Any, ...]) -> CostKeyLedger:
    import json as _json

    data = dict(zip(_COLUMNS, row))
    disabled = data.pop("key_disabled", None)
    raw = data.pop("activity_json", None)
    activity = None
    if raw:
        try:
            parsed = _json.loads(raw)
            activity = parsed if isinstance(parsed, list) else None
        except ValueError:
            activity = None
    return CostKeyLedger(
        key_disabled=None if disabled is None else bool(disabled),
        activity=activity,
        **data,
    )


def insert_ledger(db_path: Path, ledger: CostKeyLedger) -> bool:
    """插入一行。同 (scope, scope_id) 已存在时返回 False，不覆盖。

    唯一索引挡住并发/重启造出第二把 key——那会导致 run 总额重复计数。
    """
    ledger.id = ledger.id or f"ck_{uuid.uuid4().hex[:16]}"
    with _open_sync(db_path) as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO cost_key_ledgers("
            "id,run_id,scope,scope_id,agent_name,provider,api_key_hash,api_key_name,"
            "attribution_mode,status,usage_start,key_limit_usd,settle_attempts,"
            "started_at,error_code,error_message"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ledger.id,
                ledger.run_id,
                ledger.scope,
                ledger.scope_id,
                ledger.agent_name,
                ledger.provider,
                ledger.api_key_hash,
                ledger.api_key_name,
                ledger.attribution_mode,
                ledger.status,
                ledger.usage_start,
                ledger.key_limit_usd,
                ledger.settle_attempts,
                ledger.started_at or _now_iso(),
                ledger.error_code,
                ledger.error_message,
            ),
        )
        conn.commit()
        return cur.rowcount > 0


def get_by_scope(db_path: Path, scope: str, scope_id: str) -> CostKeyLedger | None:
    with _open_sync(db_path) as conn:
        row = conn.execute(
            f"SELECT {','.join(_COLUMNS)} FROM cost_key_ledgers "
            "WHERE scope=? AND scope_id=?",
            (scope, scope_id),
        ).fetchone()
    return _row_to_ledger(row) if row else None


def list_for_run(db_path: Path, run_id: str) -> list[CostKeyLedger]:
    """一个 run 的全部账（N 个 attempt + 至多 1 个 judge）。"""
    with _open_sync(db_path) as conn:
        rows = conn.execute(
            f"SELECT {','.join(_COLUMNS)} FROM cost_key_ledgers "
            "WHERE run_id=? ORDER BY scope, started_at",
            (run_id,),
        ).fetchall()
    return [_row_to_ledger(r) for r in rows]


def list_active(db_path: Path, *, limit: int = 200) -> list[CostKeyLedger]:
    """待结算的账（settler 重启恢复用）。"""
    with _open_sync(db_path) as conn:
        rows = conn.execute(
            f"SELECT {','.join(_COLUMNS)} FROM cost_key_ledgers "
            "WHERE status IN ('pending','settling') ORDER BY started_at LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row_to_ledger(r) for r in rows]


def transition(
    db_path: Path,
    ledger_id: str,
    *,
    target: str,
    expected: str | tuple[str, ...],
    **fields: Any,
) -> bool:
    """条件状态迁移。赢得迁移返回 True。

    非法迁移抛 ValueError（代码 bug）；输给并发者返回 False（正常竞争）。
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
    params.append(ledger_id)
    placeholders = ",".join("?" for _ in sources)
    params.extend(sources)

    with _open_sync(db_path) as conn:
        cur = conn.execute(
            f"UPDATE cost_key_ledgers SET {','.join(assignments)} "
            f"WHERE id=? AND status IN ({placeholders})",
            params,
        )
        conn.commit()
        return cur.rowcount > 0


def update_fields(db_path: Path, ledger_id: str, **fields: Any) -> None:
    if not fields:
        return
    assignments, params = [], []
    for name, value in fields.items():
        if name not in _COLUMNS or name == "status":
            raise ValueError(f"unknown or protected column: {name}")
        assignments.append(f"{name}=?")
        params.append(value)
    params.append(ledger_id)
    with _open_sync(db_path) as conn:
        conn.execute(
            f"UPDATE cost_key_ledgers SET {','.join(assignments)} WHERE id=?", params
        )
        conn.commit()


def bump_settle_attempt(db_path: Path, ledger_id: str) -> None:
    with _open_sync(db_path) as conn:
        conn.execute(
            "UPDATE cost_key_ledgers SET settle_attempts=settle_attempts+1,"
            "last_polled_at=? WHERE id=?",
            (_now_iso(), ledger_id),
        )
        conn.commit()


def expected_attempt_ids(db_path: Path, run_id: str) -> set[str]:
    """该 run 应该有账的全部 attempt。"""
    with _open_sync(db_path) as conn:
        return {
            r[0]
            for r in conn.execute(
                "SELECT id FROM attempts WHERE run_id=?", (run_id,)
            ).fetchall()
        }


def _judge_required(db_path: Path, run_id: str) -> bool:
    """本 run 是否应该有 judge 账。

    依据是「有没有 attempt 真的进过评分」——scoring_status 离开初始的
    `not_ready`/`skipped` 就说明评分发生过，那笔钱必须有账。
    """
    with _open_sync(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE run_id=? "
            "AND scoring_status NOT IN ('not_ready','skipped')",
            (run_id,),
        ).fetchone()
    return bool(row and row[0])


def set_activity(db_path: Path, ledger_id: str, rows: list[dict[str, Any]]) -> None:
    """写入上游 Activity 明细（三维 token）。"""
    import json as _json

    update_fields(
        db_path, ledger_id,
        activity_json=_json.dumps(rows, ensure_ascii=False, sort_keys=True),
    )


def list_awaiting_activity(db_path: Path, *, limit: int = 50) -> list[CostKeyLedger]:
    """已结算、有 key hash、但还没补 Activity 明细的账。"""
    with _open_sync(db_path) as conn:
        rows = conn.execute(
            f"SELECT {','.join(_COLUMNS)} FROM cost_key_ledgers "
            "WHERE status IN ('final','upper_bound') AND api_key_hash IS NOT NULL "
            "AND (activity_json IS NULL OR activity_json='') "
            "ORDER BY finalized_at LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row_to_ledger(r) for r in rows]


def aggregate_run(db_path: Path, run_id: str) -> dict[str, Any]:
    """聚合一个 run 的成本。

    总额 = Σ attempt 行 + judge 行。**不另建 run 级 key**——那会重复计数。

    **必须与 attempts 表对账**：只检查「已有的账是否都 final」
    是不够的——run 有 6 个 attempt 却只成功建了 5 行账时，那 5 行全 final
    就会被误报成 auditable，而总额少了一个 agent 的钱。缺账等于低估，
    必须显式识别并拒绝标 final。
    """
    ledgers = list_for_run(db_path, run_id)
    if not ledgers:
        return {
            "run_id": run_id,
            "auditable": False,
            "status": "unavailable",
            "missing_attempt_ids": sorted(expected_attempt_ids(db_path, run_id)),
            "total_cost_usd": None,
            "partial_total_cost_usd": None,
            "unpriced_ledger_count": 0,
            "unpriced_ledgers": [],
            "execution_cost_usd": None,
            "scoring_cost_usd": None,
            "by_attempt": [],
            "activity_summary": None,
            "judge": None,
        }

    attempts = [x for x in ledgers if x.scope == "attempt"]
    judges = [x for x in ledgers if x.scope == "judge"]
    from .activity import summarize_activity

    execution_activity = [
        row
        for ledger in attempts
        for row in (ledger.activity or [])
        if isinstance(row, dict)
    ]
    activity_summary = summarize_activity(execution_activity)

    def _sum(rows: list[CostKeyLedger]) -> float | None:
        vals = [r.cost_usd for r in rows if r.cost_usd is not None]
        return sum(vals) if vals else None

    # **有账但没金额 = 部分和**：降级/失败的行 cost_usd
    # 为 NULL，_sum 会跳过它们。此时把其余行相加当作总额并标"上界"是错的
    # ——那是**不含**缺失部分的偏低值，不是上界。总额置 NULL，
    # 已有部分单列 partial_total_cost_usd。
    # 部分值：金额缺失，或归因模式本身就说明"只结算到一部分"
    # （租约超时/重启丢明文/judge 回落——这些账里的钱只是独占 key 那段，
    # 剩下的走了 fallback key，从未被采集）。两者都让总额不完整。
    priced_missing = [
        r for r in ledgers
        if r.cost_usd is None or r.attribution_mode in PARTIAL_MODES
    ]
    execution = _sum(attempts)
    scoring = _sum(judges)
    partial = None
    if execution is not None or scoring is not None:
        partial = (execution or 0.0) + (scoring or 0.0)
    total = None if priced_missing else partial

    # 与 attempts 表对账：少一行账就少一个 agent 的钱。
    expected = expected_attempt_ids(db_path, run_id)
    covered = {x.scope_id for x in attempts}
    missing = sorted(expected - covered)

    # judge 同理：本 run 若发生过评分却没有 judge 账，
    # scoring_cost 是缺的，总额同样不完整——不能因为 attempt 都齐了就标 final。
    judge_required = _judge_required(db_path, run_id)
    judge_missing = judge_required and not judges

    complete = not missing and not judge_missing and all(x.auditable for x in ledgers)
    if complete:
        status = "final"
    elif any(x.status in ("pending", "settling") for x in ledgers):
        status = "settling"
    elif priced_missing:
        # 有账却拿不到金额：总额确定不完整，既不是实扣也不是上界。
        status = "incomplete"
    elif missing or judge_missing:
        # 账已全部终结却仍缺行 → 总额确定不完整，不能当上界也不能当实扣。
        status = "incomplete"
    elif any(x.attribution_mode == "shared_key_upper_bound" for x in ledgers):
        status = "upper_bound"
    else:
        status = "failed"

    return {
        "run_id": run_id,
        "auditable": complete,
        "status": status,
        # 缺账的 attempt 必须显式暴露——它们的钱没有被计入 total
        "missing_attempt_ids": missing,
        "judge_required": judge_required,
        "judge_missing": judge_missing,
        "total_cost_usd": total,
        # 已有账的部分和。**不是**总额，也不是上界——缺失部分未计入。
        "partial_total_cost_usd": partial,
        "unpriced_ledger_count": len(priced_missing),
        # 具体是哪几笔、为什么没金额——"2 笔账没有金额"看不出是
        # "blade 天然拿不到"还是"judge 还没跑完"，两者处理方式完全不同。
        "unpriced_ledgers": [
            {
                "scope": r.scope,
                "agent_name": r.agent_name,
                "status": r.status,
                "attribution_mode": r.attribution_mode,
                "error_code": r.error_code,
                # 还在跑 vs 永远拿不到，是两回事
                "pending": r.status in ("pending", "settling"),
            }
            for r in priced_missing
        ],
        "execution_cost_usd": execution,
        "scoring_cost_usd": scoring,
        "scoring_cost_ratio": (
            scoring / total if scoring is not None and total else None
        ),
        "by_attempt": [
            {
                "attempt_id": x.scope_id,
                "agent_name": x.agent_name,
                "cost_usd": x.cost_usd,
                "status": x.status,
                "auditable": x.auditable,
                "error_code": x.error_code,
                "activity": summarize_activity(x.activity),
            }
            for x in attempts
        ],
        "activity_summary": activity_summary,
        "judge": judges[0].to_dict() if judges else None,
    }
