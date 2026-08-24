"""压缩评测 summary：五种状态判定。

**唯一的状态判定职责方**。detector 只产出 ``context_compaction`` record
列表（可为空），本模块把「record 列表 + manifest（压力材料是否覆盖声明窗口）+
observability completeness（call boundary/usage/session/identity 证据是否齐全）+
capability gaps（aggregate-only、unattributed、session broken 等）」综合成一个
可对外声明的 ``compaction_status``，并汇总 count/scope/retention/task score/
completeness/limitations。

状态定义：

| 状态 | 含义 |
|---|---|
| ``observed`` | 有 canonical compaction evidence（record 非空） |
| ``not_observed_under_budget`` | 证据完整且压力超过声明窗口，仍未见压缩 |
| ``unsupported`` | 缺逐调用 usage / identity / session continuity（证据不足） |
| ``incomplete`` | 采集/解析失败，不能作结论 |
| ``insufficient_calls`` | 同一 agent/session 内不足两个可比较 calls |

**核心原则**：
- **不把未触发自动解释为不支持**——没 record 不等于 unsupported，更不等于「agent
  不会压缩」。只有证据覆盖完整且压力**超过声明窗口**仍未触发，才可报
  ``not_observed_under_budget``；证据不足是 ``unsupported``；采集失败是
  ``incomplete``；材料没压到声明窗口是 ``incomplete``（覆盖不足，不能下结论）；
- **有 record → observed**，completeness 撤销不了这个结论：真实检出
  的压缩是正向事实，元数据不齐只降 completeness 标注、进 limitations，不翻案成
  unsupported。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

CompactionStatus = Literal[
    "observed",
    "not_observed_under_budget",
    "unsupported",
    "incomplete",
    "insufficient_calls",
]

# observability completeness 三档（output 字段）。
Completeness = Literal["complete", "partial", "incomplete"]

AgentScope = Literal["main", "subagent", "mixed", "none"]


@dataclass(frozen=True)
class EvaluationInputs:
    """判定输入（由上层从 manifest/completeness/capability 映射，纯数据）。

    刻意用扁平的显式字段而非整个 manifest：让状态判定是可 table-test 的纯函数，
    manifest→inputs 的映射（哪个 source capability 对应 aggregate_only 等）由调用方
    负责、单独测。
    """

    # detector 的返回值（context_compaction record 列表，可空）。
    compaction_records: list[dict[str, Any]] = field(default_factory=list)

    # ---- 判定信号（都来自 manifest / capability / completeness）----
    # 采集或解析失败：capture finalize 失败、source failed、spool 截断到无法判定。
    # True → 不能作结论（incomplete 最高优先，压过其它）。
    collection_failed: bool = False
    # 逐调用 usage 不可得（Codex aggregate-only）：无法比较相邻 call token 曲线。
    aggregate_only: bool = False
    # 子 agent identity 不可归属（unattributed calls）：不能用于子 agent 压缩结论。
    identity_unattributed: bool = False
    # session 连续性破裂（多 session ID / resume mismatch）：相邻段不可比较。
    session_broken: bool = False
    # 同一 agent/session 内**可比较** conversational calls 的最大段内数量。
    # < 2 → insufficient_calls（无相邻对可判定）。
    max_comparable_calls: int = 0
    # 压力材料是否**超过**声明的 context window（manifest 记的 bytes/estimated
    # tokens vs 声明窗口）。只有 True 才允许 not_observed_under_budget。
    pressure_exceeds_declared_window: bool = False

    # ---- 汇总透传（不参与状态判定，只进 summary）----
    retention_score: float | None = None
    task_score: float | None = None
    observability_completeness: Completeness = "complete"
    agent_scope: AgentScope = "main"
    # 额外 limitation 文本（如 capability 版本限定），会与自动推导的合并去重。
    extra_limitations: list[str] = field(default_factory=list)


def _classify_status(inp: EvaluationInputs) -> CompactionStatus:
    """五种状态的确定性判定（优先级见实现注释）。"""
    records = inp.compaction_records

    # 1) 有 record → observed。最高正向优先级：真实检出的压缩不被 completeness
    #    翻案。即便元数据不全，也如实报 observed + limitations。
    if records:
        return "observed"

    # 以下均为**空 record**分支。

    # 2) 采集/解析失败 → incomplete。证据链断裂，任何「没压缩」的结论都不可信。
    if inp.collection_failed:
        return "incomplete"

    # 3) 证据不足 → unsupported（缺逐调用 usage / identity / session continuity）。
    #    这些让「相邻 call 可比较」这一前提不成立，不能声称「支持但没触发」。
    if inp.aggregate_only or inp.identity_unattributed or inp.session_broken:
        return "unsupported"

    # 4) 可比较 call 不足两个 → insufficient_calls（无相邻对，判不了）。
    if inp.max_comparable_calls < 2:
        return "insufficient_calls"

    # 5) 证据完整 + 有可比较相邻对，但没检出压缩：
    #    - 压力**超过**声明窗口仍未触发 → not_observed_under_budget（唯一
    #      允许此结论的条件）；
    #    - 压力**没**压到声明窗口 → incomplete（覆盖不足，不能把「没压到」误当
    #      「不压缩」；不把未触发解释为不支持）。
    if inp.pressure_exceeds_declared_window:
        return "not_observed_under_budget"
    return "incomplete"


def _derive_limitations(inp: EvaluationInputs, status: CompactionStatus) -> list[str]:
    """把 capability gap 汇成人类可读 limitations（进 summary，供 UI 显示 gap）。

    有 record（observed）时元数据不全也照报，但 limitation 里如实标出证据缺口——
    这样「observed 但 completeness=partial」的样本不会被误读为证据完整。
    """
    lims: list[str] = []
    if inp.aggregate_only:
        lims.append("aggregate-only-usage")
    if inp.identity_unattributed:
        lims.append("subagent-identity-unattributed")
    if inp.session_broken:
        lims.append("session-continuity-broken")
    if inp.collection_failed:
        lims.append("capture-incomplete")
    if status in ("incomplete", "not_observed_under_budget") and not (
        inp.pressure_exceeds_declared_window
    ):
        lims.append("pressure-below-declared-window")
    # extra（如 codex 的版本限定声明）合并，去重且保序。
    for x in inp.extra_limitations:
        if x not in lims:
            lims.append(x)
    return lims


def inputs_from_wire(
    *,
    manifest: dict[str, Any] | None,
    records: list[dict[str, Any]] | None,
    session_continuity: str | None = None,
    pressure_exceeds_declared_window: bool = False,
    retention_score: float | None = None,
    task_score: float | None = None,
    extra_limitations: list[str] | None = None,
) -> EvaluationInputs:
    """从 wire manifest + canonical records 映射出 EvaluationInputs（API 层用）。

    映射规则（与状态判定解耦，单独可测）：
    - ``compaction_records`` = records 里的 ``context_compaction``；
    - ``collection_failed`` = manifest 缺失 / status in {failed, incomplete} /
      有 capture-incomplete gap；
    - ``aggregate_only`` = 任一 source capability 声明 ``call_boundary ==
      "aggregate-only"``（Codex 无逐调用 usage）；
    - ``identity_unattributed`` = 任一 source ``subagent_identity == False``；
    - ``session_broken`` = conversation summary 的 session_continuity == "broken"
      或 manifest gap 里有 session 断裂；
    - ``max_comparable_calls`` = 最大 (agent_id, session) 段内 conversational
      llm_call 数量；
    - ``agent_scope`` 从 records 的 agent_id 推导（main/subagent/mixed/none）。
    """
    records = records or []
    comp_records = [
        r for r in records if r.get("record_type") == "context_compaction"
    ]

    status = (manifest or {}).get("status")
    gaps = (manifest or {}).get("gaps") or []
    gap_reasons = {g.get("reason") for g in gaps if isinstance(g, dict)}
    collection_failed = (
        manifest is None
        or status in ("failed", "incomplete")
        or "capture-incomplete" in gap_reasons
        or "capture_finalize_failed" in gap_reasons
    )

    sources = (manifest or {}).get("sources") or []
    aggregate_only = any(
        (s.get("capabilities") or {}).get("call_boundary") == "aggregate-only"
        for s in sources
    )
    identity_unattributed = any(
        (s.get("capabilities") or {}).get("subagent_identity") is False
        for s in sources
    )
    session_broken = session_continuity == "broken" or any(
        "session" in str(r) for r in gap_reasons
    )

    max_comparable = _max_comparable_calls(records)
    agent_scope = _derive_agent_scope(records)

    return EvaluationInputs(
        compaction_records=comp_records,
        collection_failed=collection_failed,
        aggregate_only=aggregate_only,
        identity_unattributed=identity_unattributed,
        session_broken=session_broken,
        max_comparable_calls=max_comparable,
        pressure_exceeds_declared_window=pressure_exceeds_declared_window,
        retention_score=retention_score,
        task_score=task_score,
        observability_completeness=_completeness_from_status(status),
        agent_scope=agent_scope,
        extra_limitations=list(extra_limitations or []),
    )


# 参与压缩检测的对话主线角色（与 compaction._CONVERSATIONAL_ROLES 同集合）。
_CONVERSATIONAL_ROLES = frozenset({"main", "subagent"})


def _max_comparable_calls(records: list[dict[str, Any]]) -> int:
    """最大 (attempt, agent, session) 段内可比较 conversational llm_call 数量。"""
    segments: dict[tuple[str, str, str], int] = {}
    for r in records:
        if r.get("record_type") != "llm_call":
            continue
        if (r.get("data") or {}).get("call_role") not in _CONVERSATIONAL_ROLES:
            continue
        corr = r.get("correlation") or {}
        key = (
            r.get("attempt_id") or "",
            corr.get("agent_id") or "main",
            corr.get("producer_session_id") or "",
        )
        segments[key] = segments.get(key, 0) + 1
    return max(segments.values(), default=0)


def _derive_agent_scope(records: list[dict[str, Any]]) -> AgentScope:
    agents = {
        (r.get("correlation") or {}).get("agent_id") or "main"
        for r in records
        if r.get("record_type") == "llm_call"
    }
    if not agents:
        return "none"
    has_sub = any(a != "main" for a in agents)
    has_main = "main" in agents
    if has_sub and has_main:
        return "mixed"
    if has_sub:
        return "subagent"
    return "main"


def _completeness_from_status(status: str | None) -> Completeness:
    if status in ("failed", "incomplete"):
        return "incomplete"
    if status in ("partial", "recovered"):
        return "partial"
    if status == "complete":
        return "complete"
    return "partial"  # not-applicable/unknown → partial（不冒充 complete）


def evaluate_compaction(inp: EvaluationInputs) -> dict[str, Any]:
    """输出 evaluation summary 形状。

    ``compaction_count`` 是 record 数；``retention_score`` 与 ``task_score`` 独立透传
    （压缩发生但信息丢失不算优秀，未观察到但任务成功也不声称压缩能力优秀）。
    """
    status = _classify_status(inp)
    limitations = _derive_limitations(inp, status)
    return {
        "compaction_status": status,
        "compaction_count": len(inp.compaction_records),
        "retention_score": inp.retention_score,
        "task_score": inp.task_score,
        "observability_completeness": inp.observability_completeness,
        "agent_scope": inp.agent_scope,
        "limitations": limitations,
    }
