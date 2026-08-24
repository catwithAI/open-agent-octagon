"""Wire reverse-proxy 的短期 capture token。

反向代理路由 ``POST /internal/wire-proxy/{attempt_id}/{provider}/{path}`` 不能
用 URL 里的 attempt_id 作为授权——那是 correlation 信息，不是凭证。本模块维护
一个进程内的 token→attempt 映射：

- attempt wire prepare 时 ``issue(attempt_id)`` 签发一个随机 token，注入子进程
  环境 ``OCTAGON_WIRE_CAPTURE_TOKEN``；
- 代理路由用 ``Authorization: Bearer <token>`` 携带，``resolve(token)`` 校验并
  返回它绑定的 attempt_id；
- token 与 URL attempt_id 必须一致，防止拿到 A 的 token 去代理 B 的流量；
- attempt finalize 时 ``revoke(attempt_id)``，之后该 token 立即失效。

token 只在进程内存在，不落 wire、不落盘。单进程可信：多进程部署
时换成签名/共享存储。
"""

from __future__ import annotations

import secrets
import threading


class _CaptureTokenRegistry:
    """token ↔ attempt_id 双向映射，进程级单例，线程安全。"""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._by_token: dict[str, str] = {}
        self._by_attempt: dict[str, str] = {}

    def issue(self, attempt_id: str) -> str:
        """为 attempt 签发（或复用）一个 capture token。幂等：同 attempt 多次
        prepare 复用同一 token，避免子进程环境与 registry 不一致。"""
        with self._guard:
            existing = self._by_attempt.get(attempt_id)
            if existing is not None:
                return existing
            token = secrets.token_urlsafe(32)
            self._by_token[token] = attempt_id
            self._by_attempt[attempt_id] = token
            return token

    def resolve(self, token: str | None) -> str | None:
        """返回 token 绑定的 attempt_id；无效 token 返回 None。"""
        if not token:
            return None
        with self._guard:
            return self._by_token.get(token)

    def revoke(self, attempt_id: str) -> None:
        """attempt finalize 后失效其 token。幂等。"""
        with self._guard:
            token = self._by_attempt.pop(attempt_id, None)
            if token is not None:
                self._by_token.pop(token, None)

    def reset(self) -> None:
        """测试 teardown 用。"""
        with self._guard:
            self._by_token.clear()
            self._by_attempt.clear()


_REGISTRY = _CaptureTokenRegistry()


def issue(attempt_id: str) -> str:
    return _REGISTRY.issue(attempt_id)


def resolve(token: str | None) -> str | None:
    return _REGISTRY.resolve(token)


def revoke(attempt_id: str) -> None:
    _REGISTRY.revoke(attempt_id)


def reset() -> None:
    _REGISTRY.reset()
