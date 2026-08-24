"""Trajectory 独立模型。

`trajectory.json` 的正式 schema，独立于任何具体 normalizer——此前 schema
"恰好长在" `claude_code.py` 里（`_Step`/`_step_dict`），codex 靠 import 同一
份代码维持一致；本模块把它转正为独立契约，CC/Codex/Blade 三个 normalizer
都产出本模块的 `Trajectory` 实例。

两层校验，语义不同、处理方式不同：

- **构造期类型/取值校验**（`__post_init__`，全部字段）：失败抛 ``ValueError``
  ——这是 normalizer 自身的编程错误（programmer error），**不走 fail-open**，
  异常穿过 ``run_native_normalizer()`` 向上传播：在线由 lifecycle 的
  ``except Exception`` 兜底记 ``native_normalize_failed``，离线 rebuild
  fail-fast。
- **结构校验**（`validate()`，sequence 连续性、tool_call 引用自洽）：失败
  返回错误列表不抛异常——这是 producer 原始事件的语义异常（如断线导致
  结果事件丢失），走 fail-open：错误计入 capture_event，`trajectory.json`
  照常写出全部已产出 step。

`step_id` 与 `sequence` 的语义分工：`step_id` 是字符串稳定 ID
（`ids.trajectory_step_id()` 派生，跨重跑幂等），`sequence` 是整数序号
（1 起连续递增，仅表达 trajectory 内部相对顺序）——两者是不同维度，不要求
互相可推导。

写盘用 `trajectory_to_dict()`，**不要**用裸 ``dataclasses.asdict()``：后者
会把仅供校验分发的 `producer` 序列化进文件、给每个 step 带出多余的
``"attributes": null``，破坏与迁移前产物的逐字节一致。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

TRAJECTORY_SCHEMA_VERSION = "octagon-trajectory-v1"

_VALID_PRODUCERS = frozenset({
    "claude-code", "codex", "blade-agent",
    # opencode 与其 fork mimo：工具调用与结果打包在同一个 part（同 codex），
    # normalizer 不产独立的 tool_result step，故 _check_tool_call_references
    # 的默认分支（不配对）对它们同样正确。
    "opencode", "mimo-code",
    # dsh：有独立的 tool/result 事件且带 callId，与 CC 同构，
    # 故走 _check_cc_tool_call_pairing（见 _check_tool_call_references）。
    "dsh",
})


def _req_str(name: str, value: Any) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} 必须是非空字符串: {value!r}")


def _opt_str(name: str, value: Any) -> None:
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{name} 必须是 str 或 None: {value!r}")


@dataclass(frozen=True)
class TrajectoryStep:
    """归一化后的单个 agent 行为步骤。

    `attributes` 是唯一允许动态 key 的扩展位，承载 adapter
    特有业务语义（如 blade skill 的 skill_id/skill_tool/skill_args、
    tool_call_id_source、parent_agent_resolution）；只记录能从 producer
    原始事件直接读到的信息，不为对齐显示改写 `tool_name` 等证据字段。
    """

    step_id: str
    sequence: int
    timestamp: str | None
    agent_id: str
    parent_agent_id: str | None
    kind: str
    producer_event_refs: tuple[dict[str, Any], ...]
    tool_call_id: str | None = None
    tool_name: str | None = None
    logical_call_id: str | None = None
    content_hash: str | None = None
    content_bytes: int | None = None
    attributes: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        # 类型/取值校验覆盖全部字段（只查几个关键字段不能自称
        # "类型级严格模型"）。失败=programmer error，直接抛。
        _req_str("step_id", self.step_id)
        _req_str("agent_id", self.agent_id)
        _req_str("kind", self.kind)
        _opt_str("parent_agent_id", self.parent_agent_id)
        _opt_str("timestamp", self.timestamp)
        _opt_str("tool_call_id", self.tool_call_id)
        _opt_str("tool_name", self.tool_name)
        _opt_str("logical_call_id", self.logical_call_id)
        _opt_str("content_hash", self.content_hash)
        if (
            not isinstance(self.sequence, int)
            or isinstance(self.sequence, bool)
            or self.sequence < 1
        ):
            raise ValueError(f"sequence 必须是 >=1 的 int: {self.sequence!r}")
        if self.content_bytes is not None and (
            not isinstance(self.content_bytes, int)
            or isinstance(self.content_bytes, bool)
            or self.content_bytes < 0
        ):
            raise ValueError(
                f"content_bytes 必须是非负 int 或 None: {self.content_bytes!r}"
            )
        if not isinstance(self.producer_event_refs, tuple) or not all(
            isinstance(r, dict) for r in self.producer_event_refs
        ):
            raise ValueError("producer_event_refs 必须是 dict 组成的 tuple")
        if self.attributes is not None and not isinstance(self.attributes, dict):
            raise ValueError(
                f"attributes 非 None 时必须是 dict: {type(self.attributes)!r}"
            )


@dataclass(frozen=True)
class Trajectory:
    """一次 attempt 的完整归一化轨迹。

    `producer` 仅用于 `validate()` 按 producer 分发校验规则，**不写入
    trajectory.json**（文件顶层保持 schema_version/attempt_id/steps 三字段
    不变）——若未来要在文件里追溯 producer，那是一次独立的 schema 版本升级。
    """

    schema_version: str
    attempt_id: str
    steps: tuple[TrajectoryStep, ...]
    producer: str

    def __post_init__(self) -> None:
        if self.schema_version != TRAJECTORY_SCHEMA_VERSION:
            raise ValueError(f"未知 schema_version: {self.schema_version!r}")
        _req_str("attempt_id", self.attempt_id)
        if self.producer not in _VALID_PRODUCERS:
            raise ValueError(f"未知 producer: {self.producer!r}")
        if not isinstance(self.steps, tuple) or not all(
            isinstance(s, TrajectoryStep) for s in self.steps
        ):
            raise ValueError("steps 必须是 TrajectoryStep 组成的 tuple")
        seen_ids = [s.step_id for s in self.steps]
        if len(seen_ids) != len(set(seen_ids)):
            raise ValueError("step_id 在同一 trajectory 内必须唯一")

    def validate(self) -> list[str]:
        """结构校验。返回问题列表，空列表=通过，不抛异常。

        字段级/类型级校验已在 `__post_init__` 完成（通不过在构造阶段就抛
        ValueError）；这里只查"字段合法但语义上有结构问题"的情况，调用方
        按 fail-open 处理（记 capture_event、照常写盘）。
        """
        errors: list[str] = []
        errors.extend(_check_step_sequence(self.steps))
        errors.extend(_check_tool_call_references(self.steps, self.producer))
        return errors


def _check_step_sequence(steps: tuple[TrajectoryStep, ...]) -> list[str]:
    """通用规则：sequence 必须从 1 连续递增。"""
    expected = 1
    errors: list[str] = []
    for s in steps:
        if s.sequence != expected:
            errors.append(
                f"sequence 不连续: 期望 {expected}, 实得 {s.sequence}"
                f" (step_id={s.step_id})"
            )
        expected = s.sequence + 1
    return errors


def _check_tool_call_references(
    steps: tuple[TrajectoryStep, ...], producer: str
) -> list[str]:
    """按 producer 分发，不是单一通用规则。

    codex 的 kind 集合里没有独立的 "tool_result"（一次 item.completed 既是
    发起也是结果），套 CC 的配对规则会产生假失败——天然跳过，不特判。
    """
    if producer in ("claude-code", "dsh"):
        # dsh 与 CC 同构：tool/result 是独立事件且带 callId，配得上对。
        # codex/opencode 把调用与结果打包在同一事件里，套这条会假失败。
        return _check_cc_tool_call_pairing(steps)
    if producer == "blade-agent":
        return _check_blade_tool_call_pairing(steps)
    return []


def _check_cc_tool_call_pairing(steps: tuple[TrajectoryStep, ...]) -> list[str]:
    """CC 专属：tool_result 的 tool_call_id 必须能在同一 trajectory 中找到
    kind=tool_call 的发起 step。"""
    seen = {s.tool_call_id for s in steps if s.tool_call_id and s.kind == "tool_call"}
    return [
        f"tool_result 引用了不存在的 tool_call_id: {s.tool_call_id}"
        for s in steps
        if s.kind == "tool_result" and s.tool_call_id not in seen
    ]


def _check_blade_tool_call_pairing(steps: tuple[TrajectoryStep, ...]) -> list[str]:
    """blade 专属：tool_call_id 唯一性范围是同一 agent_id（loop）内，
    不同 loop 间允许重复——先按 agent_id 分组再比对，跨 agent 全局比对会在
    并行 fork 场景产生大量误报孤儿。agent_id 总是真实 loop_name（父子关系
    解析失败也不改写它），分组不受解析成败影响。"""
    errors: list[str] = []
    by_agent: dict[str, list[TrajectoryStep]] = {}
    for s in steps:
        by_agent.setdefault(s.agent_id, []).append(s)
    for agent_id, agent_steps in by_agent.items():
        seen = {
            s.tool_call_id
            for s in agent_steps
            if s.tool_call_id and s.kind == "tool_call"
        }
        errors.extend(
            f"[{agent_id}] tool_result 引用了不存在的 tool_call_id: {s.tool_call_id}"
            for s in agent_steps
            if s.kind == "tool_result" and s.tool_call_id not in seen
        )
    return errors


# ---------- 写盘序列化（不用裸 dataclasses.asdict） -----------------------


def trajectory_step_to_dict(s: TrajectoryStep) -> dict[str, Any]:
    """step 写盘序列化。`attributes` 为 None 时整个 key 不出现（不是
    ``"attributes": null``）——CC/Codex 现状产出只有 11 个固定字段，多出的
    key 即使值为 null 也是文件格式变化，破坏逐字节回归。"""
    result: dict[str, Any] = {
        "step_id": s.step_id,
        "sequence": s.sequence,
        "timestamp": s.timestamp,
        "agent_id": s.agent_id,
        "parent_agent_id": s.parent_agent_id,
        "kind": s.kind,
        "producer_event_refs": [dict(r) for r in s.producer_event_refs],
        "tool_call_id": s.tool_call_id,
        "tool_name": s.tool_name,
        "logical_call_id": s.logical_call_id,
        "content_hash": s.content_hash,
        "content_bytes": s.content_bytes,
    }
    if s.attributes is not None:
        result["attributes"] = s.attributes
    return result


def trajectory_to_dict(t: Trajectory) -> dict[str, Any]:
    """trajectory 写盘序列化：只导出现有三个顶层字段，`producer` 不落盘。"""
    return {
        "schema_version": t.schema_version,
        "attempt_id": t.attempt_id,
        "steps": [trajectory_step_to_dict(s) for s in t.steps],
    }


def empty_trajectory(attempt_id: str) -> dict[str, Any]:
    """raw 缺失时的空 trajectory dict（形状与迁移前 _empty_trajectory 一致）。"""
    return {
        "schema_version": TRAJECTORY_SCHEMA_VERSION,
        "attempt_id": attempt_id,
        "steps": [],
    }
