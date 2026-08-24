"""Activity 明细补齐与 API 聚合。

**Activity 不是实时接口。** 它面向最近 30 个**已完成的 UTC 日期**——run 刚结束时
当天的数据通常还查不到。因此：

- 资金总额只取 Key API 的累计 usage，**不等** Activity；
- 明细由本模块在 UTC 日结后补齐，缺失不阻塞 `final`；
- 跨 UTC 日期的 run 按涉及的每个日期逐日查询并求和。

**覆盖边界**：Activity 提供 prompt / completion / reasoning 三维，
**不提供** cache read / cache write——那两维只能靠
`backend/adapters/token_usage.py` 的本地观测，且五维 token 不得反向覆盖资金口径。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..config import Settings
from . import repo
from .errors import CostAuditError
from .models import ActivityRow, RunCostAudit
from .openrouter import OpenRouterManagementClient

logger = logging.getLogger(__name__)


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def utc_dates_spanned(started_at: str | None, finalized_at: str | None) -> list[str]:
    """run 覆盖的 UTC 日期列表。

    跨零点的 run 必须逐日查询再求和——Activity 按自然日切分，只查一天会漏掉
    另一半。终点缺失时按起点当天算（run 还没结算完，明细本来也不全）。
    """
    start = _parse(started_at)
    if start is None:
        return []
    end = _parse(finalized_at) or start
    if end < start:
        end = start
    start_day = start.astimezone(timezone.utc).date()
    end_day = end.astimezone(timezone.utc).date()
    out: list[str] = []
    day = start_day
    while day <= end_day:
        out.append(day.isoformat())
        day += timedelta(days=1)
    # 极端情况下（时间字段异常）避免无界循环，最多取 Activity 的 30 天窗口。
    return out[:30]


def _is_settled_utc_date(date: str, *, now: datetime | None = None) -> bool:
    """该 UTC 日期是否已经结束。未结束的日期查了也拿不到完整数据。"""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()
    try:
        target = datetime.fromisoformat(date).date()
    except ValueError:
        return False
    return target < current


async def backfill_activity(
    db_path: Path,
    audit: RunCostAudit,
    *,
    settings: Settings,
    client: OpenRouterManagementClient,
    now: datetime | None = None,
) -> int:
    """为一条已结算的审计记录补 Activity 明细。返回写入的行数。

    只对已有 key hash 且已进终态的记录做——`pending`/`settling` 的 run 还在花钱，
    这时候拉明细会拿到半截数据。
    """
    if not audit.api_key_hash:
        return 0
    dates = [
        d
        for d in utc_dates_spanned(audit.started_at, audit.finalized_at)
        if _is_settled_utc_date(d, now=now)
    ]
    if not dates:
        return 0

    rows: list[ActivityRow] = []
    for date in dates:
        try:
            rows.extend(
                await client.get_activity(date=date, key_hash=audit.api_key_hash)
            )
        except CostAuditError as exc:
            # 明细缺失**不得**阻塞资金口径：记日志、跳过这一天。
            logger.warning(
                "activity backfill failed run=%s date=%s: %s",
                audit.run_id,
                date,
                exc.error_code,
            )
            continue

    if not rows:
        return 0
    repo.set_activity(db_path, audit.run_id, [r.to_dict() for r in rows])
    logger.info(
        "activity backfilled run=%s rows=%d dates=%s", audit.run_id, len(rows), dates
    )
    return len(rows)


async def backfill_pending_activity(
    db_path: Path,
    *,
    settings: Settings,
    client: OpenRouterManagementClient | None = None,
    limit: int = 50,
    now: datetime | None = None,
) -> int:
    """扫描已结算但还没有明细的记录，逐个补齐。返回补齐的记录数。

    由后台 settler 顺带调用——与结算同一节奏即可，明细本就晚一天才有。
    """
    from .audit import build_client

    targets = repo.list_awaiting_activity(db_path, limit=limit)
    if not targets:
        return 0

    owns = client is None
    client = client or build_client(settings)
    if client is None:
        return 0

    filled = 0
    try:
        for audit in targets:
            try:
                if await backfill_activity(
                    db_path, audit, settings=settings, client=client, now=now
                ):
                    filled += 1
            except Exception:  # pragma: no cover - 单条失败不拖垮整轮
                logger.exception("activity backfill crashed run=%s", audit.run_id)
    finally:
        if owns:
            await client.aclose()
    return filled


async def backfill_ledger_activity(
    db_path: Path,
    *,
    settings: Settings,
    client: OpenRouterManagementClient | None = None,
    limit: int = 50,
    now: datetime | None = None,
) -> int:
    """给逐 key 账补上游 Activity 明细（三维 token）。

    与 run 级的 `backfill_pending_activity` 并存：那个填旧表，这个填
    `cost_key_ledgers`。明细缺失**不影响金额**——金额来自 key 用量，实时可得。
    """
    from . import ledger as L
    from .audit import build_client

    targets = L.list_awaiting_activity(db_path, limit=limit)
    if not targets:
        return 0

    owns = client is None
    client = client or build_client(settings)
    if client is None:
        return 0

    filled = 0
    try:
        for row in targets:
            if not row.api_key_hash:
                continue
            dates = [
                d
                for d in utc_dates_spanned(row.started_at, row.finalized_at)
                if _is_settled_utc_date(d, now=now)
            ]
            if not dates:
                continue
            rows: list[dict] = []
            for date in dates:
                try:
                    rows.extend(
                        r.to_dict()
                        for r in await client.get_activity(
                            date=date, key_hash=row.api_key_hash
                        )
                    )
                except CostAuditError as exc:
                    logger.warning(
                        "ledger activity backfill failed %s=%s date=%s: %s",
                        row.scope, row.scope_id, date, exc.error_code,
                    )
            if rows:
                L.set_activity(db_path, row.id, rows)
                filled += 1
    finally:
        if owns:
            await client.aclose()
    return filled


# ---------- API presentation -----------------------------------------------

_INT_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "reasoning_tokens",
    "requests",
)


def _add_number(
    bucket: dict[str, Any], output_key: str, value: Any, *, integer: bool
) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return
    normalized: int | float = int(value) if integer else float(value)
    current = bucket.get(output_key)
    bucket[output_key] = normalized if current is None else current + normalized


def _group_activity(
    rows: list[dict[str, Any]],
    key: str,
    *,
    related_key: str,
    related_output: str,
) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    related: dict[str, set[str]] = {}
    endpoints: dict[str, set[str]] = {}
    for row in rows:
        raw_name = row.get(key)
        name = raw_name if isinstance(raw_name, str) and raw_name else "unknown"
        bucket = buckets.setdefault(
            name,
            {
                key: name,
                "usage_usd": None,
                "requests": None,
                "prompt_tokens": None,
                "completion_tokens": None,
                "reasoning_tokens": None,
            },
        )
        _add_number(bucket, "usage_usd", row.get("usage"), integer=False)
        for field in _INT_FIELDS:
            _add_number(bucket, field, row.get(field), integer=True)
        related_value = row.get(related_key)
        if isinstance(related_value, str) and related_value:
            related.setdefault(name, set()).add(related_value)
        endpoint = row.get("endpoint")
        if isinstance(endpoint, str) and endpoint:
            endpoints.setdefault(name, set()).add(endpoint)

    out: list[dict[str, Any]] = []
    for name, bucket in buckets.items():
        bucket[related_output] = sorted(related.get(name, set()))
        bucket["endpoints"] = sorted(endpoints.get(name, set()))
        out.append(bucket)
    return sorted(
        out,
        key=lambda item: (
            item["usage_usd"] is None,
            -(item["usage_usd"] or 0),
            str(item[key]),
        ),
    )


def summarize_activity(
    rows: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """Return totals plus explicit model/provider splits.

    OpenRouter's ``usage`` is already the upstream billed amount.  It is named
    ``usage_usd`` in our API so callers do not confuse it with a token count.
    Missing fields remain ``None`` rather than being fabricated as zero.
    """
    if not rows:
        return None
    clean = [row for row in rows if isinstance(row, dict)]
    if not clean:
        return None
    totals: dict[str, Any] = {
        "usage_usd": None,
        "requests": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "reasoning_tokens": None,
    }
    for row in clean:
        _add_number(totals, "usage_usd", row.get("usage"), integer=False)
        for field in _INT_FIELDS:
            _add_number(totals, field, row.get(field), integer=True)
    return {
        **totals,
        "by_model": _group_activity(
            clean,
            "model",
            related_key="provider",
            related_output="providers",
        ),
        "by_provider": _group_activity(
            clean,
            "provider",
            related_key="model",
            related_output="models",
        ),
    }
