"""attempt 沙盒会话 → ATIF trajectory 的编排层。

在 wire/insight 数据结构**之外**，直接从沙盒 HOME 里各家 CLI 的本地会话转录
还原 ATIF-v1.7 对话流，用于归因。判 adapter：显式 ``agent_name`` 优先，否则按
沙盒 home 推断（``.cc-iso-home`` → claude-code，``.codex-iso-home/sessions`` →
codex）。源缺失（如 codex 历史单轮 ``--ephemeral`` 无会话）→ ``not_available``。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .schema import Trajectory
from .converters import claude_code as cc_converter
from .converters import codex as codex_converter

PRODUCER_VERSION = "octagon-atif-v1"

_SUPPORTED_AGENTS = frozenset({"claude-code", "codex"})


@dataclass(frozen=True)
class EmitOutcome:
    status: Literal["ready", "not_available"]
    attempt_id: str
    trajectory: dict[str, Any] | None = None
    reason: str | None = None

    @property
    def producer(self) -> str:
        return PRODUCER_VERSION


def _infer_agent(attempt_dir: Path) -> str | None:
    """按沙盒 home 推断 adapter。``.cc-iso-home/.claude/projects`` → claude-code；
    ``.codex-iso-home``（含无 sessions 的历史单轮）→ codex。两者都无 → None。"""
    if (attempt_dir / ".cc-iso-home" / ".claude" / "projects").is_dir():
        return "claude-code"
    if (attempt_dir / ".codex-iso-home").is_dir():
        return "codex"
    return None


def emit_attempt_atif(
    attempt_dir: Path | str,
    *,
    agent_name: str | None = None,
    attempt_id: str | None = None,
) -> EmitOutcome:
    """把一个 attempt 目录还原成 ATIF trajectory。

    参数：
    - ``attempt_dir``：attempt 数据目录（含 ``.cc-iso-home`` / ``.codex-iso-home``）。
    - ``agent_name``：显式指定 adapter（"claude-code" / "codex"）；缺省按沙盒推断。
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
            reason="no supported sandbox home found (.cc-iso-home / .codex-iso-home)",
        )
    if agent not in _SUPPORTED_AGENTS:
        return EmitOutcome(
            status="not_available",
            attempt_id=aid,
            reason=f"agent {agent!r} not supported (supported: {sorted(_SUPPORTED_AGENTS)})",
        )

    if agent == "claude-code":
        cc_home = attempt_dir / ".cc-iso-home"
        events = cc_converter.find_session_events(cc_home, attempt_dir)
        trajectory = cc_converter.convert_events_to_trajectory(events, attempt_id=aid)
        source_hint = ".cc-iso-home/.claude/projects"
    else:
        codex_home = attempt_dir / ".codex-iso-home"
        events = codex_converter.find_session_events(codex_home)
        trajectory = codex_converter.convert_events_to_trajectory(events, attempt_id=aid)
        source_hint = ".codex-iso-home/sessions"

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
