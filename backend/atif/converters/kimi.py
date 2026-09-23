"""kimi-code 沙盒会话 → ATIF-v1.7 trajectory。

源：attempt 目录的 ``events.jsonl``——kimi ``-p --output-format stream-json``
的事件流（adapter 逐行落盘，每行 ``{"role", "content", ...}``）。

注意：agent-octagon 的 kimi 契约（0.29.1 实测）是 `role/content` 流，**不是**
Harbor ``kimi_cli.py`` 读的 jsonrpc wire 协议（那是另一条 kimi 通道）。本转换器
按 agent-octagon 的 role/content 事件结构写：

- ``role=user`` → user step；
- ``role=assistant`` → agent step（message=content，``tool_calls`` → tool_calls）；
- ``role=thinking`` / ``type=think`` → 附到下一个 assistant step 的
  ``reasoning_content``；
- ``type=session.resume_hint`` → session_id。

sandbox 里 kimi 是 1.50.0（与 0.29.1 契约可能不同）——跑通后需按实际事件流核对
（docs/agents.md 已预警）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..schema import Agent, FinalMetrics, Metrics, Step, ToolCall, Trajectory


def find_session_events(attempt_dir: Path) -> list[dict[str, Any]]:
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


def _role(event: dict[str, Any]) -> str:
    return str(event.get("role") or "")


def _content_text(event: dict[str, Any]) -> str:
    content = event.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return ""


def convert_events_to_trajectory(
    events: list[dict[str, Any]],
    *,
    attempt_id: str,
    default_model_name: str | None = None,
) -> Trajectory | None:
    if not events:
        return None

    session_id: str | None = None
    for event in events:
        if event.get("type") == "session.resume_hint":
            sid = event.get("session_id")
            if isinstance(sid, str) and sid:
                session_id = sid
                break

    steps: list[Step] = []
    pending_reasoning: list[str] = []

    def flush_reasoning() -> None:
        pending_reasoning.clear()

    for event in events:
        role = _role(event)
        if role == "meta":
            continue
        if role == "user":
            text = _content_text(event)
            if text.strip():
                steps.append(
                    Step(
                        step_id=len(steps) + 1,
                        source="user",
                        message=text,
                    )
                )
            continue

        # assistant（含 tool_calls）与 thinking 归入同一个 agent step。
        if role == "thinking" or event.get("type") == "think":
            text = _content_text(event)
            if text.strip():
                pending_reasoning.append(text)
            continue

        if role == "assistant":
            text = _content_text(event)
            raw_calls = event.get("tool_calls")
            if not isinstance(raw_calls, list):
                nested = event.get("message")
                raw_calls = (
                    nested.get("tool_calls") if isinstance(nested, dict) else None
                )
            tool_calls: list[ToolCall] = []
            for call in raw_calls or []:
                if not isinstance(call, dict):
                    continue
                arguments = call.get("arguments")
                if not isinstance(arguments, dict):
                    arguments = (
                        {"arguments": arguments}
                        if arguments is not None
                        else {}
                    )
                tool_calls.append(
                    ToolCall(
                        tool_call_id=str(call.get("id") or ""),
                        function_name=str(call.get("name") or ""),
                        arguments=arguments,
                    )
                )

            step_kwargs: dict[str, Any] = {
                "step_id": len(steps) + 1,
                "source": "agent",
                "message": text,
                "model_name": default_model_name,
                "llm_call_count": 1,
            }
            if pending_reasoning:
                step_kwargs["reasoning_content"] = "\n\n".join(pending_reasoning)
                flush_reasoning()
            if tool_calls:
                step_kwargs["tool_calls"] = tool_calls
            steps.append(Step(**step_kwargs))

    if not steps:
        return None

    return Trajectory(
        schema_version="ATIF-v1.7",
        session_id=session_id or attempt_id,
        trajectory_id=attempt_id,
        agent=Agent(
            name="kimi-code",
            version="unknown",
            model_name=default_model_name,
        ),
        steps=steps,
        notes=(
            "reconstructed from kimi-code stream-json event stream "
            f"(producer=octagon-atif-v1, session={session_id or attempt_id})"
        ),
        final_metrics=FinalMetrics(total_steps=len(steps)),
    )
