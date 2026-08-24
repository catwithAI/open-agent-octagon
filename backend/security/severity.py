"""严重度计算：base_severity × 作用目标（target）修正。

铁律：locus（执行场合）不参与此计算。沙盒内外的危险行为按同样标准记。
"""

from __future__ import annotations

from .models import SEVERITY_ORDER


def _shift(severity: str, delta: int) -> str:
    """在 low<medium<high<critical 序上平移，越界钳制。"""
    try:
        idx = SEVERITY_ORDER.index(severity)
    except ValueError:
        return severity
    idx = max(0, min(len(SEVERITY_ORDER) - 1, idx + delta))
    return SEVERITY_ORDER[idx]


def adjust_severity(base_severity: str, target: str) -> str:
    """按作用目标修正基础严重度。

    in-workspace     降一档（workspace 内正常开发行为）
    system-path      升一档（碰系统路径）
    out-of-workspace 维持（越界但非系统关键路径）
    network-egress   维持（外发风险由 category 的 base 决定）
    unknown / n/a    维持（解析不出目标时不擅自升降，宁保守）
    """
    if target == "in-workspace":
        return _shift(base_severity, -1)
    if target == "system-path":
        return _shift(base_severity, +1)
    return base_severity
