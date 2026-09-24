"""kimi-code 沙盒会话 → ATIF-v1.7 trajectory。

kimi 把会话写在 ``<home>/.kimi/sessions/<workdir-hash>/<session-uuid>/``，目录里
三个文件：

- ``wire.jsonl``：**本转换器的唯一数据源**。带时间戳的事件流，含显式步号
  （``StepBegin.payload.n``）、思考/正文（``ContentPart``）、工具调用与结果
  （``ToolCall`` / ``ToolResult``），以及**真实的分步 token**
  （``StatusUpdate.payload.token_usage``）。
- ``context.jsonl``：同一段对话的扁平快照（OpenAI 风格 role 记录）。内容与
  wire 重复，但**没有时间戳**，token 只有 ``_usage`` 的累计上下文大小，要靠
  前后做差才能反推单步用量。仅在这里取一次 ``_system_prompt``。
- ``state.json``：审批/归档等会话状态，与对话无关。

选 wire 而非 context，是因为 ATIF 的 ``timestamp`` 和 ``metrics`` 在 context 里
要么缺失、要么只能估算——能拿到上报值就不该去做差推断。

**一步 = 一次 LLM 推理**，由 ``StepBegin`` 界定；该步的 ContentPart / ToolCall /
StatusUpdate / ToolResult 都归到当前步，直到下一个 StepBegin。

事件契约（实测 kimi-code 1.50，2026-09-23 att_5b9d7a896d8c）：
- 每行 ``{"timestamp": <epoch 秒 float>, "message": {"type": ..., "payload": ...}}``；
  首行是 ``{"protocol_version": "1.10", "type": "metadata"}``，无 message。
- ``TurnBegin.payload.user_input``：用户输入。
- ``StepBegin.payload.n``：步号，从 1 开始。
- ``ContentPart.payload``：``{"type":"think","think":...}`` 或 ``{"type":"text","text":...}``。
- ``ToolCall.payload``：``{"id", "function":{"name","arguments"}}``，arguments 是 JSON **字符串**。
- ``ToolResult.payload``：``{"tool_call_id", "return_value":{"is_error","output",...}}``。
- ``StatusUpdate.payload.token_usage``：``{"input_other","output","input_cache_read",
  "input_cache_creation"}`` —— 分别映射 prompt / completion / cached。
- 转录里**没有模型名**（wire 和 context 都没有），故 ``model_name`` 为 None。
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


def find_session_dir(kimi_home: Path) -> Path | None:
    """定位会话目录。

    ``<home>/.kimi/kimi.json`` 的 ``work_dirs[].last_session_id`` 给出最后一次
    会话的 uuid，优先按它精确匹配；读不到或没命中时回退到「最新修改的那个
    wire.jsonl 所在目录」。
    """
    root = kimi_home / ".kimi" / "sessions"
    if not root.is_dir():
        return None

    wanted: set[str] = set()
    manifest = kimi_home / ".kimi" / "kimi.json"
    if manifest.is_file():
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        for entry in (payload.get("work_dirs") or []):
            if isinstance(entry, dict) and entry.get("last_session_id"):
                wanted.add(str(entry["last_session_id"]))

    candidates = [p.parent for p in root.glob("*/*/wire.jsonl") if p.is_file()]
    if not candidates:
        return None
    for directory in candidates:
        if directory.name in wanted:
            return directory
    return max(candidates, key=lambda d: (d / "wire.jsonl").stat().st_mtime)


def find_session_events(kimi_home: Path) -> list[dict[str, Any]]:
    """读会话 wire 事件，返回有序事件。找不到返回 []。"""
    directory = find_session_dir(kimi_home)
    if directory is None:
        return []
    return _read_jsonl(directory / "wire.jsonl")


def find_system_prompt(kimi_home: Path) -> str | None:
    """从 context.jsonl 取系统提示（wire 里没有）。"""
    directory = find_session_dir(kimi_home)
    if directory is None:
        return None
    for record in _read_jsonl(directory / "context.jsonl"):
        if record.get("role") == "_system_prompt":
            content = record.get("content")
            if isinstance(content, str) and content:
                return content
    return None


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
            # agent 被 kill 时末行可能截断；丢这一行不影响前面的步。
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


def _iso(seconds: Any) -> str | None:
    """kimi 的时间戳是 epoch **秒**（float）。非法值返回 None。"""
    if not isinstance(seconds, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
    except (OverflowError, OSError, ValueError):
        return None


def _parse_arguments(raw: Any) -> dict[str, Any]:
    """arguments 是 JSON 字符串；非 dict 结果包进 ``{"_raw": ...}`` —— schema
    要求 arguments 是 dict，不能让一个畸形参数毁掉整条 trajectory。"""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw}
    return parsed if isinstance(parsed, dict) else {"_raw": parsed}


def _tool_output(return_value: Any) -> str:
    if not isinstance(return_value, dict):
        return ""
    for key in ("output", "message", "display"):
        value = return_value.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def describe_empty(events: list[dict[str, Any]]) -> str | None:
    """转录存在却产不出 step 时的真实原因。

    「文件不存在」是采集缺陷，「文件在但一步没跑成」是执行/上游故障——两者
    报成同一句，等于把设施故障伪装成采集缺陷。
    """
    if not events:
        return None
    kinds = {
        (e.get("message") or {}).get("type")
        for e in events
        if isinstance(e.get("message"), dict)
    }
    if "StepBegin" not in kinds:
        return "session wire present but no StepBegin was recorded (the turn never reached the model)"
    return "session wire present but no step produced content or tool calls"


def convert_events_to_trajectory(
    events: list[dict[str, Any]],
    *,
    attempt_id: str,
    system_prompt: str | None = None,
) -> Trajectory | None:
    """kimi 会话 wire 事件 → ATIF Trajectory。无有效 step 返回 None。"""
    if not events:
        return None

    protocol_version: str | None = None
    user_input: str | None = None
    #: 步号 → 聚合槽。StepBegin 之前出现的内容没有归属，丢弃。
    buckets: dict[int, dict[str, Any]] = {}
    order: list[int] = []
    current: int | None = None

    for record in events:
        if record.get("type") == "metadata" or "message" not in record:
            protocol_version = record.get("protocol_version") or protocol_version
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        mtype = message.get("type")
        payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
        when = _iso(record.get("timestamp"))

        if mtype == "TurnBegin":
            user_input = payload.get("user_input") or user_input
            continue
        if mtype == "StepBegin":
            n = payload.get("n")
            current = int(n) if isinstance(n, int) else (len(order) + 1)
            if current not in buckets:
                buckets[current] = {
                    "timestamp": when, "message": "", "reasoning": "",
                    "tool_calls": [], "results": [], "usage": None,
                }
                order.append(current)
            continue
        if current is None:
            # StepBegin 之前的内容没有步归属。kimi 实测不会出现，保守跳过。
            continue
        slot = buckets[current]

        if mtype == "ContentPart":
            if payload.get("type") == "think":
                slot["reasoning"] += payload.get("think") or ""
            elif payload.get("type") == "text":
                slot["message"] += payload.get("text") or ""
        elif mtype == "ToolCall":
            call_id = payload.get("id")
            function = payload.get("function") if isinstance(payload.get("function"), dict) else {}
            if call_id:
                slot["tool_calls"].append(ToolCall(
                    tool_call_id=str(call_id),
                    function_name=str(function.get("name") or "unknown"),
                    arguments=_parse_arguments(function.get("arguments")),
                ))
        elif mtype == "ToolResult":
            return_value = payload.get("return_value")
            is_error = bool(return_value.get("is_error")) if isinstance(return_value, dict) else False
            slot["results"].append(ObservationResult(
                source_call_id=str(payload["tool_call_id"]) if payload.get("tool_call_id") else None,
                content=_tool_output(return_value),
                # is_error 是 kimi 自己的判定，保留下来——「工具报错」和
                # 「agent 决策失误」在归因时必须分得开。
                extra={"is_error": is_error} if is_error else None,
            ))
        elif mtype == "StatusUpdate":
            usage = payload.get("token_usage")
            if isinstance(usage, dict):
                slot["usage"] = usage

    steps: list[Step] = []
    for n in order:
        slot = buckets[n]
        if not (slot["message"] or slot["reasoning"] or slot["tool_calls"]):
            continue
        calls = slot["tool_calls"] or None
        call_ids = {c.tool_call_id for c in calls or []}
        # schema 约束：observation 的 source_call_id 必须配得上**同一步**的
        # tool_calls。配不上的置 None 而非丢弃——观测内容是真的，只是失去配对。
        results = [
            r if (r.source_call_id is None or r.source_call_id in call_ids)
            else ObservationResult(source_call_id=None, content=r.content, extra=r.extra)
            for r in slot["results"]
        ]
        usage = slot["usage"] or {}
        metrics = None
        if usage:
            metrics = Metrics(
                prompt_tokens=usage.get("input_other"),
                completion_tokens=usage.get("output"),
                cached_tokens=usage.get("input_cache_read"),
                extra=(
                    {"input_cache_creation": usage["input_cache_creation"]}
                    if usage.get("input_cache_creation") else None
                ),
            )
        steps.append(Step(
            step_id=len(steps) + 1,
            timestamp=slot["timestamp"],
            source="agent",
            message=slot["message"],
            reasoning_content=slot["reasoning"] or None,
            tool_calls=calls,
            observation=Observation(results=results) if results else None,
            metrics=metrics,
            llm_call_count=1,
            extra={"kimi_step": n},
        ))

    if not steps:
        return None

    agent_extra: dict[str, Any] = {}
    if protocol_version:
        agent_extra["wire_protocol_version"] = protocol_version
    if system_prompt:
        agent_extra["system_prompt"] = system_prompt
    if user_input:
        agent_extra["user_input"] = user_input

    return Trajectory(
        schema_version="ATIF-v1.7",
        session_id=attempt_id,
        trajectory_id=attempt_id,
        agent=Agent(
            name="kimi-code",
            version="unknown",  # 转录里不带 CLI 版本号。
            # wire 与 context 都不记模型名——宁可留空，也不从别处猜一个填进去。
            model_name=None,
            extra=agent_extra or None,
        ),
        steps=steps,
        notes=(
            "reconstructed from kimi-code sandbox session wire "
            f"(producer=octagon-atif-v1, attempt={attempt_id})"
        ),
        final_metrics=FinalMetrics(
            total_prompt_tokens=sum(
                (buckets[n]["usage"] or {}).get("input_other") or 0 for n in order
            ) or None,
            total_completion_tokens=sum(
                (buckets[n]["usage"] or {}).get("output") or 0 for n in order
            ) or None,
            total_cached_tokens=sum(
                (buckets[n]["usage"] or {}).get("input_cache_read") or 0 for n in order
            ) or None,
            total_steps=len(steps),
        ),
    )
