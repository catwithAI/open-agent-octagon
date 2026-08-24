"""Token usage parsing helpers shared by agent adapters."""

from __future__ import annotations

import json
from typing import Any


INPUT_KEYS = ("input_tokens", "prompt_tokens", "inputTokens", "promptTokens")
OUTPUT_KEYS = ("output_tokens", "completion_tokens", "outputTokens", "completionTokens")

# ---------- 五维用量（token_cost_accounting）--------------------------------
#
# 只统计 input/output 无法算准成本：cache read 的单价普遍是 prompt 的 1/10
# （实测 OpenRouter：sonnet-5 $2/M vs $0.2/M，ds4-flash $0.14/M vs $0.028/M），
# 把它和真实 input 混在一起会把成本高估近一个数量级。reasoning token 则按
# output 计价但不体现在正文里，漏掉会低估。故 attempt 级用量必须保留五维。
#
# **None 与 0 有本质区别**（沿用 wire normalizer 的既有原则）：None = 该
# producer 不提供这个字段（如 codex 没有 cache write 概念），0 = 确实是零。
# 前者不能参与求和/计价，后者可以。用 dict 而非 dataclass 承载，是为了与
# AdapterResult.token_usage 的既有 dict 形状兼容，旧读取方零改动。
#
# 跨 agent 比较的口径警告：五维的**采集面按 agent 分化**——
# blade-agent 只上报 input/output，其余五家还带 cache_read / cache_write /
# reasoning。实测中位数（最近 60 个样本/agent）：blade 的 cache_read 与
# reasoning 恒为 0，claude-code 的 cache_read 是 281,856，kimi-code 是 466,432。
# 于是 in+out 比 in+out 是公平的（同口径），但任何据此得出的「token 效率」倍数
# 都必须显式标注口径是 in+out，否则读者会默认那是总消耗——而对五家 CLI agent
# 来说，总消耗里被略去的那部分往往比 in+out 本身还大。

USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)

CACHE_READ_KEYS = (
    "cache_read_tokens",
    "cache_read_input_tokens",  # claude-code
    "cached_input_tokens",      # codex
    "cached_tokens",            # OpenAI 风格 prompt_tokens_details
)
CACHE_WRITE_KEYS = (
    "cache_write_tokens",
    "cache_creation_input_tokens",  # claude-code
)
REASONING_KEYS = (
    "reasoning_tokens",
    "reasoning_output_tokens",  # codex
)


def usage_input_tokens(usage: dict[str, Any] | None) -> int:
    return _first_int(usage, INPUT_KEYS)


def usage_output_tokens(usage: dict[str, Any] | None) -> int:
    return _first_int(usage, OUTPUT_KEYS)


def _first_optional_int(
    usage: dict[str, Any] | None, keys: tuple[str, ...]
) -> int | None:
    """取第一个命中的键；**全部缺失返回 None**（不可得），不退化成 0。"""
    if not usage:
        return None
    for key in keys:
        value = usage.get(key)
        if value is None:
            continue
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return None


def usage_detail(usage: dict[str, Any] | None) -> dict[str, int | None]:
    """OpenAI/Anthropic 风格 usage → 五维（含嵌套 *_details 展开）。

    嵌套形状（blade / OpenRouter 原样透传）：
        prompt_tokens_details.cached_tokens / .cache_write_tokens
        completion_tokens_details.reasoning_tokens
    """
    if not isinstance(usage, dict):
        return dict.fromkeys(USAGE_FIELDS)
    flat = dict(usage)
    for nested_key in ("prompt_tokens_details", "completion_tokens_details"):
        nested = usage.get(nested_key)
        if isinstance(nested, dict):
            # 嵌套值不覆盖同名顶层键：顶层是 producer 显式给的，优先级更高。
            for k, v in nested.items():
                flat.setdefault(k, v)
    output = _first_optional_int(flat, OUTPUT_KEYS)
    reasoning = _first_optional_int(flat, REASONING_KEYS)
    # Codex's top-level reasoning_output_tokens is disjoint from output_tokens
    # (unlike OpenAI completion_tokens_details.reasoning_tokens, which is a
    # subset).  Canonical output_tokens is the total billable completion tier.
    if (
        output is not None
        and reasoning is not None
        and flat.get("reasoning_output_tokens") is not None
    ):
        output += reasoning
    return {
        "input_tokens": _first_optional_int(flat, INPUT_KEYS),
        "output_tokens": output,
        "cache_read_tokens": _first_optional_int(flat, CACHE_READ_KEYS),
        "cache_write_tokens": _first_optional_int(flat, CACHE_WRITE_KEYS),
        "reasoning_tokens": reasoning,
    }


def merge_usage(
    base: dict[str, int | None], delta: dict[str, int | None]
) -> dict[str, int | None]:
    """累加两份五维用量。

    None 是"不可得"而非零，故 None + N = N（有一方给了就用它），
    None + None = None（始终没人给过，保持不可得）。这样一个 attempt 里
    只要有任意一轮报了 cache read，总量就不会被 None 抹掉。
    """
    out: dict[str, int | None] = {}
    for f in USAGE_FIELDS:
        a, b = base.get(f), delta.get(f)
        out[f] = b if a is None else (a if b is None else a + b)
    return out


def empty_usage() -> dict[str, int | None]:
    return dict.fromkeys(USAGE_FIELDS)


def compact_usage(usage: dict[str, int | None]) -> dict[str, int]:
    """落盘形状：丢掉 None 字段。

    历史 `token_usage_json` 只有 input/output 两个键，读取方直接 `.get()`。
    保留 None 会让"不可得"被 JSON 序列化成 null，下游 `int(x)` 炸掉；
    丢掉键则与旧行为一致（缺键即不可得），向后兼容。
    """
    return {k: v for k, v in usage.items() if v is not None}


def result_usage_tokens(data: dict[str, Any]) -> tuple[int, int]:
    """Return exact usage from a result-like event.

    Claude-compatible CLIs have emitted several shapes over time:
    `usage.input_tokens`, OpenAI-style `prompt_tokens`, camelCase fields, and
    per-model `modelUsage` objects. Keep all of those compatible here.
    """
    usage = data.get("usage") or data.get("token_usage") or {}
    input_tokens = usage_input_tokens(usage if isinstance(usage, dict) else None)
    output_tokens = usage_output_tokens(usage if isinstance(usage, dict) else None)

    model_usage = data.get("modelUsage") or data.get("model_usage") or {}
    if isinstance(model_usage, dict):
        for item in model_usage.values():
            if not isinstance(item, dict):
                continue
            input_tokens += usage_input_tokens(item)
            output_tokens += usage_output_tokens(item)

    return input_tokens, output_tokens


def estimate_tokens_from_event(data: dict[str, Any]) -> tuple[int, int]:
    """Fallback estimate when a CLI emits zero usage.

    This is intentionally conservative and flagged by callers via external refs;
    it prevents a run with rich events from appearing as "no token accounting".
    """
    msg_type = data.get("type")
    if msg_type == "assistant":
        message = data.get("message", {})
        return 0, _estimate_text_tokens(_message_text(message))
    if msg_type == "user":
        message = data.get("message", {})
        return _estimate_text_tokens(_message_text(message)), 0
    if msg_type == "system":
        return _estimate_text_tokens(json.dumps(data, ensure_ascii=False)), 0
    return 0, 0


def _first_int(usage: dict[str, Any] | None, keys: tuple[str, ...]) -> int:
    if not usage:
        return 0
    for key in keys:
        value = usage.get(key)
        if value is None:
            continue
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return 0


def _message_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    parts: list[str] = []
    content = message.get("content", [])
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            for key in ("text", "thinking", "content"):
                value = block.get(key)
                if isinstance(value, str):
                    parts.append(value)
            tool_input = block.get("input")
            if tool_input is not None:
                parts.append(json.dumps(tool_input, ensure_ascii=False))
    return "\n".join(parts)


def _estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    non_ascii_chars = len(text) - ascii_chars
    return max(1, (ascii_chars + 3) // 4 + non_ascii_chars)
