"""资金口径的唯一出口。

**问题**：运行详情、实验汇总、导出和 HTML 报告各自去数据库里拿"成本"，
有的读 `cost_key_ledgers.cost_usd`（上游实扣），有的读 `attempts.cost_usd`
（token × 本地价表估算）。两者相差可达一个数量级——Talent / Luna 的 Kimi
上游实扣 $0.1866，本地估算 $4.25，报告据后者写出 $13.56 的"总成本"。
同一个 run 因此同时存在两套互相矛盾的金额。

**纪律**：

1. 单个 attempt 的**实际成本**只有一个来源——其临时 key 结算为 `final`
   且归因模式为 `ephemeral_run_key` 时的上游实扣。除此之外一律没有金额，
   `audited_cost_usd` 为 None，不回落估算、不填 0。
2. 本地估算只是**异常探针**。它在这里以 `estimate` 子对象出现，字段名
   自带 `estimate` 前缀，任何消费端都不可能把它误当资金口径展示。
3. run / 实验级总额只在**全部**预期 attempt 与 judge 都可审计时存在。
   缺任何一笔，`total_cost_usd` 为 None，已有部分单列 `partial_cost_usd`,
   并且**不得**被当作总额、上界、预算依据或性价比排名输入。

新增消费端一律调用本模块，不要自己拼 SQL 取成本列。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

#: 缺账原因 → 面向人的解释。消费端展示这个，而不是裸状态码。
_UNAUDITED_REASON = {
    "pending": "结算中",
    "settling": "结算中",
    "failed": "上游结算失败",
    "upper_bound": "共享 key，仅为上界",
}

#: 结构性拿不到归因的 error_code（例如 Blade 经 llm-gateway 调用上游）。
_STRUCTURAL_CODES = {"gateway_bound_agent"}


def attempt_money(ledger: Any | None, *, estimate_usd: float | None = None,
                  estimate_priced: bool | None = None) -> dict[str, Any]:
    """单个 attempt 的资金视图。

    `ledger` 是 `cost.ledger.CostKeyLedger` 或 None（从未建账）。
    只有 `ledger.auditable` 为真时 `audited_cost_usd` 才有值——那正是该
    attempt 临时 key 的 `usage_final - usage_start`。

    估算恒不参与 `audited_cost_usd`；它只出现在 `estimate` 里，供交叉核对。
    """
    audited = ledger.cost_usd if (ledger is not None and ledger.auditable) else None
    if ledger is None:
        reason = "无成本账"
        structural = False
    elif ledger.auditable:
        reason = None
        structural = False
    else:
        structural = (ledger.error_code or "") in _STRUCTURAL_CODES
        reason = (
            "经网关调用上游，无法按 attempt 归因"
            if structural
            else _UNAUDITED_REASON.get(ledger.status, ledger.status)
        )
    return {
        # 唯一的资金真值。None = 这笔钱没有可审计的归因，**不是 0**。
        "audited_cost_usd": audited,
        "auditable": audited is not None,
        "status": ledger.status if ledger is not None else "unavailable",
        "attribution_mode": ledger.attribution_mode if ledger is not None else None,
        "error_code": ledger.error_code if ledger is not None else None,
        # 为什么没有金额。"结算中"等得到，"经网关"永远等不到——两者
        # 对总额完整性的含义完全不同。
        "unaudited_reason": reason,
        "unaudited_is_structural": structural,
        # 异常探针，**不是**成本。字段名自带 estimate，避免被误取。
        "estimate": {
            "estimate_cost_usd": estimate_usd,
            # False = 有令牌缺定价，估算值只是下界
            "estimate_priced": estimate_priced,
        },
    }


def rollup(views: list[dict[str, Any]], *, expected: int | None = None,
           judge_cost_usd: float | None = None,
           judge_required: bool = False,
           judge_auditable: bool = False) -> dict[str, Any]:
    """把若干 attempt 资金视图汇成一个总额口径。

    run 级、实验级、导出和报告共用同一套判定，因此不可能再出现
    "同一批数据在两个页面上金额不同"。

    `expected` 是**应有**的 attempt 数。少于它说明有 attempt 根本没建账，
    那部分钱没被计入——此时总额必须为 None，而不是把已有的加起来当总额
    （那是偏低值，既不是实扣也不是上界）。
    """
    audited = [v["audited_cost_usd"] for v in views if v["audited_cost_usd"] is not None]
    unaudited = [v for v in views if v["audited_cost_usd"] is None]

    partial: float | None = sum(audited) if audited else None
    if judge_cost_usd is not None and judge_auditable:
        partial = (partial or 0.0) + judge_cost_usd

    missing_rows = expected is not None and len(views) < expected
    judge_gap = judge_required and not judge_auditable
    complete = not unaudited and not missing_rows and not judge_gap and bool(views)

    return {
        # 只有账全齐才有总额。任何一笔缺失/pending/settling/failed/不可审计
        # 都让它保持 None——消费端据此显示"未知"，不得用估算补齐。
        "total_cost_usd": partial if complete else None,
        # 已入账部分。**不是**总额也不是上界，只能单列展示。
        "partial_cost_usd": partial,
        "complete": complete,
        "audited_count": len(audited),
        "unaudited_count": len(unaudited),
        "expected_count": expected,
        "missing_ledger_count": (
            max(0, expected - len(views)) if expected is not None else 0
        ),
        "judge_required": judge_required,
        "judge_missing": judge_gap,
        # 缺的是"等得到"还是"永远等不到"——决定要不要继续等
        "unaudited": [
            {
                "status": v["status"],
                "reason": v["unaudited_reason"],
                "structural": v["unaudited_is_structural"],
            }
            for v in unaudited
        ],
    }


def attempt_money_for_run(db_path: Path, run_id: str) -> dict[str, dict[str, Any]]:
    """一次取出 run 内全部 attempt 的资金视图，按 attempt_id 索引。

    逐个 attempt 查库会在 N 个 agent 的对比页上放大成 N 次查询，
    列表类消费端应当用这个。
    """
    from . import ledger as L

    by_scope = {
        row.scope_id: row
        for row in L.list_for_run(db_path, run_id)
        if row.scope == "attempt"
    }
    return {aid: attempt_money(row) for aid, row in by_scope.items()}
