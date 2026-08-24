"""上游用量的稳定性判定与后台结算。

**agent 进程退出 ≠ usage 已更新**。OpenRouter 的计费入账有延迟，
刚退出时读到的累计值可能还没包含最后几次调用。因此每个 checkpoint 都要轮询：
连续 `stable_threshold` 次读到同一个值才算稳定。

超时不写 final——记录停在 `settling`，由后台 settler 继续。
宁可让 UI 显示"等待上游结算"，也不能写一个偏小的数当资金口径。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from ..config import Settings
from . import repo
from .errors import CostAuditError
from .openrouter import OpenRouterManagementClient

logger = logging.getLogger(__name__)


async def settle_usage(
    client: OpenRouterManagementClient,
    key_hash: str,
    *,
    settings: Settings,
    sleep: Any = None,
) -> tuple[float | None, str | None]:
    """轮询到累计 usage 稳定。

    返回 `(usage, error_code)`：

    - `(值, None)`  —— 判稳成功；
    - `(None, 码)`  —— 超时或上游持续失败，调用方应保持 `settling`。

    **绝不返回 (0, None)**：读不到和真的没花钱必须能区分。
    """
    cost = settings.cost
    sleep = sleep or asyncio.sleep
    stable_count = 0
    last: float | None = None
    last_error: str | None = None
    consecutive_failures = 0

    deadline_polls = max(
        1, int(cost.settle_timeout_seconds / max(cost.poll_interval_seconds, 0.001))
    )

    for poll in range(deadline_polls):
        try:
            snapshot = await client.get_key(key_hash)
        except CostAuditError as exc:
            consecutive_failures += 1
            last_error = exc.error_code
            if not exc.retryable:
                # 认证失败之类重试无益，立刻交回调用方。
                return None, exc.error_code
            if consecutive_failures >= cost.max_retries:
                return None, exc.error_code
            await sleep(cost.poll_interval_seconds)
            continue

        consecutive_failures = 0
        usage = snapshot.usage
        if last is not None and usage == last:
            stable_count += 1
            if stable_count >= cost.stable_threshold - 1:
                logger.info(
                    "usage settled hash=%s usage=%s after %d polls",
                    key_hash,
                    usage,
                    poll + 1,
                )
                return usage, None
        else:
            stable_count = 0
        last = usage

        if poll < deadline_polls - 1:
            await sleep(cost.poll_interval_seconds)

    logger.info(
        "usage not settled within timeout hash=%s last=%s — 保持 settling", key_hash, last
    )
    return None, last_error or "settle_timeout"


async def scan_once(
    db_path: Path,
    *,
    settings: Settings,
    client: OpenRouterManagementClient | None = None,
) -> int:
    """扫描一轮 pending/settling 记录并尝试推进。

    重启恢复只需要 DB 里的 `usage_start` 与 `api_key_hash`——内存中的明文
    凭据丢了不影响读 usage（读只需 Management Key）。

    返回本轮推进的记录数。
    """
    from .audit import build_client, mark_scoring_done

    active = repo.list_active(db_path)
    if not active:
        return 0

    owns_client = client is None
    client = client or build_client(settings)
    if client is None:
        return 0

    advanced = 0
    try:
        for audit in active:
            if audit.settle_attempts >= settings.cost.max_settle_attempts:
                repo.transition(
                    db_path,
                    audit.run_id,
                    target="failed",
                    expected=("pending", "settling"),
                    error_code="settle_exhausted",
                    error_message=(
                        f"未能在 {audit.settle_attempts} 次尝试内判稳，"
                        "不回落为估算值"
                    ),
                )
                continue
            try:
                if await mark_scoring_done(
                    db_path, audit.run_id, settings=settings, client=client
                ):
                    advanced += 1
            except Exception:  # pragma: no cover - 单条失败不拖垮整轮扫描
                logger.exception("settle scan failed for run=%s", audit.run_id)
    finally:
        if owns_client:
            await client.aclose()
    return advanced


async def run_background_settler(
    db_path: Path,
    *,
    settings: Settings,
    stop_event: asyncio.Event | None = None,
) -> None:
    """后台常驻任务：按间隔扫描未结算记录。随 app lifespan 启停。"""
    if not settings.cost.enabled:
        return
    interval = settings.cost.background_scan_interval_seconds
    logger.info("run cost settler started, interval=%ss", interval)
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        if getattr(settings.cost, "legacy_run_key_enabled", False):
            try:
                # 旧 run 级审计，默认关闭。
                await scan_once(db_path, settings=settings)
            except Exception:  # pragma: no cover - 后台任务不得因单轮异常退出
                logger.exception("cost settler scan failed")
        try:
            # 逐 key 账的结算与清理：新 ledger 的未结算行、
            # 卡死的建 key 租约、禁用失败的孤儿 key 都在这里收敛。
            await scan_ledgers_once(db_path, settings=settings)
        except Exception:  # pragma: no cover
            logger.exception("cost ledger scan failed")
        try:
            # Activity 明细补齐：与结算同一节奏即可——明细本就要等
            # UTC 日结才有，早查也是空。失败不影响已定的资金口径。
            from .activity import (
                backfill_ledger_activity,
                backfill_pending_activity,
            )

            await backfill_pending_activity(db_path, settings=settings)
            await backfill_ledger_activity(db_path, settings=settings)
        except Exception:  # pragma: no cover
            logger.exception("cost activity backfill failed")
        try:
            if stop_event is not None:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                return
            await asyncio.sleep(interval)
        except asyncio.TimeoutError:
            continue


# ---------- 逐 key 账的后台收敛----------------------------------

#: 建 key 租约的最长存活。超过说明进程在「插入租约后、create_key 前」崩溃，
#: 这一行永远不会有 hash——必须被抢占，否则该 attempt 永远拿不到成本。
LEASE_STALE_SECONDS = 300.0


async def scan_ledgers_once(
    db_path: Path,
    *,
    settings: Settings,
    client: OpenRouterManagementClient | None = None,
    now: Any = None,
) -> int:
    """推进逐 key 账：结算未完成的、抢占卡死的租约、重试失败的禁用。

    重启恢复只需 DB 里的 `usage_start` 与 `api_key_hash`——内存中的明文
    凭据丢了不影响读 usage（读只需 Management Key）。
    """
    from datetime import datetime, timezone

    from . import ledger as L
    from .audit import build_client
    from .keys import settle_key

    current = now or datetime.now(timezone.utc)
    active = L.list_active(db_path)
    stale_disables = _list_failed_disables(db_path)
    if not active and not stale_disables:
        return 0

    owns = client is None
    client = client or build_client(settings)
    if client is None:
        return 0

    advanced = 0
    try:
        for row in active:
            # 卡死的租约：有行无 hash 且已超时 → 标 failed，让上层知道这笔
            # 账永远不会有金额，而不是无限停在 pending。
            if row.api_key_hash is None and row.error_code == "key_pending_creation":
                started = _parse_iso(row.started_at)
                if started and (current - started).total_seconds() > LEASE_STALE_SECONDS:
                    L.transition(
                        db_path, row.id, target="failed",
                        expected=("pending", "settling"),
                        error_code="key_creation_abandoned",
                        error_message="租约超时未完成建 key（进程可能已崩溃）",
                    )
                    advanced += 1
                continue
            # **只结算已经不再花钱的 key**：后台扫描不能碰仍在
            # 运行的 attempt / 仍有排队评分的 run——judge 请求间隔一旦超过
            # 判稳窗口，会在运行中被断 key，并把未入账的费用定稿为偏小值。
            if not _scope_finished(db_path, row):
                continue
            if row.settle_attempts >= settings.cost.max_settle_attempts:
                L.transition(
                    db_path, row.id, target="failed",
                    expected=("pending", "settling"),
                    error_code="settle_exhausted",
                    error_message=f"{row.settle_attempts} 次未判稳，不回落为估算",
                )
                advanced += 1
                continue
            try:
                if await settle_key(db_path, scope=row.scope, scope_id=row.scope_id,
                                    settings=settings, client=client):
                    advanced += 1
            except Exception:  # pragma: no cover - 单条失败不拖垮整轮
                logger.exception("settle ledger failed scope=%s id=%s",
                                 row.scope, row.scope_id)

        # 禁用失败的孤儿 key：已结算但 key 仍启用，重试禁用。
        for row in stale_disables:
            try:
                await client.disable_key(row.api_key_hash)
                L.update_fields(db_path, row.id, key_disabled=1)
                advanced += 1
            except CostAuditError:
                logger.warning("retry disable failed hash=%s", row.api_key_hash)
    finally:
        if owns:
            await client.aclose()
    return advanced


def _parse_iso(value: str | None):
    from datetime import datetime

    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _list_failed_disables(db_path: Path, *, limit: int = 100) -> list[Any]:
    """已结算但 key 未成功禁用的行——孤儿 key，还能继续花钱。"""
    from . import ledger as L
    from ..db import _open_sync

    with _open_sync(db_path) as conn:
        rows = conn.execute(
            f"SELECT {','.join(L._COLUMNS)} FROM cost_key_ledgers "
            "WHERE key_disabled=0 AND api_key_hash IS NOT NULL "
            "AND status IN ('final','upper_bound','failed') LIMIT ?",
            (limit,),
        ).fetchall()
    return [L._row_to_ledger(r) for r in rows]


def _scope_finished(db_path: Path, row: Any) -> bool:
    """该 scope 是否已经不可能再产生费用。

    - `attempt`：execution 已终止（queued/running 时 agent 还在跑）；
    - `judge`：该 run 不再有排队或进行中的 scoring job。

    判不准时返回 False（宁可晚结算，也不能断掉运行中的 key）。
    """
    from ..db import _open_sync

    try:
        with _open_sync(db_path) as conn:
            if row.scope == "attempt":
                r = conn.execute(
                    "SELECT execution_status FROM attempts WHERE id=?",
                    (row.scope_id,),
                ).fetchone()
                if r is None:
                    return True  # attempt 已不存在，不会再花钱
                return r[0] not in ("queued", "running")
            if row.scope == "judge":
                r = conn.execute(
                    "SELECT COUNT(*) FROM scoring_jobs j JOIN attempts a "
                    "ON a.id=j.attempt_id WHERE a.run_id=? "
                    "AND j.status IN ('queued','running')",
                    (row.scope_id,),
                ).fetchone()
                if r is not None and r[0]:
                    return False
                # 还有 attempt 没进终态时，评分可能尚未排队
                r2 = conn.execute(
                    "SELECT COUNT(*) FROM attempts WHERE run_id=? "
                    "AND (execution_status IN ('queued','running') "
                    "OR scoring_status IN ('queued','running'))",
                    (row.scope_id,),
                ).fetchone()
                return not (r2 and r2[0])
    except Exception:  # pragma: no cover - 判不准就不动
        logger.exception("scope finished check failed scope=%s", row.scope_id)
        return False
    return True
