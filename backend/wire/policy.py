"""Capture policy 四档与 effective policy 计算。

四档从松到严的信息量排序：

    off < metadata < parsed < full

effective policy 取所有约束的最严格交集（最小档）：

    server maximum policy   —— 部署方硬上限，客户端不能请求超过它
    task requested policy   —— task 配置
    run requested policy    —— 单次 run 覆盖
    source capability       —— source 实际能力（如 metadata-only 的 sidecar）

默认 `metadata`——真实用户例子不落 payload；专用 benchmark 显式提升。
降档原因（哪个约束把 requested 压下来）需要透出到 manifest 的
``policy.downgrade_reason``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

CapturePolicy = Literal["off", "metadata", "parsed", "full"]

# 档位顺序即信息量顺序；index 越小越严格。
POLICY_ORDER: tuple[str, ...] = ("off", "metadata", "parsed", "full")

DEFAULT_POLICY: CapturePolicy = "metadata"


def policy_rank(policy: str) -> int:
    """档位 → 序数。未知档位直接报错，不静默当 off（配置错要暴露）。"""
    try:
        return POLICY_ORDER.index(policy)
    except ValueError:
        raise ValueError(f"未知 capture policy: {policy!r}，合法值 {POLICY_ORDER}") from None


def strictest(*policies: str | None) -> CapturePolicy | None:
    """取最严格（最小档）交集；None 表示该约束未指定、不参与。全 None 返回 None。"""
    present = [p for p in policies if p is not None]
    if not present:
        return None
    return min(present, key=policy_rank)  # type: ignore[return-value]


@dataclass(frozen=True)
class EffectivePolicy:
    """manifest ``policy`` 字段的来源。

    ``requested`` 是 task/run 想要的档；``effective`` 是被 server 上限和
    source capability 压完之后真正执行的档；二者不等时 ``downgrade_reason``
    指出是哪个约束压的。
    """

    requested: CapturePolicy
    effective: CapturePolicy
    downgrade_reason: str | None = None


def resolve_effective_policy(
    *,
    server_max: str | None = None,
    task_requested: str | None = None,
    run_requested: str | None = None,
    source_capability: str | None = None,
) -> EffectivePolicy:
    """计算 effective policy。

    requested = task/run 中更严格的一个（run 是覆盖但只能收紧，不能放宽——
    放宽会绕过 task 层的约束）；未指定时用 DEFAULT_POLICY。
    effective = requested 再与 server_max、source_capability 取最严格交集。
    """
    # 先各自校验，坏配置立即报错。
    for p in (server_max, task_requested, run_requested, source_capability):
        if p is not None:
            policy_rank(p)

    requested = strictest(task_requested, run_requested) or DEFAULT_POLICY

    effective = requested
    downgrade_reason: str | None = None
    # 按固定顺序应用约束，记录第一个真正造成降档的。
    for constraint, name in ((server_max, "server_max"), (source_capability, "source_capability")):
        if constraint is not None and policy_rank(constraint) < policy_rank(effective):
            effective = constraint  # type: ignore[assignment]
            downgrade_reason = name

    return EffectivePolicy(
        requested=requested,  # type: ignore[arg-type]
        effective=effective,  # type: ignore[arg-type]
        downgrade_reason=downgrade_reason,
    )
