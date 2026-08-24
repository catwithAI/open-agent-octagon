"""从各 agent 的原始日志里提取 shell 命令原文。

真实数据（data/attempts 样本）确认三种命令来源，都带 command 字段：
- CC events.jsonl：type=assistant → content[].tool_use，name ∈ {Bash, *run_shell*}
- blade trace.jsonl：tool_name ∈ {Bash, run_shell}，arguments.command
- Codex：事件字段待 Phase 0 spike 确认；缺失则空返回 + 标记覆盖缺口

统一产出 ExtractedCommand（命令原文 + 溯源信息），供 classifier 逐条判定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 命令型工具名：命中即认为 input/arguments.command 是 shell 命令原文。
# 覆盖裸 Bash、裸 run_shell、以及 MCP 前缀 mcp__octagon-*__run_shell。
_SHELL_TOOL_SUFFIXES = ("bash", "run_shell", "shell", "exec", "execute")


def _is_shell_tool(name: str) -> bool:
    n = (name or "").lower()
    return any(n == s or n.endswith(s) for s in _SHELL_TOOL_SUFFIXES)


@dataclass
class ExtractedCommand:
    command: str
    source_ref: dict[str, Any] = field(default_factory=dict)  # {log, line} / {trace_seq}
    tool_name: str = ""


def _from_tool_input(name: str, tool_input: dict[str, Any]) -> str | None:
    if not _is_shell_tool(name):
        return None
    cmd = tool_input.get("command")
    if isinstance(cmd, str) and cmd.strip():
        return cmd
    return None


def extract_from_events(events: list[dict[str, Any]]) -> list[ExtractedCommand]:
    """任意 agent 的 events.jsonl。

    走 toolcalls 的统一归一，六家形状（CC 的 tool_use 块、codex 的 item 信封、
    opencode/mimo 的 part 信封、kimi 的 OpenAI tool_calls、blade 的流式分片）都认。
    早期这里只认 CC 一种加两个猜的 codex 字段名，实测 codex 的 shell 调用其实叫
    ``item.type=command_execution``，从来没被匹配上过。
    """
    from .toolcalls import tool_calls_from_events

    out: list[ExtractedCommand] = []
    for call in tool_calls_from_events(events):
        args = call.get("arguments") or {}
        cmd = _from_tool_input(call.get("tool_name", ""), args if isinstance(args, dict) else {})
        if cmd:
            out.append(
                ExtractedCommand(
                    command=cmd,
                    source_ref=call.get("source_ref") or {"log": "events.jsonl"},
                    tool_name=call.get("tool_name", ""),
                )
            )
    return out


def extract_from_trace(trace: list[dict[str, Any]]) -> list[ExtractedCommand]:
    """blade 风格 trace.jsonl：tool_name + arguments.command。"""
    out: list[ExtractedCommand] = []
    for i, row in enumerate(trace):
        if not isinstance(row, dict):
            continue
        name = row.get("tool_name", "")
        args = row.get("arguments") or {}
        cmd = _from_tool_input(name, args if isinstance(args, dict) else {})
        if cmd:
            out.append(
                ExtractedCommand(
                    command=cmd,
                    source_ref={"log": "trace.jsonl", "trace_seq": i},
                    tool_name=name,
                )
            )
    return out


def extract_commands(
    *,
    events: list[dict[str, Any]],
    trace: list[dict[str, Any]],
) -> list[ExtractedCommand]:
    """合并 events + trace 的命令源，**同一条命令只算一次**。

    两者不是互补而是重叠：blade-agent 的 trace.jsonl 本就是它 events.jsonl 里
    tool_call 事件的投影，同一条命令两个文件各有一份。早先这里直接把两边拼起来，
    靠的是「events 解析器读不懂 blade 的形状」这个巧合——一旦 events 侧也认了
    blade 的分片流，每条命令就会被数两遍，把 blade 的系统层事件数凭空翻倍。
    那正是采集缺陷的镜像：仍然是采集通道差异冒充行为差异，只是方向反过来。

    所以：trace 在场时以 trace 为准（它已是解析好的调用记录），events 只补 trace
    里没有的命令。判重按命令原文，因为两个通道的 source_ref 天然对不上。
    """
    trace_commands = extract_from_trace(trace)
    seen = {c.command for c in trace_commands}
    extra = [c for c in extract_from_events(events) if c.command not in seen]
    return trace_commands + extra
