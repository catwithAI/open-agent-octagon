"""attempt 沙盒会话 → ATIF trajectory 的编排层。

在 wire/insight 数据结构**之外**，直接从沙盒 HOME 里各家 CLI 的本地会话转录
还原 ATIF-v1.7 对话流，用于归因。源分两类：

- **会话文件型**（claude-code / codex / kimi-code / dsh）：读 CLI 自落盘的
  转录（``.cc-iso-home/.claude/projects``、``.codex-iso-home/sessions``、
  ``.kimi-iso-home/.kimi/sessions``、``dsh_sessions``，沙箱模式为
  ``sandbox_home`` 下对应路径）。判 adapter：显式 ``agent_name`` 优先，否则
  按 home 标记推断；
- **事件流型**（opencode / mimo-code）：读 attempt 目录的 ``events.jsonl``
  （adapter 逐行落盘的 CLI 事件流，与 Harbor 读的 stdout 文件同源）。这类无
  home 标记可判别，须显式传 ``agent_name``。

源缺失（如 codex 历史单轮 ``--ephemeral`` 无会话）→ ``not_available``。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .schema import Trajectory
from .converters import claude_code as cc_converter
from .converters import codex as codex_converter
from .converters import dsh as dsh_converter
from .converters import blade_agent as blade_converter
from .converters import kimi_code as kimi_converter
from .converters import opencode as opencode_converter

PRODUCER_VERSION = "octagon-atif-v1"

_SUPPORTED_AGENTS = frozenset(
    {"blade-agent", "claude-code", "codex", "dsh", "kimi-code", "opencode", "mimo-code"}
)

#: 事件流型 agent：转换器读 attempt 目录 events.jsonl，不需 home 会话文件。
#: kimi-code **不在此列**——沙箱里的 1.50 会把带时间戳、带分步 token 的
#: wire.jsonl 落在 ``.kimi/sessions``，比 events.jsonl 的 role/content 流信息多。
#:
#: blade-agent 在此列但**契约与 opencode 族不同**（OpenAI 风格 role/message，
#: 非 opencode 的 part/state 流），故单独一个转换器。把它排除在 ATIF 之外会让
#: 它在同场竞技里证据形态与其余六家不对等——比较式评分尤其吃这个亏。
_EVENTS_AGENTS = frozenset({"blade-agent", "opencode", "mimo-code"})


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
    # dsh 的 DSH_SESSION_ROOT 指向 host_home(attempt_dir)/dsh_sessions，
    # 宿主机模式下 host_home 就是 attempt 根，故 home 根是 "."。
    "dsh": Path("."),
    "kimi-code": Path(".kimi-iso-home"),
}

#: 各 agent 在 home 根下的「存在即证明该 agent 跑过」的标记（沙箱/宿主机共用）。
#: codex 的 CODEX_HOME 指向 home 根，会话落在 ``<home>/sessions``；CC 的
#: CLAUDE_CONFIG_DIR 默认 ``$HOME/.claude``，转录在 ``<home>/.claude/projects``。
_SANDBOX_MARKERS = {
    "claude-code": ".claude/projects",
    "codex": "sessions",
    "dsh": "dsh_sessions",
    "kimi-code": ".kimi/sessions",
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
    if (attempt_dir / "dsh_sessions").is_dir():
        return "dsh"
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
    - ``agent_name``：显式指定 adapter（claude-code / codex / dsh / kimi-code /
      opencode / mimo-code）。事件流型（opencode / mimo-code）**必须显式传**
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
        # 事件流型：读 attempt 目录 events.jsonl（adapter 逐行落盘的流）。
        source_hint = "events.jsonl"
        if agent == "blade-agent":
            events = blade_converter.find_session_events(attempt_dir)
            trajectory = blade_converter.convert_events_to_trajectory(
                events, attempt_id=aid
            )
            detail = blade_converter.describe_empty(events) if trajectory is None else None
        else:  # opencode / mimo-code 契约同构，共用一个转换器
            events = opencode_converter.find_session_events(attempt_dir)
            trajectory = opencode_converter.convert_events_to_trajectory(
                events, attempt_id=aid, agent_name=agent
            )
            detail = None
        if trajectory is None:
            return EmitOutcome(
                status="not_available",
                attempt_id=aid,
                reason=(
                    f"{detail} (source: {source_hint})" if detail
                    else f"no usable event transcript under {source_hint}"
                ),
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
    elif agent == "kimi-code":
        events = kimi_converter.find_session_events(home)
        trajectory = kimi_converter.convert_events_to_trajectory(
            events, attempt_id=aid,
            system_prompt=kimi_converter.find_system_prompt(home),
        )
        source_hint = f"{home.name}/.kimi/sessions"
    elif agent == "dsh":
        # session-id 恒为 octagon-<attempt_id>，传进去做精确定位。
        events = dsh_converter.find_session_events(home, aid)
        trajectory = dsh_converter.convert_events_to_trajectory(events, attempt_id=aid)
        source_hint = f"{home.name}/dsh_sessions"
    else:
        events = codex_converter.find_session_events(home)
        trajectory = codex_converter.convert_events_to_trajectory(events, attempt_id=aid)
        source_hint = f"{home.name}/sessions"

    if trajectory is None:
        # 「转录不存在」与「转录在、但那一轮以错误收场」必须分开报——把后者
        # 说成前者，等于把上游/设施故障伪装成采集缺陷。dsh 的转录里有
        # turn/end.reason，能给出真实原因。
        if agent == "dsh":
            detail = dsh_converter.describe_empty(events)
        elif agent == "kimi-code":
            detail = kimi_converter.describe_empty(events)
        else:
            detail = None
        if detail:
            reason = f"{detail} (source: {source_hint})"
        elif agent == "codex":
            reason = (
                f"no usable session transcript under {source_hint}; "
                f"codex single-turn runs were historically started with "
                f"--ephemeral and never persisted a session"
            )
        else:
            reason = f"no usable session transcript under {source_hint}"
        return EmitOutcome(status="not_available", attempt_id=aid, reason=reason)
    return EmitOutcome(
        status="ready",
        attempt_id=aid,
        trajectory=trajectory.to_json_dict(),
    )


def validate_trajectory(trajectory: dict[str, Any]) -> Trajectory:
    """用 vendored schema 校验一个 trajectory dict（测试/外部输入用）。"""
    return Trajectory.model_validate(trajectory)
