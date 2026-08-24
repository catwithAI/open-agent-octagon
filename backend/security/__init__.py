"""安全维度：离线扫描 trace/events/thinking，识别危险行为并判定人在回路状态。

对外只暴露 scan（纯函数）与数据模型。不碰执行链路，输入是已落盘的文件内容，
输出结构化 SecurityEvent + SecuritySummary。设计见 docs/specs/security_dimension/。
"""

from __future__ import annotations

from .models import (
    HITL_STATUSES,
    SEVERITY_ORDER,
    ScanResult,
    SecurityContext,
    SecurityEvent,
    SecuritySummary,
)
from .classifier import scan

__all__ = [
    "scan",
    "SecurityEvent",
    "SecuritySummary",
    "ScanResult",
    "SecurityContext",
    "SEVERITY_ORDER",
    "HITL_STATUSES",
]
