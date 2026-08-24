"""逐 key 成本核算的生命周期（revision-per-attempt-key）。

与旧的 `audit.py`（run 级一把 key）并存但不互相调用：新链路走这里，
`audit.py` 保留只为读历史记录。

时序：

    attempt 启动前  → open_key(scope='attempt', scope_id=attempt_id)
    judge 启动前    → open_key(scope='judge',   scope_id=run_id)   一个 run 一把
    各自结束后      → settle_key(...)  判稳 → cost_usd = usage_final - usage_start

**成本直接取自上游**，不经过 `compute_cost`。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from ..config import Settings
from . import ledger as L
from .audit import GATEWAY_BOUND_AGENTS, build_client, requires_shared_key
from .credential import (
    RunCredential,
    clear_credential,
    get_credential,
    set_credential,
)
from .errors import CostAuditError
from .openrouter import OpenRouterManagementClient

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _expiry_iso(hours: float) -> str:
    """OpenRouter 只认 `Z` 结尾 UTC，`isoformat()` 的 `+00:00` 返回 400。"""
    return (_now() + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def key_name(scope: str, scope_id: str) -> str:
    """key 名称：scope + ID 短哈希。

    不直接嵌 attempt_id——它是外部可见标识，而 key 名会出现在 OpenRouter
    控制台，没必要建立可直接关联的映射。
    """
    digest = hashlib.sha256(f"{scope}:{scope_id}".encode()).hexdigest()[:12]
    return f"octagon-{scope}-{digest}"


async def open_key(
    db_path: Path,
    *,
    run_id: str,
    scope: L.LedgerScope,
    scope_id: str,
    settings: Settings,
    agent_name: str | None = None,
    agents: Sequence[str] | None = None,
    client: OpenRouterManagementClient | None = None,
) -> L.CostKeyLedger | None:
    """为一个 scope 建独立 key 并记起点。返回账行；未启用时返回 None。

    **绝不抛错到调用方**：成本观测不该阻断实验。任何失败都降级为
    `shared_key_upper_bound` 并继续。
    """
    if not settings.cost.per_attempt_enabled:
        return None

    existing = L.get_by_scope(db_path, scope, scope_id)
    if existing is not None:
        return await _await_usable(db_path, scope, scope_id, existing)

    # **先占租约再调外部 API**：原实现是「查 DB 不存在 → create_key
    # → INSERT OR IGNORE」，两个并发调用会各自向 OpenRouter 建出一把**真实
    # key**，唯一索引只挡住第二行落库——输的那一方的 key 无人记账、无人禁用，
    # 变成花钱的孤儿。先用唯一索引抢占位行，抢不到的直接返回赢家的行。
    lease = L.CostKeyLedger(
        run_id=run_id,
        scope=scope,
        scope_id=scope_id,
        agent_name=agent_name,
        attribution_mode="ephemeral_run_key",
        status="pending",
        started_at=_now_iso(),
        error_code="key_pending_creation",
    )
    if not L.insert_ledger(db_path, lease):
        # 竞争失败：**必须等赢家把凭据注册好**，否则本调用方
        # 会拿着只有占位行的账直接开跑，凭据回落到旧 key 或 provider key，
        # 而账最后仍显示 final——钱记错了地方。
        winner = L.get_by_scope(db_path, scope, scope_id)
        return await _await_usable(db_path, scope, scope_id, winner)

    owns = client is None
    client = client or build_client(settings)
    if client is None:
        L.update_fields(db_path, lease.id, error_code="management_key_missing")
        return lease

    try:
        # 经 共享网关的 agent 无法按 attempt 注入 credential，只能记上界。
        gateway_bound = (
            agent_name in GATEWAY_BOUND_AGENTS
            if agent_name
            else requires_shared_key(agents)
        )
        if gateway_bound:
            return _degrade(db_path, lease, reason="gateway_bound_agent")

        cost = settings.cost
        try:
            created = await client.create_key(
                name=key_name(scope, scope_id),
                limit=cost.key_limit_for(scope),
                expires_at=_expiry_iso(cost.run_key_expiry_hours),
            )
        except CostAuditError as exc:
            logger.warning("create key failed %s=%s: %s", scope, scope_id, exc.error_code)
            if not cost.downgrade_on_key_failure:
                raise
            return _degrade(db_path, lease, reason=exc.error_code)

        # 新 key 的 usage 理论为 0，但仍实读一次——不假设上游初值。
        usage_start, err = await _sample(client, created.api_key_hash)
        if usage_start is None:
            # **起点读不到就不能标可审计**：没有起点就算不出差值，
            # 结算时会得到 cost_usd=NULL 却状态 final 的"可审计但没有金额"记录。
            # 降级为上界——key 照建照用（钱已经要花了），只是这笔账不可审计。
            # 这把 key 是**独占**的，只是起点未知——既不 shared，也给不出
            # 上界（差值根本算不出）。单列 ephemeral_key_unbased，
            # 结算时直接判 failed，不冒充任何口径。
            logger.warning(
                "usage_start unavailable %s=%s: %s — 标记 unbased",
                scope, scope_id, err,
            )
            L.update_fields(
                db_path, lease.id,
                attribution_mode="ephemeral_key_unbased",
                api_key_hash=created.api_key_hash,
                api_key_name=created.name,
                key_limit_usd=created.limit_usd or cost.key_limit_for(scope),
                error_code=err or "usage_start_unavailable",
            )
        else:
            # 等待方可能已把这行降级（超时）——不要改回可审计。
            latest = L.get_by_scope(db_path, scope, scope_id)
            if latest is not None and latest.attribution_mode != "ephemeral_run_key":
                L.update_fields(
                    db_path, lease.id,
                    api_key_hash=created.api_key_hash,
                    api_key_name=created.name,
                    usage_start=usage_start,
                )
                set_credential(RunCredential(
                    run_id=scope_id, api_key=created.api_key,
                    api_key_hash=created.api_key_hash,
                    attribution_mode="shared_key_upper_bound",
                ))
                return L.get_by_scope(db_path, scope, scope_id)
            L.update_fields(
                db_path, lease.id,
                api_key_hash=created.api_key_hash,
                api_key_name=created.name,
                usage_start=usage_start,
                key_limit_usd=created.limit_usd or cost.key_limit_for(scope),
                error_code=None,
            )

        # 凭据按 scope 登记：attempt 用 attempt_id 作键，judge 用 run_id。
        set_credential(
            RunCredential(
                run_id=scope_id,
                api_key=created.api_key,
                api_key_hash=created.api_key_hash,
                attribution_mode=(
                    "ephemeral_run_key" if usage_start is not None
                    else "ephemeral_key_unbased"
                ),
            )
        )
        return L.get_by_scope(db_path, scope, scope_id)
    finally:
        if owns:
            await client.aclose()


def _degrade(
    db_path: Path, lease: L.CostKeyLedger, *, reason: str | None
) -> L.CostKeyLedger:
    """把已占的租约行降级为共享 key 上界。不建独占 key，终态 `upper_bound`。"""
    L.update_fields(
        db_path,
        lease.id,
        attribution_mode="shared_key_upper_bound",
        error_code=reason,
    )
    logger.info(
        "cost key degraded %s=%s reason=%s", lease.scope, lease.scope_id, reason
    )
    return L.get_by_scope(db_path, lease.scope, lease.scope_id) or lease


async def _sample(
    client: OpenRouterManagementClient, key_hash: str
) -> tuple[float | None, str | None]:
    """读一次累计 usage。失败返回 (None, code)——**不返回 0**。"""
    try:
        snap = await client.get_key(key_hash)
        return snap.usage, None
    except CostAuditError as exc:
        logger.warning("usage sample failed hash=%s: %s", key_hash, exc.error_code)
        return None, exc.error_code


async def settle_key(
    db_path: Path,
    *,
    scope: str,
    scope_id: str,
    settings: Settings,
    client: OpenRouterManagementClient | None = None,
) -> bool:
    """结算一把 key：判稳 → 写 cost_usd → 禁用 key → 清凭据。

    返回是否由本次调用推进。幂等——重复调用只有第一个赢。
    """
    row = L.get_by_scope(db_path, scope, scope_id)
    if row is None or row.status not in ("pending", "settling"):
        return False
    if row.status == "pending":
        # pending → settling 只能有一个赢家；输了说明别人已在推进。
        if not L.transition(db_path, row.id, target="settling", expected="pending"):
            return False
        # **迁移后必须重读**：上面那份 row 是迁移前的快照。open_key 可能在
        # 本次读取之后才把 usage_start / hash 补上（竞争、重启恢复路径），
        # 用陈旧快照会让 `cost = usage - None` 判成 None，最终写出
        # 「final 但 cost_usd 为空」的可审计空账。
        row = L.get_by_scope(db_path, scope, scope_id) or row
    # status 已是 settling（本次刚迁或后台 settler 重试）——直接继续判稳。

    if row.api_key_hash is None:
        # 共享 key 降级路径：没有可读的 usage，直接收成上界终态。
        L.transition(db_path, row.id, target="upper_bound", expected="settling",
                     finalized_at=_now_iso())
        clear_credential(scope_id)
        return True

    from .settler import settle_usage

    owns = client is None
    client = client or build_client(settings)
    if client is None:
        return False
    try:
        usage, err = await settle_usage(client, row.api_key_hash, settings=settings)
        if usage is None:
            # 未判稳：保持 settling，交后台 settler，绝不写 0
            L.bump_settle_attempt(db_path, row.id)
            if err:
                L.update_fields(db_path, row.id, error_code=err)
            return True

        cost = None if row.usage_start is None else usage - row.usage_start
        if cost is not None and cost < 0:
            # 累计 usage 倒退 = 数据异常，不截断为 0、不取绝对值
            L.transition(db_path, row.id, target="failed", expected="settling",
                         error_code="usage_regression",
                         error_message=f"start={row.usage_start} final={usage}",
                         finalized_at=_now_iso())
            clear_credential(scope_id)
            return True

        disabled = await _disable(client, row.api_key_hash)
        if cost is None and row.attribution_mode == "ephemeral_run_key":
            # 兜底：算不出金额就绝不标 final。`final` 的含义是"这是本 scope
            # 的资金真值"，NULL 金额的可审计记录会让聚合把它当 0 计入总额。
            L.transition(
                db_path, row.id, target="failed", expected="settling",
                usage_final=usage, key_disabled=1 if disabled else 0,
                error_code="cost_unavailable",
                error_message=f"起点缺失，无法算出差值（usage_final={usage}）",
                finalized_at=_now_iso(), last_polled_at=_now_iso(),
            )
            clear_credential(scope_id)
            return True
        if row.attribution_mode == "ephemeral_key_unbased":
            # 起点未知 → 差值不存在。不标 final（会是 NULL 金额的可审计记录），
            # 也不标 upper_bound（这把 key 不 shared，谈不上上界）。
            L.transition(
                db_path, row.id, target="failed", expected="settling",
                usage_final=usage, key_disabled=1 if disabled else 0,
                error_code="usage_start_unavailable",
                error_message="起点未知，无法算出差值",
                finalized_at=_now_iso(), last_polled_at=_now_iso(),
            )
            clear_credential(scope_id)
            return True
        target = ("final" if row.attribution_mode == "ephemeral_run_key"
                  else "upper_bound")
        L.transition(
            db_path, row.id, target=target, expected="settling",
            usage_final=usage, cost_usd=cost,
            key_disabled=1 if disabled else 0,
            finalized_at=_now_iso(), last_polled_at=_now_iso(),
        )
        clear_credential(scope_id)
        return True
    finally:
        if owns:
            await client.aclose()


async def _disable(client: OpenRouterManagementClient, key_hash: str) -> bool:
    """禁用 key。失败不阻塞成本写入——已经花的钱必须记下来。"""
    try:
        await client.disable_key(key_hash)
        return True
    except CostAuditError as exc:
        logger.warning("disable key failed hash=%s: %s", key_hash, exc.error_code)
        return False


#: 等赢家完成建 key 的上限。超过说明赢家崩了，由 settler 的租约超时收敛。
_LEASE_WAIT_SECONDS = 30.0
_LEASE_POLL_SECONDS = 0.2


async def _await_usable(
    db_path: Path,
    scope: str,
    scope_id: str,
    row: L.CostKeyLedger | None,
) -> L.CostKeyLedger | None:
    """等这一行变成「可用」：凭据已注册，或已确定降级/失败。

    两种要处理的情形：

    1. **并发竞争的输家**：赢家还在调 create_key，行里只有占位符。
       直接返回会让调用方拿不到凭据就开跑。
    2. **进程重启**：行和 hash 都在，但明文 key 不可能从 DB 恢复。
       此时凭据永远不会出现——不能无限等，返回并让调用方降级
       （用户已确认：降级继续跑，实验数据比成本数据重要）。
    """
    import asyncio

    if row is None:
        return None
    # 已经是终态或已降级：不必等
    if row.status not in ("pending", "settling"):
        return row
    if row.attribution_mode != "ephemeral_run_key":
        return row
    if get_credential(scope_id) is not None:
        return row

    waited = 0.0
    while waited < _LEASE_WAIT_SECONDS:
        current = L.get_by_scope(db_path, scope, scope_id)
        if current is None:
            return None
        if get_credential(scope_id) is not None:
            return current
        if current.attribution_mode != "ephemeral_run_key":
            return current  # 赢家降级了
        if current.api_key_hash and current.error_code != "key_pending_creation":
            # 有 hash 但没凭据 = 重启后明文已丢。不再等——标记降级让调用方
            # 回落 provider key 继续跑，账不冒充可审计。
            # 重启后明文丢失：后续请求走 fallback key，本行只能结算独占 key
            # 已花的部分——**偏低的部分值**，不是上界。
            L.update_fields(
                db_path, current.id,
                attribution_mode="partial_unattributed",
                error_code="credential_lost_after_restart",
            )
            logger.warning(
                "credential lost after restart %s=%s — 降级继续跑", scope, scope_id
            )
            return L.get_by_scope(db_path, scope, scope_id)
        await asyncio.sleep(_LEASE_POLL_SECONDS)
        waited += _LEASE_POLL_SECONDS

    # **超时不能原样返回**：调用方会拿着仍是占位符的账
    # 用 fallback key 开跑，而赢家稍后可能把账补成 final——那笔费用不在
    # 账里。原子地降级为不可审计，赢家之后的 update 也不会把它改回可审计
    # （open_key 只在 attribution_mode 仍是 ephemeral_run_key 时写起点）。
    logger.warning("wait for key creation timed out %s=%s — 降级不可审计",
                   scope, scope_id)
    L.update_fields(
        db_path, row.id,
        attribution_mode="partial_unattributed",
        error_code="key_creation_wait_timeout",
    )
    return L.get_by_scope(db_path, scope, scope_id)
