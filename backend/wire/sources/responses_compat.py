"""Responses↔Chat compatibility source。

**实验性接口（experimental API）**：本模块提供独立的 recorder 与其数据契约，
**尚未接入任何真实转换器/gateway/lifecycle**——它不会因用户跑一次 run 而自动生效。
接入真实转换层后才是运行时能力。首版有意收窄（见下）。

当一个转换层（gateway/sidecar）把 **Responses** 协议的调用转成 **Chat Completions**
（或反之）发给上游时，同一次逻辑调用产生**两跳**不同协议的 HTTP 交换：

    client ──Responses──> converter ──Chat Completions──> upstream

本 source 把这两跳**分别**记录成 `http_exchange` evidence，并让它们**归属同一
logical call**：给配对的两跳打**同一个 conversion request_id**——finalizer
的 union-find 用 `proxy-request:<request_id>` anchor 把它们并成一条 logical call。

设计约束：
- **独立 source**——不内嵌 adapter/wire schema/分析层；关闭本 source 后原生
  Responses 路径不受影响（不产 evidence 而已，其它 source 照常）；
- **不依赖 MITM CA**——转换层本来就能看到两侧明文 payload（它自己在做转换），本
  source 只是把它已有的两侧请求记录下来，无需解 TLS；
- 两跳各按自己协议用 parser 算 request 的 semantic summary（跨协议同 hash，
  证明是同一逻辑调用），落进 canonical 供分析；credential 绝不落 wire。

**首版收窄（experimental，只做以下）**：
- 只记 **request + semantic summary**——不落 response body，不做 parsed/full blob，
  无 timing/stream/partial；这些留待接入真实转换器后按策略补齐；
- inbound/outbound 各自 direction 与 status_code；
- conversion_id 全局唯一（默认 uuid4）。

用法：转换层每完成一次协议转换，调用 ``record_conversion(...)``，传两侧的 **request**
body + endpoint + 各自 status。source 生成一对 http_exchange evidence 写 spool。
"""

from __future__ import annotations

import logging
import uuid as _uuid
from datetime import datetime, timezone
from pathlib import Path

from backend.wire import ids, paths, spool
from backend.wire.evidence import (
    CorrelationHints,
    EvidenceProducer,
    EvidenceRawRef,
    EvidenceRedaction,
    EvidenceSource,
    EvidenceTime,
    HttpExchangeEvidence,
    HttpExchangePayload,
    null_payload,
)

logger = logging.getLogger(__name__)

SOURCE_KIND = "responses-compat"
PRODUCER_NAME = "octagon-responses-compat"
PARSER_VERSION = "responses-compat-v1"

_PROCESS_GENERATION = _uuid.uuid4().hex

_VALID_PHASES = frozenset(
    {"attempt_setup", "agent_run", "verification", "artifact_collection", "attempt_cleanup"}
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ResponsesCompatSource:
    """记录一次 Responses↔Chat 协议转换的两跳 http_exchange。

    ``instance`` 区分多个转换实例（多 provider）；spool 落在 attempt 的
    ``wire-sources/responses-compat@<instance>.jsonl``。
    """

    kind = SOURCE_KIND

    def __init__(self, *, data_path: Path, attempt_id: str, instance: str = "default") -> None:
        self.data_path = Path(data_path)
        self.attempt_id = attempt_id
        self.instance = instance
        self._seq = 0

    def _writer(self) -> spool.SpoolWriter:
        return spool.SpoolWriter(
            paths.source_spool_file(self.data_path, self.attempt_id, SOURCE_KIND, self.instance),
            expected_attempt_id=self.attempt_id,
        )

    def record_conversion(
        self,
        *,
        phase: str,
        # 入站（转换前）跳：客户端发给 converter 的协议。
        inbound_protocol: str,
        inbound_request_body: bytes | None,
        inbound_endpoint: str,
        inbound_status_code: int | None = None,
        # 出站（转换后）跳：converter 发给 upstream 的协议。
        outbound_protocol: str,
        outbound_request_body: bytes | None,
        outbound_endpoint: str,
        outbound_status_code: int | None = None,
        conversion_id: str | None = None,
    ) -> str:
        """记录一次转换的两跳，返回共享的 conversion_id（logical call anchor）。

        两跳各按自己协议解析 request 的 semantic summary，共享同一 request_id → union
        到同一 logical call。inbound/outbound 各自 direction 与 status_code（转换层与
        上游的响应可不同）。fail-open：任何异常只记日志，不影响转换层主流程。

        首版只记 **request + semantic summary**（experimental）：不落 response body、
        不做 parsed/full blob、无 timing/stream——那些留待接入真实转换器后按策略
        补齐。"""
        # conversion_id 全局唯一：默认用 uuid4，避免顶层便利函数每次新 source、
        # seq 从 0 重复导致不同调用被并成同一 logical call（评审）。
        cid = conversion_id or f"conv:{_uuid.uuid4().hex}"
        valid_phase = phase if phase in _VALID_PHASES else "unknown"
        try:
            writer = self._writer()
            try:
                writer.append(self._hop_evidence(
                    valid_phase, cid, hop="inbound", direction="inbound",
                    protocol=inbound_protocol, request_body=inbound_request_body,
                    endpoint=inbound_endpoint, status_code=inbound_status_code,
                ))
                writer.append(self._hop_evidence(
                    valid_phase, cid, hop="outbound", direction="outbound",
                    protocol=outbound_protocol, request_body=outbound_request_body,
                    endpoint=outbound_endpoint, status_code=outbound_status_code,
                ))
            finally:
                writer.close()
        except Exception:
            logger.exception("responses-compat 记录失败 attempt=%s", self.attempt_id)
        return cid

    def _next_seq(self) -> int:
        s = self._seq
        self._seq += 1
        return s

    def _hop_evidence(
        self, phase: str, conversion_id: str, *, hop: str, direction: str,
        protocol: str, request_body: bytes | None, endpoint: str,
        status_code: int | None,
    ) -> HttpExchangeEvidence:
        # 各按协议算 semantic summary（跨协议同 hash 证明同一逻辑调用）。
        request_summary = None
        try:
            from backend.wire.sources import parse as _parse
            request_summary = _parse.parse_request(protocol, request_body or b"")
        except Exception:
            request_summary = None

        seq = self._next_seq()
        authority, path, scheme = _split_endpoint(endpoint)
        return HttpExchangeEvidence(
            evidence_id=ids.evidence_id(
                attempt_id=self.attempt_id, source_kind=SOURCE_KIND,
                source_instance=self.instance,
                raw_ref=f"responses-compat:{_PROCESS_GENERATION}:{conversion_id}:{hop}",
                producer_id=hop,
            ),
            attempt_id=self.attempt_id,
            phase=phase,  # type: ignore[arg-type]
            source=EvidenceSource(kind=SOURCE_KIND, instance=self.instance),
            producer=EvidenceProducer(name=PRODUCER_NAME, version=PARSER_VERSION),
            time=EvidenceTime(observed_at=_now_iso(), started_at=None, finished_at=None),
            raw_ref=EvidenceRawRef(
                kind="responses-compat",
                file=f"{SOURCE_KIND}@{self.instance}.jsonl", line=None),
            # request_id=conversion_id：两跳共享 → union 到同一 logical call。
            # 协议名进扩展（不占 schema 字段），供分析区分转换前后。
            correlation_hints=CorrelationHints(
                request_id=conversion_id,
                model=request_summary.model if request_summary else None,
            ),
            capabilities={"protocol_conversion_metadata": True},
            redaction=EvidenceRedaction(policy="metadata", status="applied"),
            errors=[],
            extensions={
                "x-octagon.compat-hop": hop,       # inbound（转换前）/ outbound（转换后）
                "x-octagon.compat-protocol": protocol,
                "x-octagon.compat-conversion-id": conversion_id,
                "x-octagon.compat-seq": seq,
            },
            payload=HttpExchangePayload(
                direction=direction,  # type: ignore[arg-type]  inbound（转换前）/ outbound（转换后）
                method="POST", scheme=scheme, authority=authority, path=path,
                status_code=status_code,
                request_bytes=len(request_body) if request_body else 0,
                response_bytes=None, streamed=None, partial=False, timing=None,
                request_summary=request_summary, response_summary=None,
            ),
        )


def _split_endpoint(endpoint: str) -> tuple[str | None, str | None, str | None]:
    """把 endpoint URL 拆成 (authority, path, scheme)。解析失败保守返回 None。"""
    if not endpoint:
        return None, None, None
    scheme = None
    rest = endpoint
    if "://" in endpoint:
        scheme, rest = endpoint.split("://", 1)
    authority, _, path = rest.partition("/")
    return authority or None, ("/" + path) if path else "/", scheme


# 便于外部直接构造：不必实例化就能记录一次转换。
def record_conversion(
    *, data_path: Path, attempt_id: str, instance: str = "default", **kwargs
) -> str:
    """一次性记录一次转换（内部构造 source）。见 ResponsesCompatSource.record_conversion。"""
    src = ResponsesCompatSource(
        data_path=data_path, attempt_id=attempt_id, instance=instance)
    return src.record_conversion(**kwargs)


# null_payload 供测试/工具构造空 http_exchange（re-export 便利）。
__all__ = [
    "ResponsesCompatSource", "record_conversion", "SOURCE_KIND", "null_payload",
]
