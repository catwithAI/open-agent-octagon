"""Redaction pipeline。

写盘前的脱敏是硬边界：secret 必须在进入 spool/blob 之前被移除，
redactor 自身抛异常时丢弃 payload 只留 metadata + ``redaction_failed``，
绝不能 fallback 到未脱敏原文。

三层规则：

1. header 黑名单——五类敏感 header 的值永不落盘；
2. JSON key pattern——递归匹配 ``api_key|token|secret|password|authorization|cookie``
   （大小写不敏感），命中 key 的整个值替换为占位符；
3. 自由文本 secret pattern——payload 字符串值和日志/错误消息里的
   ``sk-...`` / ``Bearer ...`` / AWS key / JWT 等形状。

``scrub_text`` 同时供日志和错误消息使用：parse error、
manifest failure_reason 等任何可能内嵌请求片段的文本都必须先过它。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

REDACTED = "[REDACTED]"

# 永不持久化的 header（小写比较）。
BLOCKED_HEADERS: frozenset[str] = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "cookie",
        "set-cookie",
    }
)

# 默认 JSON key pattern，大小写不敏感；命中即整值替换。
DEFAULT_KEY_PATTERN = re.compile(
    r"api[_-]?key|token|secret|password|authorization|cookie", re.IGNORECASE
)

# token 用量字段白名单（token_cost_accounting）。
#
# 上面的 `token` 规则会连带命中 `input_tokens` / `cache_read_input_tokens` 这类
# **纯计数**字段，把用量抹成 [REDACTED]——实测 2026-07-27 反代抓到的响应体里
# usage 全被脱敏，导致无法从 wire 补齐用量（kimi 这类不吐 usage 的 CLI 只能
# 靠这条路），成本核算也失去交叉校验来源。
#
# 放宽必须**精确到字段名**，不能改 `token` 规则本身：`auth_token` /
# `access_token` / `refresh_token` 仍须脱敏。此处只豁免已知的计数字段，且
# 仅当值确实是数字时才豁免（见 `_is_usage_count`）——字符串值一律照旧脱敏，
# 避免有人把 secret 塞进叫 `total_tokens` 的键里蒙混过去。
USAGE_COUNT_KEYS: frozenset[str] = frozenset({
    # OpenAI / OpenRouter
    "prompt_tokens", "completion_tokens", "total_tokens",
    "cached_tokens", "cache_write_tokens", "reasoning_tokens",
    "audio_tokens", "video_tokens", "image_tokens", "text_tokens",
    "accepted_prediction_tokens", "rejected_prediction_tokens",
    # Anthropic
    "input_tokens", "output_tokens",
    "cache_read_input_tokens", "cache_creation_input_tokens",
    # codex
    "cached_input_tokens", "reasoning_output_tokens",
    # opencode / mimo（part.tokens 内为裸 input/output/reasoning，另在嵌套
    # cache 对象里；这里覆盖其扁平命名变体）
    "native_tokens_prompt", "native_tokens_completion",
    "native_tokens_reasoning", "native_tokens_cached",
})


# 用量**容器**键：本身不是计数，但整个对象都是计数（OpenAI 的
# `prompt_tokens_details` 也含 `token` 字样，不豁免会让整块被抹掉）。
# 豁免的是"继续向内递归"，容器内部仍逐字段走完整脱敏规则——里面若真有
# 非白名单的敏感键，照样会被替换。
USAGE_CONTAINER_KEYS: frozenset[str] = frozenset({
    "prompt_tokens_details", "completion_tokens_details", "tokens",
})


def _is_usage_count(key: str, value: Any) -> bool:
    """该键值对是否为可豁免脱敏的 token 计数。

    双重条件：键名在白名单内 **且** 值是数字（bool 除外——`True` 是 int 的
    子类，但它不是计数）。字符串值即使键名匹配也不豁免。
    """
    if key.lower() not in USAGE_COUNT_KEYS:
        return False
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_usage_container(key: str, value: Any) -> bool:
    """该键是否为用量容器（豁免"整块替换"，但内部仍逐字段脱敏）。"""
    return key.lower() in USAGE_CONTAINER_KEYS and isinstance(value, dict)

# 自由文本 secret 形状。保守起见宁可多替换：这里的输出只用于观测展示，
# 误伤可读性的代价远小于泄漏。
TEXT_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # OpenAI/Anthropic/blade 风格 key：sk- 前缀
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
    # Bearer/Basic 凭据
    re.compile(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9\-._~+/=]{8,}", re.IGNORECASE),
    # AWS access key id
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # GitHub token
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    # JWT（三段 base64url）
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\b"),
    # 显式 key=value / key: value 形式的赋值（值部分替换）
    re.compile(
        r"(?i)\b(api[_-]?key|token|secret|password)\s*[=:]\s*[^\s,;\"']{6,}"
    ),
)


def scrub_text(text: str) -> str:
    """自由文本 secret scrub——payload 字符串、日志、错误消息共用。"""
    for pattern in TEXT_SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def redact_headers(headers: dict[str, Any]) -> dict[str, Any]:
    """header 黑名单：命中的 header 值替换为占位符，key 保留供 metadata 观测。

    非命中 header 的值仍过 ``scrub_text``（自定义 header 可能内嵌 token）。
    """
    out: dict[str, Any] = {}
    for key, value in headers.items():
        if key.lower() in BLOCKED_HEADERS or DEFAULT_KEY_PATTERN.search(key):
            out[key] = REDACTED
        elif isinstance(value, str):
            out[key] = scrub_text(value)
        else:
            out[key] = value
    return out


def redact_json(
    value: Any, *, extra_key_patterns: tuple[re.Pattern[str], ...] = ()
) -> Any:
    """递归脱敏 JSON 结构：命中 key pattern 的值整体替换，字符串值过文本 scrub。

    ``extra_key_patterns`` 是可配置的追加规则（"configurable
    JSON path rules"）——只能追加收紧，不能移除默认规则。
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            key_str = str(k)
            # token 计数字段豁免（见 USAGE_COUNT_KEYS）：`token` 规则会误伤
            # `input_tokens` 这类纯计数，抹掉后无法从 wire 补齐用量。
            # extra_key_patterns 是调用方配置的**收紧**规则，命中时不豁免。
            tightened = any(p.search(key_str) for p in extra_key_patterns)
            if _is_usage_count(key_str, v) and not tightened:
                out[k] = v
            elif _is_usage_container(key_str, v) and not tightened:
                # 只豁免"整块替换"，内部继续递归——容器里若有非白名单敏感键
                # 仍会被脱敏。
                out[k] = redact_json(v, extra_key_patterns=extra_key_patterns)
            elif DEFAULT_KEY_PATTERN.search(key_str) or any(
                p.search(key_str) for p in extra_key_patterns
            ):
                out[k] = REDACTED
            else:
                out[k] = redact_json(v, extra_key_patterns=extra_key_patterns)
        return out
    if isinstance(value, list):
        return [redact_json(v, extra_key_patterns=extra_key_patterns) for v in value]
    if isinstance(value, str):
        return scrub_text(value)
    return value


RedactionStatus = Literal["applied", "skipped", "failed"]


@dataclass
class RedactionResult:
    """``safe_redact_payload`` 的结果，与 ``EvidenceRedaction.status`` 对齐。

    - applied：payload 已脱敏，可落盘；
    - skipped：policy 不落 payload（off/metadata），payload 为 None；
    - failed：redactor 异常，payload 已丢弃，错误信息本身已 scrub。
    """

    payload: Any = None
    status: RedactionStatus = "applied"
    error: str | None = None
    flags: list[str] = field(default_factory=list)


def safe_redact_payload(
    payload: Any,
    *,
    policy: str,
    extra_key_patterns: tuple[re.Pattern[str], ...] = (),
) -> RedactionResult:
    """policy 感知的 payload 脱敏总入口。

    off/metadata 档不落 payload（metadata 只留 endpoint/size/timing/
    usage/hash），parsed/full 档返回脱敏后的结构。任何异常都收敛为
    metadata-only + ``redaction_failed``，异常消息先过 scrub 再保留。
    """
    if policy in ("off", "metadata"):
        return RedactionResult(payload=None, status="skipped")
    try:
        cleaned = redact_json(payload, extra_key_patterns=extra_key_patterns)
        return RedactionResult(payload=cleaned, status="applied")
    except Exception as exc:  # noqa: BLE001 —— 任何失败都不能让原文落盘
        return RedactionResult(
            payload=None,
            status="failed",
            error=scrub_text(f"{type(exc).__name__}: {exc}"),
            flags=["redaction_failed"],
        )
