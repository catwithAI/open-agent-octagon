"""成本核算的结构化错误。

**为什么不复用通用 Exception**：资金口径上「失败」和「零成本」必须能区分。
`backend/experiments/scoring.py:96` 的 `except Exception: return None` 在成本
*估算*链路上是对的（成本算不出不该拖垮评分），但在这里不可接受——吞掉异常
后写 0 会让「上游查不到」冒充「这次 run 没花钱」。

每个错误都带 `error_code`，直接落 `run_cost_audits.error_code`，
供 settler 判断可重试性与 UI 展示。
"""

from __future__ import annotations


class CostAuditError(Exception):
    """成本核算异常基类。`error_code` 直接落库。"""

    error_code = "cost_audit_error"
    #: 可重试的错误（网络抖动、限流、上游 5xx）由 settler 自动重试；
    #: 不可重试的（认证失败、请求错误）需要人工介入。
    retryable = False

    def __init__(self, message: str, *, error_code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if error_code is not None:
            self.error_code = error_code


class ManagementAPIError(CostAuditError):
    """Management API 返回了非预期的响应。"""

    error_code = "management_api_error"

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
    ) -> None:
        super().__init__(message, error_code=error_code)
        self.status_code = status_code


class ManagementAuthError(ManagementAPIError):
    """Management Key 无效或权限不足。不可重试——重试只会继续 401。"""

    error_code = "management_auth_failed"
    retryable = False


class ManagementRateLimited(ManagementAPIError):
    """上游限流。可重试。"""

    error_code = "management_rate_limited"
    retryable = True


class ManagementUnavailable(ManagementAPIError):
    """上游 5xx 或连接失败。可重试。"""

    error_code = "management_unavailable"
    retryable = True


class ManagementTimeout(ManagementAPIError):
    """请求超时。

    读操作（get_key / get_activity）可重试；**创建 key 不可重试**——超时后
    无法区分「未创建」与「已创建但响应丢失」，重试会产生孤儿 key。
    该判断在调用方（`openrouter.create_key`）做，不在这里。
    """

    error_code = "management_timeout"
    retryable = True


class UsageRegression(CostAuditError):
    """上游累计 usage 出现倒退。

    累计 usage 单调不减是差值计算的前提。一旦倒退，说明上游数据异常或
    key hash 弄错了，此时**不能**截断为 0 或取绝对值——那会把数据错误
    伪装成一次便宜的 run。直接标 failed，让人看见。
    """

    error_code = "usage_regression"
    retryable = False


class KeyCreationFailed(CostAuditError):
    """创建 run key 失败。按配置降级为共享 key 上界或让 run 失败。"""

    error_code = "key_create_failed"
    retryable = False
