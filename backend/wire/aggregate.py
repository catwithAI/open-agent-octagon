"""canonical token 聚合回填。

有 canonical `llm_call` 时，从 `phase=agent_run` 的 call usage 聚合出 attempt
级 token 总量，写回 `token_usage_json`，并标 `external_refs.token_usage_source`：

- `wire`：来自 canonical calls（调用级证据聚合）；
- `adapter`：无 canonical calls，沿用 adapter result（保底）。

聚合值与 adapter 已有值冲突时**双保留**（不覆盖 adapter 值、不改历史 score），
把 wire 聚合另存 `external_refs.wire_token_usage`，差异记 `token_usage_conflict`。
不因 canonical 重建自动改历史 score。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from backend.cost.estimate import apply_cost_columns
from backend.wire import paths

_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)


def aggregate_agent_run_usage(records: Any) -> dict[str, int] | None:
    """聚合 agent_run usage；native llm_call 优先，HTTP provider usage 兜底。

    ``records`` 可为 list 或行迭代器——逐条累计，不整份进内存。

    OpenCode/CC 等 producer 能直接提供 native ``llm_call`` usage，继续以它为
    权威来源。Kimi CLI 当前不吐 usage；其请求已经过 Octagon 反代，因此在完全
    没有 native usage 时，回退汇总 outbound ``http_exchange.data.usage``。
    两类证据绝不相加，避免同一调用重复计数。
    """
    native_totals = {f: 0 for f in _USAGE_FIELDS}
    http_totals = {f: 0 for f in _USAGE_FIELDS}
    native_seen = False
    http_seen = False
    for rec in records:
        if rec.get("phase") != "agent_run":
            continue
        record_type = rec.get("record_type")
        data = rec.get("data") or {}
        if record_type == "llm_call":
            totals = native_totals
        elif record_type == "http_exchange" and data.get("direction") == "outbound":
            totals = http_totals
        else:
            continue
        usage = data.get("usage") or {}
        for f in _USAGE_FIELDS:
            v = usage.get(f)
            if isinstance(v, (int, float)):
                totals[f] += int(v)
                if record_type == "llm_call":
                    native_seen = True
                else:
                    http_seen = True
    if native_seen:
        return native_totals
    return http_totals if http_seen else None


def _iter_wire_records(wire_path: Path) -> Any:
    """逐行 yield canonical record，不整份读入。"""
    if not wire_path.exists():
        return
    with wire_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def backfill_token_usage(db_path: Path, data_path: Path, attempt_id: str) -> str:
    """把 wire 聚合回填 attempts 表，返回 token_usage_source（wire|adapter）。

    canonical 缺失时不动 token_usage_json，只标 source=adapter。冲突双保留。
    """
    wire_path = paths.wire_file(data_path, attempt_id)
    wire_usage = aggregate_agent_run_usage(_iter_wire_records(wire_path))

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT token_usage_json, external_refs_json FROM attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()
        if row is None:
            return "adapter"
        adapter_usage = json.loads(row[0] or "{}")
        external = json.loads(row[1] or "{}")

        # backfill 是幂等重算：先无条件清掉上一次的 wire 派生字段，再按本次
        # 结果重建——否则 parser 升级后 conflict → resolved / wire → adapter
        # 的状态转换不会收敛，旧 conflict 会残留。
        external.pop("wire_token_usage", None)
        external.pop("token_usage_conflict", None)

        if wire_usage is None:
            # 无 canonical calls：回退 adapter，不留任何 wire 派生态。
            source = "adapter"
            external["token_usage_source"] = source
        else:
            source = "wire"
            external["token_usage_source"] = source
            external["wire_token_usage"] = wire_usage
            # 冲突：adapter 已上报数值（含显式 0，区分 null）且与 wire 不一致
            # → 双保留，不覆盖 score 相关。用 isinstance 判定「有数据」，
            # 不能用 truthiness（0 是有效上报，不是缺失）。
            adapter_in = adapter_usage.get("input_tokens")
            adapter_out = adapter_usage.get("output_tokens")
            has_adapter = isinstance(adapter_in, (int, float)) or isinstance(
                adapter_out, (int, float)
            )
            if has_adapter and (
                adapter_in != wire_usage["input_tokens"]
                or adapter_out != wire_usage["output_tokens"]
            ):
                external["token_usage_conflict"] = {
                    "adapter": {
                        "input_tokens": adapter_in,
                        "output_tokens": adapter_out,
                    },
                    "wire": {
                        "input_tokens": wire_usage["input_tokens"],
                        "output_tokens": wire_usage["output_tokens"],
                    },
                }
            else:
                # 一致（含 resolved）：token_usage_json 采用 wire 聚合（更细）。
                #
                # **必须写全五维**（2026-07-29 修）：这里原先只取 input/output
                # 两维，把刚算出来的 cache_read_tokens 直接丢掉——落库后
                # 缓存复读被当成全价 input 计费，成本高估 3.5~4×。
                # 实测 blade-agent 一次 attempt 有 303 万 cache_read（命中率 95%），
                # 单价只有 prompt 的 1/5，丢掉这一维等于把最便宜的部分按最贵算。
                adapter_usage = dict(wire_usage)

        conn.execute(
            "UPDATE attempts SET token_usage_json=?, external_refs_json=? WHERE id=?",
            (
                json.dumps(adapter_usage, ensure_ascii=False),
                json.dumps(external, ensure_ascii=False),
                attempt_id,
            ),
        )
        # 成本估算随 token 一起重算。backfill 可能发生在
        # commit_scoring_result **之后**，若不重算，cost_usd 会停留在按旧
        # （通常偏低或缺失）token 算出的值上，与 token_usage_json 自相矛盾。
        # 用 adapter_usage 而非 wire_usage：前者才是本次真正写进库的那份
        # （冲突时保留 adapter 值，一致时已被换成 wire 值）。
        apply_cost_columns(conn, attempt_id, token_usage=adapter_usage)
        conn.commit()
    return source
