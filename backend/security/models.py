"""安全维度的数据模型。

原则（贯穿全模块）：
- 执行场合（locus）只随事件展示，不参与 severity 计算
- severity 只由基础严重度 × 作用目标（target）修正得到
- 安全轴与任务分（score_total）分离，不合并
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Layer = Literal["system", "business"]
Phase = Literal["executed", "attempted"]
Severity = Literal["low", "medium", "high", "critical"]
Target = Literal[
    "in-workspace",
    "out-of-workspace",
    "system-path",
    "network-egress",
    "unknown",
    "n/a",
]
HitlStatus = Literal[
    "sought-approval",
    "auto-executed",
    "aborted-on-denial",
    "overrode-denial",
    "not-reached",
    "n/a",
]

# severity 强弱序，用于取 max。索引越大越严重。
SEVERITY_ORDER: list[str] = ["low", "medium", "high", "critical"]

HITL_STATUSES: list[str] = [
    "sought-approval",
    "auto-executed",
    "aborted-on-denial",
    "overrode-denial",
    "not-reached",
]


def severity_rank(sev: str) -> int:
    try:
        return SEVERITY_ORDER.index(sev)
    except ValueError:
        return -1


def max_severity(sevs: list[str]) -> str | None:
    ranked = [(severity_rank(s), s) for s in sevs if severity_rank(s) >= 0]
    if not ranked:
        return None
    return max(ranked)[1]


@dataclass
class SecurityEvent:
    """一条危险行为事件。系统层与业务层共用这一结构。"""

    layer: Layer
    category: str  # destructive-fs / network-config / high-risk-action ...
    severity: Severity
    phase: Phase  # executed（做了） vs attempted（打算做但没做）
    command: str  # 命令原文 / 业务工具名+参数摘要
    target: Target  # 作用目标（参与 severity）
    locus: str  # 执行场合（展示用，不参与 severity）
    hitl_status: HitlStatus  # 人在回路判定
    rule_id: str  # 命中的规则 id，可解释、可回归
    source_ref: dict[str, Any] = field(default_factory=dict)  # {log,line} 或 {trace_seq}

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "category": self.category,
            "severity": self.severity,
            "phase": self.phase,
            "command": self.command,
            "target": self.target,
            "locus": self.locus,
            "hitl_status": self.hitl_status,
            "rule_id": self.rule_id,
            "source_ref": self.source_ref,
        }


@dataclass
class SecuritySummary:
    """写入 attempts DB 列的汇总。明细在 security_events.jsonl。"""

    event_count: int = 0
    max_severity: str | None = None
    # 各危险类别事件数（executed）
    by_category: dict[str, int] = field(default_factory=dict)
    # hitl 计数 + 头号指标 auto_exec_rate（按 layer 分组）
    hitl: dict[str, Any] = field(default_factory=dict)
    # 场景反应谱结论（无场景时 None）
    reaction: str | None = None
    # 本次扫描各通道的来源与采集情况。没有它，「扫了但真干净」和「压根没采到」
    # 在 event_count=0 上不可区分——安全轴的跨 agent 对比会变成在比谁采集得全。
    coverage: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_count": self.event_count,
            "max_severity": self.max_severity,
            "by_category": self.by_category,
            "hitl": self.hitl,
            "reaction": self.reaction,
            "coverage": self.coverage,
        }


@dataclass
class ScanResult:
    events: list[SecurityEvent]
    summary: SecuritySummary


@dataclass
class SecurityContext:
    """scan 的上下文：谁在哪执行、workspace 边界、哪些业务工具危险。"""

    agent_name: str = ""
    execution_locus: str = "unknown"  # docker-sandbox / host / remote-host / unknown
    workspace_root: str | None = None
    # 额外 workspace 根前缀（沙盒内 workspace 与宿主机 attempt_dir 不同时用），
    # 命中即算 in-workspace。如 blade 沙盒 /root/智能助手工作空间/<session>。
    extra_workspace_prefixes: tuple[str, ...] = ()
    # env meta.yaml 的 danger_tools：{tool_name: {category, severity, irreversible}}
    danger_tools: dict[str, dict[str, Any]] = field(default_factory=dict)
