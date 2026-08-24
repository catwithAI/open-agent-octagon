"""Run 级成本核算。

回答的问题：**一次 run 内并行或串行的 N 个 session，加上评分，最终扣了多少钱？**

做法是每个 run 一把动态临时 key，三次 usage 采样，一行成本快照：

    execution_cost = usage_after_execution - usage_start
    scoring_cost   = usage_final           - usage_after_execution
    run_total      = usage_final           - usage_start

**不是**逐请求账本。逐 session 成本、cache read/write 上游权威值、逐请求延迟
都拿不到（N 个 session 共用一把 key），这是方案的已知边界，不是待补的缺口。

依赖方向是单向的：编排层（run_service / run_dispatch）→ 本包。
本包不得 import run_service / run_dispatch，否则循环依赖且无法单测。
"""

from __future__ import annotations

from .errors import (
    CostAuditError,
    ManagementAPIError,
    ManagementAuthError,
    ManagementRateLimited,
    ManagementTimeout,
    ManagementUnavailable,
)
from .models import (
    ATTRIBUTION_MODES,
    AUDITABLE_STATUS,
    TERMINAL_STATUSES,
    ActivityRow,
    AttributionMode,
    AuditStatus,
    CreatedKey,
    KeySnapshot,
    RunCostAudit,
)

__all__ = [
    "ATTRIBUTION_MODES",
    "AUDITABLE_STATUS",
    "TERMINAL_STATUSES",
    "ActivityRow",
    "AttributionMode",
    "AuditStatus",
    "CostAuditError",
    "CreatedKey",
    "KeySnapshot",
    "ManagementAPIError",
    "ManagementAuthError",
    "ManagementRateLimited",
    "ManagementTimeout",
    "ManagementUnavailable",
    "RunCostAudit",
]
