"""Trace, history, event, outcome, prompt, and JSONL helpers for BladeServiceAdapter."""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..base import AdapterRunInput, prompt_context, time_budget_notice

if TYPE_CHECKING:
    from ..blade_service import AdapterEnv

_RUNNING_BACKGROUND_TASK_STATUSES = {"running", "starting"}
_RUNNING_SESSION_STATUSES = {
    "created",
    "initializing",
    "pending",
    "running",
    "starting",
    "queued",
    "in_progress",
}
_TERMINAL_SESSION_STATUSES = {
    "completed",
    "ok",
    "failed",
    "error",
    "cancelled",
    "canceled",
    "interrupted",
    "stopped",
}

# blade 沙盒里 skill 工具经 CLI 执行：`blade skill run "<skill>" <tool> --args '<json>'`。
# trace 的 tool_name 是 Bash，skill 级语义要从 command 解析（对比视图对齐 / DAG 检查用）。
_BLADE_SKILL_RUN_RE = re.compile(
    r"blade\s+skill\s+run\s+\"?([^\"\s]+)\"?\s+(\w+)(?:\s+--args\s+'(.*?)')?",
    re.DOTALL,
)

# blade 主循环的 loop_name；fork 出的子 agent 形如 "agent:<8hex>"。
_ROOT_LOOP = "root"

# 软件工厂只在这里复用前端的项目标题规则；app-dev bootstrap 消息、工作区
# 准备与 skills 全部由当前 BA 的 /api/agent-board/projects 生成，Octagon 不留副本。
_PROJECT_TITLE_MAX = 36

_TURN_TERMINAL_STATUSES = frozenset(
    {"completed", "ok", "failed", "error", "interrupted", "cancelled"}
)


def _is_not_found_error(exc: BaseException) -> bool:
    response = getattr(exc, "response", None)
    return (
        getattr(response, "status_code", None) == 404
        or getattr(exc, "status_code", None) == 404
    )


def _is_active_chat_error(result: Any) -> bool:
    """迭代续跑失败是否为瞬态 active-chat 竞态（应重试）。"""
    return "has an active chat" in (result.error_message or "")


def _event_loop_name(raw: Any) -> Any:
    """从事件里取 loop_name。

    新协议 socket 行：``raw.payload.loop_name``；旧协议 turn:end / poll entry /
    history node：``raw.loop_name``。两处都取不到时归 root——不臆造子 agent 归属。
    """
    if not isinstance(raw, dict):
        return None
    payload = raw.get("payload")
    if isinstance(payload, dict) and payload.get("loop_name"):
        return payload["loop_name"]
    return raw.get("loop_name")


def _session_create_error_detail(exc: Exception, entry: dict[str, Any]) -> str:
    """Preserve Blade's validation body instead of returning a generic 422."""
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    body = ""
    if response is not None:
        try:
            body = response.text
        except Exception:  # pragma: no cover - defensive for SDK response wrappers
            body = ""
    # 有一类异常 `str()` 是空的（httpx 的超时类尤其常见），只拼 str(exc) 会得到
    # 一条以 "; " 开头、什么都没说的错误——排查时连"是超时还是被拒"都分不出。
    # 异常类名永远非空，兜底用它。
    summary = str(exc) or f"{type(exc).__module__}.{type(exc).__qualname__}"
    parts = [summary, f"blade_entry={json.dumps(entry, ensure_ascii=False)}"]
    if status_code is not None:
        parts.append(f"status_code={status_code}")
    if body:
        parts.append(f"response_body={body[:2000]}")
    return "; ".join(parts)


def _extract_trace_from_history(
    nodes: list[dict[str, Any]],
    *,
    attempt_id: str,
    env_session_id: str,
) -> list[dict[str, Any]]:
    """把 get_history 的 nodes 归一成 Octagon trace.jsonl 行。

    结构依据 blade-agent host/sessions/history.py list_history()（Phase 0 已实测验证，
    见 docs/specs/batch_benchmark/phase0_history_sample.json）：
    - assistant 节点的 tool_calls（OpenAI 格式）与后续 role=tool 节点的 content 配对；
      tool 节点不带 tool_call_id → 同一 loop_name 内按序 FIFO 配对
    - duration_ms 用 assistant 节点 → tool 结果节点的 timestamp 差近似
    - is_error 从结果内容启发式判断（exit_code 非 0 / Error 前缀）
    - 过滤 is_deprecated 节点
    """
    rows: list[dict[str, Any]] = []
    # loop_name -> 待配对的 (tool_call, assistant_ts) FIFO 队列
    pending: dict[str, list[tuple[dict[str, Any], str | None]]] = {}

    for node in nodes:
        if node.get("is_deprecated"):
            continue
        if node.get("kind") != "message":
            continue
        role = node.get("role")
        loop = str(node.get("loop_name") or "root")
        ts = node.get("timestamp")

        if role == "assistant" and node.get("tool_calls"):
            for tc in node["tool_calls"]:
                pending.setdefault(loop, []).append((tc, ts))
            continue

        if role == "tool":
            queue = pending.get(loop) or []
            tc, call_ts = queue.pop(0) if queue else ({}, None)
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            raw_args = fn.get("arguments", "")
            try:
                arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                if not isinstance(arguments, dict):
                    arguments = {"_raw": arguments}
            except ValueError:
                arguments = {"_raw": raw_args}
            result = node.get("content", "")
            row = {
                "timestamp": ts,
                "attempt_id": attempt_id,
                "env_session_id": env_session_id,
                "tool_name": fn.get("name") or "unknown",
                "arguments": arguments,
                "result": result,
                "is_error": _looks_like_error(result),
                "duration_ms": _ts_diff_ms(call_ts, ts),
                "loop_name": loop,
                "tool_call_id": tc.get("id") if isinstance(tc, dict) else None,
            }
            skill_call = _parse_blade_skill_run(arguments)
            if skill_call:
                row["skill_id"], row["skill_tool"], row["skill_args"] = skill_call
            rows.append(row)
    return rows


def _trace_row_identity(row: dict[str, Any]) -> str:
    tool_call_id = row.get("tool_call_id")
    if isinstance(tool_call_id, str) and tool_call_id:
        return "tool_call:" + "|".join(
            (
                str(row.get("env_session_id") or ""),
                str(row.get("loop_name") or "root"),
                tool_call_id,
            )
        )
    stable = {
        key: row.get(key)
        for key in (
            "env_session_id",
            "loop_name",
            "timestamp",
            "tool_name",
            "arguments",
            "result",
        )
    }
    payload = json.dumps(
        stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return "fallback:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _append_unique_trace_rows(path: Path, rows: list[dict[str, Any]]) -> int:
    identities: set[str] = set()
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            with contextlib.suppress(json.JSONDecodeError):
                existing = json.loads(line)
                if isinstance(existing, dict):
                    identities.add(_trace_row_identity(existing))
    added = 0
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            identity = _trace_row_identity(row)
            if identity in identities:
                continue
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            identities.add(identity)
            added += 1
    return added


def _parse_blade_skill_run(arguments: Any) -> tuple[str, str, dict[str, Any]] | None:
    """从 Bash command 里解析 `blade skill run` 的 skill 级语义。"""
    if not isinstance(arguments, dict):
        return None
    command = arguments.get("command")
    if not isinstance(command, str) or "blade skill run" not in command:
        return None
    m = _BLADE_SKILL_RUN_RE.search(command)
    if not m:
        return None
    skill_id, tool_name, raw_args = m.group(1), m.group(2), m.group(3)
    try:
        skill_args = json.loads(raw_args) if raw_args else {}
        if not isinstance(skill_args, dict):
            skill_args = {"_raw": skill_args}
    except ValueError:
        skill_args = {"_raw": raw_args}
    return skill_id, tool_name, skill_args


def _looks_like_error(result: Any) -> bool:
    if not isinstance(result, str):
        return False
    stripped = result.lstrip()
    if stripped.startswith("Error") or stripped.startswith("error:"):
        return True
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except ValueError:
            return False
        if isinstance(data, dict):
            exit_code = data.get("exit_code")
            if isinstance(exit_code, int) and exit_code != 0:
                return True
            if data.get("is_error") is True or data.get("error"):
                return True
    return False


def _ts_diff_ms(start: str | None, end: str | None) -> int:
    if not start or not end:
        return 0
    try:
        s = datetime.fromisoformat(str(start))
        e = datetime.fromisoformat(str(end))
    except ValueError:
        return 0
    diff = (e - s).total_seconds() * 1000
    return max(int(diff), 0)


def _classify_outcome(
    chat_end_status: str | None,
    error_message: str | None,
    finish_reason: str | None = None,
    *,
    unexpected_interaction: bool = False,
) -> str:
    # 未声明的交互请求：agent 停在等应答，任务不可能完成。语义是
    # "agent 要人介入而场景没准备"，不是超时——归 chat_failed 并配专用 error_code。
    if unexpected_interaction:
        return "chat_failed"
    if error_message and "timeout" in error_message.lower():
        return "timeout"
    if error_message and "socket disconnected" in error_message.lower():
        return "chat_failed"
    if error_message:
        return "chat_failed"
    if finish_reason == "terminal_tool":
        return "completed"
    if chat_end_status in ("ok", "completed"):
        return "completed"
    if chat_end_status == "failed":
        return "chat_failed"
    if chat_end_status == "paused":
        # 非 headless 下 paused 是"等交互应答"。走到这里说明应答循环没能收尾
        # （已声明的 turn 用尽仍在暂停），同样不是超时。
        return "chat_failed"
    if chat_end_status == "interrupted":
        return "chat_failed"
    if chat_end_status is None:
        return "timeout"
    return "chat_failed"


def _outcome_error_code(
    status: str,
    error_message: str | None,
    finish_reason: str | None,
    *,
    unexpected_interaction: bool = False,
) -> str | None:
    if status == "completed":
        return None
    if unexpected_interaction:
        return "unexpected_interaction"
    lowered = (error_message or "").lower()
    if "socket disconnected" in lowered:
        return "transport_reconnect_exhausted"
    if "inactivity timeout" in lowered:
        return "agent_inactivity_timeout"
    if "total timeout" in lowered:
        return "agent_total_timeout"
    if "timeout" in lowered:
        return "agent_timeout"
    if "finish_reason" in lowered or "流式响应未正常结束" in lowered:
        return "llm_stream_incomplete"
    return finish_reason or "chat_error"


def _project_prompt_title(prompt: str) -> str:
    """与软件工厂前端 ``projectPromptTitle`` 保持一致。"""
    text = " ".join(prompt.strip().split())
    return text[:_PROJECT_TITLE_MAX] if text else "新对话"


def _chat_intent(prompt: str) -> str:
    """普通聊天首页用用户输入前 20 个 Unicode 字符作为会话标题。"""
    text = prompt.strip() or "新会话"
    return text[:20]


def _render_prompt(
    task: AdapterRunInput,
    env: AdapterEnv | None = None,
    data_path: Path | None = None,
) -> str:
    """只传任务消息。工具引导交给 blade 真实 AGENTS.md / SKILL.md（路线 A）。"""
    parts: list[str] = []
    # 时间预算放最前（blade ChatSendPayload 无独立 system 通道，回落 message 顶部；
    # 语义是"框架级约束"，置顶更醒目）。None（不限时）不注入。
    notice = (
        time_budget_notice(task.timeout_seconds)
        if task.notify_model_of_timeout
        else None
    )
    if notice:
        parts += [notice, ""]

    parts.append(task.task_prompt)

    if task.task_context:
        context = prompt_context(task.task_context)
        if context:
            parts += ["", "上下文:", json.dumps(context, ensure_ascii=False, indent=2)]
    return "\n".join(parts)


def _render_turn_prompt(
    task: AdapterRunInput,
    turn: Any,
    env: AdapterEnv | None = None,
    data_path: Path | None = None,
) -> str:
    """渲染某一轮的消息。

    首轮（turn_index==0）走 `_render_prompt` 的完整渲染：time budget notice 与
    task context 只在首轮注入——后续轮重复宣称"本任务限时 X"会误导 agent，
    context 也已在同一 session 里。legacy 单轮因此与改造前逐字节一致。
    后续轮只发该轮 prompt 原文。
    """
    if turn.turn_index == 0:
        # legacy 轮的 prompt 就是 task_prompt，等价于旧路径；多轮首轮用该轮
        # prompt 替换任务消息，notice/context 渲染方式不变。
        if turn.prompt == task.task_prompt:
            return _render_prompt(task, env, data_path)
        parts: list[str] = []
        notice = (
            time_budget_notice(task.timeout_seconds)
            if task.notify_model_of_timeout
            else None
        )
        if notice:
            parts += [notice, ""]
        parts.append(turn.prompt or "")
        if task.task_context:
            context = prompt_context(task.task_context)
            if context:
                parts += [
                    "", "上下文:",
                    json.dumps(context, ensure_ascii=False, indent=2),
                ]
        return "\n".join(parts)
    return turn.prompt or ""


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _append_jsonl(path: Path, data: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, default=str) + "\n")


def _turn_is_terminal(turn: dict[str, Any]) -> bool:
    """/messages 返回的 turn 是否已到终态。

    status 缺失视为终态（旧版 blade /messages 只返回已完成的 turn，部分行
    可能不带 status 字段）；只有显式的进行中状态才判非终态。
    """
    status = turn.get("status")
    if status is None:
        return True
    return str(status) in _TURN_TERMINAL_STATUSES


def _projection_identity(payload: dict[str, Any]) -> str:
    turn_id = payload.get("turn_id") or payload.get("id")
    if turn_id:
        return f"{payload.get('role', '')}:{turn_id}"
    return json.dumps(
        {
            "role": payload.get("role"),
            "status": payload.get("status"),
            "blocks": payload.get("blocks") or [],
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


def _write_progress(path: Path, progress: dict[str, Any]) -> None:
    progress["updated_at"] = _now_iso()
    _write_json(path, progress)


def _persist_cleanup_pending_checkpoint(
    attempt_dir: Path,
    session_id: str | None,
    external_refs: dict[str, Any] | None = None,
) -> None:
    if not session_id:
        return
    checkpoint_path = attempt_dir / "recovery.json"
    checkpoint: dict[str, Any] = {}
    if checkpoint_path.is_file():
        with contextlib.suppress(OSError, json.JSONDecodeError):
            loaded = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                checkpoint = loaded
    if external_refs:
        checkpoint.update(external_refs)
    checkpoint["blade_session_id"] = session_id
    checkpoint["cleanup_pending"] = True
    _write_json(checkpoint_path, checkpoint)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)
