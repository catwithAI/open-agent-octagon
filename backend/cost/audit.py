"""Run 成本审计的生命周期编排。

时序：

    begin_run                     创建 run key → 采 usage_start → 落库 pending
      ↓ （N 个 session 并发执行，共用同一把 key）
    mark_execution_done           execution 收敛 → 判稳 → usage_after_execution
      ↓ （judge 评分，同一把 key）
    mark_scoring_done             scoring 收敛 → 判稳 → usage_final → 禁用 key

**`usage_start` 必须在任何 agent 进程启动前落库**：它是进程重启后能重新结算的
唯一锚点。内存里的 key 明文会丢，但读 usage 只需 Management Key + key hash，
两者重启后都能重新拿到。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from ..config import Settings, resolve_management_key
from . import repo
from .credential import RunCredential, clear_credential, set_credential
from .errors import CostAuditError, KeyCreationFailed
from .models import RunCostAudit
from .openrouter import OpenRouterManagementClient

logger = logging.getLogger(__name__)

#: 走 共享网关的 agent。网关当前只能读一个全局静态 key，无法按 run 注入，
#: 因此含这些 agent 的 run 只能得到共享 key 上界。
GATEWAY_BOUND_AGENTS = frozenset({"blade-agent"})

#: judge 未走 run key 的 run。scoring 段费用不在这把 key 上，
#: 结算时不得标 final——`mark_scoring_done` 会读这里降级为 upper_bound。
#: 进程级集合：run 结束即移除，不持久化（重启后本来就要重新判定）。
_SCORING_FALLBACK_RUNS: set[str] = set()


def note_scoring_credential_fallback(run_id: str | None) -> None:
    """标记本 run 的 judge 走了既有配置而非 run key。"""
    if run_id:
        _SCORING_FALLBACK_RUNS.add(run_id)


def had_scoring_credential_fallback(run_id: str) -> bool:
    return run_id in _SCORING_FALLBACK_RUNS


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _expiry_iso(hours: float) -> str:
    """run key 的过期时间戳。

    **必须是 `Z` 结尾的 UTC**，不能用 `isoformat()` 的 `+00:00`——OpenRouter
    的 Zod 校验只认前者，后者返回 400 invalid_format（2026-07-28 实测）。
    这个差异 mock 测试发现不了，只有真实调用才会暴露。
    """
    stamp = _now() + timedelta(hours=hours)
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def run_key_name(run_id: str) -> str:
    """run key 的名称：run ID 的非敏感缩写。

    用短哈希而非 run_id 前缀——run_id 可能被当作外部可见标识，
    而 key 名称会出现在 OpenRouter 控制台里，没必要建立可直接关联的映射。
    """
    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:12]
    return f"octagon-run-{digest}"


@dataclass(frozen=True)
class BeginResult:
    """`begin_run` 的结果。`audit` 为 None 表示成本核算未启用。"""

    audit: RunCostAudit | None
    credential: RunCredential | None
    degraded_reason: str | None = None


def build_client(settings: Settings) -> OpenRouterManagementClient | None:
    """按配置构造 Management 客户端。未配 key 时返回 None（不抛错）。"""
    if not settings.cost.enabled:
        return None
    key = resolve_management_key(settings)
    if not key:
        logger.warning(
            "cost.enabled=true 但 %s 未设置，成本核算降级为不可用",
            settings.cost.management_key_env,
        )
        return None
    return OpenRouterManagementClient(
        key,
        base_url=settings.cost.base_url,
        timeout=settings.cost.request_timeout_seconds,
        max_retries=settings.cost.max_retries,
    )


def requires_shared_key(agents: Sequence[str] | None) -> bool:
    """run 是否因经 共享网关而只能拿共享 key 上界。"""
    if not agents:
        return False
    return any(a in GATEWAY_BOUND_AGENTS for a in agents)


async def begin_run(
    db_path: Path,
    run_id: str,
    *,
    settings: Settings,
    agents: Sequence[str] | None = None,
    client: OpenRouterManagementClient | None = None,
) -> BeginResult:
    """创建 run key、采 usage_start、落库 pending。

    **绝不抛错到调用方**：成本观测不该阻断实验。创建失败按
    `cost.downgrade_on_key_failure` 决定降级还是让 run 失败，
    后者也只是返回带 `degraded_reason` 的结果，由 run_service 决定怎么处理。
    """
    if not settings.cost.enabled:
        return BeginResult(audit=None, credential=None)

    owns_client = client is None
    client = client or build_client(settings)
    if client is None:
        return BeginResult(
            audit=None, credential=None, degraded_reason="management_key_missing"
        )

    try:
        if requires_shared_key(agents):
            # 网关无法按 run 注入 credential，只能记共享 key 的前后差。
            return await _begin_shared_key(db_path, run_id, settings, client)
        return await _begin_ephemeral_key(db_path, run_id, settings, client)
    finally:
        if owns_client:
            await client.aclose()


async def _begin_ephemeral_key(
    db_path: Path,
    run_id: str,
    settings: Settings,
    client: OpenRouterManagementClient,
) -> BeginResult:
    cost = settings.cost
    name = run_key_name(run_id)
    expires_at = _expiry_iso(cost.run_key_expiry_hours)

    try:
        created = await client.create_key(
            name=name, limit=cost.run_key_limit_usd, expires_at=expires_at
        )
    except CostAuditError as exc:
        logger.warning("create run key failed run=%s: %s", run_id, exc.error_code)
        if not cost.downgrade_on_key_failure:
            _record_failure(db_path, run_id, exc.error_code, str(exc))
            raise KeyCreationFailed(str(exc)) from exc
        return await _begin_shared_key(
            db_path, run_id, settings, client, reason=exc.error_code
        )

    # 新建 key 的 usage 理论上是 0，但仍然实读一次：不假设上游初值，
    # 差值计算只认实测到的起点。读失败时按 0 起步并记 error_code——
    # key 已经建出来了，不能因为读不到起点就丢掉整个 run 的成本。
    usage_start, start_error = await _sample_usage(client, created.api_key_hash)

    audit = RunCostAudit(
        run_id=run_id,
        attribution_mode="ephemeral_run_key",
        status="pending",
        started_at=_now_iso(),
        api_key_hash=created.api_key_hash,
        api_key_name=created.name,
        usage_start=usage_start,
        key_limit_usd=created.limit_usd or cost.run_key_limit_usd,
        error_code=start_error,
    )
    repo.insert_audit(db_path, audit)

    credential = RunCredential(
        run_id=run_id,
        api_key=created.api_key,
        api_key_hash=created.api_key_hash,
        base_url=_provider_base_url(settings),
        attribution_mode="ephemeral_run_key",
    )
    set_credential(credential)
    return BeginResult(audit=audit, credential=credential)


async def _begin_shared_key(
    db_path: Path,
    run_id: str,
    settings: Settings,
    client: OpenRouterManagementClient,
    *,
    reason: str | None = None,
) -> BeginResult:
    """共享 key 降级路径。

    仍然记 run 前后 usage 差，但差值包含同时发生的员工个人流量与其他机器
    流量，只能作为**上界**。不注册 run 凭据——agent 继续用既有 provider 配置。
    """
    key_hash = settings.cost.shared_key_hash
    usage_start: float | None = None
    start_error = reason
    if key_hash:
        usage_start, sample_error = await _sample_usage(client, key_hash)
        start_error = reason or sample_error

    audit = RunCostAudit(
        run_id=run_id,
        attribution_mode="shared_key_upper_bound",
        status="pending",
        started_at=_now_iso(),
        api_key_hash=key_hash,
        usage_start=usage_start,
        error_code=start_error,
    )
    repo.insert_audit(db_path, audit)
    logger.info(
        "run cost audit degraded to shared_key_upper_bound run=%s reason=%s",
        run_id,
        start_error,
    )
    return BeginResult(audit=audit, credential=None, degraded_reason=start_error)


async def _sample_usage(
    client: OpenRouterManagementClient, key_hash: str
) -> tuple[float | None, str | None]:
    """采一次累计 usage。失败返回 (None, error_code)——**不返回 0**。"""
    try:
        snapshot = await client.get_key(key_hash)
        return snapshot.usage, None
    except CostAuditError as exc:
        logger.warning("usage sample failed hash=%s: %s", key_hash, exc.error_code)
        return None, exc.error_code


def _provider_base_url(settings: Settings) -> str | None:
    """run key 配套的 base_url。

    留空则 adapter 沿用各自 provider 配置的 base_url——同一个 OpenRouter
    账号下换 key 不需要换 endpoint，且不同 agent 的 base_url 后缀不同
    （CC 要 `/api`，codex 要 `/api/v1`），统一覆盖反而会打错。
    """
    return None


def _record_failure(
    db_path: Path, run_id: str, error_code: str | None, message: str
) -> None:
    """创建失败且不降级时，仍留一条 failed 记录说明为什么没有成本。"""
    repo.insert_audit(
        db_path,
        RunCostAudit(
            run_id=run_id,
            attribution_mode="ephemeral_run_key",
            status="pending",
            started_at=_now_iso(),
            error_code=error_code,
        ),
    )
    repo.transition(
        db_path,
        run_id,
        target="failed",
        expected="pending",
        error_code=error_code,
        error_message=message[:500],
        finalized_at=_now_iso(),
    )


# ---------- checkpoint ----------------------------------------------------


async def mark_execution_done(
    db_path: Path,
    run_id: str,
    *,
    settings: Settings,
    client: OpenRouterManagementClient | None = None,
    settle: Callable[..., Any] | None = None,
) -> bool:
    """execution 阶段收敛：判稳后记 `usage_after_execution`。

    幂等由 `WHERE status='pending'` 保证——多个 attempt 先后终止会重复触发，
    只有第一个推进得动。返回是否由本次调用推进。
    """
    audit = repo.get_audit(db_path, run_id)
    if audit is None or audit.status != "pending":
        return False
    if not repo.transition(db_path, run_id, target="settling", expected="pending"):
        return False

    if audit.api_key_hash is None:
        # 共享 key 且没配 hash：没有可读的 usage，直接收成上界终态。
        repo.transition(
            db_path,
            run_id,
            target="upper_bound",
            expected="settling",
            finalized_at=_now_iso(),
        )
        return True

    from .settler import settle_usage

    settle = settle or settle_usage
    owns_client = client is None
    client = client or build_client(settings)
    if client is None:
        return True
    try:
        usage, error = await settle(client, audit.api_key_hash, settings=settings)
    finally:
        if owns_client:
            await client.aclose()

    if usage is None:
        # 未判稳：保持 settling，交给后台 settler 继续。
        repo.bump_settle_attempt(db_path, run_id)
        if error:
            repo.update_fields(db_path, run_id, error_code=error)
        return True

    execution_cost = _diff(audit.usage_start, usage)
    if execution_cost is not None and execution_cost < 0:
        _fail_regression(db_path, run_id, audit.usage_start, usage)
        return True

    repo.update_fields(
        db_path,
        run_id,
        usage_after_execution=usage,
        execution_cost_usd=execution_cost,
        last_polled_at=_now_iso(),
    )
    return True


async def mark_scoring_done(
    db_path: Path,
    run_id: str,
    *,
    settings: Settings,
    client: OpenRouterManagementClient | None = None,
    settle: Callable[..., Any] | None = None,
    scoring_credential_fallback: bool = False,
) -> bool:
    """scoring 阶段收敛：判稳后记 `usage_final` 并结算。

    `scoring_credential_fallback=True` 表示 judge 没走 run key，
    此时 scoring 段的费用没落在这把 key 上，结果**不得**标 final。
    """
    audit = repo.get_audit(db_path, run_id)
    if audit is None or audit.status not in {"pending", "settling"}:
        return False
    # 显式传入的优先；否则查本 run 评分期间是否发生过 judge 凭据回落。
    scoring_credential_fallback = scoring_credential_fallback or (
        had_scoring_credential_fallback(run_id)
    )
    if audit.status == "pending":
        if not repo.transition(db_path, run_id, target="settling", expected="pending"):
            return False
        audit = repo.get_audit(db_path, run_id) or audit

    if audit.api_key_hash is None:
        repo.transition(
            db_path,
            run_id,
            target="upper_bound",
            expected="settling",
            finalized_at=_now_iso(),
        )
        return True

    from .settler import settle_usage

    settle = settle or settle_usage
    owns_client = client is None
    client = client or build_client(settings)
    if client is None:
        return True
    try:
        usage, error = await settle(client, audit.api_key_hash, settings=settings)
        if usage is None:
            repo.bump_settle_attempt(db_path, run_id)
            if error:
                repo.update_fields(db_path, run_id, error_code=error)
            return True
        await _finalize(
            db_path,
            run_id,
            audit,
            usage,
            client=client,
            scoring_credential_fallback=scoring_credential_fallback,
        )
    finally:
        if owns_client:
            await client.aclose()
    return True


async def _finalize(
    db_path: Path,
    run_id: str,
    audit: RunCostAudit,
    usage_final: float,
    *,
    client: OpenRouterManagementClient,
    scoring_credential_fallback: bool = False,
) -> None:
    """写入最终差值、决定终态、禁用 key。"""
    # execution checkpoint 可能因为上游延迟而缺失（run 极短或结算慢），
    # 此时 scoring_cost 无法拆分，但 total 仍然可得——不能因为拆不开就丢总额。
    after_execution = audit.usage_after_execution
    total = _diff(audit.usage_start, usage_final)
    execution_cost = _diff(audit.usage_start, after_execution)
    scoring_cost = _diff(after_execution, usage_final)

    for value in (total, execution_cost, scoring_cost):
        if value is not None and value < 0:
            _fail_regression(db_path, run_id, audit.usage_start, usage_final)
            return

    if audit.attribution_mode == "shared_key_upper_bound":
        target, error_code = "upper_bound", audit.error_code
    elif scoring_credential_fallback:
        # judge 走了别的 key，scoring 段费用不在这把 key 上。
        target = "upper_bound"
        error_code = "scoring_credential_fallback"
    else:
        target, error_code = "final", audit.error_code

    disabled = await _disable_key(client, audit.api_key_hash)

    repo.transition(
        db_path,
        run_id,
        target=target,
        expected="settling",
        usage_final=usage_final,
        execution_cost_usd=execution_cost,
        scoring_cost_usd=scoring_cost,
        total_cost_usd=total,
        key_disabled=1 if disabled else 0,
        finalized_at=_now_iso(),
        error_code=error_code,
        last_polled_at=_now_iso(),
    )
    clear_credential(run_id)
    _SCORING_FALLBACK_RUNS.discard(run_id)


async def _disable_key(
    client: OpenRouterManagementClient, key_hash: str | None
) -> bool:
    """禁用 run key。失败不阻塞成本写入——已经花的钱必须记下来。"""
    if not key_hash:
        return False
    try:
        await client.disable_key(key_hash)
        return True
    except CostAuditError as exc:
        logger.warning("disable run key failed hash=%s: %s", key_hash, exc.error_code)
        return False


def _diff(start: float | None, end: float | None) -> float | None:
    """差值。任一端缺失返回 None，**不当 0 处理**。"""
    if start is None or end is None:
        return None
    return end - start


def _fail_regression(
    db_path: Path, run_id: str, start: float | None, end: float | None
) -> None:
    """累计 usage 倒退 → failed。不截断为 0、不取绝对值。"""
    message = f"usage regression: start={start} end={end}"
    logger.error("run cost audit %s: %s", run_id, message)
    repo.transition(
        db_path,
        run_id,
        target="failed",
        expected="settling",
        error_code="usage_regression",
        error_message=message,
        finalized_at=_now_iso(),
    )
    clear_credential(run_id)
