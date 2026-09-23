"""attempt 沙盒会话 → ATIF trajectory 的编排层。

在 wire/insight 数据结构**之外**，直接从沙盒 HOME 里各家 CLI 的本地会话转录
还原 ATIF-v1.7 对话流，用于归因。源分两类：

- **会话文件型**（claude-code / codex）：读 home 里的 CLI 自落盘转录
  （``.cc-iso-home/.claude/projects`` / ``.codex-iso-home/sessions``，
  沙箱模式为 ``sandbox_home`` 对应路径）；
- **事件流型**（kimi-code / opencode / mimo-code）：读 attempt 目录的
  ``events.jsonl``（adapter 逐行落盘的 CLI 事件流，与 Harbor 读的 stdout
  文件同源）。这类无 home 标记可判别，须显式传 ``agent_name``。

源缺失 → ``not_available``。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .schema import Trajectory
from .converters import claude_code as cc_converter
from .converters import codex as codex_converter
from .converters import kimi as kimi_converter
from .converters import opencode as opencode_converter

PRODUCER_VERSION = "octagon-atif-v1"

_SUPPORTED_AGENTS = frozenset(
    {"claude-code", "codex", "kimi-code", "opencode", "mimo-code"}
)

#: 事件流型 agent：转换器读 attempt 目录 events.jsonl，不需 home 会话文件。
_EVENTS_AGENTS = frozenset({"kimi-code", "opencode", "mimo-code"})


@dataclass(frozen=True)
class EmitOutcome:
    status: Literal["ready", "not_available"]
    attempt_id: str
    trajectory: dict[str, Any] | None = None
    reason: str | None = None

    @property
    def producer(self) -> str:
        return PRODUCER_VERSION


_HOST_HOMES = {
    "claude-code": Path(".cc-iso-home"),
    "codex": Path(".codex-iso-home"),
    "kimi-code": Path(".kimi-iso-home"),
}

#: 各 agent 在 home 根下的「存在即证明该 agent 跑过」的标记（沙箱/宿主机共用）。
#: codex 的 CODEX_HOME 指向 home 根，会话落在 ``<home>/sessions``；CC 的
#: CLAUDE_CONFIG_DIR 默认 ``$HOME/.claude``，转录在 ``<home>/.claude/projects``。
_SANDBOX_MARKERS = {
    "claude-code": ".claude/projects",
    "codex": "sessions",
    "kimi-code": ".kimi",
}


def _resolve_home(attempt_dir: Path, agent: str) -> Path:
    """agent 的 home 根：沙箱模式优先（``attempt_dir/sandbox_home``），
    否则 host iso-home（``.cc-iso-home`` / ``.codex-iso-home``）。"""
    sandbox = attempt_dir / "sandbox_home"
    if sandbox.is_dir():
        return sandbox
    return attempt_dir / _HOST_HOMES.get(agent, Path(f".{agent}-iso-home"))


def _infer_agent(attempt_dir: Path) -> str | None:
    """按 home 里的 agent 标记推断 adapter。

    沙箱模式六个 agent 共用 ``sandbox_home``，故先查 ``sandbox_home`` 里的
    标记（``.claude/projects`` → claude-code、``sessions`` → codex），再回退
    host iso-home；最后按 host iso-home 目录存在兜底（历史 codex 单轮无
    sessions 也算 codex，由 converter 报 not_available）。"""
    for agent, marker in _SANDBOX_MARKERS.items():
        if (attempt_dir / "sandbox_home" / marker).is_dir():
            return agent
        if (attempt_dir / _HOST_HOMES[agent] / marker).is_dir():
            return agent
    if (attempt_dir / ".codex-iso-home").is_dir():
        return "codex"
    if (attempt_dir / ".cc-iso-home" / ".claude" / "projects").is_dir():
        return "claude-code"
    return None


def emit_attempt_atif(
    attempt_dir: Path | str,
    *,
    agent_name: str | None = None,
    attempt_id: str | None = None,
) -> EmitOutcome:
    """把一个 attempt 目录还原成 ATIF trajectory。

    参数：
    - ``attempt_dir``：attempt 数据目录（沙箱模式下含 ``sandbox_home``；
      host 模式含 ``.cc-iso-home`` / ``.codex-iso-home``）。
    - ``agent_name``：显式指定 adapter（claude-code / codex / kimi-code /
      opencode / mimo-code）。事件流型（kimi/opencode/mimo）**必须显式传**
      ——它们读 attempt 目录 events.jsonl，无 home 标记可判别。
    - ``attempt_id``：产物 ``trajectory_id`` / ``session_id`` 兜底；缺省用目录名。

    返回：``ready`` 带校验过的 trajectory dict；``not_available`` 带原因。
    """
    attempt_dir = Path(attempt_dir)
    aid = attempt_id or attempt_dir.name

    agent = agent_name or _infer_agent(attempt_dir)
    if agent is None:
        return EmitOutcome(
            status="not_available",
            attempt_id=aid,
            reason="no supported agent home found (sandbox_home / .cc-iso-home / .codex-iso-home)",
        )
    if agent not in _SUPPORTED_AGENTS:
        return EmitOutcome(
            status="not_available",
            attempt_id=aid,
            reason=f"agent {agent!r} not supported (supported: {sorted(_SUPPORTED_AGENTS)})",
        )

    if agent in _EVENTS_AGENTS:
        # 事件流型：读 attempt 目录 events.jsonl（adapter 逐行落盘的 CLI 流）。
        if agent == "kimi-code":
            events = kimi_converter.find_session_events(attempt_dir)
            trajectory = kimi_converter.convert_events_to_trajectory(
                events, attempt_id=aid
            )
        else:  # opencode / mimo-code 契约同构
            events = opencode_converter.find_session_events(attempt_dir)
            trajectory = opencode_converter.convert_events_to_trajectory(
                events, attempt_id=aid, agent_name=agent
            )
        source_hint = "events.jsonl"
        if trajectory is None:
            return EmitOutcome(
                status="not_available",
                attempt_id=aid,
                reason=f"no usable event transcript under {source_hint}",
            )
        return EmitOutcome(
            status="ready",
            attempt_id=aid,
            trajectory=trajectory.to_json_dict(),
        )

    home = _resolve_home(attempt_dir, agent)
    if agent == "claude-code":
        events = cc_converter.find_session_events(home, attempt_dir)
        trajectory = cc_converter.convert_events_to_trajectory(events, attempt_id=aid)
        source_hint = f"{home.name}/.claude/projects"
    else:
        events = codex_converter.find_session_events(home)
        trajectory = codex_converter.convert_events_to_trajectory(events, attempt_id=aid)
        source_hint = f"{home.name}/sessions"

    if trajectory is None:
        return EmitOutcome(
            status="not_available",
            attempt_id=aid,
            reason=(
                f"no usable session transcript under {source_hint}; "
                f"codex single-turn runs were historically started with "
                f"--ephemeral and never persisted a session"
                if agent == "codex"
                else f"no usable session transcript under {source_hint}"
            ),
        )
    return EmitOutcome(
        status="ready",
        attempt_id=aid,
        trajectory=trajectory.to_json_dict(),
    )


def validate_trajectory(trajectory: dict[str, Any]) -> Trajectory:
    """用 vendored schema 校验一个 trajectory dict（测试/外部输入用）。"""
    return Trajectory.model_validate(trajectory)
