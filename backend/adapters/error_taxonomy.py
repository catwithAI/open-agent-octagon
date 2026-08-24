"""CLI adapter 的稳定错误分类与有界重试。

现场问题：

- Codex 在 Smoke · DeepSeek 成功，却在 Apple · DeepSeek 正式 Run 中以**泛化的
  ``cli_error``** 结束，使 RunGroup 只能记为 partial。现有结果不足以快速区分
  CLI 启动、配置、模型上游还是运行时错误。
- Mimo 在 Smoke · Luna 遇到 Azure upstream error，终态没有说明该错误是否可重试、
  重试了几次，或需要修改哪类配置。

这里给出**一份共用的分类**，让「配置错了」「上游抖了」「adapter 自己有 bug」
在 error_code 层就能分开，不必让下游去猜 error_message 的措辞：

============================  ==========  ==================================
error_code                    可重试      含义与处置
============================  ==========  ==================================
``configuration_error``       否          key/模型/参数错了，重试无意义，
                                          必须改配置。立即失败并给出可操作诊断。
``upstream_error``            是          上游（Azure/OpenRouter/网关）暂时性
                                          故障，有界重试。
``upstream_rate_limited``     是          限流，有界重试（退避更长）。
``upstream_unavailable``      是          上游明确不可用（502/503/504）。
``network_error``             是          连接层失败，有界重试。
``agent_timeout``             否          Agent/CLI 用尽 execution deadline，
                                          不是网络故障，不应重试。
``quota_exhausted``           否          额度耗尽，重试只会继续失败。
``cli_launch_error``          否          CLI 没装好/启动不了。
``adapter_error``             否          adapter 自身逻辑错误，需要修代码。
``cli_error``                 否          未能进一步分类的兜底（保持兼容）。
============================  ==========  ==================================

**只有安全的瞬时错误可重试**：重试必须在预算与 execution deadline 之内，
且不得在已经产生副作用之后重放（那属于副作用重放的范畴，不在这里处理）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ---- 稳定错误码 -----------------------------------------------------------

CONFIGURATION_ERROR = "configuration_error"
UPSTREAM_ERROR = "upstream_error"
UPSTREAM_RATE_LIMITED = "upstream_rate_limited"
UPSTREAM_UNAVAILABLE = "upstream_unavailable"
NETWORK_ERROR = "network_error"
AGENT_TIMEOUT = "agent_timeout"
QUOTA_EXHAUSTED = "quota_exhausted"
CLI_LAUNCH_ERROR = "cli_launch_error"
ADAPTER_ERROR = "adapter_error"
CLI_ERROR = "cli_error"

#: 可安全重试的错误码。其余一律立即失败——重试改变不了结论，只会烧钱和时间。
RETRYABLE_CODES = frozenset(
    {UPSTREAM_ERROR, UPSTREAM_RATE_LIMITED, UPSTREAM_UNAVAILABLE, NETWORK_ERROR}
)


@dataclass(frozen=True)
class ErrorClassification:
    """一次失败的稳定归因。``summary`` 已脱敏，可安全写进 DB 与 API。"""

    code: str
    retryable: bool
    summary: str
    phase: str = "agent_run"

    def as_refs(self, *, attempts: int = 0) -> dict[str, object]:
        """写进 ``external_refs`` 的诊断字段——终态可从 API 与界面读取。"""
        return {
            "error_class": self.code,
            "error_retryable": self.retryable,
            "error_phase": self.phase,
            "error_summary": self.summary,
            "error_retry_attempts": attempts,
        }


# ---- 匹配规则 -------------------------------------------------------------
#
# 顺序敏感：先判**不可重试**的确定性错误（配置/额度），再判可重试的瞬时错误。
# 反过来会把「key 写错了」当成「上游抖了」，白白重试三次还是同一个结论。

_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # --- 配置错误：重试无意义，必须改配置 ---
    (re.compile(r"\b(401|403)\b|unauthorized|forbidden", re.I), CONFIGURATION_ERROR),
    (re.compile(r"invalid[_ -]?api[_ -]?key|api[_ -]?key.*(missing|invalid|not set)", re.I),
     CONFIGURATION_ERROR),
    (re.compile(r"\b404\b|model.*(not found|does not exist)|no such model", re.I),
     CONFIGURATION_ERROR),
    (re.compile(r"unsupported (model|parameter|protocol)|invalid request", re.I),
     CONFIGURATION_ERROR),
    # --- 额度：可确定地耗尽，重试只会继续失败 ---
    (re.compile(r"quota|insufficient[_ ]?(credit|balance|funds)|billing", re.I),
     QUOTA_EXHAUSTED),
    # --- 限流：可重试，但要更长退避 ---
    (re.compile(r"\b429\b|rate[_ -]?limit|too many requests", re.I),
     UPSTREAM_RATE_LIMITED),
    # --- 上游明确不可用 ---
    (re.compile(r"\b(502|503|504)\b|bad gateway|service unavailable|gateway timeout", re.I),
     UPSTREAM_UNAVAILABLE),
    # --- 上游其它错误（含 Azure： 的 Smoke · Luna 现场）---
    (re.compile(r"\b500\b|internal server error|upstream|azure", re.I), UPSTREAM_ERROR),
    (re.compile(r"overloaded|capacity|server_error", re.I), UPSTREAM_ERROR),
    # --- Agent/CLI execution deadline ---
    # 各 CLI adapter 在本地 execution budget 耗尽时生成固定格式
    # "timeout after <seconds>s"。它说明 Agent 没有按时完成，不代表 wire、
    # provider 或网络失败；必须先于下面宽泛的网络 timeout 规则匹配。
    (re.compile(
        r"^\s*timeout after \d+(?:\.\d+)?s\s*$",
        re.I,
    ), AGENT_TIMEOUT),
    # --- 网络层 ---
    (re.compile(
        r"connection (reset|refused|error|aborted|closed)|econnreset|"
        r"timed? ?out|timeout|dns|getaddrinfo|ssl|tls|broken pipe",
        re.I,
    ), NETWORK_ERROR),
    # --- CLI 启动 ---
    (re.compile(
        r"command not found|no such file or directory|not installed|"
        r"permission denied|cannot execute|enoent",
        re.I,
    ), CLI_LAUNCH_ERROR),
)

# 摘要长度上限：够诊断即可，避免把整段上游 HTML 错误页塞进 DB。
_SUMMARY_MAX = 500

# 摘要里必须抹掉的敏感片段。分类依赖原文，但**落盘的摘要不得含凭据**。
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "sk-***"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{8,}"), r"\1 ***"),
    (re.compile(r"(?i)(api[_-]?key|token|password|secret)([\"'\s:=]+)[^\s\"',}]{8,}"),
     r"\1\2***"),
)


def redact_summary(text: str | None) -> str:
    """把原始错误压成一条可安全落盘的摘要。"""
    if not text:
        return ""
    summary = " ".join(str(text).split())
    for pattern, replacement in _REDACTIONS:
        summary = pattern.sub(replacement, summary)
    if len(summary) > _SUMMARY_MAX:
        summary = summary[:_SUMMARY_MAX] + "…"
    return summary


def classify(
    error_message: str | None,
    *,
    returncode: int | None = None,
    phase: str = "agent_run",
) -> ErrorClassification:
    """把一次 CLI/上游失败归到稳定错误码上。

    没有任何线索时回落 ``cli_error``（不可重试）——保持与既有行为兼容，
    也避免把「不知道为什么失败」当成「值得再试一次」。
    """
    text = str(error_message or "")
    for pattern, code in _RULES:
        if pattern.search(text):
            return ErrorClassification(
                code=code,
                retryable=code in RETRYABLE_CODES,
                summary=redact_summary(text),
                phase=phase,
            )
    if not text and returncode not in (None, 0):
        return ErrorClassification(
            code=CLI_ERROR,
            retryable=False,
            summary=f"CLI 以退出码 {returncode} 结束，无更多诊断信息",
            phase=phase,
        )
    return ErrorClassification(
        code=CLI_ERROR, retryable=False, summary=redact_summary(text), phase=phase
    )


def retry_delay_seconds(classification: ErrorClassification, attempt: int) -> float:
    """有界指数退避。``attempt`` 从 1 开始。

    限流退避更长——立刻重试只会再撞一次限流。
    """
    base = 5.0 if classification.code == UPSTREAM_RATE_LIMITED else 2.0
    return min(base * (2 ** max(0, attempt - 1)), 30.0)


def should_retry(
    classification: ErrorClassification,
    *,
    attempt: int,
    max_attempts: int,
    remaining_seconds: float | None,
) -> bool:
    """是否还应再试一次。

    三个闸全部通过才重试：错误本身安全可重试、没到次数上限、且
    **execution deadline 内还有余量**——重试绝不允许突破用户的耐心预算。
    """
    if not classification.retryable:
        return False
    if attempt >= max_attempts:
        return False
    if remaining_seconds is not None:
        # 留出足够跑完一次重试的余量，否则重试必然被 deadline 截断，
        # 白烧一次上游调用。
        if remaining_seconds <= retry_delay_seconds(classification, attempt) + 30:
            return False
    return True
