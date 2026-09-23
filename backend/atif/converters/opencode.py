"""opencode / mimo-code 沙盒会话 → ATIF-v1.7 trajectory。

源：attempt 目录的 ``events.jsonl``——opencode ``run --format=json`` 的 CLI
事件流由 adapter 逐行落盘（与 Harbor 读的 ``opencode.txt`` 同一份事件）。

映射移植自 Harbor ``src/harbor/agents/installed/opencode.py``：
- ``step_start`` / ``step_finish`` 划分 agent turn（一次 agent step）；
- turn 内 ``text`` → message、``reasoning`` → reasoning_content、
  ``tool``（part.state.input/output）→ tool_calls + observation 同步配对；
- ``step_finish`` 的 ``part.tokens/cost`` → Metrics；
- 顶层 ``user`` 事件（新版 opencode）作为首个 user step；缺失时由调用方注入。
"""

from __future__ import annotations

import json
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


def find_session_events(attempt_dir: Path) -> list[dict[str, Any]]:
    """读 ``attempt_dir/events.jsonl``（adapter 逐行落盘的 CLI 事件流）。"""
    path = attempt_dir / "events.jsonl"
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            events.append(json.loads(stripped))
        except json.JSONDecodeError:
            continue
    return events


def _millis_to_iso(ts: int | float | None) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()
    except (OSError, ValueError, OverflowError):
        return None


def _user_event_text(event: dict[str, Any]) -> str:
    """opencode ``user`` 事件的 prompt 文本。"""
    payload = event.get("payload")
    if isinstance(payload, dict):
        return str(payload.get("prompt") or "")
    text = event.get("text")
    return text if isinstance(text, str) else ""


def convert_events_to_trajectory(
    events: list[dict[str, Any]],
    *,
    attempt_id: str,
    agent_name: str = "opencode",
    default_model_name: str | None = None,
    instruction: str | None = None,
) -> Trajectory | None:
    """opencode/mimo CLI 事件 → ATIF Trajectory。无有效 steps 返回 None。"""
    if not events:
        return None

    session_id: str | None = None
    for event in events:
        sid = event.get("sessionID")
        if isinstance(sid, str) and sid:
            session_id = sid
            break

    turns: list[dict[str, Any]] = []
    current_turn: dict[str, Any] | None = None
    user_message: str | None = None
    user_timestamp: int | None = None

    for event in events:
        etype = event.get("type")
        if etype == "user":
            if user_message is None:
                user_message = _user_event_text(event) or None
                user_timestamp = event.get("timestamp")
            continue
        if etype == "step_start":
            current_turn = {
                "parts": [],
                "finish": None,
                "timestamp": event.get("timestamp"),
            }
            continue
        if etype == "step_finish":
            if current_turn is not None:
                current_turn["finish"] = event.get("part", {})
                turns.append(current_turn)
                current_turn = None
            continue
        if current_turn is not None and etype in ("text", "reasoning", "tool_use"):
            current_turn["parts"].append(event.get("part", {}))

    steps: list[Step] = []
    total_cost = 0.0
    total_input_tokens = 0
    total_output_tokens = 0
    total_cache_read = 0

    for turn in turns:
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls_list: list[ToolCall] = []
        observation_results: list[ObservationResult] = []
        timestamp = _millis_to_iso(turn.get("timestamp"))

        for part in turn["parts"]:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "text":
                text = part.get("text", "")
                if text:
                    text_parts.append(text)
            elif ptype == "reasoning":
                reasoning = part.get("text", "")
                if reasoning:
                    reasoning_parts.append(reasoning)
            elif ptype == "tool":
                state = part.get("state", {})
                if not isinstance(state, dict):
                    state = {}
                tool_name = part.get("tool", "")
                tool_input = state.get("input", {})
                tool_output = state.get("output")
                call_id = part.get("callID", part.get("id", ""))
                if not isinstance(tool_input, dict):
                    tool_input = {"value": tool_input} if tool_input else {}

                tool_calls_list.append(
                    ToolCall(
                        tool_call_id=call_id,
                        function_name=tool_name,
                        arguments=tool_input,
                    )
                )
                if tool_output is not None:
                    observation_results.append(
                        ObservationResult(
                            source_call_id=call_id or None,
                            content=str(tool_output),
                        )
                    )

        finish = turn.get("finish", {})
        if not isinstance(finish, dict):
            finish = {}
        tokens = finish.get("tokens", {})
        if not isinstance(tokens, dict):
            tokens = {}
        cost = finish.get("cost", 0) or 0
        input_tok = tokens.get("input", 0) or 0
        output_tok = tokens.get("output", 0) or 0
        reasoning_tok = tokens.get("reasoning", 0) or 0
        cache = tokens.get("cache", {})
        if not isinstance(cache, dict):
            cache = {}
        cache_read = cache.get("read", 0) or 0
        cache_write = cache.get("write", 0) or 0

        total_cost += cost
        total_input_tokens += input_tok + cache_read
        total_output_tokens += output_tok
        total_cache_read += cache_read

        metrics: Metrics | None = None
        if input_tok or output_tok or cache_read:
            metrics = Metrics(
                prompt_tokens=input_tok + cache_read,
                completion_tokens=output_tok,
                cached_tokens=cache_read if cache_read else None,
                cost_usd=cost if cost else None,
                extra={
                    k: v
                    for k, v in {
                        "reasoning_tokens": reasoning_tok,
                        "cache_write_tokens": cache_write,
                    }.items()
                    if v
                }
                or None,
            )

        message_text = "\n".join(text_parts) if text_parts else ""
        step_kwargs: dict[str, Any] = {
            "step_id": len(steps) + 1,
            "timestamp": timestamp,
            "source": "agent",
            "message": message_text,
            "model_name": default_model_name,
            "llm_call_count": 1,
        }
        if reasoning_parts:
            step_kwargs["reasoning_content"] = "\n\n".join(reasoning_parts)
        if tool_calls_list:
            step_kwargs["tool_calls"] = tool_calls_list
        if observation_results:
            step_kwargs["observation"] = Observation(results=observation_results)
        if metrics:
            step_kwargs["metrics"] = metrics
        steps.append(Step(**step_kwargs))

    if not steps:
        return None

    # 顶层 user 事件（新版 opencode）作为首个 user step；缺失时回退调用方注入的
    # instruction。
    user_text = user_message or instruction
    if user_text and not any(s.source == "user" for s in steps):
        steps.insert(
            0,
            Step(
                step_id=1,
                timestamp=_millis_to_iso(user_timestamp),
                source="user",
                message=user_text,
            ),
        )
        for index, step in enumerate(steps, start=1):
            step.step_id = index

    return Trajectory(
        schema_version="ATIF-v1.7",
        session_id=session_id or attempt_id,
        trajectory_id=attempt_id,
        agent=Agent(
            name=agent_name,
            version="unknown",
            model_name=default_model_name,
        ),
        steps=steps,
        notes=(
            "reconstructed from opencode-family run --format=json event stream "
            f"(producer=octagon-atif-v1, session={session_id or attempt_id})"
        ),
        final_metrics=FinalMetrics(
            total_prompt_tokens=total_input_tokens or None,
            total_completion_tokens=total_output_tokens or None,
            total_cached_tokens=total_cache_read or None,
            total_cost_usd=total_cost if total_cost else None,
            total_steps=len(steps),
        ),
    )
