"""blade-agent 会话 → ATIF-v1.7 trajectory。

源：attempt 目录的 ``events.jsonl``——支持两种由 blade-agent adapter 落盘的格式：

1. **CLI 格式（``blade_cli.py``）**：OpenAI 风格扁平消息序列：
   - ``{"type":"message","role":"user"|"assistant"|"tool","content":...}``
   - ``{"type":"context"|"memory_inject",...}``：平台注入上下文，不计入步骤。

2. **SDK 格式（``blade_service.py``）**：``{"kind", "timestamp", "raw", "source"}``
   事件信封：
   - ``kind == "turn:end"``（``messages_poll`` / ``blade_cli_history`` / 旧 SDK
     ``socket``）：``raw.message``（或顶层 ``raw``）带 ``role``、``content``、
     ``_blocks``/``blocks``、``_reasoning``/``reasoning_content``、``tool_calls``、
     ``tool_call_id``、``usage``、``model``；
   - ``kind == "llm:thinking:delta"`` + ``kind == "llm:response:done"`` +
     ``kind == "tool:result:done"``（新流式协议 ``socket``）：当 ``turn:end``
     缺席或为空壳时直接从中重建推理步、thinking、tool_calls、tool_result 与
     per-step token usage；当与 ``messages_poll`` 的 ``turn:end`` 双写并存时，
     去重合并并回填 usage / model / reasoning，不产生重复步骤。

当转录中存在分步 token usage 时填充 ``Step.metrics`` 与 ``FinalMetrics``；
拿不到上报值时保持为 None（不做差值估算）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..schema import (
    Agent,
    FinalMetrics,
    Metrics,
    Observation,
    ObservationResult,
    Step,
    ToolCall,
    Trajectory,
)

#: 非行为事件：平台注入的上下文，不构成 agent 的一步。
_NON_BEHAVIOR_TYPES = frozenset({"context", "memory_inject"})
_SDK_SOURCES = frozenset({"socket", "messages_poll", "blade_cli_history", "octagon"})
_TOOL_RESULT_KINDS = frozenset({"tool:result:done", "tool:result", "tool:end"})


def find_session_events(attempt_dir: Path) -> list[dict[str, Any]]:
    """读 attempt 目录的 events.jsonl。文件缺失/整行损坏 → 跳过该行。"""
    path = Path(attempt_dir) / "events.jsonl"
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
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


def _is_sdk_event(event: dict[str, Any]) -> bool:
    if "kind" in event and isinstance(event.get("raw"), dict):
        return True
    if event.get("source") in _SDK_SOURCES and isinstance(event.get("raw"), dict):
        return True
    return False


def _is_legacy_message(event: dict[str, Any]) -> bool:
    etype = event.get("type")
    if etype is not None:
        return etype == "message" and str(etype) not in _NON_BEHAVIOR_TYPES
    if _is_sdk_event(event):
        return False
    return event.get("role") in {"user", "assistant", "tool"}


def _valid_iso_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # 兼容毫秒与秒级 epoch
        secs = value / 1000.0 if abs(value) > 1e11 else float(value)
        try:
            return (
                datetime.fromtimestamp(secs, tz=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return text
    return None


def _nonneg_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        iv = int(value)
    except (TypeError, ValueError):
        return None
    return iv if iv >= 0 else None


def _metrics_from_usage(usage: Any) -> Metrics | None:
    if not isinstance(usage, dict) or not usage:
        return None
    prompt_details = usage.get("prompt_tokens_details")
    prompt_details = prompt_details if isinstance(prompt_details, dict) else {}
    completion_details = usage.get("completion_tokens_details")
    completion_details = (
        completion_details if isinstance(completion_details, dict) else {}
    )

    prompt_tokens = _nonneg_int(
        usage.get("prompt_tokens", usage.get("input_tokens"))
    )
    completion_tokens = _nonneg_int(
        usage.get("completion_tokens", usage.get("output_tokens"))
    )
    cached_tokens = _nonneg_int(
        prompt_details.get(
            "cached_tokens",
            usage.get("cached_tokens", usage.get("cache_read_tokens")),
        )
    )
    reasoning_tokens = _nonneg_int(
        completion_details.get("reasoning_tokens", usage.get("reasoning_tokens"))
    )
    cache_write_tokens = _nonneg_int(
        prompt_details.get("cache_write_tokens", usage.get("cache_write_tokens"))
    )

    extra: dict[str, Any] = {}
    if reasoning_tokens is not None:
        extra["reasoning_tokens"] = reasoning_tokens
    if cache_write_tokens is not None:
        extra["cache_write_tokens"] = cache_write_tokens

    if (
        prompt_tokens is None
        and completion_tokens is None
        and cached_tokens is None
        and not extra
    ):
        return None
    return Metrics(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_tokens=cached_tokens,
        extra=extra or None,
    )


def _tool_calls(raw: Any) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for index, item in enumerate(raw or []):
        if not isinstance(item, dict):
            continue
        function = item.get("function")
        function = function if isinstance(function, dict) else {}
        name = str(function.get("name") or item.get("name") or "")
        if not name:
            continue
        arguments = function.get("arguments", item.get("arguments", item.get("input")))
        if isinstance(arguments, str):
            # blade-agent 落的是 JSON 字符串。解不开就原样保留——宁可让下游看到
            # 原始文本，也不要丢掉这次调用。
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {"_raw": arguments}
        if arguments is None:
            arguments = {}
        elif not isinstance(arguments, dict):
            arguments = {"_raw": arguments}
        calls.append(
            ToolCall(
                tool_call_id=str(
                    item.get("id") or item.get("tool_call_id") or f"call_{index + 1}"
                ),
                function_name=name,
                arguments=arguments,
            )
        )
    return calls


def _extract_text_and_reasoning(
    source_obj: dict[str, Any],
    *,
    extra_blocks: Any = None,
) -> tuple[str, str | None, list[ToolCall]]:
    """从消息 dict（及可选 blocks）提取 (text, reasoning, block_tool_calls)。"""
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    block_calls_raw: list[dict[str, Any]] = []

    raw_content = source_obj.get("content")
    if isinstance(raw_content, str):
        if raw_content:
            text_parts.append(raw_content)
    elif isinstance(raw_content, list):
        for part in raw_content:
            if isinstance(part, str):
                if part:
                    text_parts.append(part)
                continue
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype in {"thinking", "reasoning"}:
                t = part.get("thinking") or part.get("content") or part.get("text")
                if isinstance(t, str) and t:
                    thinking_parts.append(t)
            elif ptype in {"text", "output_text"}:
                t = part.get("text") or part.get("content")
                if isinstance(t, str) and t:
                    text_parts.append(t)
            elif ptype in {"tool_use", "tool_call"}:
                block_calls_raw.append(part)

    # 显式 reasoning 字段（_reasoning / reasoning_content / reasoning）
    for rkey in ("_reasoning", "reasoning_content", "reasoning"):
        rval = source_obj.get(rkey)
        if isinstance(rval, str) and rval:
            if not thinking_parts:
                thinking_parts.append(rval)
            break

    # _blocks / blocks：补充 thinking、缺失的 text 以及缺失的 tool_calls
    blocks_lists: list[Any] = []
    for bkey in ("_blocks", "blocks"):
        if isinstance(source_obj.get(bkey), list):
            blocks_lists.append(source_obj[bkey])
    if isinstance(extra_blocks, list):
        blocks_lists.append(extra_blocks)

    block_texts: list[str] = []
    block_thinkings: list[str] = []
    for blocks in blocks_lists:
        for block in blocks:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype in {"thinking", "reasoning"}:
                t = block.get("content") or block.get("thinking") or block.get("text")
                if isinstance(t, str) and t:
                    block_thinkings.append(t)
            elif btype in {"text", "output_text"}:
                t = block.get("content") or block.get("text")
                if isinstance(t, str) and t:
                    block_texts.append(t)
            elif btype in {"tool_use", "tool_call"}:
                block_calls_raw.append(block)

    if not text_parts and block_texts:
        text_parts = block_texts
    if not thinking_parts and block_thinkings:
        thinking_parts = block_thinkings

    calls = _tool_calls(source_obj.get("tool_calls"))
    if not calls and block_calls_raw:
        calls = _tool_calls(block_calls_raw)

    message_text = "\n".join(text_parts) if text_parts else ""
    reasoning_text = "\n".join(thinking_parts) if thinking_parts else None
    return message_text, reasoning_text, calls


def _result_content_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False)


def _result_content(event: dict[str, Any]) -> str:
    """工具结果的文本。blade-agent 落的是 JSON 字符串，原样交出。

    不解包成结构：exit_code / output / cwd 对 judge 都是有效信息，拆了反而
    要替它决定哪些重要。
    """
    for key in ("content", "output", "result", "error"):
        if key in event and event.get(key) is not None:
            return _result_content_value(event.get(key))
    return ""


def _extract_turn_end_message(event: dict[str, Any]) -> dict[str, Any] | None:
    """从 SDK ``turn:end`` 信封中提取标准化消息 dict；空壳返回 None。"""
    raw = event.get("raw")
    if not isinstance(raw, dict):
        return None
    msg = raw.get("message") if isinstance(raw.get("message"), dict) else None
    source = msg if msg is not None else raw
    role = source.get("role")
    if role not in {"user", "assistant", "tool"}:
        # 旧 SDK 的 turn:end 可能省略 role 但带 blocks / content / tool_calls
        has_assistant_data = bool(
            source.get("content")
            or source.get("tool_calls")
            or source.get("_blocks")
            or source.get("blocks")
            or source.get("_reasoning")
            or source.get("reasoning_content")
            or raw.get("blocks")
        )
        if not has_assistant_data:
            return None
        role = "assistant"

    extra_blocks = raw.get("blocks") if msg is not None else None
    text, reasoning, calls = _extract_text_and_reasoning(
        source, extra_blocks=extra_blocks
    )
    usage = source.get("usage") or raw.get("usage")
    model = source.get("model") or raw.get("model")
    ts = _valid_iso_timestamp(
        event.get("timestamp") or raw.get("timestamp") or source.get("timestamp")
    )
    entry_id = raw.get("id")
    if not isinstance(entry_id, str) or not entry_id:
        entry_id = None
    tool_call_id = source.get("tool_call_id") or raw.get("tool_call_id")
    if not isinstance(tool_call_id, str) or not tool_call_id:
        tool_call_id = None

    if role == "assistant" and not (text or reasoning or calls):
        return None

    return {
        "entry_id": entry_id,
        "role": role,
        "timestamp": ts,
        "text": text,
        "reasoning": reasoning,
        "tool_calls": calls,
        "tool_call_id": tool_call_id,
        "tool_content": _result_content(source) if role == "tool" else "",
        "usage": usage if isinstance(usage, dict) else None,
        "model": model if isinstance(model, str) and model else None,
    }


def _socket_event_type(event: dict[str, Any]) -> str | None:
    raw = event.get("raw")
    if isinstance(raw, dict) and isinstance(raw.get("type"), str):
        return raw["type"]
    kind = event.get("kind")
    return kind if isinstance(kind, str) else None


def _socket_payload(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("raw")
    if not isinstance(raw, dict):
        return {}
    payload = raw.get("payload")
    return payload if isinstance(payload, dict) else raw


@dataclass
class _SocketResponse:
    timestamp: str | None
    text: str
    reasoning: str | None
    tool_calls: list[ToolCall]
    metrics: Metrics | None
    model: str | None
    matched: bool = False


@dataclass
class _SocketToolResult:
    timestamp: str | None
    tool_call_id: str | None
    content: str
    is_error: bool = False


def describe_empty(events: list[dict[str, Any]]) -> str | None:
    """转录在、但构不成轨迹时给出真实原因。

    「转录不存在」与「转录在、但里面没有 agent 行为」必须分开报——把后者说成
    前者，等于把一次空跑伪装成采集缺陷。
    """
    if not events:
        return None

    legacy_behavior = [e for e in events if _is_legacy_message(e)]
    if legacy_behavior:
        if not any(e.get("role") == "assistant" for e in legacy_behavior):
            return "events.jsonl 里没有 assistant 消息——该轮 agent 未产生任何推理"
        return None

    sdk_events = [e for e in events if _is_sdk_event(e)]
    if sdk_events:
        has_any_msg = False
        has_assistant = False
        for e in sdk_events:
            kind = e.get("kind")
            etype = _socket_event_type(e)
            if kind == "turn:end":
                extracted = _extract_turn_end_message(e)
                if extracted is not None:
                    has_any_msg = True
                    if extracted["role"] == "assistant":
                        has_assistant = True
            elif etype == "llm:response:done":
                payload = _socket_payload(e)
                text, reasoning, calls = _extract_text_and_reasoning(payload)
                if text or reasoning or calls:
                    has_any_msg = True
                    has_assistant = True
        if not has_any_msg:
            kinds = sorted({str(e.get("kind") or _socket_event_type(e)) for e in events})
            return f"events.jsonl 有 {len(events)} 条但无 message 事件（只有 {kinds}）"
        if not has_assistant:
            return "events.jsonl 里没有 assistant 消息——该轮 agent 未产生任何推理"
        return None

    kinds = sorted({str(e.get("type") or e.get("kind")) for e in events})
    return f"events.jsonl 有 {len(events)} 条但无 message 事件（只有 {kinds}）"


def _build_final_metrics(steps: list[Step]) -> FinalMetrics:
    prompt_total = 0
    completion_total = 0
    cached_total = 0
    saw_prompt = False
    saw_completion = False
    saw_cached = False
    for step in steps:
        m = step.metrics
        if m is None:
            continue
        if m.prompt_tokens is not None:
            prompt_total += m.prompt_tokens
            saw_prompt = True
        if m.completion_tokens is not None:
            completion_total += m.completion_tokens
            saw_completion = True
        if m.cached_tokens is not None:
            cached_total += m.cached_tokens
            saw_cached = True
    return FinalMetrics(
        total_prompt_tokens=prompt_total if saw_prompt else None,
        total_completion_tokens=completion_total if saw_completion else None,
        total_cached_tokens=cached_total if saw_cached else None,
        total_steps=len(steps),
    )


def _safe_observation(
    results: list[ObservationResult],
    calls: list[ToolCall] | None,
) -> Observation | None:
    if not results:
        return None
    valid_ids = {c.tool_call_id for c in (calls or [])}
    sanitized = [
        r
        if (r.source_call_id is None or r.source_call_id in valid_ids)
        else ObservationResult(
            source_call_id=None, content=r.content, extra=r.extra
        )
        for r in results
    ]
    return Observation(results=sanitized)


def _convert_legacy_events(
    events: list[dict[str, Any]],
    *,
    attempt_id: str,
    model_name: str | None = None,
) -> Trajectory | None:
    messages = [e for e in events if _is_legacy_message(e)]
    if not messages:
        return None

    steps: list[Step] = []
    pending: Step | None = None
    pending_results: list[ObservationResult] = []
    pending_call_ids: list[str] = []
    resolved_model = model_name

    def flush() -> None:
        nonlocal pending, pending_results, pending_call_ids
        if pending is None:
            return
        if pending_results:
            pending.observation = _safe_observation(pending_results, pending.tool_calls)
        steps.append(pending)
        pending = None
        pending_results = []
        pending_call_ids = []

    for event in messages:
        role = event.get("role")
        timestamp = _valid_iso_timestamp(event.get("timestamp"))
        if role == "user":
            flush()
            text, _, _ = _extract_text_and_reasoning(event)
            steps.append(
                Step(
                    step_id=len(steps) + 1,
                    timestamp=timestamp,
                    source="user",
                    message=text,
                )
            )
            continue
        if role == "assistant":
            flush()
            text, reasoning, calls = _extract_text_and_reasoning(event)
            ev_model = event.get("model")
            step_model = (
                ev_model if isinstance(ev_model, str) and ev_model else model_name
            )
            if resolved_model is None and step_model is not None:
                resolved_model = step_model
            pending = Step(
                step_id=len(steps) + 1,
                timestamp=timestamp,
                source="agent",
                message=text,
                reasoning_content=reasoning,
                model_name=step_model,
                metrics=_metrics_from_usage(event.get("usage")),
                llm_call_count=1,
                tool_calls=calls or None,
            )
            pending_call_ids = [c.tool_call_id for c in calls]
            continue
        if role == "tool":
            explicit_id = event.get("tool_call_id")
            if (
                isinstance(explicit_id, str)
                and explicit_id
                and explicit_id in pending_call_ids
            ):
                call_id: str | None = explicit_id
            elif pending is not None and len(pending_results) < len(pending_call_ids):
                call_id = pending_call_ids[len(pending_results)]
            else:
                call_id = None
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

    final_metrics = _build_final_metrics(steps)
    has_usage = any(s.metrics is not None for s in steps)
    return Trajectory(
        schema_version="ATIF-v1.7",
        session_id=attempt_id,
        trajectory_id=attempt_id,
        agent=Agent(name="blade-agent", version="unknown", model_name=resolved_model),
        steps=steps,
        notes=(
            "reconstructed from blade-agent events.jsonl message stream "
            f"(producer=octagon-atif-v1, attempt={attempt_id})"
            + (
                ""
                if has_usage
                else "; per-step token usage is not present in this transcript"
            )
        ),
        final_metrics=final_metrics,
    )


def _convert_sdk_events(
    events: list[dict[str, Any]],
    *,
    attempt_id: str,
    model_name: str | None = None,
) -> Trajectory | None:
    """解析 ``blade_service.py`` SDK ``kind``/``raw`` 事件信封。"""
    seen_entry_ids: set[str] = set()
    seen_stream_seqs: set[int] = set()
    pending_thinking_by_loop: dict[str, list[str]] = {}

    turn_end_msgs: list[dict[str, Any]] = []
    socket_responses: list[_SocketResponse] = []
    socket_tool_results: list[_SocketToolResult] = []
    socket_timeline: list[tuple[str, Any]] = []

    for event in events:
        if not _is_sdk_event(event):
            continue
        kind = event.get("kind")
        raw = event.get("raw")
        if not isinstance(raw, dict):
            continue

        if kind == "turn:end":
            extracted = _extract_turn_end_message(event)
            if extracted is None:
                continue
            eid = extracted["entry_id"]
            if eid is not None:
                if eid in seen_entry_ids:
                    continue
                seen_entry_ids.add(eid)
            turn_end_msgs.append(extracted)
            continue

        # socket 事件：按 stream_sequence 去重（断线重连回放）
        seq = raw.get("stream_sequence")
        if isinstance(seq, int):
            if seq in seen_stream_seqs:
                continue
            seen_stream_seqs.add(seq)

        etype = _socket_event_type(event)
        payload = _socket_payload(event)
        loop = str(payload.get("loop_name") or raw.get("loop_name") or "root")
        ts = _valid_iso_timestamp(event.get("timestamp") or raw.get("timestamp"))

        if etype == "llm:thinking:delta":
            part = payload.get("content") or payload.get("delta") or payload.get("text")
            if isinstance(part, str) and part:
                pending_thinking_by_loop.setdefault(loop, []).append(part)
            continue

        if etype == "llm:response:done":
            accumulated = "".join(pending_thinking_by_loop.pop(loop, []))
            text, reasoning, calls = _extract_text_and_reasoning(payload)
            if not reasoning and accumulated:
                reasoning = accumulated
            if not (text or reasoning or calls):
                continue
            ev_model = payload.get("model")
            resp = _SocketResponse(
                timestamp=ts,
                text=text,
                reasoning=reasoning,
                tool_calls=calls,
                metrics=_metrics_from_usage(payload.get("usage")),
                model=ev_model if isinstance(ev_model, str) and ev_model else None,
            )
            socket_responses.append(resp)
            socket_timeline.append(("response", resp))
            continue

        if etype in _TOOL_RESULT_KINDS:
            tc_id = payload.get("tool_call_id") or payload.get("id")
            tr = _SocketToolResult(
                timestamp=ts,
                tool_call_id=tc_id if isinstance(tc_id, str) and tc_id else None,
                content=_result_content(payload),
                is_error=bool(payload.get("is_error")),
            )
            socket_tool_results.append(tr)
            socket_timeline.append(("tool_result", tr))
            continue

    has_turn_end_assistant = any(m["role"] == "assistant" for m in turn_end_msgs)
    steps: list[Step] = []
    resolved_model = model_name

    # tool_call_id -> _SocketToolResult（供补齐 observation）
    socket_results_by_call_id: dict[str, _SocketToolResult] = {}
    for tr in socket_tool_results:
        if tr.tool_call_id and tr.tool_call_id not in socket_results_by_call_id:
            socket_results_by_call_id[tr.tool_call_id] = tr

    if has_turn_end_assistant:
        pending: Step | None = None
        pending_results: list[ObservationResult] = []
        pending_call_ids: list[str] = []

        def flush_turn_step() -> None:
            nonlocal pending, pending_results, pending_call_ids
            if pending is None:
                return
            # 若 turn:end 缺了某个 tool_call 的结果但 socket tool:result:done 有，则补齐
            seen_obs_ids = {
                r.source_call_id for r in pending_results if r.source_call_id
            }
            for cid in pending_call_ids:
                if cid not in seen_obs_ids and cid in socket_results_by_call_id:
                    tr = socket_results_by_call_id[cid]
                    pending_results.append(
                        ObservationResult(
                            source_call_id=cid,
                            content=tr.content,
                            extra={"is_error": True} if tr.is_error else None,
                        )
                    )
            if pending_results:
                pending.observation = _safe_observation(
                    pending_results, pending.tool_calls
                )
            steps.append(pending)
            pending = None
            pending_results = []
            pending_call_ids = []

        for msg in turn_end_msgs:
            role = msg["role"]
            if role == "user":
                flush_turn_step()
                steps.append(
                    Step(
                        step_id=len(steps) + 1,
                        timestamp=msg["timestamp"],
                        source="user",
                        message=msg["text"],
                    )
                )
                continue
            if role == "assistant":
                flush_turn_step()
                step_model = msg["model"] or model_name
                if resolved_model is None and step_model is not None:
                    resolved_model = step_model
                calls: list[ToolCall] = msg["tool_calls"]
                pending = Step(
                    step_id=len(steps) + 1,
                    timestamp=msg["timestamp"],
                    source="agent",
                    message=msg["text"],
                    reasoning_content=msg["reasoning"],
                    model_name=step_model,
                    metrics=_metrics_from_usage(msg["usage"]),
                    llm_call_count=1,
                    tool_calls=calls or None,
                )
                pending_call_ids = [c.tool_call_id for c in calls]
                continue
            if role == "tool":
                explicit_id = msg["tool_call_id"]
                if explicit_id and explicit_id in pending_call_ids:
                    call_id: str | None = explicit_id
                elif pending is not None and len(pending_results) < len(
                    pending_call_ids
                ):
                    call_id = pending_call_ids[len(pending_results)]
                else:
                    call_id = None
                pending_results.append(
                    ObservationResult(
                        source_call_id=call_id,
                        content=msg["tool_content"],
                    )
                )
                continue
        flush_turn_step()

        # 用同 attempt 的 socket llm:response:done 回填 metrics / model / reasoning
        agent_steps = [s for s in steps if s.source == "agent"]
        unmatched_agent_steps: list[Step] = []
        for step in agent_steps:
            step_call_ids = {c.tool_call_id for c in (step.tool_calls or [])}
            matched_resp: _SocketResponse | None = None
            if step_call_ids:
                for resp in socket_responses:
                    if resp.matched:
                        continue
                    resp_call_ids = {c.tool_call_id for c in resp.tool_calls}
                    if step_call_ids & resp_call_ids:
                        matched_resp = resp
                        break
            if matched_resp is not None:
                matched_resp.matched = True
                _enrich_step_from_socket(step, matched_resp)
                if resolved_model is None and step.model_name is not None:
                    resolved_model = step.model_name
            else:
                unmatched_agent_steps.append(step)

        remaining_socket = [r for r in socket_responses if not r.matched]
        for step, resp in zip(unmatched_agent_steps, remaining_socket):
            resp.matched = True
            _enrich_step_from_socket(step, resp)
            if resolved_model is None and step.model_name is not None:
                resolved_model = step.model_name

        # 若 socket 末尾还有未被 poll 追上的 response:done，追加为 step
        for resp in socket_responses:
            if resp.matched:
                continue
            step_model = resp.model or resolved_model
            if resolved_model is None and step_model is not None:
                resolved_model = step_model
            obs_results = [
                ObservationResult(
                    source_call_id=c.tool_call_id,
                    content=socket_results_by_call_id[c.tool_call_id].content,
                    extra=(
                        {"is_error": True}
                        if socket_results_by_call_id[c.tool_call_id].is_error
                        else None
                    ),
                )
                for c in resp.tool_calls
                if c.tool_call_id in socket_results_by_call_id
            ]
            steps.append(
                Step(
                    step_id=len(steps) + 1,
                    timestamp=resp.timestamp,
                    source="agent",
                    message=resp.text,
                    reasoning_content=resp.reasoning,
                    model_name=step_model,
                    metrics=resp.metrics,
                    llm_call_count=1,
                    tool_calls=resp.tool_calls or None,
                    observation=_safe_observation(obs_results, resp.tool_calls or None),
                )
            )
    else:
        # 无 turn:end assistant 消息：保留已有的 user 消息，并从 socket 流构建 agent 步
        for msg in turn_end_msgs:
            if msg["role"] == "user":
                steps.append(
                    Step(
                        step_id=len(steps) + 1,
                        timestamp=msg["timestamp"],
                        source="user",
                        message=msg["text"],
                    )
                )

        pending = None
        pending_results = []
        pending_call_ids = []

        def flush_socket_step() -> None:
            nonlocal pending, pending_results, pending_call_ids
            if pending is None:
                return
            if pending_results:
                pending.observation = _safe_observation(
                    pending_results, pending.tool_calls
                )
            steps.append(pending)
            pending = None
            pending_results = []
            pending_call_ids = []

        for item_kind, item in socket_timeline:
            if item_kind == "response":
                flush_socket_step()
                resp = item
                step_model = resp.model or model_name
                if resolved_model is None and step_model is not None:
                    resolved_model = step_model
                pending = Step(
                    step_id=len(steps) + 1,
                    timestamp=resp.timestamp,
                    source="agent",
                    message=resp.text,
                    reasoning_content=resp.reasoning,
                    model_name=step_model,
                    metrics=resp.metrics,
                    llm_call_count=1,
                    tool_calls=resp.tool_calls or None,
                )
                pending_call_ids = [c.tool_call_id for c in resp.tool_calls]
            elif item_kind == "tool_result":
                tr = item
                if tr.tool_call_id and tr.tool_call_id in pending_call_ids:
                    call_id = tr.tool_call_id
                elif pending is not None and len(pending_results) < len(
                    pending_call_ids
                ):
                    call_id = pending_call_ids[len(pending_results)]
                else:
                    call_id = None
                pending_results.append(
                    ObservationResult(
                        source_call_id=call_id,
                        content=tr.content,
                        extra={"is_error": True} if tr.is_error else None,
                    )
                )
        flush_socket_step()

    if not any(s.source == "agent" for s in steps):
        return None

    for index, step in enumerate(steps, start=1):
        step.step_id = index

    final_metrics = _build_final_metrics(steps)
    has_usage = any(s.metrics is not None for s in steps)
    return Trajectory(
        schema_version="ATIF-v1.7",
        session_id=attempt_id,
        trajectory_id=attempt_id,
        agent=Agent(name="blade-agent", version="unknown", model_name=resolved_model),
        steps=steps,
        notes=(
            "reconstructed from blade-agent SDK events.jsonl "
            f"(producer=octagon-atif-v1, attempt={attempt_id})"
            + (
                ""
                if has_usage
                else "; per-step token usage is not present in this transcript"
            )
        ),
        final_metrics=final_metrics,
    )


def _enrich_step_from_socket(step: Step, resp: _SocketResponse) -> None:
    if step.metrics is None and resp.metrics is not None:
        step.metrics = resp.metrics
    if step.model_name is None and resp.model is not None:
        step.model_name = resp.model
    if step.reasoning_content is None and resp.reasoning is not None:
        step.reasoning_content = resp.reasoning


def convert_events_to_trajectory(
    events: list[dict[str, Any]],
    *,
    attempt_id: str,
    model_name: str | None = None,
) -> Trajectory | None:
    """blade-agent events.jsonl → ATIF Trajectory（兼容 CLI 扁平消息与 SDK 信封流）。"""
    if not events:
        return None
    if any(_is_legacy_message(e) for e in events):
        return _convert_legacy_events(
            events, attempt_id=attempt_id, model_name=model_name
        )
    if any(_is_sdk_event(e) for e in events):
        return _convert_sdk_events(
            events, attempt_id=attempt_id, model_name=model_name
        )
    return None
