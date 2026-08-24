"""`GET /runs/{run_id}/cost` 的响应组装。

**核心呈现原则**：上游实扣是主值，本地估算是对照值。两者并存不是冗余——
差异率本身就是要观测的指标（本地估算漏了什么、价格表过期多久）。

绝不把估算标成 audited cost；Activity 未补齐时相关字段返回 `null`
而非 0，与 `backend/pricing.py` 既有的 `None ≠ 0` 纪律一致。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from ..db import _open_sync
from . import repo
from .models import RunCostAudit


def _estimate_for_run(db_path: Path, run_id: str) -> dict[str, Any]:
    """汇总本次 run 全部 attempt 的本地估算成本。

    `priced=False`（有 token 缺定价）任一为真则整体标为下界；
    `attempts_missing_cost` 是有 token 却算不出成本的 attempt 数——
    它是"估算不完整"的直接证据，比一个孤零零的总额有用。
    """
    with _open_sync(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT cost_usd, cost_priced, token_usage_json FROM attempts"
            " WHERE run_id=?",
            (run_id,),
        ).fetchall()

    total: float | None = None
    priced = True
    missing = 0
    for row in rows:
        cost = row["cost_usd"]
        if cost is None:
            try:
                usage = json.loads(row["token_usage_json"] or "{}")
            except json.JSONDecodeError:
                usage = {}
            # 有 token 却没成本 = 估算链路有缺口（模型未收录/定价表缺失）。
            # 没 token 的 attempt 本来就算不出，不计入缺口。
            if any(isinstance(v, (int, float)) and v for v in usage.values()):
                missing += 1
            continue
        total = cost if total is None else total + cost
        if row["cost_priced"] == 0:
            priced = False

    return {
        "total_cost_usd": total,
        "priced": priced if total is not None else None,
        "attempts_missing_cost": missing,
        "attempt_count": len(rows),
    }


def _divergence(upstream: float | None, estimate: float | None) -> float | None:
    """|上游 - 估算| / 上游。上游为 0 或缺失时返回 None，不做除零。"""
    if upstream is None or estimate is None or upstream == 0:
        return None
    return abs(upstream - estimate) / upstream


def _derived(audit: RunCostAudit, attempt_count: int) -> dict[str, Any]:
    """派生指标。拿不到的一律 None，不填 0。"""
    total = audit.total_cost_usd
    scoring_ratio = (
        audit.scoring_cost_usd / total
        if audit.scoring_cost_usd is not None and total
        else None
    )
    per_session = total / attempt_count if total is not None and attempt_count else None

    by_model: list[dict[str, Any]] | None = None
    by_provider: list[dict[str, Any]] | None = None
    requests: int | None = None
    if audit.activity:
        by_model = _group(audit.activity, "model")
        by_provider = _group(audit.activity, "provider")
        counted = [r.get("requests") for r in audit.activity if r.get("requests")]
        requests = sum(counted) if counted else None

    return {
        "session_count": attempt_count,
        # **均摊值，不是逐 session 实耗**：N 个 session 共用一把 key，
        # 物理上无法拆分到每个 session。字段名与此注释都必须保留这个限定。
        "avg_cost_per_session_usd": per_session,
        "avg_cost_per_session_is_amortized": True,
        "scoring_cost_ratio": scoring_ratio,
        "budget_consumed_ratio": audit.budget_consumed_ratio,
        # Activity 未日结时为 null，**不是 0**。
        "by_model": by_model,
        "by_provider": by_provider,
        "requests": requests,
        "avg_cost_per_request_usd": (
            total / requests if total is not None and requests else None
        ),
    }


def _group(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = row.get(key) or "unknown"
        bucket = buckets.setdefault(
            name, {key: name, "usage": None, "requests": None}
        )
        usage = row.get("usage")
        if usage is not None:
            bucket["usage"] = usage if bucket["usage"] is None else bucket["usage"] + usage
        req = row.get("requests")
        if req is not None:
            bucket["requests"] = (
                req if bucket["requests"] is None else bucket["requests"] + req
            )
    return sorted(
        buckets.values(), key=lambda b: (b["usage"] is None, -(b["usage"] or 0))
    )


def build_run_cost_view(db_path: Path, run_id: str) -> dict[str, Any] | None:
    """组装成本响应。没有审计记录时返回 None（调用方转 404 或 disabled 态）。

    **优先逐 key 账**：它能给出每个 agent 各花多少，正是六平台对比
    实验要的。没有逐 key 账时回落旧的 run 级记录（历史 run）。
    """
    from . import ledger as L

    agg = L.aggregate_run(db_path, run_id)
    if agg["status"] != "unavailable":
        estimate = _estimate_for_run(db_path, run_id)
        activity = agg.get("activity_summary")
        requests = activity.get("requests") if activity else None
        execution_cost = agg["execution_cost_usd"]
        return {
            "run_id": run_id,
            "provider": "openrouter",
            "granularity": "per_attempt",
            "attribution_mode": None,
            "status": agg["status"],
            "auditable": agg["auditable"],
            "upstream": {
                "execution_cost_usd": agg["execution_cost_usd"],
                "scoring_cost_usd": agg["scoring_cost_usd"],
                "total_cost_usd": agg["total_cost_usd"],
                # 逐 key 口径没有 run 级 usage 起止点——每把 key 各自有。
                "key_limit_usd": None,
                "usage_start": None,
                "usage_after_execution": None,
                "usage_final": None,
            },
            "estimate": estimate,
            "divergence_ratio": _divergence(
                agg["total_cost_usd"], estimate["total_cost_usd"]
            ),
            "derived": {
                "session_count": len(agg["by_attempt"]),
                # 逐 attempt 是**实耗**，不是均摊——这正是改 per-attempt 的目的
                "avg_cost_per_session_usd": None,
                "avg_cost_per_session_is_amortized": False,
                "scoring_cost_ratio": agg.get("scoring_cost_ratio"),
                "budget_consumed_ratio": None,
                "by_model": activity.get("by_model") if activity else None,
                "by_provider": activity.get("by_provider") if activity else None,
                "requests": requests,
                "avg_cost_per_request_usd": (
                    execution_cost / requests
                    if execution_cost is not None and requests
                    else None
                ),
            },
            # 逐 agent 实耗——横向比较的核心数据
            "by_attempt": agg["by_attempt"],
            "judge": agg["judge"],
            "missing_attempt_ids": agg.get("missing_attempt_ids", []),
            # 已有账的部分和。**不是**总额也不是上界——缺失部分未计入
            # （之前没返回，前端提示成了死代码）。
            "partial_total_cost_usd": agg.get("partial_total_cost_usd"),
            "unpriced_ledger_count": agg.get("unpriced_ledger_count", 0),
            "unpriced_ledgers": agg.get("unpriced_ledgers", []),
            "judge_required": agg.get("judge_required"),
            "judge_missing": agg.get("judge_missing"),
            "error_code": None,
        }

    audit = repo.get_audit(db_path, run_id)
    estimate = _estimate_for_run(db_path, run_id)
    if audit is None:
        # 成本核算未启用/该 run 早于本功能：仍然给出本地估算，
        # 但显式标明没有上游口径，不让前端把估算当实扣。
        return {
            "run_id": run_id,
            "provider": None,
            "attribution_mode": None,
            "status": "unavailable",
            "auditable": False,
            "upstream": None,
            "estimate": estimate,
            "divergence_ratio": None,
            "derived": None,
            "error_code": None,
        }

    upstream = {
        "execution_cost_usd": audit.execution_cost_usd,
        "scoring_cost_usd": audit.scoring_cost_usd,
        "total_cost_usd": audit.total_cost_usd,
        "key_limit_usd": audit.key_limit_usd,
        "usage_start": audit.usage_start,
        "usage_after_execution": audit.usage_after_execution,
        "usage_final": audit.usage_final,
    }

    return {
        "run_id": run_id,
        "provider": audit.provider,
        # key hash 非敏感，可返回，便于在 OpenRouter 控制台核对；
        # 明文 key 与 Management Key 绝不出现。
        "api_key_hash": audit.api_key_hash,
        "attribution_mode": audit.attribution_mode,
        "status": audit.status,
        "auditable": audit.auditable,
        "upstream": upstream,
        "estimate": estimate,
        "divergence_ratio": _divergence(
            audit.total_cost_usd, estimate["total_cost_usd"]
        ),
        "derived": _derived(audit, estimate["attempt_count"]),
        "key_disabled": audit.key_disabled,
        "settle_attempts": audit.settle_attempts,
        "started_at": audit.started_at,
        "finalized_at": audit.finalized_at,
        "error_code": audit.error_code,
        "error_message": audit.error_message,
    }
