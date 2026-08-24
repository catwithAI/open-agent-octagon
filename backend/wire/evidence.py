"""WireEvidence v1：跨进程 spool 契约。

source spool 的每行必须是这个最小 envelope，而不是 canonical record 或 producer
私有 JSON。Python / Go / Node / sidecar 都写这个格式，finalizer 只吃它——因此
它是唯一的跨语言边界，必须严格：

- envelope 按 ``evidence_type`` 做 discriminated union：每个 variant 的
  ``payload`` 是封闭的 versioned 模型（``extra="forbid"``），不能装任意私有字段；
- **最小字段是 required-but-nullable**：字段不可得时必须显式写 ``null``，不允许
  省略——省略无法区分「producer 版本过旧 / 实现遗漏 / 字段不可观测」三种情况
  （「字段不可得时写 null，不用 0 冒充」的可执行化）；
- ``phase``/``evidence_schema_version``/``redaction.policy`` 是枚举/常量，
  不接受任意字符串；unknown phase 合法但必须显式写 ``"unknown"``；
- ``extensions`` 是唯一扩展通道，key 必须带 namespace 前缀（``x-<ns>.``）；
- 导出 ``wire-evidence-v1.schema.json``（oneOf + additionalProperties=false +
  required 列表），供 Go/Node contract test 校验同一份 schema。

spool 里只能出现已脱敏的值；原始 raw event 留在既有 raw 文件（events.jsonl 等），
``raw_ref`` 指向它。append 前的 schema/attempt/policy 校验由 spool writer 执行
（spool.py），不依赖 producer 自觉。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

EVIDENCE_SCHEMA_VERSION = "octagon-wire-evidence-v1"

# 与 canonical 的 models.Phase 同一集合（避免 import 循环，这里显式列出；
# 两处的一致性由 tests/test_wire_models.py 断言）。
Phase = Literal[
    "attempt_setup",
    "agent_run",
    "verification",
    "artifact_collection",
    "attempt_cleanup",
    "unknown",
]

CapturePolicyLiteral = Literal["off", "metadata", "parsed", "full"]

EvidenceType = Literal[
    "native_llm_call",
    "aggregate_usage",
    "http_exchange",
    "stream_chunk",
    "mcp_frame",
    "capture_event",
    "compaction_hint",
]

# extensions key 必须带 namespace 前缀，如 ``x-blade.session``。
_EXTENSION_KEY_RE = re.compile(r"^x-[a-z0-9][a-z0-9-]*\..+\Z")


class _Strict(BaseModel):
    # 跨进程写入契约：多字段即错误，未知字段只能进 extensions（见 envelope）。
    model_config = ConfigDict(extra="forbid")


class EvidenceSource(_Strict):
    kind: str
    instance: str
    version: str | None = None


class EvidenceProducer(_Strict):
    name: str
    version: str | None = None
    event_id: str | None = None


class EvidenceTime(_Strict):
    observed_at: str
    started_at: str | None = None
    finished_at: str | None = None


class EvidenceRawRef(_Strict):
    kind: str
    file: str
    line: int | None = None


class CorrelationHints(_Strict):
    producer_session_id: str | None = None
    producer_call_id: str | None = None
    request_id: str | None = None
    provider_response_id: str | None = None
    jsonrpc_id: str | None = None
    model: str | None = None
    sequence: int | None = None


class EvidenceRedaction(_Strict):
    policy: CapturePolicyLiteral
    status: Literal["applied", "skipped", "failed"]
    hash_algorithm: Literal["sha256"] | None = None
    hash_domain: str | None = None


# ---------- 封闭的共享子结构（"$defs 封闭定义"）------------------


class UsagePayload(_Strict):
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    reasoning_tokens: int | None
    estimated: bool | None


class RequestSummary(_Strict):
    model: str | None
    message_count: int | None
    message_bytes: int | None
    system_hash: str | None
    messages_hash: str | None
    tools_hash: str | None
    hash_domain: str | None


class ResponseSummary(_Strict):
    content_hash: str | None
    hash_domain: str | None
    message_bytes: int | None
    output_blocks: int | None


class TimingPayload(_Strict):
    started_at: str | None
    finished_at: str | None
    duration_ms: float | None
    ttft_ms: float | None


# ---------- payload variants ------------------------------------------------
#
# 最小字段全部 required-but-nullable（无默认值）：producer 必须
# 显式写出每个 key，值不可得时写 null。


class NativeLlmCallPayload(_Strict):
    producer_call_id: str | None
    model: str | None
    call_role: str | None
    request_summary: RequestSummary | None
    response_summary: ResponseSummary | None
    usage: UsagePayload | None
    finish_reason: str | None


class AggregateUsagePayload(_Strict):
    scope: str | None
    usage: UsagePayload | None
    producer_event_type: str | None


class HttpExchangePayload(_Strict):
    # direction 由 source 声明（inbound=Env Server 收到工具请求，
    # outbound=Octagon 发往 provider）。这是 v1 内的**追加可选字段**（默认
    # None）——旧 v1 http_exchange 不带此键仍能 validate，finalizer fallback
    # 到 outbound（不做破坏性 schema 变更、不需升 version）。
    direction: Literal["inbound", "outbound"] | None = None
    method: str | None
    scheme: str | None
    authority: str | None
    path: str | None
    status_code: int | None
    request_bytes: int | None
    response_bytes: int | None
    streamed: bool | None
    partial: bool | None
    timing: TimingPayload | None
    # v1 追加可选字段（默认 None，旧 http_exchange 不带仍 validate）：反代解析
    # request/response body 得到的跨协议 semantic summary。transport
    # source 能看到明文 payload 时填，否则 None——finalizer 落进 canonical，供
    # 跨 agent/source semantic hash 对比。native_llm_call 走各自 summary，二者
    # hash_domain 相同即可跨源比对。
    request_summary: RequestSummary | None = None
    response_summary: ResponseSummary | None = None
    # v1 追加可选字段：反代从 provider 响应中只提取结构化 token 计数，不持久化
    # response 正文。这样 metadata policy 也能为不产 native usage 事件的 CLI
    # （当前主要是 kimi-code）提供五维统计；旧 evidence 不带该字段仍可读取。
    usage: UsagePayload | None = None


class StreamChunkPayload(_Strict):
    hop_anchor: str | None
    sequence: int | None
    relative_ms: float | None
    event_type: str | None
    bytes: int | None
    content_hash: str | None
    terminal: bool | None
    dropped_before: int | None


class McpFramePayload(_Strict):
    direction: Literal["client-to-server", "server-to-client"] | None
    jsonrpc_id: str | None
    message_kind: Literal["request", "response", "notification"] | None
    method: str | None
    tool_name: str | None
    bytes: int | None
    is_error: bool | None
    truncated: bool | None


class CaptureEventPayload(_Strict):
    event: Literal[
        "start", "ready", "phase_change", "drop", "error", "stop", "finalize"
    ]
    # canonical CaptureEventData 要求 source_instance；lifecycle 写入时必填，
    # 且必须是 resolved instance（finalizer 按 instance 归属，不做 kind 扇出）。
    source_instance: str | None
    status: str | None
    reason_code: str | None
    message: str | None
    # counters 是 **cumulative** 语义：每次汇报该 instance 至今的累计值
    # （如 records_dropped/parse_errors/records_written），finalizer 对同名
    # counter 取 max，不跨事件相加——delta 语义的计数请换用独立 drop/error 事件。
    counters: dict[str, int] | None
    effective_capabilities: dict[str, Any] | None


class CompactionHintPayload(_Strict):
    producer_call_id: str | None
    before_anchor: str | None
    after_anchor: str | None
    strategy: str | None
    confidence: str | None


# ---------- envelope（discriminated union on evidence_type）-----------------


class _EvidenceBase(_Strict):
    """envelope 公共字段。variant 只追加 ``evidence_type``（Literal）与 payload。

    ``raw_ref``/``correlation_hints``/``capabilities``/``errors``/``extensions``
    同样 required：没有就写 null / 空对象 / 空数组，不允许省略。
    """

    evidence_schema_version: Literal["octagon-wire-evidence-v1"] = (
        EVIDENCE_SCHEMA_VERSION
    )
    evidence_id: str
    attempt_id: str
    phase: Phase
    source: EvidenceSource
    producer: EvidenceProducer
    time: EvidenceTime
    raw_ref: EvidenceRawRef | None
    correlation_hints: CorrelationHints
    capabilities: dict[str, Any]
    redaction: EvidenceRedaction
    errors: list[dict[str, Any]]
    extensions: dict[str, Any]

    @field_validator("extensions")
    @classmethod
    def _extensions_namespaced(cls, v: dict[str, Any]) -> dict[str, Any]:
        for key in v:
            if not _EXTENSION_KEY_RE.match(key):
                raise ValueError(
                    f"extensions key 必须带 namespace 前缀（x-<ns>.<field>）: {key!r}"
                )
        return v


class NativeLlmCallEvidence(_EvidenceBase):
    evidence_type: Literal["native_llm_call"] = "native_llm_call"
    payload: NativeLlmCallPayload


class AggregateUsageEvidence(_EvidenceBase):
    evidence_type: Literal["aggregate_usage"] = "aggregate_usage"
    payload: AggregateUsagePayload


class HttpExchangeEvidence(_EvidenceBase):
    evidence_type: Literal["http_exchange"] = "http_exchange"
    payload: HttpExchangePayload


class StreamChunkEvidence(_EvidenceBase):
    evidence_type: Literal["stream_chunk"] = "stream_chunk"
    payload: StreamChunkPayload


class McpFrameEvidence(_EvidenceBase):
    evidence_type: Literal["mcp_frame"] = "mcp_frame"
    payload: McpFramePayload


class CaptureEventEvidence(_EvidenceBase):
    evidence_type: Literal["capture_event"] = "capture_event"
    payload: CaptureEventPayload


class CompactionHintEvidence(_EvidenceBase):
    evidence_type: Literal["compaction_hint"] = "compaction_hint"
    payload: CompactionHintPayload


WireEvidence = Annotated[
    Union[
        NativeLlmCallEvidence,
        AggregateUsageEvidence,
        HttpExchangeEvidence,
        StreamChunkEvidence,
        McpFrameEvidence,
        CaptureEventEvidence,
        CompactionHintEvidence,
    ],
    Field(discriminator="evidence_type"),
]

_EVIDENCE_ADAPTER: TypeAdapter[Any] = TypeAdapter(WireEvidence)

EVIDENCE_VARIANTS: dict[str, type[_EvidenceBase]] = {
    "native_llm_call": NativeLlmCallEvidence,
    "aggregate_usage": AggregateUsageEvidence,
    "http_exchange": HttpExchangeEvidence,
    "stream_chunk": StreamChunkEvidence,
    "mcp_frame": McpFrameEvidence,
    "capture_event": CaptureEventEvidence,
    "compaction_hint": CompactionHintEvidence,
}

# payload 最小字段的全 null 模板：producer 构造起点 / 测试基线。
PAYLOAD_MODELS: dict[str, type[_Strict]] = {
    "native_llm_call": NativeLlmCallPayload,
    "aggregate_usage": AggregateUsagePayload,
    "http_exchange": HttpExchangePayload,
    "stream_chunk": StreamChunkPayload,
    "mcp_frame": McpFramePayload,
    "capture_event": CaptureEventPayload,
    "compaction_hint": CompactionHintPayload,
}


def null_payload(evidence_type: str) -> dict[str, Any]:
    """该 variant 的最小合法 payload：所有 required-nullable 字段显式 null。

    capture_event 的 ``event`` 无 null 语义，模板给 "start"，调用方必须覆盖。
    """
    model = PAYLOAD_MODELS[evidence_type]
    out: dict[str, Any] = {name: None for name in model.model_fields}
    if evidence_type == "capture_event":
        out["event"] = "start"
    return out


def validate_evidence(data: Any):
    """spool 行 → 具体 variant 实例；未知 evidence_type/字段/非法 phase、
    缺失 required 字段（含显式 null 要求）都报错。"""
    return _EVIDENCE_ADAPTER.validate_python(data)


def evidence_json_schema() -> dict[str, Any]:
    """导出 WireEvidence v1 的 JSON Schema（oneOf discriminated union，
    所有 envelope/variant/$defs 均 additionalProperties=false + required）。"""
    return _EVIDENCE_ADAPTER.json_schema()


def write_schema_file(dest: Path) -> Path:
    """把 JSON Schema 写到 spec 目录，供 Go/Node 侧引用同一份契约。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps(evidence_json_schema(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return dest
