"""成本审计的数据模型与状态机。

状态机：

                        ┌──────────────────────────────► failed
                        │                                  ▲
      begin_run ──► pending ──► settling ──► final         │
                        │            │                     │
                        │            └────► upper_bound    │
                        └──────────────────────────────────┘

`final` 是终态，不可迁出——差值一旦稳定就是这次 run 的资金口径，
后续任何"修正"都意味着前面的判稳逻辑错了，应该修判稳而不是改已定的数。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, get_args

# ---------- 枚举 ----------------------------------------------------------

AuditStatus = Literal["pending", "settling", "final", "upper_bound", "failed"]

#: `ephemeral_run_key`  独占临时 key，起点已知 → 差值即实扣，可审计
#: `shared_key_upper_bound`  共享 key，差值含背景流量 → 只能当上界
#: `ephemeral_key_unbased`  独占 key **但起点未知**（建成后首读 usage 失败）。
#:     它既不 shared 也给不出上界——差值根本算不出来。单列一档，
#:     避免与"含背景流量的上界"混淆。
#: `partial_unattributed`  部分请求走了独占 key、部分走了 fallback key
#:     （租约超时、重启后明文丢失、judge 回落）。结算只能拿到独占 key 那部分，
#:     是**偏低的部分值**，既不是实扣也不是上界。
#:     `upper_bound` 只保留给**真正采到共享 key 前后差**的场景。
AttributionMode = Literal[
    "ephemeral_run_key",
    "shared_key_upper_bound",
    "ephemeral_key_unbased",
    "partial_unattributed",
]

#: 这些归因模式下拿到的金额是**部分值**，不能当总额也不能当上界。
PARTIAL_MODES: frozenset[str] = frozenset(
    {"ephemeral_key_unbased", "partial_unattributed"}
)

ATTRIBUTION_MODES: tuple[str, ...] = get_args(AttributionMode)

#: 终态：不再有出边，settler 扫描时跳过。
TERMINAL_STATUSES: frozenset[str] = frozenset({"final", "upper_bound", "failed"})

#: settler 需要继续推进的状态。
ACTIVE_STATUSES: frozenset[str] = frozenset({"pending", "settling"})

#: 唯一可作为资金真值的状态。`upper_bound` 含背景流量，`failed` 无值——
#: 两者都**不是** audited cost，UI 必须区别对待。
AUDITABLE_STATUS: str = "final"

#: 合法迁移。键是源状态，值是允许的目标状态集合。
_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"settling", "failed"}),
    "settling": frozenset({"final", "upper_bound", "failed"}),
    "final": frozenset(),
    "upper_bound": frozenset(),
    "failed": frozenset(),
}


def can_transition(source: str, target: str) -> bool:
    """是否允许从 source 迁移到 target。未知状态一律拒绝。"""
    return target in _TRANSITIONS.get(source, frozenset())


def allowed_targets(source: str) -> frozenset[str]:
    return _TRANSITIONS.get(source, frozenset())


def is_auditable(status: str, attribution_mode: str) -> bool:
    """能否作为可审计的资金真值对外呈现。

    两个条件缺一不可：状态是 `final`，且归因是独占的 run key。
    共享 key 即使差值稳定也只是上界（含员工个人流量与其他机器流量）。
    """
    return status == AUDITABLE_STATUS and attribution_mode == "ephemeral_run_key"


# ---------- 上游 API 的返回结构 -------------------------------------------


@dataclass(frozen=True)
class CreatedKey:
    """Management API 创建 key 的结果。

    `api_key` 是**明文**，只在内存中流转：不进 DB、不进日志、不进
    `external_refs_json`、不进 wire 记录、不进 API 响应。
    `__repr__` 已抑制，防止无意中被格式化进日志。
    """

    api_key: str
    api_key_hash: str
    name: str
    limit_usd: float | None = None
    expires_at: str | None = None

    def __repr__(self) -> str:  # pragma: no cover - 防泄漏，行为即定义
        return (
            f"CreatedKey(name={self.name!r}, hash={self.api_key_hash!r}, "
            f"limit_usd={self.limit_usd!r})"
        )

    __str__ = __repr__


@dataclass(frozen=True)
class KeySnapshot:
    """某一时刻 key 的用量快照。

    `usage` 是**累计**实扣，不在 UTC 零点清零——这是差值计算的唯一依据。
    `usage_daily` / `usage_weekly` / `usage_monthly` 只供运营报表，
    **不得**参与单次 run 结算（跨天 run 会因清零算出负数）。
    """

    api_key_hash: str
    usage: float
    limit_usd: float | None = None
    limit_remaining: float | None = None
    usage_daily: float | None = None
    usage_weekly: float | None = None
    usage_monthly: float | None = None
    disabled: bool | None = None
    created_at: str | None = None
    expires_at: str | None = None


@dataclass(frozen=True)
class ActivityRow:
    """Activity API 的一行明细（按日期 × 模型 × provider 聚合）。

    注意 Activity 面向最近 30 个**已完成的 UTC 日期**，非实时。
    另外它**不提供** cache read / cache write 两维，那两维只能靠
    `backend/adapters/token_usage.py` 的本地观测。
    """

    date: str
    model: str | None = None
    model_permaslug: str | None = None
    provider: str | None = None
    endpoint: str | None = None
    usage: float | None = None
    requests: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "model": self.model,
            "model_permaslug": self.model_permaslug,
            "provider": self.provider,
            "endpoint": self.endpoint,
            "usage": self.usage,
            "requests": self.requests,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }


# ---------- 审计记录 ------------------------------------------------------


@dataclass
class RunCostAudit:
    """`run_cost_audits` 一行的内存表示。

    三个 `usage_*` 是采样点，三个 `*_cost_usd` 是它们的差值。差值不在读取时
    重算而是落库：判稳发生在特定时刻，事后重算可能读到已经继续增长的 usage。
    """

    run_id: str
    attribution_mode: AttributionMode
    status: AuditStatus
    started_at: str
    provider: str = "openrouter"
    api_key_hash: str | None = None
    api_key_name: str | None = None
    usage_start: float | None = None
    usage_after_execution: float | None = None
    usage_final: float | None = None
    execution_cost_usd: float | None = None
    scoring_cost_usd: float | None = None
    total_cost_usd: float | None = None
    key_limit_usd: float | None = None
    activity: list[dict[str, Any]] | None = None
    key_disabled: bool | None = None
    settle_attempts: int = 0
    last_polled_at: str | None = None
    finalized_at: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    #: 非持久化字段，仅供 API 层组装响应。
    extra: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def auditable(self) -> bool:
        return is_auditable(self.status, self.attribution_mode)

    @property
    def budget_consumed_ratio(self) -> float | None:
        """预算消耗率 = 实扣 / key limit。无 limit 时返回 None，不返回 0。"""
        if self.total_cost_usd is None or not self.key_limit_usd:
            return None
        return self.total_cost_usd / self.key_limit_usd

    def to_dict(self) -> dict[str, Any]:
        """序列化。**不含**任何 key 明文——本对象根本不持有明文。"""
        return {
            "run_id": self.run_id,
            "provider": self.provider,
            "api_key_hash": self.api_key_hash,
            "api_key_name": self.api_key_name,
            "attribution_mode": self.attribution_mode,
            "status": self.status,
            "auditable": self.auditable,
            "usage_start": self.usage_start,
            "usage_after_execution": self.usage_after_execution,
            "usage_final": self.usage_final,
            "execution_cost_usd": self.execution_cost_usd,
            "scoring_cost_usd": self.scoring_cost_usd,
            "total_cost_usd": self.total_cost_usd,
            "key_limit_usd": self.key_limit_usd,
            "budget_consumed_ratio": self.budget_consumed_ratio,
            "key_disabled": self.key_disabled,
            "settle_attempts": self.settle_attempts,
            "last_polled_at": self.last_polled_at,
            "started_at": self.started_at,
            "finalized_at": self.finalized_at,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }
