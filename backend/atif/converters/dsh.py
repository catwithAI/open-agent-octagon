"""dsh 沙盒会话 → ATIF-v1.7 trajectory。

dsh（DeepSeek Harness）把整条会话写在
``$DSH_SESSION_ROOT/<slug>/<session-id>/session.jsonl``，其中 ``DSH_SESSION_ROOT``
由 adapter 指向 ``<home>/dsh_sessions``（``backend/adapters/dsh.py:541``），
session-id 恒为 ``octagon-<attempt_id>``（同文件 :616）——所以定位会话是确定的，
不像 claude-code 那样要靠 cwd 猜。

**一步 = 一次 LLM 推理**，由 ``step/start`` / ``step/end`` 成对界定。dsh 的
``assistant/message`` 已经把该步的 reasoning / text / tool-call 聚合好了，所以
本转换器**完全不看**那几千条 ``reasoning-chunks`` / ``text-chunks`` /
``tool-call-chunks`` 流式增量——它们是同一份内容的分片，重复消费只会自找对齐
麻烦。实测一个 14 步的会话里流式 chunk 占 1052 条记录中的 971 条。

事件契约（实测 dsh 0.1.0rc6，2026-09-23 att_24a673bb5a23）：
- ``session``：``{id, createdAt, cwd}``，首行。
- ``request/header``：``{header:{config:{provider,model,maxTokens}, system, tools}}``
  —— 取 model 作默认 model_name、tools 作 agent.tool_definitions。
- ``user/message``：``{content:[{type:"text",text}]}``。
- ``step/start`` / ``step/end``：``{turn, step}``。
- ``assistant/message``：``{turn, step, usage:{inputTokens,outputTokens},
  message:{id, role, content:[{type:"reasoning"|"text"|"tool-call", ...}]}}``。
- ``tool/call``：``{turn, step, callId, name, arguments}``（arguments 是 JSON **字符串**）。
- ``tool/result``：``{turn, step, message:{content:[{type:"tool-result",
  toolCallId, content:[{type:"text",text}]}]}}``。
- ``turn/end``：``{turn, reason:{kind}}``。
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

#: 流式增量事件：与 assistant/message 的聚合内容重复，一律跳过。
_CHUNK_TYPES = frozenset({
    "reasoning-chunks", "text-chunks", "tool-call-chunks", "assistant/chunk",
})


def find_session_events(dsh_home: Path, attempt_id: str | None = None) -> list[dict[str, Any]]:
    """读 dsh 会话转录，返回有序事件。找不到返回 []。

    ``dsh_home`` 是 agent 的 home 根（沙箱 ``sandbox_home``，宿主机 attempt 根）。
    session-id 确定（``octagon-<attempt_id>``），优先精确匹配；attempt_id 未给或
    没命中时回退到「最新修改的那个 session.jsonl」——重试会清空 dsh_sessions
    （见 adapter :608），正常情况下目录里只有一个会话。
    """
    root = dsh_home / "dsh_sessions"
    if not root.is_dir():
        return []

    candidates: list[Path] = []
    if attempt_id:
        candidates = sorted(root.glob(f"*/octagon-{attempt_id}/session.jsonl"))
    if not candidates:
        candidates = sorted(
            root.glob("*/*/session.jsonl"),
            key=lambda p: p.stat().st_mtime if p.is_file() else 0.0,
            reverse=True,
        )
    for path in candidates:
        events = _read_jsonl(path)
        if events:
            return events
    return []


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            # 会话可能在 agent 被 kill 时截断在半行；丢这一行不影响前面的步。
            continue
        if isinstance(record, dict) and record.get("type") not in _CHUNK_TYPES:
            out.append(record)
    return out


def _iso(millis: Any) -> str | None:
    """dsh 的时间戳是 epoch 毫秒。非法值返回 None（schema 允许 timestamp 缺省）。"""
    if not isinstance(millis, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(millis / 1000.0, tz=timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
    except (OverflowError, OSError, ValueError):
        return None


def _text_blocks(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    return "".join(
        block.get("text") or ""
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _reasoning_blocks(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    return "".join(
        block.get("text") or ""
        for block in content
        if isinstance(block, dict) and block.get("type") == "reasoning"
    )


def _parse_arguments(raw: Any) -> dict[str, Any]:
    """tool-call 的 arguments 是 JSON 字符串；非 dict 结果包进 ``{"_raw": ...}``
    —— schema 要求 arguments 是 dict，不能让一个畸形参数毁掉整条 trajectory。"""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw}
    return parsed if isinstance(parsed, dict) else {"_raw": parsed}


def _tool_result_text(message: Any) -> str:
    """tool/result 的正文：``message.content[].content[]`` 两层嵌套。"""
    if not isinstance(message, dict):
        return ""
    parts: list[str] = []
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        inner = block.get("content")
        if isinstance(inner, str):
            parts.append(inner)
        elif isinstance(inner, list):
            parts.append(_text_blocks(inner))
    return "".join(parts)


def describe_empty(events: list[dict[str, Any]]) -> str | None:
    """转录存在却产不出 step 时，给出**真实**原因。

    「文件不存在」和「文件在、但那一轮以错误收场」是两回事：前者是采集缺陷，
    后者是 agent/上游真的失败了。统一报成「no usable session transcript」会把
    后者伪装成前者——而把设施故障误读成 agent 表现，正是本项目反复踩的坑。

    实测 att_eae0b8a4864a：13 条记录、有 ``step/start``，但 ``turn/end`` 带
    ``reason.kind=error``（上游 404 explicit source in model spec does not
    exist），没有任何 ``assistant/message``。
    """
    if not events:
        return None
    for event in reversed(events):
        if event.get("type") != "turn/end":
            continue
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        reason = data.get("reason") if isinstance(data.get("reason"), dict) else {}
        kind = reason.get("kind")
        if kind and kind != "completed":
            error = reason.get("error")
            detail = ""
            if isinstance(error, dict):
                detail = str(error.get("message") or "")
            elif isinstance(error, str):
                detail = error
            suffix = f": {detail[:300]}" if detail else ""
            return f"session transcript present but the turn ended with {kind!r}{suffix}"
    return "session transcript present but contains no completed reasoning step"


def convert_events_to_trajectory(
    events: list[dict[str, Any]], *, attempt_id: str
) -> Trajectory | None:
    """dsh 会话事件 → ATIF Trajectory。无有效 step 返回 None。"""
    if not events:
        return None

    session_id = attempt_id
    default_model: str | None = None
    provider: str | None = None
    tool_definitions: list[dict[str, Any]] | None = None
    system_prompt: str | None = None
    context_window: Any = None
    cwd: str | None = None

    for event in events:
        etype = event.get("type")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if etype == "session":
            session_id = event.get("id") or attempt_id
            cwd = event.get("cwd")
        elif etype == "request/header":
            header = data.get("header") if isinstance(data.get("header"), dict) else {}
            config = header.get("config") if isinstance(header.get("config"), dict) else {}
            default_model = config.get("model") or default_model
            provider = config.get("provider") or provider
            system_prompt = header.get("system") or system_prompt
            tools = header.get("tools")
            if isinstance(tools, list):
                tool_definitions = [t for t in tools if isinstance(t, dict)]
        elif etype == "request/context":
            context_window = data.get("contextWindow") or context_window

    # ---- 按 (turn, step) 归并 ----
    # dsh 的 step 编号在每个 turn 内从 1 重新开始，单看 step 会把不同 turn 的
    # 同号步并到一起；必须用 (turn, step) 复合键。
    buckets: dict[tuple[Any, Any], dict[str, Any]] = {}
    order: list[tuple[Any, Any]] = []

    def bucket(turn: Any, step: Any) -> dict[str, Any]:
        key = (turn, step)
        if key not in buckets:
            buckets[key] = {
                "timestamp": None, "message": "", "reasoning": "",
                "tool_calls": [], "results": [], "usage": None,
            }
            order.append(key)
        return buckets[key]

    for event in events:
        etype = event.get("type")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if etype not in (
            "step/start", "assistant/message", "tool/call", "tool/result"
        ):
            continue
        slot = bucket(data.get("turn"), data.get("step"))
        when = _iso(event.get("time") if "time" in event else event.get("time0"))

        if etype == "step/start":
            slot["timestamp"] = slot["timestamp"] or when
        elif etype == "assistant/message":
            message = data.get("message") if isinstance(data.get("message"), dict) else {}
            content = message.get("content")
            slot["message"] += _text_blocks(content)
            slot["reasoning"] += _reasoning_blocks(content)
            usage = data.get("usage")
            if isinstance(usage, dict):
                slot["usage"] = usage
            slot["timestamp"] = slot["timestamp"] or when
        elif etype == "tool/call":
            call_id = data.get("callId")
            if call_id:
                slot["tool_calls"].append(ToolCall(
                    tool_call_id=str(call_id),
                    function_name=str(data.get("name") or "unknown"),
                    arguments=_parse_arguments(data.get("arguments")),
                ))
        elif etype == "tool/result":
            message = data.get("message") if isinstance(data.get("message"), dict) else {}
            source = message.get("source") if isinstance(message.get("source"), dict) else {}
            slot["results"].append(ObservationResult(
                source_call_id=str(source.get("callId")) if source.get("callId") else None,
                content=_tool_result_text(message),
            ))

    steps: list[Step] = []
    for key in order:
        slot = buckets[key]
        if not (slot["message"] or slot["reasoning"] or slot["tool_calls"]):
            # step/start 有了但 agent 被 kill，没产出任何内容——跳过空步，
            # 否则 step_id 连续性靠补空壳维持，反而误导归因。
            continue
        calls = slot["tool_calls"] or None
        call_ids = {c.tool_call_id for c in calls or []}
        # schema 约束：observation 里的 source_call_id 必须配得上**同一步**的
        # tool_calls。配不上的（跨步结果、非标准调用）置 None 而不是丢弃——
        # 内容本身是真实观测，只是失去了配对关系。
        results = [
            r if (r.source_call_id is None or r.source_call_id in call_ids)
            else ObservationResult(source_call_id=None, content=r.content)
            for r in slot["results"]
        ]
        usage = slot["usage"] or {}
        metrics = None
        if usage.get("inputTokens") is not None or usage.get("outputTokens") is not None:
            metrics = Metrics(
                prompt_tokens=usage.get("inputTokens"),
                completion_tokens=usage.get("outputTokens"),
            )
        steps.append(Step(
            step_id=len(steps) + 1,
            timestamp=slot["timestamp"],
            source="agent",
            model_name=default_model,
            message=slot["message"],
            reasoning_content=slot["reasoning"] or None,
            tool_calls=calls,
            observation=Observation(results=results) if results else None,
            metrics=metrics,
            llm_call_count=1,
            extra={"turn": key[0], "step": key[1]},
        ))

    if not steps:
        return None

    total_in = sum(
        (buckets[k]["usage"] or {}).get("inputTokens") or 0 for k in order
    )
    total_out = sum(
        (buckets[k]["usage"] or {}).get("outputTokens") or 0 for k in order
    )
    agent_extra: dict[str, Any] = {}
    if provider:
        agent_extra["provider"] = provider
    if context_window:
        agent_extra["context_window"] = context_window
    if system_prompt:
        agent_extra["system_prompt"] = system_prompt
    if cwd:
        agent_extra["cwd"] = cwd

    return Trajectory(
        schema_version="ATIF-v1.7",
        session_id=session_id,
        trajectory_id=attempt_id,
        agent=Agent(
            name="dsh",
            version="unknown",  # dsh 的会话转录里不带 runtime 版本号。
            model_name=default_model,
            tool_definitions=tool_definitions,
            extra=agent_extra or None,
        ),
        steps=steps,
        notes=(
            "reconstructed from dsh sandbox session transcript "
            f"(producer=octagon-atif-v1, session={session_id})"
        ),
        final_metrics=FinalMetrics(
            total_prompt_tokens=total_in or None,
            total_completion_tokens=total_out or None,
            total_steps=len(steps),
        ),
    )
