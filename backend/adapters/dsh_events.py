"""dsh 事件的共享解析原语。

**为什么单独一个模块**：adapter（`backend/adapters/dsh.py`）与 wire
normalizer（`backend/wire/normalizers/dsh.py`）都要从同一批事件里读用量，
但它们跑在不同时机、写进不同产物（`AdapterResult.token_usage` vs
`aggregate_usage` evidence）。两边各写一份解析的结果是**语义漂移**——
同一个事件被算出两个数，而这种不一致只会在有人对账时才暴露。

这里放两边共用的：字段映射、取值规则、累计规则。

取值规则（三条，两边必须一致）：

- `bool` **不算数字**。Python 里 `isinstance(True, int)` 为真，不排除的话
  `{"inputTokens": true}` 会被当成 1 —— 那是把协议错误伪装成合法用量。
- 负数**截到 0**。token 计数不可能为负；出现负数说明上游算错了，
  截断比透传更安全（透传会让成本核算出现负费用）。
- 缺失/非数字 → `None`（不可得），**不是 0**。`token_usage.py:14-31` 的核心
  约定：`None` = 该 producer 不提供，`0` = 确实是零。走 pi-ai route 时
  cache 与 reasoning 就是不可得，填 0 等于谎称观测到了零。
"""

from __future__ import annotations

from typing import Any

#: dsh 的 camelCase → Octagon 五维。
USAGE_KEY_MAP: dict[str, str] = {
    "input_tokens": "inputTokens",
    "output_tokens": "outputTokens",
    "cache_read_tokens": "cacheReadTokens",
    "cache_write_tokens": "cacheWriteTokens",
    "reasoning_tokens": "reasoningTokens",
}


def coerce_token_count(value: Any) -> int | None:
    """单个 token 计数的取值规则（见模块 docstring 的三条）。"""
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return None


def usage_from_event(usage: Any) -> dict[str, int | None]:
    """`assistant/message.data.usage` → 五维（缺的写 None）。"""
    if not isinstance(usage, dict):
        return dict.fromkeys(USAGE_KEY_MAP)
    return {
        ours: coerce_token_count(usage.get(theirs))
        for ours, theirs in USAGE_KEY_MAP.items()
    }


def merge_usage(
    base: dict[str, int | None] | None, delta: dict[str, int | None]
) -> dict[str, int | None]:
    """逐条求和。`None` 视作不可得：None + N = N，None + None = None。

    dsh 的 usage 是**该 step 的量**而不是累计快照（实测多轮各自独立计数），
    所以求和而非取 max——取 max 会把多轮消耗压成最大的那一轮。
    """
    if base is None:
        return dict(delta)
    out: dict[str, int | None] = {}
    for key in set(base) | set(delta):
        a, b = base.get(key), delta.get(key)
        out[key] = b if a is None else (a if b is None else a + b)
    return out


def tool_result_call_id(data: dict[str, Any]) -> str | None:
    """`tool/result` 的配对键：埋在 `data.message.content[0].toolCallId`。

    不在顶层——这是 dsh 与 CC 唯一形状不同的地方，两边解析都要走这里。
    """
    message = data.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list) or not content:
        return None
    block = content[0]
    if not isinstance(block, dict):
        return None
    call_id = block.get("toolCallId")
    return call_id if isinstance(call_id, str) and call_id else None


def module_available(module: str) -> bool:
    """SDK 类 agent 的可用性判据：模块能否被找到。

    **不 import**——`deepseek_harness` 会牵出 50 MiB 的 runtime 包，
    而 `/agents` 端点只是想知道装没装。

    `api.py` 与 `experiments/protocol.py` 都要这个判断，共用一份避免
    异常处理语义漂移（`find_spec` 对未安装抛 `ModuleNotFoundError`、
    对畸形包名抛 `ValueError`，两处各写一遍很容易只 catch 其中一个）。
    """
    import importlib.util

    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False
