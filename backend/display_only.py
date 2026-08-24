"""纯展示部署的只读边界。

部署指南提到过「纯展示部署」，但此前没有一个明确、可验证的只读运行模式：
运营侧只能依赖「不安装执行依赖、不配置密钥」来降低误执行风险。那不是产品
安全边界——它无法证明一台公网历史展示节点不会发起模型调用或改变历史状态，
而且失败发生在很晚的阶段（撞上缺失的 CLI 或 key），不是在边界上直接拒绝。

本模块提供的是**后端**边界，不依赖前端隐藏：

- 执行型写请求（创建/调度 Run、Experiment、RunGroup，停止，重跑）一律 403；
- **启动期的恢复与常驻后台任务一律不调度**（见 ``skip_background_work``）；
- 历史数据的读取、导出与展示完全不受影响；
- ``/api/capabilities`` 与 ``/api/healthz`` 明确报告本节点为 display-only，
  便于部署验收和监控。

HTTP 判定基于**请求方法 + 路径**，默认放行 GET/HEAD/OPTIONS，写方法默认拒绝。
这个方向是刻意的：新增执行型端点时无需记得来这里登记，天然是安全的一侧。

**只拦 HTTP 是不够的**：启动恢复完全不经过请求路径。
``schedule_startup_scoring_recovery`` 会把 scoring job 重新入队并派发 judge
——那是一次真实的模型调用，需要凭据；``schedule_startup_recovery`` /
``schedule_run_group_recovery`` / deadline sweeper / 成本结算则会改写历史
attempt、run 与 group 的状态。一台纯展示节点因此既可能发起模型调用，
也可能改变它本应只负责展示的历史结论，与本模块开头声明的边界直接矛盾。
所以边界必须同时覆盖**进程内后台任务**，而不仅仅是入站请求。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# 纯展示节点上仍然允许的写请求：不触发执行、不改变历史结论的本地操作。
# 与执行链路无关的展示辅助（例如导出预览的按需渲染）走这里显式放行。
_WRITE_ALLOWLIST: tuple[re.Pattern[str], ...] = (
    # artifact 预览的按需渲染：只读产物 → 生成本地预览，不调用模型。
    re.compile(r"^/api/runs/[^/]+/attempts/[^/]+/artifact-previews/"),
)

DISPLAY_ONLY_ERROR = {
    "code": "display_only_deployment",
    "message": (
        "本节点以纯展示模式部署，只提供历史数据的只读访问；"
        "创建、调度、停止和重跑等执行型操作已在后端禁用。"
    ),
}


def is_write_blocked(method: str, path: str) -> bool:
    """该请求是否应被纯展示边界拒绝。"""
    if method.upper() in _READ_METHODS:
        return False
    return not any(pattern.search(path) for pattern in _WRITE_ALLOWLIST)


#: 纯展示节点上必须跳过的后台工作，及跳过的理由（用于启动日志与验收）。
#: 值同时是给运维看的说明——「为什么这台节点没有自动收敛」。
BACKGROUND_WORK_SKIPPED: dict[str, str] = {
    "scoring_recovery": "重新入队并派发 judge，是一次真实模型调用",
    "attempt_recovery": "改写历史 attempt 状态",
    "run_group_recovery": "改写历史 run group 状态",
    "score_outbox": "改写历史评分投影",
    "research_reconciliation": "改写历史 research 派生状态",
    "cost_settler": "持续访问上游 Management API 并改写成本账",
    "deadline_sweeper": "按 deadline 收敛历史 attempt / run",
    "rubric_evolution": "调用分析模型并生成候选 Rubric Artifact",
    "wire_recovery": "改写历史 wire manifest",
}


def skip_background_work(enabled: bool) -> bool:
    """纯展示节点是否应跳过启动恢复与常驻后台任务。

    单独成函数而不是在 lifespan 里裸写 ``if cfg.octagon.display_only``：
    边界规则集中在本模块，新增后台任务时能在这里一并看到该不该跳过。
    """
    return bool(enabled)


def install(app: Any, enabled: bool) -> None:
    """把只读边界装到 app 上。``enabled=False`` 时完全不介入请求路径。"""
    if not enabled:
        return

    @app.middleware("http")
    async def _display_only_guard(request: Request, call_next):
        if is_write_blocked(request.method, request.url.path):
            logger.info(
                "display-only 拒绝写请求：%s %s", request.method, request.url.path
            )
            return JSONResponse(status_code=403, content={"error": DISPLAY_ONLY_ERROR})
        return await call_next(request)

    logger.warning(
        "纯展示部署已启用：后端拒绝一切执行型写请求，不要求模型或成本凭据"
    )
