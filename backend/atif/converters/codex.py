"""Codex 沙盒会话 → ATIF-v1.7 trajectory。

源：attempt 沙盒 HOME 里 codex 持久化的 rollout
``.codex-iso-home/sessions/<workspace>/<session>.jsonl``（session_meta /
turn_context / response_item / event_msg 事件流）。仅当 codex 不以 ``--ephemeral``
运行时落盘——历史单轮 attempt 没有这些文件，converter 返回空（emitter 报
not_available）。

映射逻辑移植自 Harbor ``src/harbor/agents/installed/codex.py``：
- ``session_meta`` → agent header（cli_version / originator / cwd / git）；
- ``turn_context`` → 默认 model；
- ``response_item`` 的 reasoning / message / function_call / web_search_call /
  function_call_output → 归一化事件；``event_msg`` 的 token_count 闭合一次
  LLM API call（metrics）；
- 按 ``api_call_id`` 把同一模型请求的 message + tool_calls 聚合成一个 agent
  step（RFC one-LLM-per-step），observation 挂同一步。

cost 估算不移植 Harbor 的 litellm 定价表（agent-octagon 无 litellm 依赖）：
CLI 未报 cost 时 ``cost_usd`` 保持 None（RFC 允许——ATIF 不记录 per-token 定价）。
"""

from __future__ import annotations

import json
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


# ---------- 会话发现 ------------------------------------------------------------


def find_session_events(codex_home: Path) -> list[dict[str, Any]]:
    """读 codex 会话 rollout，返回有序事件。

    会话在 ``$CODEX_HOME/sessions``；CODEX_HOME 由 adapter 指向 home 根
    （沙箱：``sandbox_home``；宿主机：``.codex-iso-home``），个别配置也可能把
    CODEX_HOME 设成 ``<home>/.codex``——两个位置都扫。找不到（如历史单轮
    ``--ephemeral`` 无落盘）返回 []。
    """
    roots = [codex_home / "sessions", codex_home / ".codex" / "sessions"]
    files: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for workspace in sorted(root.iterdir()):
            if workspace.is_dir():
                files.extend(sorted(workspace.glob("*.jsonl")))
            elif workspace.suffix == ".jsonl":
                files.append(workspace)
    if not files:
        return []

    events: list[dict[str, Any]] = []
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                events.append(json.loads(stripped))
            except json.JSONDecodeError:
                continue
    return events


# ---------- 纯函数（移植自 Harbor codex.py）-----------------------------------


def _extract_message_text(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _parse_output_blob(raw: Any) -> tuple[str | None, dict[str, Any] | None]:
    """codex tool output → (文本, metadata)。"""
    if raw is None:
        return None, None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return raw, None
    else:
        parsed = raw
    if isinstance(parsed, dict):
        output = parsed.get("output")
        if output is None and parsed:
            output = json.dumps(parsed, ensure_ascii=False)
        metadata = parsed.get("metadata")
        return output, metadata if isinstance(metadata, dict) else None
    return str(parsed), None


def _metrics_from_token_count_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    info = payload.get("info")
    if not isinstance(info, dict):
        return None
    last_usage = info.get("last_token_usage")
    if not isinstance(last_usage, dict):
        return None
    return {
        "prompt_tokens": last_usage.get("input_tokens") or None,
        "completion_tokens": last_usage.get("output_tokens") or None,
        "cached_tokens": last_usage.get("cached_input_tokens") or None,
        "extra": {
            "reasoning_output_tokens": last_usage.get("reasoning_output_tokens"),
            "total_tokens": last_usage.get("total_tokens"),
        },
    }


# ---------- 归一化 → ATIF steps ------------------------------------------------


def _normalize(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """codex 会话事件 → 归一化事件（message / tool_call / bundled 分组）。"""
    normalized: list[dict[str, Any]] = []
    pending_calls: dict[str, dict[str, Any]] = {}
    pending_reasoning: str | None = None
    codex_turn_id: str | None = None
    api_call_index = 1
    current_api_call_id = f"api_call_{api_call_index}"
    api_call_metrics: dict[str, dict[str, Any]] = {}
    saw_model_output_in_api_call = False
    tool_order_counter = 0

    def record_model_output() -> None:
        nonlocal saw_model_output_in_api_call
        saw_model_output_in_api_call = True

    def finish_api_call(token_count_payload: dict[str, Any]) -> None:
        nonlocal api_call_index, current_api_call_id, saw_model_output_in_api_call
        nonlocal tool_order_counter
        if not saw_model_output_in_api_call:
            return
        metrics = _metrics_from_token_count_payload(token_count_payload)
        if metrics:
            api_call_metrics[current_api_call_id] = metrics
        api_call_index += 1
        current_api_call_id = f"api_call_{api_call_index}"
        saw_model_output_in_api_call = False
        tool_order_counter = 0

    for event in events:
        etype = event.get("type")
        payload = event.get("payload", {})
        timestamp = event.get("timestamp")

        if etype == "event_msg" and isinstance(payload, dict):
            event_type = payload.get("type")
            if event_type in {"task_started", "turn_started"}:
                turn_id = payload.get("turn_id")
                codex_turn_id = turn_id if isinstance(turn_id, str) else None
            elif event_type in {"task_complete", "turn_complete", "turn_aborted"}:
                codex_turn_id = None
            elif event_type == "token_count":
                finish_api_call(payload)
            continue

        if etype == "turn_context":
            turn_id = payload.get("turn_id") if isinstance(payload, dict) else None
            if isinstance(turn_id, str) and codex_turn_id is None:
                codex_turn_id = turn_id
            continue

        if etype != "response_item":
            continue

        payload_type = payload.get("type")
        if payload_type == "reasoning":
            summary = payload.get("summary")
            if isinstance(summary, list) and summary:
                parts: list[str] = []
                for item in summary:
                    if isinstance(item, str):
                        parts.append(item)
                    elif isinstance(item, dict):
                        text = item.get("text")
                        if isinstance(text, str):
                            parts.append(text)
                pending_reasoning = "\n".join(parts) if parts else None
            else:
                pending_reasoning = None
            continue

        if payload_type == "message":
            content = payload.get("content", [])
            text = _extract_message_text(content)
            normalized.append(
                {
                    "kind": "message",
                    "api_call_id": current_api_call_id,
                    "codex_turn_id": codex_turn_id,
                    "timestamp": timestamp,
                    "role": payload.get("role", "user"),
                    "text": text,
                    "reasoning": pending_reasoning if payload.get("role") == "assistant" else None,
                }
            )
            if payload.get("role") == "assistant":
                record_model_output()
            pending_reasoning = None
            continue

        if payload_type == "web_search_call":
            action = payload.get("action") or {}
            arguments: dict[str, Any] = {"action_type": action.get("type", "")}
            for key in ("query", "queries", "url"):
                if key in action:
                    arguments[key] = action[key]
            normalized.append(
                {
                    "kind": "tool_call",
                    "api_call_id": current_api_call_id,
                    "codex_turn_id": codex_turn_id,
                    "tool_order": tool_order_counter,
                    "timestamp": timestamp,
                    "call_id": "",
                    "tool_name": "web_search_call",
                    "arguments": arguments,
                    "raw_arguments": None,
                    "reasoning": pending_reasoning,
                    "status": payload.get("status"),
                    "message": None,
                }
            )
            tool_order_counter += 1
            record_model_output()
            pending_reasoning = None
            continue

        if payload_type in {"function_call", "custom_tool_call"}:
            call_id = payload.get("call_id")
            if not call_id:
                continue
            raw_args_key = "arguments" if payload_type == "function_call" else "input"
            raw_arguments = payload.get(raw_args_key)
            try:
                parsed_args = json.loads(raw_arguments)
            except (json.JSONDecodeError, TypeError):
                if isinstance(raw_arguments, str):
                    parsed_args = {"input": raw_arguments}
                elif raw_arguments is None:
                    parsed_args = {}
                else:
                    parsed_args = {"value": raw_arguments}

            pending_calls[call_id] = {
                "kind": "tool_call",
                "api_call_id": current_api_call_id,
                "codex_turn_id": codex_turn_id,
                "tool_order": tool_order_counter,
                "timestamp": timestamp,
                "call_id": call_id,
                "tool_name": payload.get("name") or "",
                "arguments": parsed_args,
                "raw_arguments": raw_arguments,
                "reasoning": pending_reasoning,
                "status": payload.get("status"),
                "message": None,
            }
            tool_order_counter += 1
            record_model_output()
            pending_reasoning = None
            continue

        if payload_type in {"function_call_output", "custom_tool_call_output"}:
            call_id = payload.get("call_id")
            output_text, metadata = _parse_output_blob(payload.get("output"))

            call_info = pending_calls.pop(call_id, None) if call_id else None
            if call_info is None:
                call_info = {
                    "kind": "tool_call",
                    "api_call_id": current_api_call_id,
                    "codex_turn_id": codex_turn_id,
                    "tool_order": tool_order_counter,
                    "timestamp": timestamp,
                    "call_id": call_id or "",
                    "tool_name": payload.get("name", "") or "",
                    "arguments": {},
                    "raw_arguments": None,
                    "reasoning": pending_reasoning,
                    "status": None,
                    "message": None,
                }
                tool_order_counter += 1
            call_info["output"] = output_text
            call_info["metadata"] = metadata
            call_info["timestamp"] = call_info.get("timestamp") or timestamp
            normalized.append(call_info)
            pending_reasoning = None
            continue

    for norm_event in normalized:
        api_id = norm_event.get("api_call_id")
        if isinstance(api_id, str) and api_id in api_call_metrics:
            norm_event["metrics"] = api_call_metrics[api_id]

    return _group_events_by_api_call_id(normalized)


def _group_events_by_api_call_id(
    normalized_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """同一 codex 模型请求的 assistant 事件合并成一个 step。"""
    result: list[dict[str, Any]] = []
    groups: dict[str, dict[str, Any]] = {}
    group_order: list[str] = []

    def flush() -> None:
        for group_id in group_order:
            group = groups.pop(group_id, None)
            if group is None:
                continue
            group["tool_calls"].sort(key=lambda tc: tc.get("tool_order", 0))
            message_parts = [
                p for p in group.pop("message_parts") if isinstance(p, str) and p
            ]
            group["text"] = "\n\n".join(message_parts)
            result.append(group)
        group_order.clear()

    for event in normalized_events:
        api_call_id = event.get("api_call_id")
        kind = event.get("kind")
        role = event.get("role")

        if kind == "message" and role != "assistant":
            flush()
            result.append(event)
            continue
        if not isinstance(api_call_id, str):
            flush()
            result.append(event)
            continue

        if api_call_id not in groups:
            groups[api_call_id] = {
                "kind": "bundled",
                "api_call_id": api_call_id,
                "codex_turn_id": event.get("codex_turn_id"),
                "timestamp": event.get("timestamp"),
                "message_parts": [],
                "reasoning": None,
                "tool_calls": [],
                "metrics": event.get("metrics"),
            }
            group_order.append(api_call_id)

        group = groups[api_call_id]
        if kind == "message":
            text = event.get("text")
            if isinstance(text, str) and text:
                group["message_parts"].append(text)
            if event.get("reasoning"):
                group["reasoning"] = event["reasoning"]
            if event.get("timestamp"):
                group["timestamp"] = event["timestamp"]
        elif kind == "tool_call":
            group["tool_calls"].append(event)
            if not group["reasoning"] and event.get("reasoning"):
                group["reasoning"] = event["reasoning"]
            if not group.get("metrics") and event.get("metrics"):
                group["metrics"] = event["metrics"]

    flush()
    return result


def _convert_event_to_step(
    event: dict[str, Any], step_id: int, *, default_model_name: str | None
) -> Step:
    """归一化事件 → ATIF Step。"""
    timestamp = event.get("timestamp")

    if event["kind"] == "message":
        role = event.get("role", "user")
        source = "agent" if role == "assistant" else ("user" if role == "user" else "system")
        return Step(
            step_id=step_id,
            timestamp=timestamp,
            source=source,
            message=event.get("text", ""),
            model_name=default_model_name if source == "agent" else None,
            reasoning_content=event.get("reasoning") if source == "agent" else None,
            llm_call_count=1 if source == "agent" else None,
            extra=event.get("extra") or None,
        )

    if event["kind"] == "bundled":
        text = event.get("text", "")
        tool_calls: list[ToolCall] = []
        observation_results: list[ObservationResult] = []
        tool_details: dict[str, Any] = {}
        for tc in event.get("tool_calls", []):
            call_id = tc.get("call_id", "")
            arguments = tc.get("arguments") or {}
            if not isinstance(arguments, dict):
                arguments = {"value": arguments}
            tool_calls.append(
                ToolCall(
                    tool_call_id=call_id,
                    function_name=tc.get("tool_name", ""),
                    arguments=arguments,
                )
            )
            observation_results.append(
                ObservationResult(
                    source_call_id=call_id or None,
                    content=tc.get("output"),
                )
            )
            details: dict[str, Any] = {}
            for source_key, target_key in (
                ("metadata", "metadata"),
                ("raw_arguments", "raw_arguments"),
                ("status", "status"),
            ):
                value = tc.get(source_key)
                if value:
                    details[target_key] = value
            if details:
                tool_details[call_id] = details

        extra: dict[str, Any] | None = None
        if event.get("api_call_id"):
            extra = {"api_call_id": event["api_call_id"]}
        if event.get("codex_turn_id"):
            extra = extra or {}
            extra["codex_turn_id"] = event["codex_turn_id"]
        if tool_details:
            extra = extra or {}
            extra["tool_call_details"] = tool_details

        return Step(
            step_id=step_id,
            timestamp=timestamp,
            source="agent",
            message=text,
            model_name=default_model_name,
            reasoning_content=event.get("reasoning") or None,
            tool_calls=tool_calls or None,
            observation=Observation(results=observation_results) if observation_results else None,
            metrics=Metrics(**event["metrics"]) if event.get("metrics") else None,
            llm_call_count=1,
            extra=extra,
        )

    raise ValueError(f"unsupported event kind {event.get('kind')!r}")


def convert_events_to_trajectory(
    events: list[dict[str, Any]], *, attempt_id: str
) -> Trajectory | None:
    """codex 会话事件 → ATIF Trajectory。无有效 steps 返回 None。"""
    if not events:
        return None

    session_id = attempt_id
    agent_version: str = "unknown"
    agent_extra: dict[str, Any] | None = None
    default_model_name: str | None = None

    for event in events:
        if event.get("type") == "session_meta":
            payload = event.get("payload", {})
            if isinstance(payload, dict):
                sid = payload.get("id")
                if isinstance(sid, str) and sid:
                    session_id = sid
                ver = payload.get("cli_version")
                if isinstance(ver, str) and ver:
                    agent_version = ver
                extra: dict[str, Any] = {}
                for key in ("originator", "cwd", "git", "instructions"):
                    value = payload.get(key)
                    if value is not None:
                        extra[key] = value
                agent_extra = extra or None
            break

    for event in events:
        if event.get("type") == "turn_context":
            payload = event.get("payload", {})
            model_name = payload.get("model") if isinstance(payload, dict) else None
            if isinstance(model_name, str):
                default_model_name = model_name
                break

    normalized = _normalize(events)
    steps: list[Step] = []
    for event in normalized:
        try:
            step = _convert_event_to_step(
                event, len(steps) + 1, default_model_name=default_model_name
            )
        except (ValueError, TypeError):
            continue
        if step.source == "agent" and not step.model_name and default_model_name:
            step.model_name = default_model_name
        steps.append(step)

    if not steps:
        return None

    total_metrics: FinalMetrics | None = None
    for event in reversed(events):
        if event.get("type") != "event_msg":
            continue
        payload = event.get("payload", {})
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            continue
        info = payload.get("info")
        if not isinstance(info, dict):
            continue
        total_usage = info.get("total_token_usage")
        if not isinstance(total_usage, dict):
            continue
        prompt_tokens = total_usage.get("input_tokens")
        completion_tokens = total_usage.get("output_tokens")
        cached_tokens = total_usage.get("cached_input_tokens")
        reasoning_tokens = total_usage.get("reasoning_output_tokens")
        overall_tokens = total_usage.get("total_tokens")
        total_metrics = FinalMetrics(
            total_prompt_tokens=prompt_tokens or None,
            total_completion_tokens=completion_tokens or None,
            total_cached_tokens=cached_tokens or None,
            total_cost_usd=info.get("total_cost") or info.get("cost_usd"),
            total_steps=len(steps),
            extra={
                "reasoning_output_tokens": reasoning_tokens,
                "total_tokens": overall_tokens,
                "last_token_usage": info.get("last_token_usage"),
            },
        )
        break

    return Trajectory(
        schema_version="ATIF-v1.7",
        session_id=session_id,
        trajectory_id=attempt_id,
        agent=Agent(
            name="codex",
            version=agent_version,
            model_name=default_model_name,
            extra=agent_extra,
        ),
        steps=steps,
        notes=(
            "reconstructed from Codex sandbox session rollout "
            f"(producer=octagon-atif-v1, session={session_id})"
        ),
        final_metrics=total_metrics,
    )
