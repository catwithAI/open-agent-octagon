"""Octagon reverse HTTP capture proxy source。

adapter 把 provider base URL 注入为
``http://127.0.0.1:8100/internal/wire-proxy/<attempt>/<provider>``，客户端
（CC/Codex）对 LLM 的每次调用都经这里转发到真实 upstream。我们在中间**透明**
观测：request/response size、timing、SSE 逐 chunk、semantic hash，写进该 attempt
的 ``wire-sources/octagon-http.jsonl``。

核心约束：
- **模型完整性**：LLM 请求的 ``model`` 必须与 attempt 契约一致；不一致在
  upstream 之前 fail-closed。该边界独立于 capture policy；
- **透明**：通过完整性校验后，转发不改变客户端可见行为——upstream 的
  status/headers/body/SSE 分帧原样透传，capture 解析失败只降级 metadata；
- **主通信优先**：response chunk 一边转发给客户端、一边投递给一个 bounded queue
  由单 writer task 落 stream_chunk evidence；**队列满时 drop capture chunk 并
  计数**，绝不阻塞对客户端的转发；
- **防 SSRF**：upstream 只从 server-side provider config 查，客户端不能提交任意
  URL；
- **凭证隔离**：inbound（客户端→proxy）的 Authorization 不转发给 upstream，
  upstream auth 由 provider config 注入；
- **client 断开**：取消 upstream 请求并写 partial。

fail-open：capture 侧任何异常只记日志，绝不影响转发本身。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import uuid as _uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic as _monotonic

import httpx

from backend.model_providers import ModelProviderSection, resolve_api_key
from backend.wire import ids, paths, spool
from backend.wire import turn_correlation as turn_corr
from backend.wire.evidence import (
    CaptureEventEvidence,
    CaptureEventPayload,
    CorrelationHints,
    EvidenceProducer,
    EvidenceRawRef,
    EvidenceRedaction,
    EvidenceSource,
    EvidenceTime,
    HttpExchangeEvidence,
    HttpExchangePayload,
    RequestSummary,
    ResponseSummary,
    StreamChunkEvidence,
    StreamChunkPayload,
    TimingPayload,
    UsagePayload,
)
from backend.wire.hashing import raw_bytes_hash
from backend.wire.redaction import redact_json, safe_redact_payload, scrub_text
from backend.wire.writer import BlobWriter

logger = logging.getLogger(__name__)

SOURCE_KIND = "octagon-http"
PRODUCER_NAME = "octagon-http-proxy"
PARSER_VERSION = "octagon-http-v1"

# 进程 generation anchor：live source，混入 evidence ID 保证重启后
# hop seq 复用也不撞 ID。
_PROCESS_GENERATION = _uuid.uuid4().hex

UNKNOWN_PHASE = "unknown"
_VALID_PHASES = frozenset(
    {"attempt_setup", "agent_run", "verification", "artifact_collection", "attempt_cleanup"}
)

# bounded queue 上限：response chunk 投递给 capture writer 的缓冲深度。满了就 drop
# capture chunk（计数进 manifest），保证转发给客户端不被 I/O 慢的 spool 拖住。
_CHUNK_QUEUE_MAXSIZE = 256

# 正文大小上限：防大 tool schema/长上下文/并发 comparison 打爆内存。
# 这些只影响**采集**（解析/落 blob），不影响转发本身——转发始终逐 chunk 流式、
# 无上限。request 超限：仍转发，但不解析/不落 blob，标 truncated。response 超限：
# 停止缓存后续 chunk（已缓存的仍解析/落 blob），标 truncated。可经环境变量覆盖。
def _int_env(name: str, default: int) -> int:
    import os
    try:
        v = int(os.environ.get(name, ""))
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


_MAX_CAPTURE_REQUEST_BYTES = _int_env("OCTAGON_WIRE_MAX_REQUEST_BYTES", 8 * 1024 * 1024)
_MAX_CAPTURE_RESPONSE_BYTES = _int_env("OCTAGON_WIRE_MAX_RESPONSE_BYTES", 16 * 1024 * 1024)

# correlation probe 上限：所有 policy 下都扫这么多字节找 provider call ID
# （id 一般在首个 SSE 事件 / body 头部）。扫到即停，绝不落盘。小上限——只为拿 id，
# 不是缓存正文。
_CORRELATION_PROBE_MAX_BYTES = _int_env("OCTAGON_WIRE_CORRELATION_PROBE_BYTES", 64 * 1024)
# metadata policy 下不落 response 正文，但允许在内存中限量扫描 provider 返回的
# usage 数字。SSE 每行解析后立即丢弃；非流式 JSON 只保留这个上限内的响应。
_USAGE_PROBE_MAX_BYTES = _int_env("OCTAGON_WIRE_USAGE_PROBE_BYTES", 1024 * 1024)

# inbound 请求里绝不转发给 upstream 的 header（凭证/correlation/hop-by-hop）。
_INBOUND_STRIP_HEADERS = frozenset({
    "authorization", "x-api-key", "host", "content-length",
    "connection", "keep-alive", "proxy-authorization", "te", "trailer",
    "transfer-encoding", "upgrade",
    # capture token 与内部 correlation 头绝不能泄漏给第三方 upstream。
    "x-octagon-capture-token",
})
# 内部 header 前缀：任何以此开头的 header 都是 Octagon/eval 内部 correlation，
# 不转发给第三方 provider（防泄漏内部 attempt/session ID）。allowlist
# 式前缀剥离，杜绝将来新增 x-octagon-* / x-eval-* 头忘记加进上面集合的漏网。
_INBOUND_STRIP_PREFIXES = ("x-octagon-", "x-eval-")
# upstream response 里不回传给客户端的 hop-by-hop header（其余透传）。
# content-encoding/content-length 单独处理（见 _upstream_response_headers）：
# 我们用 StreamingResponse 走 chunked，content-length 恒剥；content-encoding 仅在
# upstream 遵守 identity（未压缩）时剥，否则保留让客户端能解压。
_UPSTREAM_STRIP_HEADERS = frozenset({
    "connection", "keep-alive", "transfer-encoding",
    "content-length", "te", "trailer", "upgrade",
})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def snapshot_capture_state(data_path: Path, attempt_id: str) -> tuple[bool, str, str]:
    """请求到达时快照 (capture_enabled, phase, policy)。

    与 env_capture 同源：从 lifecycle 原子写的 phase-state.json 读；
    文件不存在=capture 未启用（policy off / 无 prepare）；损坏=控制面故障，
    仍采集但 phase=unknown；显式 capture_enabled=False=policy off。
    policy 缺失时降 "metadata"（保守：只记 size/timing，不落 body）。
    """
    state_path = paths.phase_state_file(data_path, attempt_id)
    if not state_path.exists():
        return False, UNKNOWN_PHASE, "off"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True, UNKNOWN_PHASE, "metadata"
    if state.get("capture_enabled") is False:
        return False, UNKNOWN_PHASE, "off"
    policy = state.get("policy") or "metadata"
    if state.get("attempt_id") != attempt_id:
        return True, UNKNOWN_PHASE, policy
    phase = state.get("phase")
    return True, (phase if phase in _VALID_PHASES else UNKNOWN_PHASE), policy


# ---------- spool 生命周期 registry（复刻 env_capture 的 seal/drain）----------

class _Entry:
    __slots__ = ("writer", "lock", "sealed", "inflight", "cond", "dropped_hops")

    def __init__(self, writer: spool.SpoolWriter) -> None:
        self.writer = writer
        self.lock = asyncio.Lock()        # 串行 append（SpoolWriter 非线程/协程安全）
        self.sealed = False
        self.inflight = 0                 # 已 begin 未 end 的 hop 数（drain 用）
        self.cond = asyncio.Condition()
        self.dropped_hops = 0             # sealed 后被丢弃的 hop 数


class _SpoolRegistry:
    """(data_path, attempt_id) → attempt-scoped writer entry。进程级单例。

    与 env_capture 一致：active → sealed；close 时 seal → drain in-flight →
    close writer。异步版（代理跑在 event loop 上，用 asyncio 锁/Condition）。
    """

    def __init__(self) -> None:
        self._guard = asyncio.Lock()
        self._entries: dict[tuple[str, str], _Entry] = {}
        self._sealed: set[tuple[str, str]] = set()

    async def begin(self, data_path: Path, attempt_id: str) -> _Entry | None:
        key = (str(data_path), attempt_id)
        async with self._guard:
            if key in self._sealed:
                return None
            entry = self._entries.get(key)
            if entry is None:
                entry = _Entry(spool.SpoolWriter(
                    paths.source_spool_file(data_path, attempt_id, SOURCE_KIND),
                    expected_attempt_id=attempt_id,
                ))
                self._entries[key] = entry
        async with entry.cond:
            entry.inflight += 1
        return entry

    @staticmethod
    async def end(entry: _Entry) -> None:
        async with entry.cond:
            entry.inflight -= 1
            if entry.inflight <= 0:
                entry.cond.notify_all()

    async def close(
        self, data_path: Path, attempt_id: str, *, drain_timeout: float = 30.0
    ) -> None:
        # drain_timeout 给 30s：close 在 attempt_end 触发，正常情况下
        # agent 的 LLM 请求已结束；但尾部若有慢 SSE 长回复在途，5s 太短会误判
        # stuck 并 drop summary。30s 覆盖绝大多数收尾，仍有上限防某 hop 卡死。
        key = (str(data_path), attempt_id)
        async with self._guard:
            already = key in self._sealed
            self._sealed.add(key)
            entry = self._entries.pop(key, None)
        if entry is None or already:
            return
        # drain：等待已 begin 的 hop end（有上限，防某 hop 卡死）。
        stuck = 0
        try:
            async with entry.cond:
                await asyncio.wait_for(
                    entry.cond.wait_for(lambda: entry.inflight <= 0),
                    timeout=drain_timeout,
                )
        except asyncio.TimeoutError:
            stuck = max(entry.inflight, 0)
        async with entry.lock:
            entry.sealed = True
            if stuck:
                entry.dropped_hops += stuck
            if entry.dropped_hops:
                try:
                    _append_drop_event(entry, attempt_id, entry.dropped_hops)
                except Exception:
                    logger.exception("octagon-http drop 事件写入失败 attempt=%s", attempt_id)
            try:
                entry.writer.close()
            except Exception:
                logger.exception("octagon-http spool close 失败 attempt=%s", attempt_id)


_REGISTRY = _SpoolRegistry()


async def close_attempt(data_path: Path, attempt_id: str) -> None:
    """attempt finalize 时 seal→drain→close 该 attempt 的 proxy spool。"""
    try:
        await _REGISTRY.close(data_path, attempt_id)
    except Exception:
        logger.exception("octagon-http close_attempt 失败 attempt=%s", attempt_id)


def reset() -> None:
    """测试 teardown。"""
    global _REGISTRY
    _REGISTRY = _SpoolRegistry()
    # model-integrity hot-path caches share the proxy lifecycle.
    from backend.model_integrity import reset_caches

    reset_caches()


# ---------- 转发结果与 hop 上下文 -------------------------------------------

@dataclass
class _HopContext:
    """单次 hop（一次入站请求→一次 upstream 转发）的采集上下文。"""

    entry: _Entry
    data_path: Path
    attempt_id: str
    provider: str
    phase: str
    policy: str
    hop_anchor: str
    method: str
    scheme: str
    authority: str
    path: str
    request_bytes: int
    request_summary: RequestSummary | None
    request_body_ref: str | None
    started_at: str
    start_monotonic: float
    # 本轮的显式 turn（adapter 每轮请求前设的 X-Octagon-Turn-Id/-Index），
    # 从 inbound header 提取。写进 evidence extensions x-octagon.turn-id/-index，
    # finalizer 据此投影 canonical correlation。None=adapter 未设（走时间窗口兜底）。
    turn_id: str | None = None
    turn_index: int | None = None
    # 正文超采集上限标记：转发不受影响，仅表示采集侧未完整解析/落 blob。
    request_body_truncated: bool = False
    response_body_truncated: bool = False
    # 流式采集态：
    chunk_seq: int = 0
    dropped_chunks: int = 0
    # writer 已在 stream_chunk 上累计上报的 dropped 数：terminal chunk
    # 的 dropped_before 用「总 dropped - 已上报」的**增量**，与普通 chunk 语义一致，
    # 下游可无重复地累加得到总 dropped。
    dropped_reported: int = 0
    first_chunk_ms: float | None = None
    response_bytes: int = 0
    # provider call ID：从响应 header（request-id）或 body id 提取，
    # 写进 correlation_hints，finalizer 据此把 hop 关联到同 ID 的 native llm_call。
    provider_response_id: str | None = None
    # provider 响应里的结构化 usage；只保留五维数字，不保留正文。
    usage: UsagePayload | None = None


# ---------- evidence 构造 ---------------------------------------------------

def _http_exchange_evidence(
    hop: _HopContext,
    *,
    status_code: int | None,
    streamed: bool,
    partial: bool,
    finished_at: str,
    duration_ms: float,
    ttft_ms: float | None,
    response_summary: ResponseSummary | None,
    response_body_ref: str | None,
    redaction_status: str,
) -> HttpExchangeEvidence:
    extensions: dict[str, object] = {
        "x-octagon.http-hop-seq": hop.chunk_seq,
        "x-octagon.provider": hop.provider,
    }
    if hop.request_body_ref is not None:
        extensions["x-octagon.request-body-ref"] = hop.request_body_ref
    if response_body_ref is not None:
        extensions["x-octagon.response-body-ref"] = response_body_ref
    if hop.dropped_chunks:
        extensions["x-octagon.dropped-chunks"] = hop.dropped_chunks
    # 正文超采集上限：转发完整、采集未完整解析/落 blob。
    if hop.request_body_truncated:
        extensions["x-octagon.request-body-truncated"] = True
    if hop.response_body_truncated:
        extensions["x-octagon.response-body-truncated"] = True
    # 本轮显式 turn。finalizer 投影进 canonical correlation，
    # confidence=explicit。adapter 未设 turn header 时不写（走时间窗口 inferred）。
    if hop.turn_id:
        extensions[turn_corr.EXT_TURN_ID] = hop.turn_id
        if hop.turn_index is not None:
            extensions[turn_corr.EXT_TURN_INDEX] = hop.turn_index
    return HttpExchangeEvidence(
        evidence_id=ids.evidence_id(
            attempt_id=hop.attempt_id, source_kind=SOURCE_KIND,
            source_instance=hop.provider,
            raw_ref=f"octagon-http:{_PROCESS_GENERATION}:{hop.hop_anchor}",
            producer_id="hop",
        ),
        attempt_id=hop.attempt_id,
        phase=hop.phase,  # type: ignore[arg-type]
        source=EvidenceSource(kind=SOURCE_KIND, instance=hop.provider),
        producer=EvidenceProducer(name=PRODUCER_NAME, version=PARSER_VERSION),
        time=EvidenceTime(
            observed_at=finished_at, started_at=hop.started_at, finished_at=finished_at
        ),
        raw_ref=EvidenceRawRef(kind="octagon-http", file="octagon-http.jsonl", line=None),
        correlation_hints=CorrelationHints(
            model=hop.request_summary.model if hop.request_summary else None,
            # request_id=hop_anchor：finalizer 的 _hop_anchor_for 用
            # request_id 派生 hop_id；http_exchange 与 stream_chunk 携带同一
            # request_id → 派生出一致 hop_id，chunk 时序能挂到对应 hop。
            request_id=hop.hop_anchor,
            # provider call ID 桥接：Anthropic 的 message.id 既是反代看到的
            # provider-response-id，也是 CC normalizer 写进 events 的 producer_call_id
            # ——同一个 msg_xxx。反代**同时**写 provider_response_id + producer_call_id
            # （同值），correlate 的 union-find 才能把 hop 与 native llm_call（走
            # producer-call 空间）归并到同一 logical call；只写 provider-response 空间
            # 会落进不相交 namespace，永远 unmatched。无 id 时两者皆 None。
            provider_response_id=hop.provider_response_id,
            producer_call_id=hop.provider_response_id,
        ),
        capabilities={"http_transport_metadata": True},
        redaction=EvidenceRedaction(policy=hop.policy, status=redaction_status),  # type: ignore[arg-type]
        errors=[],
        extensions=extensions,
        payload=HttpExchangePayload(
            direction="outbound",
            method=hop.method, scheme=hop.scheme, authority=hop.authority, path=hop.path,
            status_code=status_code,
            request_bytes=hop.request_bytes, response_bytes=hop.response_bytes,
            streamed=streamed, partial=partial,
            timing=TimingPayload(
                started_at=hop.started_at, finished_at=finished_at,
                duration_ms=duration_ms, ttft_ms=ttft_ms,
            ),
            # 跨协议 semantic summary：由 parse.py 解析明文 body 得到，
            # 落进 canonical 供跨 agent/source hash 对比（不能只算不落）。
            request_summary=hop.request_summary,
            response_summary=response_summary,
            usage=hop.usage,
        ),
    )


def _stream_chunk_evidence(
    hop: _HopContext, *, seq: int, relative_ms: float, nbytes: int,
    content_hash: str | None, terminal: bool, dropped_before: int,
) -> StreamChunkEvidence:
    return StreamChunkEvidence(
        evidence_id=ids.evidence_id(
            attempt_id=hop.attempt_id, source_kind=SOURCE_KIND,
            source_instance=hop.provider,
            raw_ref=f"octagon-http:{_PROCESS_GENERATION}:{hop.hop_anchor}:chunk:{seq}",
            producer_id="chunk",
        ),
        attempt_id=hop.attempt_id,
        phase=hop.phase,  # type: ignore[arg-type]
        source=EvidenceSource(kind=SOURCE_KIND, instance=hop.provider),
        producer=EvidenceProducer(name=PRODUCER_NAME, version=PARSER_VERSION),
        time=EvidenceTime(observed_at=_now_iso(), started_at=None, finished_at=None),
        raw_ref=EvidenceRawRef(kind="octagon-http", file="octagon-http.jsonl", line=None),
        # request_id=hop_anchor：与 http_exchange 同一 anchor，finalizer
        # 据此派生一致 hop_id 把 chunk 挂到 hop。payload.hop_anchor 留 None——不能
        # 塞已派生的 hop_id，否则 finalizer 会对它再 hash 一次得到不同 hop_id。
        correlation_hints=CorrelationHints(request_id=hop.hop_anchor),
        capabilities={},
        redaction=EvidenceRedaction(policy=hop.policy, status="applied"),  # type: ignore[arg-type]
        errors=[],
        extensions={},
        payload=StreamChunkPayload(
            hop_anchor=None, sequence=seq, relative_ms=relative_ms,
            event_type=None, bytes=nbytes, content_hash=content_hash,
            terminal=terminal, dropped_before=dropped_before,
        ),
    )


def _append_drop_event(entry: _Entry, attempt_id: str, dropped: int) -> None:
    ev = CaptureEventEvidence(
        evidence_id=ids.evidence_id(
            attempt_id=attempt_id, source_kind=SOURCE_KIND,
            source_instance=SOURCE_KIND, raw_ref=f"octagon-http:{_PROCESS_GENERATION}:drop",
            producer_id="registry",
        ),
        attempt_id=attempt_id,
        phase=UNKNOWN_PHASE,
        source=EvidenceSource(kind=SOURCE_KIND, instance=SOURCE_KIND),
        producer=EvidenceProducer(name=PRODUCER_NAME, version=PARSER_VERSION),
        time=EvidenceTime(observed_at=_now_iso(), started_at=None, finished_at=None),
        raw_ref=None,
        correlation_hints=CorrelationHints(),
        capabilities={},
        redaction=EvidenceRedaction(policy="metadata", status="applied"),
        errors=[],
        extensions={},
        payload=CaptureEventPayload(
            event="drop", source_instance=SOURCE_KIND, status="partial",
            reason_code="hop_drained_on_close", message=None,
            counters={"hops_dropped": dropped}, effective_capabilities=None,
        ),
    )
    entry.writer.append(ev)


# ---------- 转发引擎 --------------------------------------------------------

class ProxyError(Exception):
    """转发前的可预期失败（SSRF 拒绝、provider 未知等），路由映射为 4xx/502。"""

    def __init__(self, status_code: int, reason: str) -> None:
        super().__init__(reason)
        self.status_code = status_code
        self.reason = reason


@dataclass
class ForwardResult:
    """转发结果：status/headers 立即可用，body 由 async 迭代器逐 chunk 产出。"""

    status_code: int
    headers: list[tuple[str, str]]
    body: "object"  # async iterator[bytes]
    media_type: str | None


class UpstreamPathError(ProxyError):
    """path 试图逃出 provider base（`..` 段）——拒绝，防 path 逃逸 SSRF。"""

    def __init__(self, path: str) -> None:
        super().__init__(400, f"illegal proxy path: {path!r}")


def _build_upstream_url(provider_cfg: ModelProviderSection, path: str) -> str:
    base = provider_cfg.base_url.rstrip("/")
    suffix = path.lstrip("/")
    # 防 path 逃逸：`..` 段会被 httpx 的 RFC3986 normalize 折叠，可能
    # 越出 provider 预期的 path 前缀打到同 host 的其它 endpoint。base_url 决定
    # host，host 无法被换；这里额外禁掉 `..` 段，确保 path 也钉在 base 下。
    if suffix:
        segments = suffix.split("/")
        if any(seg == ".." for seg in segments):
            raise UpstreamPathError(path)
    return f"{base}/{suffix}" if suffix else base


#: attempt_id → run_id 的进程级缓存。转发是热路径，每个请求查一次库不可接受；
#: attempt 与 run 的归属一经建立就不再变化，缓存无失效问题。
_ATTEMPT_RUN_CACHE: dict[str, str | None] = {}


def _run_id_for(attempt_id: str) -> str | None:
    """attempt → run_id，供反代查 run 专属 key。

    查不到（脱离 runtime_state 的单测、attempt 尚未落库）返回 None，
    调用方回落 provider 配置——**不抛错**：成本核算绝不能拖垮转发本身。
    """
    if attempt_id in _ATTEMPT_RUN_CACHE:
        return _ATTEMPT_RUN_CACHE[attempt_id]
    run_id: str | None = None
    try:
        from backend import runtime_state
        from backend.db import _open_sync

        with _open_sync(runtime_state.get().db_path) as conn:
            row = conn.execute(
                "SELECT run_id FROM attempts WHERE id=?", (attempt_id,)
            ).fetchone()
        run_id = row[0] if row else None
    except Exception:  # pragma: no cover - fail-open，转发优先
        return None
    _ATTEMPT_RUN_CACHE[attempt_id] = run_id
    return run_id


def _upstream_auth_headers(
    provider_cfg: ModelProviderSection,
    run_id: str | None = None,
    attempt_id: str | None = None,
) -> dict[str, str]:
    """provider config 注入 upstream auth（不复用 inbound 凭证）。

    **成本核算的关键交叉点**：agent 子进程被注入的是 run 专属 key，
    但请求经反代时 inbound Authorization 会被剥掉、由这里重新注入。若这里仍
    只读 provider 配置，wire 一开启，全部流量就打回共享 key——成本口径被静默
    污染且不报错（差值仍算得出来，只是算的是别人的钱）。因此这里必须与
    adapter 用同一把 key。
    """
    from ...cost.credential import resolve_agent_key

    key, _used_run_key = resolve_agent_key(
        run_id, resolve_api_key(provider_cfg), attempt_id, provider_cfg.base_url
    )
    if not key:
        return {}
    if provider_cfg.effective_auth_mode() == "api-key":
        return {"x-api-key": key}
    return {"Authorization": f"Bearer {key}"}


# 代理控制的保留 header：provider custom_headers 不得覆盖这些——
# 覆盖 auth 会替换 proxy 注入的 upstream 凭证、覆盖 accept-encoding 会破坏透传、
# 重新加 capture token/内部 correlation 会泄漏。
_RESERVED_UPSTREAM_HEADERS = frozenset({
    "authorization", "x-api-key", "accept-encoding",
    "content-length", "connection", "keep-alive", "transfer-encoding",
    "te", "trailer", "upgrade", "proxy-authorization", "host",
})


def _is_stripped_inbound_header(name: str) -> bool:
    """该 inbound header 是否不转发给 upstream（凭证/hop-by-hop/内部 correlation）。"""
    low = name.lower()
    if low in _INBOUND_STRIP_HEADERS:
        return True
    return any(low.startswith(prefix) for prefix in _INBOUND_STRIP_PREFIXES)


def _extract_turn(headers: dict[str, str]) -> tuple[str | None, int | None]:
    """从 inbound header 提取本轮 turn。

    ``X-Octagon-Turn-Id``（稳定 turn_id）+ 可选 ``X-Octagon-Turn-Index``（十进制
    整数）。这两个头属 x-octagon-* inbound-strip 前缀——只用于内部 correlation，
    绝不转发给 upstream。header dict 已由 forward 归一成小写 key。turn_id 缺失返回
    (None, None)；turn_index 非整数字符串时降 None（有 id 无 index 仍算显式）。
    """
    turn_id = headers.get(turn_corr.HEADER_TURN_ID)
    if not turn_id:
        return None, None
    raw_idx = headers.get(turn_corr.HEADER_TURN_INDEX)
    turn_index: int | None = None
    if raw_idx is not None:
        try:
            turn_index = int(raw_idx)
        except (ValueError, TypeError):
            turn_index = None
    return turn_id, turn_index


def _is_reserved_upstream_header(name: str) -> bool:
    """该 header 是否为代理控制的保留头，custom_headers 不得覆盖。"""
    low = name.lower()
    if low in _RESERVED_UPSTREAM_HEADERS:
        return True
    # 内部 correlation 前缀（capture token 亦以 x-octagon- 开头）绝不允许 custom 引入。
    return any(low.startswith(prefix) for prefix in _INBOUND_STRIP_PREFIXES)


def _forward_headers(
    inbound: dict[str, str],
    provider_cfg: ModelProviderSection,
    run_id: str | None = None,
    attempt_id: str | None = None,
) -> dict[str, str]:
    """构造发往 upstream 的 header：去掉 inbound 凭证/hop-by-hop，注入 provider auth
    与 provider 自定义头。"""
    out = {
        k: v for k, v in inbound.items()
        if not _is_stripped_inbound_header(k)
    }
    # 禁上游压缩：我们用 aiter_raw() 透传原始字节且会剥掉 upstream 的
    # content-encoding；若上游返回 gzip，客户端收到压缩字节却不知要解压。请求
    # identity 编码，upstream 返回明文，透传字节与（被剥掉的）编码头才自洽。
    out["accept-encoding"] = "identity"
    out.update(_upstream_auth_headers(provider_cfg, run_id, attempt_id))
    # provider custom_headers 先应用，但**不得**覆盖代理控制的 header：
    # 否则配置里写 Authorization/x-octagon-capture-token/accept-encoding 就能覆盖
    # proxy 注入的 upstream auth、重新引入 capture token、改回 gzip。custom_headers
    # 只允许追加非保留头（如 gateway 记账用的 x-user-id）。
    if provider_cfg.custom_headers:
        for line in provider_cfg.custom_headers.split("\n"):
            if ":" not in line:
                continue
            name, _, val = line.partition(":")
            name = name.strip()
            if _is_reserved_upstream_header(name):
                logger.warning(
                    "octagon-http 忽略 custom_headers 中的保留头 %r（不得覆盖代理控制头）",
                    name,
                )
                continue
            out[name] = val.strip()
    return out


def _is_llm_request(method: str, path: str) -> bool:
    """Whether this route carries a model-selecting inference request."""
    if method.upper() != "POST":
        return False
    normalized = path.strip("/").lower()
    return (
        normalized == "responses"
        or normalized.endswith("/responses")
        or normalized == "responses/compact"
        or normalized.endswith("/responses/compact")
        or normalized == "messages"
        or normalized.endswith("/messages")
        or normalized == "messages/count_tokens"
        or normalized.endswith("/messages/count_tokens")
        or normalized == "chat/completions"
        or normalized.endswith("/chat/completions")
    )


def _request_model(body: bytes) -> str | None:
    """Extract only the model field for the pre-forward integrity gate."""
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    model = payload.get("model")
    return model.strip() if isinstance(model, str) and model.strip() else None


def _enforce_model_integrity(
    *, attempt_id: str, provider: str, method: str, path: str, body: bytes
) -> None:
    """Reject an undeclared model before constructing/sending the upstream request."""
    if not _is_llm_request(method, path):
        return
    from backend.model_integrity import validate_proxy_model

    decision = validate_proxy_model(
        attempt_id,
        provider,
        _request_model(body),
    )
    if decision.allowed:
        return
    status_code = (
        503
        if decision.error_code == "model_integrity_unavailable"
        else 409
    )
    raise ProxyError(
        status_code,
        decision.reason or decision.error_code or "model integrity violation",
    )


async def forward(
    *,
    data_path: Path,
    attempt_id: str,
    provider: str,
    provider_cfg: ModelProviderSection,
    method: str,
    path: str,
    query_string: str,
    headers: dict[str, str],
    body: bytes,
    phase: str,
    policy: str,
    client: httpx.AsyncClient,
    is_disconnected=None,
) -> ForwardResult:
    """把一次入站请求透明转发到 upstream，并在中间观测。

    - 立即建立 upstream 流式连接，返回 status/headers；
    - body 迭代器逐 chunk 转发给客户端，同时投递给 bounded queue 采集；
    - client 断开（is_disconnected() 为 True）时取消 upstream 并写 partial。

    capture 侧全程 fail-open：spool/hash 异常不影响转发。模型完整性校验是
    独立的 fail-closed 比较有效性边界，不属于 capture 降级范围。
    """
    # 必须先于 URL/header/upstream request 构造，确保违规请求没有任何机会
    # 到达 OpenRouter（也不会产生费用）。
    _enforce_model_integrity(
        attempt_id=attempt_id,
        provider=provider,
        method=method,
        path=path,
        body=body,
    )
    upstream_url = _build_upstream_url(provider_cfg, path)
    if query_string:
        upstream_url = f"{upstream_url}?{query_string}"
    # 成本核算：反代重新注入的 upstream auth 必须与 adapter 注入
    # 子进程的是同一把 run key，否则 wire 一开启成本口径就静默污染。
    fwd_headers = _forward_headers(
        headers, provider_cfg, _run_id_for(attempt_id), attempt_id
    )

    # hop 采集上下文（capture 未启用时 entry=None，全程只转发不落盘）。
    entry = None
    if policy != "off":
        try:
            entry = await _REGISTRY.begin(data_path, attempt_id)
        except Exception:
            logger.exception("octagon-http begin 失败 attempt=%s", attempt_id)
            entry = None

    # 仅在拿到 entry（会写 evidence）时才解析/落 request blob：
    # begin 失败/sealed 时不落任何 blob，避免产生无 evidence 引用的孤儿敏感文件。
    request_summary = None
    req_body_ref = None
    redaction_status = "skipped"
    req_truncated = False
    if entry is not None:
        request_summary, req_body_ref, redaction_status, req_truncated = _summarize_request(
            provider_cfg, path, body, policy, data_path, attempt_id,
        )
    started_at = _now_iso()
    hop_anchor = f"{provider}:{_PROCESS_GENERATION}:{id(body)}:{started_at}"
    authority = _authority_of(provider_cfg.base_url)
    scheme = "https" if provider_cfg.base_url.startswith("https") else "http"
    # 本轮显式 turn（inbound header，小写归一后查找）。adapter 每轮请求前设，
    # 只作内部 correlation、不转发给 upstream（x-octagon-* 属 inbound-strip 前缀）。
    turn_id, turn_index = _extract_turn(
        {k.lower(): v for k, v in headers.items()}
    )

    hop = None
    if entry is not None:
        hop = _HopContext(
            entry=entry, data_path=data_path, attempt_id=attempt_id, provider=provider,
            phase=phase, policy=policy, hop_anchor=hop_anchor,
            method=method, scheme=scheme, authority=authority, path="/" + path.lstrip("/"),
            request_bytes=len(body),
            request_summary=request_summary, request_body_ref=req_body_ref,
            request_body_truncated=req_truncated,
            started_at=started_at, start_monotonic=_monotonic(),
            turn_id=turn_id, turn_index=turn_index,
        )

    # 建立 upstream 流式连接。连接/建立阶段的失败：写一条 partial hop 并抛
    # ProxyError 让路由回 502（透传上游不可达）。
    req = client.build_request(method, upstream_url, headers=fwd_headers, content=body)
    try:
        resp = await client.send(req, stream=True)
    except httpx.HTTPError as exc:
        await _finish_hop(
            hop, status_code=None, streamed=False, partial=True,
            ttft_ms=None, response_summary=None, response_body_ref=None,
            redaction_status=redaction_status,
        )
        if entry is not None:
            await _REGISTRY.end(entry)
        raise ProxyError(502, scrub_text(f"upstream unreachable: {exc}")) from None

    out_headers = _upstream_response_headers(resp)
    media_type = resp.headers.get("content-type")

    body_iter = _stream_body(
        resp=resp, hop=hop, entry=entry, redaction_status=redaction_status,
        is_disconnected=is_disconnected, provider_cfg=provider_cfg, policy=policy,
    )
    return ForwardResult(
        status_code=resp.status_code, headers=out_headers,
        body=body_iter, media_type=media_type,
    )


def _upstream_response_headers(resp: httpx.Response) -> list[tuple[str, str]]:
    """构造回传客户端的 header：剥 hop-by-hop + content-length（走 chunked）；
    content-encoding 仅在 upstream 未压缩（identity/缺失）时剥——若上游无视我们的
    Accept-Encoding: identity 仍压缩，则**保留** content-encoding，让客户端能正确
    解压（透传压缩字节就必须透传编码头）。"""
    out: list[tuple[str, str]] = []
    # multi_items() 保留重复头：items() 会把两个 Set-Cookie 合并成
    # "a=1, b=2"，对 Set-Cookie 是错误语义。逐条透传每个重复头。
    for k, v in resp.headers.multi_items():
        low = k.lower()
        if low in _UPSTREAM_STRIP_HEADERS:
            continue
        if low == "content-encoding":
            if v.strip().lower() in ("", "identity"):
                continue  # 未压缩：剥掉冗余头
            # 上游确实压缩了：保留 content-encoding，透传的压缩字节与之自洽。
        out.append((k, v))
    return out


def _parse_provider_id(raw: bytes) -> "str | None":
    """从（可能不完整的）response 前缀提取 provider call ID。fail-open。"""
    try:
        from backend.wire.sources import parse as _parse
        return _parse.extract_provider_response_id(raw)
    except Exception:
        return None


def _usage_int(*values: object) -> int | None:
    """取第一个非负整数 token 值；bool 不算整数。"""
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and value >= 0:
            return int(value)
    return None


def _usage_from_obj(obj: object) -> UsagePayload | None:
    """从 OpenAI/Anthropic 兼容响应对象提取五维 usage。

    provider 网关的字段命名并不完全一致，兼容 OpenAI Chat/Responses 的
    nested details，以及 Anthropic 的 cache_*_input_tokens。没有任何可用维度时
    返回 None，绝不把缺失字段伪造为 0。
    """
    if not isinstance(obj, dict):
        return None
    usage = obj.get("usage")
    if not isinstance(usage, dict):
        for container_name in ("response", "message"):
            container = obj.get(container_name)
            if isinstance(container, dict) and isinstance(container.get("usage"), dict):
                usage = container["usage"]
                break
    if not isinstance(usage, dict):
        return None

    prompt_details = usage.get("prompt_tokens_details")
    if not isinstance(prompt_details, dict):
        prompt_details = usage.get("input_tokens_details")
    prompt_details = prompt_details if isinstance(prompt_details, dict) else {}
    completion_details = usage.get("completion_tokens_details")
    if not isinstance(completion_details, dict):
        completion_details = usage.get("output_tokens_details")
    completion_details = completion_details if isinstance(completion_details, dict) else {}

    values = {
        "input_tokens": _usage_int(
            usage.get("prompt_tokens"), usage.get("input_tokens"),
        ),
        "output_tokens": _usage_int(
            usage.get("completion_tokens"), usage.get("output_tokens"),
        ),
        "cache_read_tokens": _usage_int(
            usage.get("cache_read_tokens"),
            usage.get("cache_read_input_tokens"),
            prompt_details.get("cached_tokens"),
        ),
        "cache_write_tokens": _usage_int(
            usage.get("cache_write_tokens"),
            usage.get("cache_creation_input_tokens"),
            prompt_details.get("cache_write_tokens"),
        ),
        "reasoning_tokens": _usage_int(
            usage.get("reasoning_tokens"),
            completion_details.get("reasoning_tokens"),
        ),
    }
    if all(value is None for value in values.values()):
        return None
    return UsagePayload(**values, estimated=False)


class _UsageProbe:
    """增量扫描 SSE/JSON usage，只留数字和有限未完成缓冲。

    SSE 行解析后立即从缓冲移除，因此长输出不会累计正文；非流式 JSON 最多保留
    ``_USAGE_PROBE_MAX_BYTES``。解析失败仅影响统计，绝不影响透明转发。
    """

    def __init__(self) -> None:
        self._line_buf = bytearray()
        self._raw_buf = bytearray()
        self._raw_truncated = False
        self.usage: UsagePayload | None = None

    def feed(self, chunk: bytes) -> None:
        if not self._raw_truncated:
            if len(self._raw_buf) + len(chunk) <= _USAGE_PROBE_MAX_BYTES:
                self._raw_buf.extend(chunk)
            else:
                self._raw_buf = bytearray()
                self._raw_truncated = True

        self._line_buf.extend(chunk)
        while True:
            idx = self._line_buf.find(b"\n")
            if idx < 0:
                if len(self._line_buf) > _USAGE_PROBE_MAX_BYTES:
                    self._line_buf = bytearray()
                return
            line = bytes(self._line_buf[:idx]).rstrip(b"\r")
            del self._line_buf[:idx + 1]
            self._parse_sse_line(line)

    def finish(self) -> UsagePayload | None:
        if self._line_buf:
            self._parse_sse_line(bytes(self._line_buf).rstrip(b"\r"))
            self._line_buf = bytearray()
        if self.usage is not None or self._raw_truncated or not self._raw_buf:
            return self.usage
        try:
            parsed = json.loads(bytes(self._raw_buf))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self.usage
        return _usage_from_obj(parsed) or self.usage

    def _parse_sse_line(self, line: bytes) -> None:
        if not line.startswith(b"data:"):
            return
        payload = line[5:].strip()
        if not payload or payload == b"[DONE]":
            return
        try:
            parsed = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        usage = _usage_from_obj(parsed)
        if usage is not None:
            self.usage = usage


def _authority_of(base_url: str) -> str:
    rest = base_url.split("://", 1)[-1]
    return rest.split("/", 1)[0]


async def _stream_body(
    *, resp: httpx.Response, hop: "_HopContext | None", entry: "_Entry | None",
    redaction_status: str, is_disconnected, provider_cfg: ModelProviderSection,
    policy: str,
):
    """逐 chunk 转发 upstream body 给客户端，同时投递给 bounded queue 采集。

    **主通信优先**：chunk 先 yield 给客户端；采集用 put_nowait，队列满则 drop 计数，
    绝不 await 队列（不让慢 spool 阻塞转发）。writer task 单独 drain 队列落
    stream_chunk。client 断开时取消 upstream 并写 partial。
    """
    # 采集侧：bounded queue + 单 writer task。None 表示不采集。
    queue: "asyncio.Queue | None" = None
    writer_task = None
    if hop is not None and entry is not None:
        queue = asyncio.Queue(maxsize=_CHUNK_QUEUE_MAXSIZE)
        writer_task = asyncio.create_task(_chunk_writer(queue, hop, entry))

    partial = False
    ttft_ms: float | None = None
    response_chunks: list[bytes] = []  # 仅 parsed/full 档缓存（供 body blob）
    buffered_bytes = 0
    stop_buffering = False  # 超限后 latch，保证缓存的是连续前缀
    want_body = policy in ("parsed", "full")
    # correlation probe：**所有 policy** 下都在内存扫前几个 SSE 事件提
    # provider call ID（关联 native call），提到即停。限量、绝不落盘——metadata 下
    # 也能关联 hop↔call，但不保存任何正文。与 body blob 缓存解耦。
    probe_buf = bytearray()
    probe_done = False
    usage_probe = _UsageProbe() if hop is not None else None
    try:
        async for chunk in resp.aiter_raw():
            if is_disconnected is not None:
                try:
                    if await is_disconnected():
                        partial = True
                        break
                except Exception:
                    pass
            # 1) 主通信：先转发给客户端。
            yield chunk
            # 2) 采集：非阻塞投递；队列满 drop 计数（保主通信）。
            if hop is not None:
                now_ms = (_monotonic() - hop.start_monotonic) * 1000.0
                if ttft_ms is None:
                    ttft_ms = now_ms
                hop.response_bytes += len(chunk)
                # usage probe 与 body capture policy 解耦：只保留结构化五维数字，
                # metadata 下也能统计 kimi-code，同时不持久化任何响应正文。
                if usage_probe is not None:
                    usage_probe.feed(chunk)
                # correlation probe：累积早期字节（有硬上限），扫到 id 立即停并清空
                # probe buffer（不保存正文）。id 通常在首个 SSE 事件 / 非流式 body 头部。
                if not probe_done and hop.provider_response_id is None:
                    probe_buf.extend(chunk)
                    rid = _parse_provider_id(bytes(probe_buf))
                    if rid is not None:
                        hop.provider_response_id = rid
                        probe_done = True
                        probe_buf = bytearray()  # 提到即弃，不落盘
                    elif len(probe_buf) >= _CORRELATION_PROBE_MAX_BYTES:
                        probe_done = True  # 前几个事件都没 id：放弃 probe（诚实 unmatched）
                        probe_buf = bytearray()
                # response body 缓存上限（#6）：一旦某 chunk 会超限，**latch
                # 停止缓存**并标 truncated——绝不再缓存后续 chunk，否则形成「前缀+跳过+
                # 后缀」的非连续错误正文。缓存的始终是**连续前缀**。转发不受影响。
                if want_body and not stop_buffering:
                    if buffered_bytes + len(chunk) > _MAX_CAPTURE_RESPONSE_BYTES:
                        hop.response_body_truncated = True
                        stop_buffering = True  # latch：此后一律不缓存
                    else:
                        response_chunks.append(chunk)
                        buffered_bytes += len(chunk)
                if queue is not None:
                    try:
                        queue.put_nowait((chunk, now_ms))
                    except asyncio.QueueFull:
                        hop.dropped_chunks += 1
    except (httpx.HTTPError, asyncio.CancelledError):
        partial = True
    finally:
        try:
            await resp.aclose()
        except Exception:
            pass
        # 关闭采集队列并等 writer drain 完。哨兵用 put_nowait：队列满时（writer
        # 落后）不能阻塞转发侧 cleanup——直接给 writer 有限时间自行 drain，超时则
        # cancel（已落盘的 chunk 保留）。
        if queue is not None and writer_task is not None:
            try:
                queue.put_nowait(None)  # 哨兵：writer drain 到它后退出
            except asyncio.QueueFull:
                pass  # 队列满：writer 会先 drain 掉积压，靠 timeout/cancel 收尾
            try:
                await asyncio.wait_for(writer_task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                writer_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await writer_task
            except Exception:
                writer_task.cancel()
        # 完成 hop：写 http_exchange summary。
        if hop is not None:
            if usage_probe is not None:
                hop.usage = usage_probe.finish()
            # provider call ID 主路径是转发时的 correlation probe（所有 policy 均生效）。
            # 这里对 parsed/full 已缓存 body 做一次兜底提取——probe 因 id 靠后
            # 超出 probe 上限而漏掉时，用完整缓存 body 补上。
            if hop.provider_response_id is None and want_body and response_chunks:
                hop.provider_response_id = _parse_provider_id(b"".join(response_chunks))
            resp_summary, resp_body_ref, resp_redaction = _summarize_response(
                response_chunks if want_body else None, policy, hop,
            )
            # 合并 request/response 脱敏状态：任一 failed → evidence
            # redaction.status=failed，不能因 request applied 就掩盖 response 未安全落盘。
            merged_redaction = _merge_redaction_status(redaction_status, resp_redaction)
            await _finish_hop(
                hop, status_code=resp.status_code, streamed=True, partial=partial,
                ttft_ms=ttft_ms, response_summary=resp_summary,
                response_body_ref=resp_body_ref, redaction_status=merged_redaction,
            )
        if entry is not None:
            await _REGISTRY.end(entry)


async def _chunk_writer(queue: "asyncio.Queue", hop: "_HopContext", entry: "_Entry") -> None:
    """单 writer task：从 bounded queue drain chunk，落 stream_chunk evidence。

    收到 None 哨兵后退出（terminal chunk 由主协程 _finish_hop 补写）。dropped_before
    记录该 chunk 之前因队列满新丢弃的**增量**（统一增量语义，下游累加
    即得总 dropped）。已上报增量累计到 hop.dropped_reported 供 terminal 续算。"""
    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            chunk, rel_ms = item
            seq = hop.chunk_seq
            hop.chunk_seq += 1
            # 队列满丢弃的数量在 hop.dropped_chunks 上累计；记录本 chunk 前的增量。
            dropped_before = hop.dropped_chunks - hop.dropped_reported
            hop.dropped_reported = hop.dropped_chunks
            try:
                ev = _stream_chunk_evidence(
                    hop, seq=seq, relative_ms=rel_ms, nbytes=len(chunk),
                    content_hash=raw_bytes_hash(chunk), terminal=False,
                    dropped_before=dropped_before,
                )
                async with entry.lock:
                    if not entry.sealed:
                        entry.writer.append(ev)
            except Exception:
                logger.exception("octagon-http stream_chunk 写入失败")
    except asyncio.CancelledError:
        pass


def _summarize_request(
    provider_cfg: ModelProviderSection, path: str, body: bytes, policy: str,
    data_path: Path, attempt_id: str,
) -> tuple["RequestSummary | None", "str | None", str, bool]:
    """从 request body 提取 semantic summary + 按 policy 落 body blob。

    返回 (request_summary, request_body_ref, redaction_status, truncated)。
    request 超过 _MAX_CAPTURE_REQUEST_BYTES时：**仍照常转发**（转发在
    route 层，不受此影响），但不解析、不落 blob，truncated=True 让 evidence 标记。
    """
    from backend.wire.sources import parse as _parse

    if len(body) > _MAX_CAPTURE_REQUEST_BYTES:
        logger.warning(
            "octagon-http request body %d 超采集上限 %d，跳过解析/blob（转发不受影响）",
            len(body), _MAX_CAPTURE_REQUEST_BYTES,
        )
        return None, None, "skipped", True

    summary = None
    redaction_status = "skipped"
    try:
        summary = _parse.parse_request(provider_cfg.kind, body)
    except Exception:
        logger.exception("octagon-http request 解析失败（透明转发继续）")
        summary = None

    body_ref = None
    if policy in ("parsed", "full"):
        body_ref, redaction_status = _maybe_write_body_blob(
            body, policy, data_path, attempt_id,
        )
    return summary, body_ref, redaction_status, False


def _summarize_response(
    chunks: "list[bytes] | None", policy: str, hop: "_HopContext",
) -> tuple["ResponseSummary | None", "str | None", str]:
    """从缓存的 response chunk 提取 summary + 落 body blob（parsed/full）。

    返回 (response_summary, response_body_ref, response_redaction_status)——
    response 的脱敏状态不能被丢弃：response blob 脱敏失败时 evidence
    的 redaction.status 必须反映 failed，不能沿用 request 侧的 applied。
    """
    if chunks is None:
        return None, None, "skipped"
    raw = b"".join(chunks)
    from backend.wire.sources import parse as _parse

    summary = None
    try:
        summary = _parse.parse_response(hop.provider, raw)
    except Exception:
        logger.exception("octagon-http response 解析失败")
        summary = None
    body_ref, status = _maybe_write_body_blob(
        raw, policy, hop.data_path, hop.attempt_id,
    )
    return summary, body_ref, status


def _merge_redaction_status(req_status: str, resp_status: str) -> str:
    """合并 request/response 两侧脱敏状态：failed 优先（安全侧），
    其次 applied（至少一侧真的脱敏落盘了），否则 skipped。"""
    statuses = {req_status, resp_status}
    if "failed" in statuses:
        return "failed"
    if "applied" in statuses:
        return "applied"
    return "skipped"


# SSE data 里唯一容忍的非 JSON 值：流终止 sentinel。其余非 JSON data 一律 fail-closed。
_SSE_SENTINELS = frozenset({"[DONE]"})

# event 名合法字符（安全 token）：字母数字 . _ -。retry 必须纯数字。
_SSE_EVENT_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_SSE_RETRY_RE = re.compile(r"^[0-9]+$")


def _sanitize_sse_meta_line(line: str) -> "str | None":
    """SSE 非 data 字段（event/id/retry/comment）的结构化安全处理。

    这些字段可能被塞入 JSON/secret（如 ``id: {"cookie":"..."}``），文本 scrub 拦不住。
    按 SSE 语义只保留各字段的合法形态，返回安全行；无保留价值/不合法则返回 None（丢弃）：
    - comment（``:`` 开头）：无协议语义，直接丢弃；
    - ``retry:``：必须纯数字毫秒，否则丢弃；
    - ``event:``：事件名 token 白名单，否则丢弃；
    - ``id:``：last-event-id 不可信且可能带内容，只保留其 sha256 短 hash（保序不泄漏）。
    """
    if line.startswith(":"):
        return None  # comment
    field, _, value = line.partition(":")
    value = value.lstrip(" ")
    if field == "retry":
        return f"retry: {value}" if _SSE_RETRY_RE.match(value) else None
    if field == "event":
        return f"event: {value}" if _SSE_EVENT_NAME_RE.match(value) else None
    if field == "id":
        if not value:
            return "id: "
        digest = raw_bytes_hash(value.encode("utf-8"))[:16]
        return f"id: sha256:{digest}"
    return None  # 未知字段（正则已挡住，但保守丢弃）


def _redact_sse_text(raw: bytes) -> "str | None":
    """SSE 事件流的**结构化**脱敏（fail-closed）。

    按 SSE 规范解析：空行分隔 event，event 内多个 ``data:`` 字段按换行拼接成完整
    payload（多行 SSE event 支持）。每个 event 的 data payload 必须是可解析 JSON
    （或 ``[DONE]`` sentinel），才能字段级脱敏（redact_json：api_key/cookie/token
    等 key 整体 [REDACTED]）。**任何 event 的 data 无法结构化验证（损坏 JSON、
    多值、纯文本）→ 返回 None，整个 body fail-closed 不落盘**——不再回退到不足以
    保证安全的整行正则。非 UTF-8 同样 fail-closed。

    只有当整个 body 是「一串可结构化验证的 SSE event」时才返回脱敏文本；否则 None。
    """
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None

    # 归一换行，按空行切 event（SSE：event 以一个或多个空行分隔）。
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    raw_events = [e for e in re.split(r"\n\s*\n", normalized) if e.strip()]
    if not raw_events:
        return None  # 空 body 无需落 blob

    # 合法 SSE 字段行前缀（规范）：data/event/id/retry/以 : 开头的 comment。
    _sse_field_re = re.compile(r"^(data|event|id|retry):|^:")
    out_events: list[str] = []
    saw_data = False
    for raw_event in raw_events:
        data_parts: list[str] = []
        other_lines: list[str] = []
        for line in raw_event.split("\n"):
            if not line:
                continue
            if not _sse_field_re.match(line):
                # 出现非 SSE 字段行 → 这不是干净的 SSE 流，无法结构化验证 →
                # 整个 body fail-closed（不回退整行正则）。
                return None
            if line.startswith("data:"):
                data_parts.append(line[len("data:"):].lstrip(" "))
            else:
                # 非 data 字段（event/id/retry/comment）按类型结构化处理：
                # scrub_text 对 id: 里塞的 JSON 无效，会漏 secret——改为按 SSE 语义
                # 只保留各字段合法形态，其余丢弃/hash。
                safe = _sanitize_sse_meta_line(line)
                if safe is not None:
                    other_lines.append(safe)
        if not data_parts:
            out_events.append("\n".join(other_lines))
            continue
        saw_data = True
        payload = "\n".join(data_parts)  # 多行 data 拼接成完整 payload（SSE 规范）
        if payload.strip() in _SSE_SENTINELS:
            redacted_data = payload.strip()
        else:
            try:
                obj = json.loads(payload)
            except (json.JSONDecodeError, ValueError):
                # 损坏 / 非 JSON data：无法结构化验证 → 整个 body fail-closed。
                return None
            redacted_data = json.dumps(redact_json(obj), ensure_ascii=False)
        event_lines = list(other_lines)
        event_lines.append(f"data: {redacted_data}")
        out_events.append("\n".join(event_lines))
    if not saw_data:
        # 没有任何 data event（纯 comment/心跳）：无正文可采，fail-closed 不落盘。
        return None
    return "\n\n".join(out_events) + "\n\n"


def _maybe_write_body_blob(
    raw: bytes, policy: str, data_path: Path, attempt_id: str,
) -> tuple["str | None", str]:
    """policy=parsed/full 时把（已脱敏的）body 落 content-addressed blob。

    返回 (blob_ref, redaction_status)。脱敏失败时不落 blob（fail-closed，
    不泄漏未脱敏内容），redaction_status="failed"。
    """
    if policy not in ("parsed", "full") or not raw:
        return None, "skipped"
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        # 非 JSON body（如 SSE 拼接）：full 档也**必须脱敏**再落盘（
        # full ≠ 不脱敏——SSE data: {...} 里可能有 api_key/cookie 等**字段级**
        # secret，只对整行 scrub_text 不解析 JSON 会漏）。走 SSE 感知的结构化脱敏；
        # 无法安全处理则 fail-closed 不落盘。parsed 档跳过。
        if policy != "full":
            return None, "skipped"
        scrubbed = _redact_sse_text(raw)
        if scrubbed is None:
            return None, "failed"
        try:
            ref = BlobWriter(data_path, attempt_id).write_bytes(scrubbed.encode("utf-8"))
            return ref.ref, "applied"
        except Exception:
            logger.exception("octagon-http body blob 写入失败")
            return None, "failed"
    result = safe_redact_payload(parsed, policy=policy)
    if result.status == "failed":
        return None, "failed"
    try:
        ref = BlobWriter(data_path, attempt_id).write_json(result.payload)
        return ref.ref, result.status
    except Exception:
        logger.exception("octagon-http body blob 写入失败")
        return None, "failed"


async def _finish_hop(
    hop: "_HopContext | None", *, status_code: int | None, streamed: bool,
    partial: bool, ttft_ms: float | None, response_summary: "ResponseSummary | None",
    response_body_ref: "str | None", redaction_status: str,
) -> None:
    """写 hop 的 http_exchange summary evidence。fail-open。"""
    if hop is None:
        return
    finished_at = _now_iso()
    duration_ms = (_monotonic() - hop.start_monotonic) * 1000.0
    # terminal stream_chunk（标记流结束）。dropped_before 用增量：
    # writer 退出后队列里可能还有因满被丢的 chunk 未被任何 stream_chunk 上报，
    # 这里补上「总 dropped - 已上报」的余量，下游累加所有 chunk 即得总 dropped。
    try:
        if streamed and hop.entry is not None:
            remaining_dropped = hop.dropped_chunks - hop.dropped_reported
            hop.dropped_reported = hop.dropped_chunks
            term = _stream_chunk_evidence(
                hop, seq=hop.chunk_seq, relative_ms=duration_ms, nbytes=0,
                content_hash=None, terminal=True,
                dropped_before=remaining_dropped,
            )
            async with hop.entry.lock:
                if not hop.entry.sealed:
                    hop.entry.writer.append(term)
            hop.chunk_seq += 1
    except Exception:
        logger.exception("octagon-http terminal chunk 写入失败")
    try:
        ev = _http_exchange_evidence(
            hop, status_code=status_code, streamed=streamed, partial=partial,
            finished_at=finished_at, duration_ms=duration_ms, ttft_ms=ttft_ms,
            response_summary=response_summary, response_body_ref=response_body_ref,
            redaction_status=redaction_status,
        )
        async with hop.entry.lock:
            if hop.entry.sealed:
                hop.entry.dropped_hops += 1
                return
            hop.entry.writer.append(ev)
    except Exception:
        logger.exception("octagon-http http_exchange 写入失败 attempt=%s", hop.attempt_id)
