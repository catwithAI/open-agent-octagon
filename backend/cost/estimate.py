"""attempt 级成本估算的共用写入路径。

**定位**：这是 `token × 本地价格表` 的**估算**，不是资金真值。资金真值来自
`backend/cost/audit.py` 的上游 key 累计实扣差值。两者并存是有意的——差异率本身
就是要观测的指标（本地估算漏了什么、价格表过期多久）。

**为什么要抽出来**：原先只有 `experiments/scoring.py:commit_scoring_result`
会写 `cost_*` 四列，于是：

- timeout / cli_error / cancelled 等终态（`runner.py` 两条 finalize 路径）
  有 token 却无成本——这些 attempt 同样已经花了钱；
- `wire/aggregate.py:backfill_token_usage` 事后修正 token 后不会重算成本。

三处共用同一套计算与列写入，口径才一致。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: 四个成本列，写入顺序固定，供三处调用方共用。
COST_COLUMNS = ("cost_usd", "cost_breakdown_json", "cost_priced", "cost_model")


def compute_breakdown(
    token_usage: dict[str, Any] | None,
    model: str | None,
    pricing_path: Path | None = None,
    *,
    agent_name: str | None = None,
) -> Any:
    """五维用量 + 定价表 → CostBreakdown。任何一环缺失都返回 None。

    成本算不出**不是错误**（模型未收录、CLI 不吐 usage 都会走到这里），
    但绝不能猜或填 0——那会让"不可得"和"真的免费"混为一谈。

    与资金口径不同，这里保留宽异常兜底：成本*估算*不得拖垮评分主链路。
    """
    if not token_usage:
        return None
    try:
        from ..experiments.scoring import _load_pricing
        from ..pricing import compute_cost

        # Producer APIs disagree on whether input_tokens already includes
        # cache tiers.  Agent identity is authoritative; magnitude heuristics
        # are only a fallback for legacy/direct callers.
        semantics = (
            "disjoint"
            if agent_name in {
                "claude-code",
                "ssh-claude-code",
                "kimi-code",
                "opencode",
                "mimo-code",
                # dsh 的 TokenUsage 注释明写 "Counts are DISJOINT: inputTokens
                # is uncached input only"（llm/src/types.ts:128-134）。
                "dsh",
            }
            else "inclusive"
            if agent_name in {"codex", "blade-agent"}
            else "auto"
        )
        return compute_cost(
            token_usage,
            model,
            _load_pricing(pricing_path),
            input_semantics=semantics,
        )
    except Exception:  # pragma: no cover - 估算不得拖垮主链路
        logger.debug("cost estimate failed for model=%s", model, exc_info=True)
        return None


def cost_columns(breakdown: Any) -> tuple[float | None, str | None, int | None, str | None]:
    """CostBreakdown → 四列值。None 时四列全 NULL（不填 0）。

    `cost_priced=0` 表示有 token 因缺定价未计入，此时 `cost_usd` 只是**下界**，
    读取方必须一并检查——静默低估比缺值更危险。
    """
    if breakdown is None:
        return (None, None, None, None)
    return (
        breakdown.total_usd,
        json.dumps(breakdown.to_dict(), ensure_ascii=False, sort_keys=True),
        1 if breakdown.priced else 0,
        breakdown.model,
    )


def _resolve_attempt_context(
    conn: sqlite3.Connection,
    attempt_id: str,
    model: str | None,
) -> tuple[str | None, str | None]:
    """Resolve model plus producer identity from the authoritative attempt row.

    runner 的两条 finalize 路径作用域里都没有 model，必须回落读行。
    """
    row = conn.execute(
        "SELECT model,agent_name FROM attempts WHERE id=?",
        (attempt_id,),
    ).fetchone()
    return (
        model if model is not None else row[0] if row else None,
        row[1] if row else None,
    )


def apply_cost_columns(
    conn: sqlite3.Connection,
    attempt_id: str,
    *,
    token_usage: dict[str, Any] | None,
    model: str | None = None,
    pricing_path: Path | None = None,
) -> Any:
    """在**调用方已开启的连接/事务内**写入四个成本列。

    不自己开连接、不 commit——让成本与调用方的其他写入（分数、终态）落在
    同一事务里。否则会出现"有分数没成本"的中间态，聚合方读到就得区分
    "还没算"与"算不出"。

    返回计算出的 CostBreakdown（可能为 None），供调用方记录或断言。
    """
    effective_model, agent_name = _resolve_attempt_context(
        conn,
        attempt_id,
        model,
    )
    breakdown = compute_breakdown(
        token_usage,
        effective_model,
        pricing_path,
        agent_name=agent_name,
    )
    values = cost_columns(breakdown)
    conn.execute(
        "UPDATE attempts SET cost_usd=?,cost_breakdown_json=?,cost_priced=?,"
        "cost_model=? WHERE id=?",
        (*values, attempt_id),
    )
    return breakdown


def recompute_attempt_cost(
    db_path: Path,
    attempt_id: str,
    *,
    token_usage: dict[str, Any] | None,
    model: str | None = None,
    pricing_path: Path | None = None,
) -> Any:
    """独立事务内重算成本（wire backfill 修正 token 后调用）。

    与 `apply_cost_columns` 的区别是自带连接与 commit，供不在事务上下文里的
    调用方使用。
    """
    from ..db import _open_sync

    with _open_sync(db_path) as conn:
        breakdown = apply_cost_columns(
            conn,
            attempt_id,
            token_usage=token_usage,
            model=model,
            pricing_path=pricing_path,
        )
        conn.commit()
    return breakdown
