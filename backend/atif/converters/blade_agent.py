"""blade-agent 会话 → ATIF-v1.7 trajectory。

源：attempt 目录的 ``events.jsonl``——blade-agent 由 Env Attempt Server 逐行
落盘的会话流。它已经是 OpenAI 风格的消息序列，结构比其他五家都规整：

- ``{"type":"message","role":"user","content":...}``：任务输入；
- ``{"type":"message","role":"assistant","content":...,"tool_calls":[
  {"id","function":{"name","arguments"}}]}``：一次推理，arguments 是 JSON **字符串**；
- ``{"type":"message","role":"tool","content":"<JSON 字符串>"}``：工具结果，
  内容形如 ``{"command","cwd","exit_code","output"}``。**没有 tool_call_id**
  ——按出现顺序与上一个 assistant 的 tool_calls 配对（见 ``_pair_results``）；
- ``{"type":"context",...}``：平台注入的上下文快照（platform-services /
  network-reach / system-reminder 等），**不是 agent 的行为**，不计入步骤；
- ``{"type":"memory_inject",...}``：同上，属注入而非行为。

为什么要有这个转换器：blade-agent 此前不在 ``_SUPPORTED_AGENTS`` 里，emit
直接 ``not_available``。它与其余五家同场竞技时，judge 拿到的证据形态就不对等
——别人是归一化的 ATIF，它只能退回读 raw events.jsonl。比较式评分里这会让
排序被证据形态本身带偏，而不是被交付物质量带偏。

转录里没有分步 token（blade-agent 的用量走 wire/cost 另一条链），故
``metrics`` 为 None、``final_metrics`` 只给 ``total_steps``。不做差值估算——
拿不到上报值就不该编一个出来。
"""

from __future__ import annotations

import json
from typing import Any

from pathlib import Path

from ..schema import (
    Agent,
    FinalMetrics,
    Observation,
    ObservationResult,
    Step,
    ToolCall,
    Trajectory,
)

#: 非行为事件：平台注入的上下文，不构成 agent 的一步。
_NON_BEHAVIOR_TYPES = frozenset({"context", "memory_inject"})


def find_session_events(attempt_dir: Path) -> list[dict[str, Any]]:
    """读 attempt 目录的 events.jsonl。文件缺失/整行损坏 → 跳过该行。"""
    path = Path(attempt_dir) / "events.jsonl"
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def describe_empty(events: list[dict[str, Any]]) -> str | None:
    """转录在、但构不成轨迹时给出真实原因。

    「转录不存在」与「转录在、但里面没有 agent 行为」必须分开报——把后者说成
    前者，等于把一次空跑伪装成采集缺陷。
    """
    if not events:
        return None
    behavior = [
        e for e in events
        if e.get("type") == "message" and e.get("role") in {"user", "assistant", "tool"}
    ]
    if not behavior:
        kinds = sorted({str(e.get("type")) for e in events})
        return f"events.jsonl 有 {len(events)} 条但无 message 事件（只有 {kinds}）"
    if not any(e.get("role") == "assistant" for e in behavior):
        return "events.jsonl 里没有 assistant 消息——该轮 agent 未产生任何推理"
    return None


def _tool_calls(raw: Any) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for index, item in enumerate(raw or []):
        if not isinstance(item, dict):
            continue
        function = item.get("function") or {}
        name = str(function.get("name") or item.get("name") or "")
        if not name:
            continue
        arguments = function.get("arguments", item.get("arguments"))
        if isinstance(arguments, str):
            # blade-agent 落的是 JSON 字符串。解不开就原样保留——宁可让下游看到
            # 原始文本，也不要丢掉这次调用。
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {"_raw": arguments}
        if not isinstance(arguments, dict):
            arguments = {"_raw": arguments}
        calls.append(
            ToolCall(
                tool_call_id=str(item.get("id") or f"call_{index + 1}"),
                function_name=name,
                arguments=arguments,
            )
        )
    return calls


def _result_content(event: dict[str, Any]) -> str:
    """工具结果的文本。blade-agent 落的是 JSON 字符串，原样交出。

    不解包成结构：exit_code / output / cwd 对 judge 都是有效信息，拆了反而
    要替它决定哪些重要。
    """
    content = event.get("content")
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False) if content is not None else ""


def convert_events_to_trajectory(
    events: list[dict[str, Any]],
    *,
    attempt_id: str,
    model_name: str | None = None,
) -> Trajectory | None:
    """按「一个 assistant 消息 = 一步」切分，其后的 tool 结果归入该步的 observation。"""
    messages = [
        e for e in events
        if e.get("type") == "message" and str(e.get("type")) not in _NON_BEHAVIOR_TYPES
    ]
    if not messages:
        return None

    steps: list[Step] = []
    pending: Step | None = None
    pending_results: list[ObservationResult] = []
    pending_call_ids: list[str] = []

    def flush() -> None:
        nonlocal pending, pending_results, pending_call_ids
        if pending is None:
            return
        if pending_results:
            pending.observation = Observation(results=pending_results)
        steps.append(pending)
        pending = None
        pending_results = []
        pending_call_ids = []

    for event in messages:
        role = event.get("role")
        timestamp = event.get("timestamp")
        if role == "user":
            flush()
            steps.append(
                Step(
                    step_id=len(steps) + 1,
                    timestamp=timestamp,
                    source="user",
                    message=str(event.get("content") or ""),
                )
            )
            continue
        if role == "assistant":
            flush()
            calls = _tool_calls(event.get("tool_calls"))
            pending = Step(
                step_id=len(steps) + 1,
                timestamp=timestamp,
                source="agent",
                message=str(event.get("content") or ""),
                model_name=model_name,
                llm_call_count=1,
                tool_calls=calls or None,
            )
            pending_call_ids = [c.tool_call_id for c in calls]
            continue
        if role == "tool":
            # 结果事件不带 tool_call_id，按出现顺序与上一步的 tool_calls 配对。
            # 配不上（结果多于调用、或没有 pending 步）时 source_call_id 留空，
            # 而不是硬塞一个可能错的 id——错误的归属比缺失更难发现。
            call_id = (
                pending_call_ids[len(pending_results)]
                if pending is not None and len(pending_results) < len(pending_call_ids)
                else None
            )
            pending_results.append(
                ObservationResult(
                    source_call_id=call_id,
                    content=_result_content(event),
                )
            )
            continue
    flush()

    if not steps:
        return None
    for index, step in enumerate(steps, start=1):
        step.step_id = index

    return Trajectory(
        schema_version="ATIF-v1.7",
        session_id=attempt_id,
        trajectory_id=attempt_id,
        agent=Agent(name="blade-agent", version="unknown", model_name=model_name),
        steps=steps,
        notes=(
            "reconstructed from blade-agent events.jsonl message stream "
            f"(producer=octagon-atif-v1, attempt={attempt_id}); "
            "per-step token usage is not present in this transcript"
        ),
        final_metrics=FinalMetrics(total_steps=len(steps)),
    )
