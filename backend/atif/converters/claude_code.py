"""Claude Code 沙盒会话 → ATIF-v1.7 trajectory。

源：attempt 沙盒 HOME 里的原生会话转录
``.cc-iso-home/.claude/projects/<slug>/<session>.jsonl``（+ ``<session>/subagents/*.jsonl``）。

映射逻辑移植自 Harbor ``src/harbor/agents/installed/claude_code.py``（已验证与
agent-octagon 转录同构）：

- 按 ``message.id`` 把一次 LLM 推理的 text + thinking + 全部 tool_use 聚合成
  **一个 agent step**（RFC one-LLM-per-step）：thinking → ``reasoning_content``、
  text → ``message``、tool_use → ``tool_calls[]``；
- user 事件（str content）→ ``source:"user"`` step；isMeta 续话提示跳过；
- tool_result → 按 ``tool_use_id`` 配对 pending call → 挂到**同一步**的
  ``observation.results[]``（schema 校验 source_call_id 必须落在同一步 tool_calls）；
  orphan / 重复结果去重；
- usage 多版累积取同 ``message.id`` 链最后一版；
- 基础设施事件（queue-operation / attachment / atis-latch / ai-title /
  last-prompt）跳过。

会话选择：``.claude/projects/`` 下可能有多个 slug（CC 在 skill_workspace 和
memory 各建一个），选 cwd 匹配 attempt 的 ``skill_workspace`` 的 session 文件；
匹配不到按 mtime 最新。子 agent 事件与主链按时间线合并、以 ``is_sidechain``
标记（同 Harbor），不嵌入 subagent_trajectories。
"""

from __future__ import annotations

import base64
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

_ATTACHMENT_TYPES = {
    "attachment",
    "queue-operation",
    "atis-latch",
    "ai-title",
    "last-prompt",
}


# ---------- 纯函数（移植自 Harbor claude_code.py）-----------------------------


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except TypeError:
        return str(value)


def extract_text_reasoning_tool_uses(
    content: Any,
) -> tuple[str, str | None, list[dict[str, Any]]]:
    """content[] → (message 文本, reasoning, tool_use blocks)。"""
    if isinstance(content, str):
        return content.strip(), None, []

    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_blocks: list[dict[str, Any]] = []

    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                text_parts.append(_stringify(block))
                continue

            block_type = block.get("type")
            if block_type == "tool_use":
                tool_blocks.append(block)
                continue

            if block_type in {"thinking", "reasoning", "analysis"}:
                text_value = (
                    block.get("text")
                    if block.get("text") is not None
                    else block.get("thinking")
                )
                reasoning_parts.append(
                    text_value.strip() if isinstance(text_value, str) else _stringify(text_value)
                )
                continue

            if block_type == "redacted_thinking":
                # OpenRouter 把非 Anthropic 模型的明文推理塞进 redacted_thinking
                # 的 data 字段（`openrouter.reasoning:<b64>`）；真 Anthropic 密文
                # 直接丢弃（客户端解不了，不应进可读 message）。
                data = block.get("data")
                if isinstance(data, str) and data.startswith("openrouter.reasoning:"):
                    try:
                        payload = data[len("openrouter.reasoning:") :]
                        decoded = base64.b64decode(payload + "==").decode(
                            "utf-8", "replace"
                        )
                        inner = json.loads(decoded)
                        inner_text = inner.get("text")
                        if isinstance(inner_text, str):
                            reasoning_parts.append(inner_text.strip())
                    except (ValueError, json.JSONDecodeError):
                        pass
                continue

            if block_type == "code" and isinstance(block.get("code"), str):
                text_parts.append(block["code"])
                continue

            text_value = block.get("text")
            if isinstance(text_value, str):
                text_parts.append(text_value)
            else:
                text_parts.append(_stringify(block))
    elif content is not None:
        text_parts.append(_stringify(content))

    text = "\n\n".join(
        part.strip() for part in text_parts if part and str(part).strip()
    )
    reasoning = "\n\n".join(
        part.strip() for part in reasoning_parts if part and str(part).strip()
    )
    return text, (reasoning or None), tool_blocks


def build_metrics(usage: Any) -> Metrics | None:
    """CC 的 usage → ATIF Metrics。流式被中断时 usage 字段可能是 None 而非缺失，
    用 ``or 0`` 防 TypeError。"""
    if not isinstance(usage, dict):
        return None

    cached_tokens = usage.get("cache_read_input_tokens") or 0
    creation = usage.get("cache_creation_input_tokens") or 0
    input_tokens = usage.get("input_tokens") or 0
    # 对齐 Anthropic session 总计：input + cache read + cache creation。
    prompt_tokens = input_tokens + cached_tokens + creation
    completion_tokens = usage.get("output_tokens") or 0

    extra = {k: v for k, v in usage.items() if k not in {"input_tokens", "output_tokens"}}

    if prompt_tokens == 0 and completion_tokens == 0 and cached_tokens == 0 and not extra:
        return None
    return Metrics(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_tokens=cached_tokens,
        cost_usd=None,
        extra=extra or None,
    )


def format_tool_result(
    block: dict[str, Any], tool_use_result: dict[str, Any] | None
) -> tuple[str | None, dict[str, Any] | None]:
    """tool_result block + 顶层 toolUseResult → (文本, metadata)。"""
    parts: list[str] = []

    content = block.get("content")
    if isinstance(content, str):
        if content.strip():
            parts.append(content.strip())
    elif isinstance(content, list):
        for item in content:
            # `{"type": "text", "text": "..."}` block 取内层文本，其余 JSON 化。
            if isinstance(item, dict) and item.get("type") == "text":
                text_value = item.get("text")
                text_value = text_value if isinstance(text_value, str) else _stringify(text_value)
            else:
                text_value = _stringify(item)
            if text_value.strip():
                parts.append(text_value.strip())
    elif content not in (None, ""):
        parts.append(_stringify(content))

    metadata: dict[str, Any] | None = None
    if tool_use_result and isinstance(tool_use_result, dict):
        metadata = {"tool_use_result": tool_use_result}
        stdout = tool_use_result.get("stdout")
        stderr = tool_use_result.get("stderr")
        exit_code = tool_use_result.get("exitCode") or tool_use_result.get("exit_code")
        interrupted = tool_use_result.get("interrupted")
        is_image = tool_use_result.get("isImage")

        formatted_chunks: list[str] = []
        if stdout:
            formatted_chunks.append(f"[stdout]\n{stdout}".rstrip())
        if stderr:
            formatted_chunks.append(f"[stderr]\n{stderr}".rstrip())
        if exit_code not in (None, 0):
            formatted_chunks.append(f"[exit_code] {exit_code}")
        if interrupted:
            formatted_chunks.append(f"[interrupted] {interrupted}")
        if is_image:
            formatted_chunks.append(f"[is_image] {is_image}")

        remaining_meta = {
            k: v
            for k, v in tool_use_result.items()
            if k
            not in {"stdout", "stderr", "exitCode", "exit_code", "interrupted", "isImage"}
        }
        if remaining_meta:
            formatted_chunks.append(
                f"[metadata] {json.dumps(remaining_meta, ensure_ascii=False)}"
            )

        if formatted_chunks:
            parts.append("\n".join(c for c in formatted_chunks if c))

    if block.get("is_error") is True:
        parts.append("[error] tool reported failure")
        metadata = metadata or {}
        metadata["is_error"] = True

    if metadata is not None:
        metadata.setdefault("raw_tool_result", block)

    result_text = "\n\n".join(p for p in parts if p).strip()
    return (result_text or None), metadata


# ---------- 会话发现 ------------------------------------------------------------


def _read_events(files: list[Path]) -> list[dict[str, Any]]:
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


def find_session_events(cc_home: Path, attempt_dir: Path) -> list[dict[str, Any]]:
    """在 ``.cc-iso-home/.claude/projects`` 下找 attempt 的会话转录。

    返回按时间排序、按 uuid 去重的事件列表（主链 + 子 agent sidechain 合并）。
    匹配规则：事件里 cwd 指向 attempt 的 ``skill_workspace`` 的项目 slug 优先；
    无匹配取 mtime 最新。找不到返回 []。
    """
    projects = cc_home / ".claude" / "projects"
    if not projects.is_dir():
        return []

    candidates: list[Path] = []
    for slug in sorted(projects.iterdir()):
        if not slug.is_dir():
            continue
        candidates.extend(sorted(slug.glob("*.jsonl")))
        candidates.extend(sorted(slug.rglob("subagents/*.jsonl")))
    if not candidates:
        return []

    workspace = (attempt_dir / "skill_workspace").resolve()
    target: Path | None = None
    fallback: Path | None = None
    for path in sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True):
        if fallback is None:
            fallback = path
        for event in _read_events([path]):
            cwd = event.get("cwd")
            if isinstance(cwd, str) and cwd and workspace.as_posix() in cwd:
                target = path
                break
        if target is not None:
            break

    selected = target or fallback or candidates[0]
    # 主会话 + 其子 agent 转录（同 session-id 目录下 subagents/）。
    parent = selected.parent
    files = [selected] if selected.is_file() else []
    subagents = sorted((parent / selected.stem).rglob("subagents/*.jsonl")) if selected.stem else []
    if selected.parent.name == selected.stem:
        # 形如 <slug>/<session>/subagents/… 时主会话在 <slug>/<session>.jsonl
        pass
    files.extend(subagents)
    events = _read_events(files)
    if not events:
        # 兜底：整个 projects 树都扫一遍（多 slug 时主会话可能不在同目录）。
        events = _read_events(candidates)

    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for event in events:
        uuid = event.get("uuid")
        if isinstance(uuid, str) and uuid:
            if uuid in seen:
                continue
            seen.add(uuid)
        deduped.append(event)
    deduped.sort(key=lambda e: e.get("timestamp", ""))
    return deduped


# ---------- 归一化 → ATIF steps ------------------------------------------------


def _normalize(
    events: list[dict[str, Any]], *, default_model_name: str | None
) -> list[dict[str, Any]]:
    """原始会话事件 → 归一化事件（message / agent_step / tool_call）。"""
    normalized: list[dict[str, Any]] = []
    pending_calls: dict[str, dict[str, Any]] = {}
    completed_call_ids: set[str] = set()
    seen_message_ids: set[str] = set()
    turn_by_msgid: dict[str, dict[str, Any]] = {}

    for event in events:
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        event_type = event.get("type")
        timestamp = event.get("timestamp")

        if event_type == "assistant":
            text, reasoning, tool_blocks = extract_text_reasoning_tool_uses(
                message.get("content")
            )
            msg_id = message.get("id")
            model_name = message.get("model") or default_model_name

            usage = message.get("usage")
            metrics = None if (msg_id and msg_id in seen_message_ids) else build_metrics(usage)
            if msg_id:
                seen_message_ids.add(msg_id)

            extra: dict[str, Any] = {}
            for key in ("stop_reason", "stop_sequence", "requestId", "id"):
                value = message.get(key) if key != "id" else event.get("id")
                if value is not None:
                    extra[key] = value
            if event.get("agent_id"):
                extra["agent_id"] = event["agent_id"]
            extra["is_sidechain"] = event.get("isSidechain", False)

            turn = turn_by_msgid.get(msg_id) if msg_id else None
            if turn is None:
                turn = {
                    "kind": "agent_step",
                    "timestamp": timestamp,
                    "text": "",
                    "reasoning": None,
                    "metrics": None,
                    "extra": extra or None,
                    "model_name": model_name,
                    "tool_calls": [],
                }
                normalized.append(turn)
                if msg_id:
                    turn_by_msgid[msg_id] = turn

            if text:
                turn["text"] = f"{turn['text']}\n\n{text}".strip() if turn["text"] else text
            if reasoning and turn["reasoning"] is None:
                turn["reasoning"] = reasoning
            if turn["metrics"] is None and metrics is not None:
                turn["metrics"] = metrics

            turn_calls = turn["tool_calls"]
            for tool_block in tool_blocks:
                call_id = tool_block.get("id") or tool_block.get("tool_use_id")
                if not call_id:
                    continue
                if call_id in pending_calls or call_id in completed_call_ids:
                    continue

                raw_arguments = tool_block.get("input")
                if isinstance(raw_arguments, dict):
                    arguments = raw_arguments
                else:
                    arguments = {"input": raw_arguments}

                call_extra: dict[str, Any] = {}
                if raw_arguments is not None:
                    call_extra["raw_arguments"] = raw_arguments
                if tool_block.get("status") is not None:
                    call_extra["status"] = tool_block.get("status")
                if tool_block.get("is_error") is not None:
                    call_extra["tool_use_is_error"] = tool_block.get("is_error")
                if tool_block.get("name"):
                    call_extra.setdefault("tool_use_name", tool_block.get("name"))

                spec = {
                    "call_id": call_id,
                    "tool_name": tool_block.get("name") or "",
                    "arguments": arguments or {},
                    "extra": call_extra or None,
                    "output": None,
                    "result_extra": None,
                }
                turn_calls.append(spec)
                pending_calls[call_id] = spec
            continue

        if event_type == "user":
            content = message.get("content")
            if isinstance(content, str):
                # isMeta 的续话提示（"你的上一条没有可见输出…"）是框架生成的
                # 系统消息，不是用户意图，不进 user step（归因干净）。
                if content.strip() and not event.get("isMeta"):
                    normalized.append(
                        {
                            "kind": "message",
                            "timestamp": timestamp,
                            "role": "user",
                            "text": content,
                            "extra": {"is_sidechain": event.get("isSidechain", False)},
                        }
                    )
                continue

            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_result":
                        call_id = block.get("tool_use_id")
                        formatted_output, metadata = format_tool_result(
                            block, event.get("toolUseResult")
                        )
                        call_info = pending_calls.pop(call_id, None) if call_id else None
                        if call_info is not None:
                            result_extra: dict[str, Any] = {}
                            if metadata:
                                result_extra["tool_result_metadata"] = metadata
                            if block.get("is_error") is not None:
                                result_extra["tool_result_is_error"] = block.get("is_error")
                            call_info["output"] = formatted_output
                            call_info["result_extra"] = result_extra or None
                            if call_id:
                                completed_call_ids.add(call_id)
                            continue

                        # orphan tool_result（compaction 重放等）→ 独立 tool_call step。
                        if call_id and call_id in completed_call_ids:
                            continue
                        tool_name = block.get("name") or block.get("tool_name") or ""
                        if not call_id or not tool_name:
                            continue
                        call_info = {
                            "kind": "tool_call",
                            "timestamp": timestamp,
                            "call_id": call_id or "",
                            "tool_name": tool_name,
                            "arguments": {},
                            "raw_arguments": None,
                            "reasoning": None,
                            "message": None,
                            "extra": None,
                            "metrics": None,
                            "model_name": default_model_name,
                        }
                        call_info["output"] = formatted_output
                        call_info["result_extra"] = (
                            {"tool_result_metadata": metadata} if metadata else None
                        )
                        normalized.append(call_info)
            continue

        # 其余事件类型（system/assistant 子事件等）忽略
    return normalized


def _convert_event_to_step(event: dict[str, Any], step_id: int) -> Step:
    """归一化事件 → ATIF Step。"""
    timestamp = event.get("timestamp")

    if event["kind"] == "message":
        role = event.get("role", "user")
        source = "agent" if role == "assistant" else ("user" if role == "user" else "system")
        step = Step(
            step_id=step_id,
            timestamp=timestamp,
            source=source,
            message=event.get("text", ""),
            llm_call_count=1 if source == "agent" else None,
            extra=event.get("extra"),
        )
        if source == "agent":
            if event.get("reasoning"):
                step.reasoning_content = event["reasoning"]
            if event.get("model_name"):
                step.model_name = event["model_name"]
        return step

    if event["kind"] == "agent_step":
        tool_calls: list[ToolCall] = []
        results: list[ObservationResult] = []
        for spec in event.get("tool_calls", []):
            call_id = spec.get("call_id")
            if not call_id:
                continue
            tool_calls.append(
                ToolCall(
                    tool_call_id=call_id,
                    function_name=spec.get("tool_name") or "",
                    arguments=spec.get("arguments") or {},
                    extra=spec.get("extra"),
                )
            )
            if spec.get("output") is not None:
                results.append(
                    ObservationResult(
                        source_call_id=call_id,
                        content=spec.get("output"),
                        extra=spec.get("result_extra"),
                    )
                )
        step = Step(
            step_id=step_id,
            timestamp=timestamp,
            source="agent",
            message=event.get("text", ""),
            tool_calls=tool_calls or None,
            observation=Observation(results=results) if results else None,
            llm_call_count=1,
            extra=event.get("extra"),
        )
        if event.get("reasoning"):
            step.reasoning_content = event["reasoning"]
        if event.get("model_name"):
            step.model_name = event["model_name"]
        if event.get("metrics"):
            step.metrics = event["metrics"]
        return step

    if event["kind"] == "tool_call":
        call_id = event.get("call_id", "")
        tool_call = ToolCall(
            tool_call_id=call_id,
            function_name=event.get("tool_name", ""),
            arguments=event.get("arguments") or {},
        )
        output = event.get("output")
        observation = (
            Observation(results=[ObservationResult(source_call_id=call_id or None, content=output)])
            if output is not None
            else None
        )
        step = Step(
            step_id=step_id,
            timestamp=timestamp,
            source="agent",
            message=event.get("message") or "",
            tool_calls=[tool_call],
            observation=observation,
            llm_call_count=1,
            extra=event.get("extra"),
        )
        if event.get("model_name"):
            step.model_name = event["model_name"]
        if event.get("reasoning"):
            step.reasoning_content = event["reasoning"]
        if event.get("metrics"):
            step.metrics = event["metrics"]
        return step

    raise ValueError(f"unsupported event kind {event.get('kind')!r}")


def convert_events_to_trajectory(
    events: list[dict[str, Any]], *, attempt_id: str
) -> Trajectory | None:
    """会话事件 → ATIF Trajectory。无有效 steps 返回 None。"""
    if not events:
        return None

    session_id: str = attempt_id
    for event in events:
        sid = event.get("sessionId")
        if isinstance(sid, str) and sid:
            session_id = sid
            break

    agent_version: str = "unknown"
    for event in events:
        ver = event.get("version")
        if isinstance(ver, str) and ver:
            agent_version = ver
            break

    cwds = {
        event.get("cwd")
        for event in events
        if isinstance(event.get("cwd"), str) and event.get("cwd")
    }
    git_branches = {
        event.get("gitBranch")
        for event in events
        if isinstance(event.get("gitBranch"), str) and event.get("gitBranch")
    }
    agent_ids = {
        event.get("agentId")
        for event in events
        if isinstance(event.get("agentId"), str) and event.get("agentId")
    }
    agent_extra: dict[str, Any] | None = {}
    if cwds:
        agent_extra["cwds"] = sorted(cwds)
    if git_branches:
        agent_extra["git_branches"] = sorted(git_branches)
    if agent_ids:
        agent_extra["agent_ids"] = sorted(agent_ids)
    if not agent_extra:
        agent_extra = None

    default_model_name: str | None = None
    for event in events:
        if event.get("type") != "assistant":
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        model_name = message.get("model")
        if isinstance(model_name, str) and model_name:
            default_model_name = model_name
            break

    normalized = _normalize(events, default_model_name=default_model_name)
    steps: list[Step] = []
    for event in normalized:
        try:
            step = _convert_event_to_step(event, len(steps) + 1)
        except (ValueError, TypeError):
            continue
        if step.source == "agent" and not step.model_name and default_model_name:
            step.model_name = default_model_name
        steps.append(step)

    if not steps:
        return None

    return Trajectory(
        schema_version="ATIF-v1.7",
        session_id=session_id,
        trajectory_id=attempt_id,
        agent=Agent(
            name="claude-code",
            version=agent_version,
            model_name=default_model_name,
            extra=agent_extra,
        ),
        steps=steps,
        notes=(
            "reconstructed from Claude Code sandbox session transcript "
            f"(producer=octagon-atif-v1, session={session_id})"
        ),
        final_metrics=FinalMetrics(
            total_steps=len(steps),
        ),
    )
