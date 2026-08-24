"""Canonical wire record 模型（Pydantic v2）。

这是 finalized ``wire.jsonl`` 一行的形状。与 source spool 的 ``WireEvidence``
（evidence.py）分层：evidence 是各 source 写的原始证据，canonical record 是
finalizer 归一 + correlate 后的统一语义。前端和分析层只读 canonical，不理解
任何厂商私有事件格式。

版本演进：canonical reader 必须容忍读取多出的未知字段——用
``model_config = ConfigDict(extra="allow")`` 保留而非拒绝，这样新版本写的文件
能被旧代码读。这与 ``WireEvidence`` 的 ``extra="forbid"`` 是相反策略：spool 是
跨进程写入契约必须严格，canonical 是长期存档必须向后兼容读取。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "octagon-wire-v1"

RecordType = Literal[
    "llm_call",
    "http_exchange",
    "stream_chunk",
    "mcp_frame",
    "capture_event",
    "context_compaction",
]

Phase = Literal[
    "attempt_setup",
    "agent_run",
    "verification",
    "artifact_collection",
    "attempt_cleanup",
    "unknown",
]

Confidence = Literal["explicit", "high", "medium", "low", "inferred", "unmatched"]


class _WireBase(BaseModel):
    # 容忍读取未知字段：旧代码读新版本文件不炸。
    model_config = ConfigDict(extra="allow")


class SourceRef(_WireBase):
    kind: str
    instance: str
    version: str | None = None
    parser_version: str | None = None


class TimeInfo(_WireBase):
    timestamp: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: float | None = None


class Correlation(_WireBase):
    logical_call_id: str | None = None
    hop_id: str | None = None
    parent_hop_id: str | None = None
    trajectory_step_id: str | None = None
    tool_call_id: str | None = None
    agent_id: str = "main"
    parent_agent_id: str | None = None
    producer_session_id: str | None = None
    confidence: Confidence = "explicit"


class ProvenanceEntry(_WireBase):
    evidence_id: str
    raw_ref: dict[str, Any] | None = None
    confidence: str | None = None


class Conflict(_WireBase):
    field: str
    selected: Any = None
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    rule: str | None = None


class WireRecord(_WireBase):
    """canonical envelope。

    ``data`` 承载各 record_type 的 payload（下面的 *Data 模型之一，序列化为
    dict）。envelope 层字段统一，data 层随类型变化——这样 API/UI 可以先按
    envelope 过滤（record_type/phase/logical_call_id），再按需解析 data。
    """

    schema_version: str = SCHEMA_VERSION
    record_id: str
    record_type: RecordType
    attempt_id: str
    phase: Phase
    source: SourceRef
    time: TimeInfo = Field(default_factory=TimeInfo)
    correlation: Correlation = Field(default_factory=Correlation)
    provenance: list[ProvenanceEntry] = Field(default_factory=list)
    # 字段级来源与冲突：不为每个字段套 ObservedValue，
    # 改用扁平 field_sources 映射 + conflicts 列表，保留 provenance 又不臃肿。
    field_sources: dict[str, str] = Field(default_factory=dict)
    conflicts: list[Conflict] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)


# ---------- 各 record_type 的 data payload ------------
#
# 这些是 data 字段的结构约束，finalizer 构造时用它们校验，读取时按需解析。
# 未知字段同样 allow，保持前向兼容。


class Usage(_WireBase):
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    estimated: bool = False
    estimator: str | None = None


class LlmCallData(_WireBase):
    protocol: str | None = None
    call_role: Literal[
        "main", "compaction", "planning", "meta", "subagent", "unknown"
    ] = "main"
    model_requested: str | None = None
    model_resolved: str | None = None
    provider: str | None = None
    routing_path: str | None = None
    streamed: bool | None = None
    partial: bool = False
    request: dict[str, Any] = Field(default_factory=dict)
    response: dict[str, Any] = Field(default_factory=dict)
    usage: Usage = Field(default_factory=Usage)
    timing: dict[str, Any] = Field(default_factory=dict)
    transport: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)
    hop_refs: list[str] = Field(default_factory=list)


class HttpExchangeData(_WireBase):
    hop_id: str
    direction: Literal["outbound", "inbound"] = "outbound"
    protocol: str | None = None
    method: str | None = None
    scheme: str | None = None
    authority: str | None = None
    path: str | None = None
    status_code: int | None = None
    request_bytes: int | None = None
    response_bytes: int | None = None
    streamed: bool | None = None
    chunk_count: int | None = None
    partial: bool | None = None
    parent_hop_id: str | None = None
    request_body_ref: str | None = None
    response_body_ref: str | None = None
    # body 采集超上限被截断：blob 只是**连续前缀**，非完整正文。前端
    # 据此提示「正文已截断」，不能把残缺 blob 当完整响应展示。
    request_body_truncated: bool = False
    response_body_truncated: bool = False
    headers: dict[str, Any] = Field(default_factory=dict)


class StreamChunkData(_WireBase):
    hop_id: str
    sequence: int
    relative_ms: float | None = None
    event_type: str | None = None
    bytes: int | None = None
    content_hash: str | None = None
    hash_domain: str | None = None
    payload_ref: str | None = None
    is_terminal: bool | None = None
    partial: bool | None = None
    dropped_before: int | None = None


class McpFrameData(_WireBase):
    # direction 等布尔/枚举允许 null：不可观测 ≠ 默认值
    direction: Literal["client-to-server", "server-to-client"] | None = None
    jsonrpc_id: str | None = None
    message_kind: Literal["request", "response", "notification"] | None = None
    method: str | None = None
    tool_name: str | None = None
    bytes: int | None = None
    paired_record_id: str | None = None
    is_error: bool | None = None
    truncated: bool | None = None
    payload_ref: str | None = None


class CaptureEventData(_WireBase):
    event: Literal[
        "start", "ready", "phase_change", "drop", "error", "stop", "finalize"
    ]
    source_instance: str
    status: str | None = None
    reason_code: str | None = None
    error_class: str | None = None
    message: str | None = None
    counters: dict[str, int] = Field(default_factory=dict)
    effective_capabilities: dict[str, Any] = Field(default_factory=dict)


class ContextCompactionData(_WireBase):
    before_call_id: str | None = None
    after_call_id: str | None = None
    # 压缩边界所属 turn（turn correlation 已写进 llm_call.correlation）。
    before_turn_id: str | None = None
    after_turn_id: str | None = None
    summary_call_id: str | None = None
    before_tokens: int | None = None
    after_tokens: int | None = None
    dropped_messages: int | None = None
    inserted_messages: int | None = None
    kept_prefix: int | None = None
    kept_suffix: int | None = None
    strategy: str | None = None
    source: str | None = None
    confidence: Confidence | None = None
    analyzer_version: str | None = None


# record_type → data 模型，供 finalizer 构造与读取时校验。
DATA_MODELS: dict[str, type[_WireBase]] = {
    "llm_call": LlmCallData,
    "http_exchange": HttpExchangeData,
    "stream_chunk": StreamChunkData,
    "mcp_frame": McpFrameData,
    "capture_event": CaptureEventData,
    "context_compaction": ContextCompactionData,
}
