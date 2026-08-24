"""Octagon reverse HTTP capture proxy 路由。

    POST /internal/wire-proxy/{attempt_id}/{provider}/{path:path}
    （GET/PUT/... 同形，method 透传）

adapter 把 provider base URL 注入为
``http://127.0.0.1:8100/internal/wire-proxy/<attempt>/<provider>``，客户端对
LLM 的调用经此转发到真实 upstream，中途透明观测（见 sources/http_proxy.py）。

授权：用独立短期 **capture token**（``Authorization: Bearer <token>``
或 ``X-Octagon-Capture-Token``），**不复用 URL 里的 attempt_id 作为凭证**。token
由 wire prepare 签发、绑定 attempt，注入子进程环境。token 校验通过且其绑定的
attempt 与 URL attempt 一致才放行。

防 SSRF：upstream 只从 server-side ``settings.model_providers`` 查 ``provider``，
客户端不能提交任意 URL。
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse

from .. import runtime_state
from . import capture_token
from .sources import http_proxy

logger = logging.getLogger(__name__)

# proxy 到 upstream 的单请求时限（含流式）。LLM 长回复可能几十秒，给足冗余。
_UPSTREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=10.0)

_PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]

# 传输级请求正文硬上限：默认 100MB，远大于采集解析上限，只拦病态巨型
# 请求。可经环境变量覆盖。
def _int_env(name: str, default: int) -> int:
    import os
    try:
        v = int(os.environ.get(name, ""))
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


_MAX_PROXY_REQUEST_BYTES = _int_env("OCTAGON_WIRE_MAX_PROXY_REQUEST_BYTES", 100 * 1024 * 1024)


def _extract_capture_token(request: Request) -> str | None:
    header = request.headers.get("x-octagon-capture-token")
    if header:
        return header
    auth = request.headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def build_proxy_router() -> APIRouter:
    router = APIRouter(tags=["wire-proxy"])

    @router.api_route(
        "/internal/wire-proxy/{attempt_id}/{provider}/{path:path}",
        methods=_PROXY_METHODS,
    )
    async def wire_proxy(
        request: Request, attempt_id: str, provider: str, path: str
    ) -> Response:
        # 1) capture token 授权（不复用 URL attempt_id 作凭证）。
        token = _extract_capture_token(request)
        bound_attempt = capture_token.resolve(token)
        if bound_attempt is None:
            return Response(status_code=401, content=b"capture token required")
        if bound_attempt != attempt_id:
            # 拿 A 的 token 代理 B 的流量：拒绝。
            return Response(status_code=403, content=b"capture token / attempt mismatch")

        # 2) provider 只从 server-side config 查（防 SSRF）。
        state = runtime_state.get()
        settings = getattr(request.app.state, "settings", None)
        providers = getattr(settings, "model_providers", {}) or {}
        provider_cfg = providers.get(provider)
        if provider_cfg is None:
            return Response(status_code=404, content=b"unknown provider")

        # 3) 请求到达时快照 phase/policy（不在完成时读，避免跨 phase 误归属）。
        _enabled, phase, policy = http_proxy.snapshot_capture_state(
            state.data_path, attempt_id
        )

        # 请求正文硬上限：防病态大请求打爆内存。这是**传输级**上限
        # （远大于采集解析上限），仅拦截真正异常的巨型 body；正常 LLM 请求不受影响。
        # 边读边计数，超限立即 413 而非先全读进内存。用 bytearray.extend 避免
        # b"" += 的二次方复制开销。
        buf = bytearray()
        too_large = False
        async for chunk in request.stream():
            buf.extend(chunk)
            if len(buf) > _MAX_PROXY_REQUEST_BYTES:
                too_large = True
                break
        if too_large:
            return Response(status_code=413, content=b"request body too large")
        body = bytes(buf)
        inbound_headers = {k: v for k, v in request.headers.items()}

        client: httpx.AsyncClient = request.app.state.wire_proxy_client
        try:
            result = await http_proxy.forward(
                data_path=state.data_path,
                attempt_id=attempt_id,
                provider=provider,
                provider_cfg=provider_cfg,
                method=request.method,
                path=path,
                query_string=request.url.query,
                headers=inbound_headers,
                body=body,
                phase=phase,
                policy=policy,
                client=client,
                is_disconnected=request.is_disconnected,
            )
        except http_proxy.ProxyError as exc:
            return Response(status_code=exc.status_code, content=exc.reason.encode())

        # 用 raw_headers 保留重复头：dict(headers) 会把两个 Set-Cookie
        # 合并成一条，破坏语义。result.headers 已是逐条 (name, value) 列表，且含
        # upstream 原始 content-type——直接整体覆写 raw_headers 透传，不传 media_type
        # 以免 Starlette 再塞一条 content-type。
        response = StreamingResponse(result.body, status_code=result.status_code)
        response.raw_headers = [
            (k.encode("latin-1"), v.encode("latin-1")) for k, v in result.headers
        ]
        return response

    return router


async def open_proxy_client(app) -> None:
    """lifespan startup：建共享 httpx client（连接池复用）。"""
    app.state.wire_proxy_client = httpx.AsyncClient(timeout=_UPSTREAM_TIMEOUT)


async def close_proxy_client(app) -> None:
    client = getattr(app.state, "wire_proxy_client", None)
    if client is not None:
        await client.aclose()
