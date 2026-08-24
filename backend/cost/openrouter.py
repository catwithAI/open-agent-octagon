"""OpenRouter Management API 客户端。

只做四件事：创建 key、查询 key 用量、禁用 key、查 Activity 明细。

**与 `backend/api.py:_openrouter_models` 的区别**：那个用普通 key 查 `/models`
（公开目录，可缓存）。这个用 **Management Key** 管理其他 key，权限高得多——
Management Key 泄漏等于对方能创建无限额度的 key，因此本模块的脱敏纪律比别处严。

三条不可妥协的约束：

1. **不吞异常**。所有失败抛结构化 `CostAuditError` 子类，绝不 return None
   ——资金口径上「查不到」和「没花钱」必须能区分。
2. **create_key 不重试**。超时后无法区分「未创建」与「已创建但响应丢失」，
   重试会产生孤儿 key（靠 `expires_at` 兜底，但不该主动制造）。
3. **日志只出现 hash**。明文 key 用 SecretStr 包装，HTTP 错误体脱敏后再记录。

接口依据：
- https://openrouter.ai/docs/api/api-reference/api-keys/create-keys
- https://openrouter.ai/docs/api/api-reference/api-keys/get-key
- https://openrouter.ai/docs/api/api-reference/analytics/get-user-activity
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Iterable

import httpx

from .errors import (
    CostAuditError,
    ManagementAPIError,
    ManagementAuthError,
    ManagementRateLimited,
    ManagementTimeout,
    ManagementUnavailable,
)
from .models import ActivityRow, CreatedKey, KeySnapshot

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

#: 任何形如 sk-or-... 的串在进日志前都要被打掉。宁可误伤也不能漏。
_KEY_PATTERN = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")


def redact(text: str | None) -> str:
    """脱敏：把疑似 key 的串替换掉。用于日志与异常消息。"""
    if not text:
        return ""
    return _KEY_PATTERN.sub("sk-***REDACTED***", text)


def _f(value: Any) -> float | None:
    """宽松取 float。缺字段返回 None，**不返回 0**。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _i(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class OpenRouterManagementClient:
    """Management API 客户端。

    `management_key` 只在本对象内存活，不导出、不进子进程环境
    （**特意不走** `backend/config.py:208` 的 `os.environ.setdefault` 桥接）。
    """

    def __init__(
        self,
        management_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 15.0,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not management_key:
            raise ManagementAuthError("management key is empty")
        self._key = management_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max(1, max_retries)
        self._backoff_base = backoff_base
        self._client = client
        self._owns_client = client is None

    def __repr__(self) -> str:  # pragma: no cover - 防泄漏
        return f"OpenRouterManagementClient(base_url={self._base_url!r})"

    __str__ = __repr__

    # ---------- HTTP 底座 -------------------------------------------------

    async def _client_ctx(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "OpenRouterManagementClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _raise_for_status(self, resp: httpx.Response, *, what: str) -> None:
        """HTTP 状态 → 结构化错误。错误体脱敏后才进消息。"""
        if resp.status_code < 400:
            return
        body = redact(resp.text)[:500]
        detail = f"{what} failed: HTTP {resp.status_code} {body}"
        if resp.status_code in (401, 403):
            raise ManagementAuthError(detail, status_code=resp.status_code)
        if resp.status_code == 429:
            raise ManagementRateLimited(detail, status_code=resp.status_code)
        if resp.status_code >= 500:
            raise ManagementUnavailable(detail, status_code=resp.status_code)
        raise ManagementAPIError(detail, status_code=resp.status_code)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        what: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        retry: bool,
    ) -> dict[str, Any]:
        """发一次请求。`retry=False` 时**只发一次**，超时也不重发。

        retry 由调用方按操作语义决定，不由本方法猜——创建 key 的不可重试性
        是本模块的硬约束，不能因为「看起来是网络抖动」就放宽。
        """
        client = await self._client_ctx()
        url = f"{self._base_url}{path}"
        attempts = self._max_retries if retry else 1
        last: Exception | None = None

        for attempt in range(attempts):
            try:
                resp = await client.request(
                    method,
                    url,
                    headers=self._headers(),
                    json=json_body,
                    params=params,
                    timeout=self._timeout,
                )
                self._raise_for_status(resp, what=what)
                try:
                    payload = resp.json()
                except ValueError as exc:
                    raise ManagementAPIError(
                        f"{what}: response is not JSON", status_code=resp.status_code
                    ) from exc
                if not isinstance(payload, dict):
                    raise ManagementAPIError(f"{what}: unexpected response shape")
                return payload
            except httpx.TimeoutException as exc:
                last = ManagementTimeout(f"{what}: timeout after {self._timeout}s")
                last.__cause__ = exc
            except httpx.HTTPError as exc:
                last = ManagementUnavailable(f"{what}: {redact(str(exc))}")
                last.__cause__ = exc
            except CostAuditError as exc:
                if not getattr(exc, "retryable", False):
                    raise
                last = exc

            if attempt < attempts - 1:
                await asyncio.sleep(self._backoff_base * (2**attempt))

        assert last is not None
        logger.warning("management api %s failed: %s", what, redact(str(last)))
        raise last

    # ---------- 四个操作 --------------------------------------------------

    async def create_key(
        self,
        *,
        name: str,
        limit: float | None = None,
        limit_reset: str | None = None,
        expires_at: str | None = None,
    ) -> CreatedKey:
        """创建一把 run 专属 key。

        **不重试**：超时后无法区分「未创建」与「已创建但响应丢失」，
        重试会留下孤儿 key。超时一律按失败处理，由调用方走降级路径；
        真正的孤儿由 `expires_at` 兜底。
        """
        body: dict[str, Any] = {"name": name}
        if limit is not None:
            body["limit"] = limit
        if limit_reset is not None:
            body["limit_reset"] = limit_reset
        if expires_at is not None:
            body["expires_at"] = expires_at

        payload = await self._request(
            "POST", "/keys", what="create_key", json_body=body, retry=False
        )
        # OpenRouter 把明文 key 放在顶层 `key`，元数据放在 `data`。
        plaintext = payload.get("key")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        key_hash = data.get("hash") or payload.get("hash")
        if not isinstance(plaintext, str) or not plaintext:
            raise ManagementAPIError("create_key: response missing plaintext key")
        if not isinstance(key_hash, str) or not key_hash:
            raise ManagementAPIError("create_key: response missing key hash")

        logger.info("created run key name=%s hash=%s", name, key_hash)
        return CreatedKey(
            api_key=plaintext,
            api_key_hash=key_hash,
            name=str(data.get("name") or name),
            limit_usd=_f(data.get("limit")),
            expires_at=data.get("expires_at") or expires_at,
        )

    async def get_key(self, key_hash: str) -> KeySnapshot:
        """读某把 key 的累计用量。幂等，可重试。

        `usage` 是**累计**实扣，不在 UTC 零点清零——差值计算只认它。
        """
        payload = await self._request(
            "GET", f"/keys/{key_hash}", what="get_key", retry=True
        )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        usage = _f(data.get("usage"))
        if usage is None:
            raise ManagementAPIError(f"get_key: missing usage for hash={key_hash}")
        return KeySnapshot(
            api_key_hash=str(data.get("hash") or key_hash),
            usage=usage,
            limit_usd=_f(data.get("limit")),
            limit_remaining=_f(data.get("limit_remaining")),
            usage_daily=_f(data.get("usage_daily")),
            usage_weekly=_f(data.get("usage_weekly")),
            usage_monthly=_f(data.get("usage_monthly")),
            disabled=bool(data["disabled"]) if data.get("disabled") is not None else None,
            created_at=data.get("created_at"),
            expires_at=data.get("expires_at"),
        )

    async def disable_key(self, key_hash: str) -> None:
        """禁用 key。幂等，可重试。"""
        await self._request(
            "PATCH",
            f"/keys/{key_hash}",
            what="disable_key",
            json_body={"disabled": True},
            retry=True,
        )
        logger.info("disabled run key hash=%s", key_hash)

    async def get_activity(
        self, *, date: str, key_hash: str | None = None
    ) -> list[ActivityRow]:
        """查某个**已完成 UTC 日期**的明细。

        注意 Activity 非实时：run 刚结束时可能查不到当天数据，这不是错误，
        资金总额取自 `get_key`，明细日结后补。
        """
        params: dict[str, Any] = {"date": date}
        if key_hash:
            params["api_key_hash"] = key_hash
        payload = await self._request(
            "GET", "/activity", what="get_activity", params=params, retry=True
        )
        raw = payload.get("data")
        if not isinstance(raw, list):
            return []
        rows: list[ActivityRow] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            rows.append(
                ActivityRow(
                    date=str(item.get("date") or date),
                    model=item.get("model"),
                    model_permaslug=item.get("model_permaslug"),
                    provider=item.get("provider_name") or item.get("provider"),
                    endpoint=item.get("endpoint_id") or item.get("endpoint"),
                    usage=_f(item.get("usage")),
                    requests=_i(item.get("requests")),
                    prompt_tokens=_i(item.get("prompt_tokens")),
                    completion_tokens=_i(item.get("completion_tokens")),
                    reasoning_tokens=_i(item.get("reasoning_tokens")),
                )
            )
        return rows


def sum_activity_usage(rows: Iterable[ActivityRow]) -> float | None:
    """Activity 行的 usage 求和。全为 None 时返回 None，**不返回 0**。"""
    total: float | None = None
    for row in rows:
        if row.usage is None:
            continue
        total = row.usage if total is None else total + row.usage
    return total
