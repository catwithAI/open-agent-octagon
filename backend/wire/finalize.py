"""finalizer：source evidence → canonical wire.jsonl + manifest（表）。

确定性映射（同输入必得同输出，离线重建幂等）：

| evidence type | canonical 输出 |
|---|---|
| native_llm_call | `llm_call`（semantic/usage 字段，显式 anchor 关联） |
| aggregate_usage | 不伪造 call——进 manifest.aggregates 供对账 |
| http_exchange | `http_exchange` hop；有显式 call anchor 时关联 llm_call |
| stream_chunk | `stream_chunk`（hop_anchor → hop_id） |
| mcp_frame | `mcp_frame`，按 (instance, jsonrpc_id) 配对 |
| capture_event | `capture_event` 并驱动 manifest source status |
| compaction_hint | 证据不足，不伪造 `context_compaction`，只保留 hint provenance |

phase 归属：finalizer 只校验 evidence 自带的显式 phase（schema 已强制枚举），
绝不按 wall-clock 推断；attempt 不匹配的行计 dropped。verification/unknown
phase 的 call 通过 ``select_agent_run_calls`` 从 agent_run 聚合排除。

manifest 双层状态：per-source status + 整体 status；
「零通信」（干净关闭、0 行）与「source 没工作」（无 spool / .partial / 启动
失败 gap）可区分。generation 每次成功 finalize 单调递增。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.wire import correlate, evidence as ev, ids, paths, spool, writer
from backend.wire import turn_correlation as correlate_turn
from backend.wire.policy import EffectivePolicy

logger = logging.getLogger(__name__)

MANIFEST_SCHEMA_VERSION = "octagon-wire-manifest-v1"

# per-attempt finalize 锁（进程内）：两个并发 finalizer（如 attempt_end 与
# rebuild）串行执行，避免生成相同 generation 或交错写坏三个输出文件。
_FINALIZE_LOCKS: dict[str, threading.Lock] = {}
_FINALIZE_LOCKS_GUARD = threading.Lock()


def _finalize_lock(attempt_id: str) -> threading.Lock:
    with _FINALIZE_LOCKS_GUARD:
        return _FINALIZE_LOCKS.setdefault(attempt_id, threading.Lock())

# wire-sources/ 下不是 evidence spool 的框架文件。
_NON_SPOOL_FILES = ("correlation-map.json", "phase-state.json")

_SPOOL_NAME_RE = re.compile(
    r"^(?P<kind>[A-Za-z0-9._-]+)(?:@(?P<instance>[A-Za-z0-9._-]+))?\.jsonl(?:\.partial)?\Z"
)


@dataclass
class SourceScan:
    kind: str
    instance: str
    file: Path
    records: list[Any] = field(default_factory=list)  # 已验证的 evidence 实例
    partial: bool = False
    truncated_tail: bool = False
    parse_errors: int = 0
    dropped: int = 0
    # evidence envelope 声明的 source capabilities（后到覆盖，进 manifest）
    capabilities: dict[str, Any] = field(default_factory=dict)


def _scan_sources(data_path: Path, attempt_id: str) -> list[SourceScan]:
    src_dir = paths.sources_dir(data_path, attempt_id)
    if not src_dir.is_dir():
        return []
    scans: list[SourceScan] = []
    for file in sorted(src_dir.iterdir()):
        if file.name in _NON_SPOOL_FILES or not file.is_file():
            continue
        m = _SPOOL_NAME_RE.match(file.name)
        if m is None:
            continue
        file_kind = m.group("kind")
        file_instance = m.group("instance") or file_kind
        # 一个共享 spool（当前 HTTP proxy 的 octagon-http.jsonl）可以承载多个
        # provider instance。manifest 的 source identity 必须以 evidence envelope
        # 为准，不能只看文件名；否则文件内明明是 or-cc/or-codex，finalizer 却
        # 生成 octagon-http complete + provider no-spool 的虚假 partial。
        grouped: dict[tuple[str, str], SourceScan] = {}
        file_parse_errors = 0
        file_dropped = 0
        # 流式读：不保留原始 dict，只留校验后的 evidence 实例。二者同时在内存里
        # 会让大 spool 把恢复路径的 RSS 推到危险水平，甚至触发 OOM。
        stream = spool.iter_spool(file)
        read: spool.SpoolReadResult | None = None
        while True:
            try:
                raw = next(stream)
            except StopIteration as stop:
                read = stop.value
                break
            try:
                item = ev.validate_evidence(raw)
            except Exception:
                file_parse_errors += 1
                continue
            del raw
            if item.attempt_id != attempt_id:
                file_dropped += 1
                continue
            key = (item.source.kind, item.source.instance)
            scan = grouped.setdefault(
                key,
                SourceScan(kind=key[0], instance=key[1], file=file),
            )
            if item.capabilities:
                scan.capabilities.update(item.capabilities)
            scan.records.append(item)
        # 空 spool 仍表示文件名声明的 source 已干净启动但零通信；有 evidence 时
        # 则只报告 envelope 中实际观察到的 instance。文件级损坏无法可靠归给
        # 某一组，保守地让所有观察到的组都降级；若没有组则归文件名 source。
        if not grouped:
            grouped[(file_kind, file_instance)] = SourceScan(
                kind=file_kind, instance=file_instance, file=file,
            )
        for scan in grouped.values():
            scan.partial = read.partial
            scan.truncated_tail = read.truncated_tail
            scan.parse_errors += file_parse_errors + read.parse_errors
            scan.dropped += file_dropped
            scans.append(scan)
    return scans


# ---------- evidence → canonical record ------------------------------------


def _base_record(item: Any, record_type: str, attempt_id: str, data: dict) -> dict:
    # 显式 turn：evidence extensions 带 x-octagon.turn-id 时投影进
    # canonical correlation，confidence=explicit。HTTP proxy source 把 adapter 每轮
    # 设的 X-Octagon-Turn-Id 头写进这个 extension；旧 reader 忽略新字段。
    # inferred（时间窗口）兜底与 logical-call 合并在 _finalize_attempt_locked 里的
    # turn_correlation.project_turn_correlation 完成。
    exp_turn = correlate_turn.explicit_turn(item.extensions or {})
    correlation: dict[str, Any] = {
        # sub-agent 拓扑：normalizer 把非 main call 的 agent_id 写进
        # extensions（x-octagon.agent-id），默认 main。子 agent 有独立 agent_id
        # + parent_agent_id，不压成普通 tool result。
        "agent_id": (item.extensions or {}).get("x-octagon.agent-id") or "main",
        "parent_agent_id": (item.extensions or {}).get("x-octagon.parent-agent-id"),
        "confidence": "explicit",
        "producer_session_id": item.correlation_hints.producer_session_id,
    }
    if exp_turn is not None:
        correlation["turn_id"] = exp_turn[0]
        correlation["turn_index"] = exp_turn[1]
        correlation["turn_confidence"] = "explicit"
    return {
        "schema_version": "octagon-wire-v1",
        "record_id": ids_record(attempt_id, record_type, item.evidence_id),
        "record_type": record_type,
        "attempt_id": attempt_id,
        "phase": item.phase,
        "source": {
            "kind": item.source.kind,
            "instance": item.source.instance,
            "version": item.source.version,
        },
        "time": {
            "timestamp": item.time.observed_at,
            "started_at": item.time.started_at,
            "finished_at": item.time.finished_at,
        },
        "correlation": correlation,
        "provenance": [
            {
                "evidence_id": item.evidence_id,
                "raw_ref": item.raw_ref.model_dump() if item.raw_ref else None,
            }
        ],
        "field_sources": {},
        "conflicts": [],
        "data": data,
    }


def ids_record(attempt_id: str, record_type: str, anchor: str) -> str:
    return ids.record_id(
        attempt_id=attempt_id, record_kind=record_type, record_anchor=anchor
    )


def _native_call_hints(item: Any) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """native_llm_call 的 anchor 输入：correlation_hints 与 payload 的
    producer_call_id 合并。

    v1 payload 契约本身就带 producer_call_id——producer 只按 payload 填、不
    重复写 hints 时不能退化成 sequence anchor。两边都有且不一致时取 hints
    并登记 conflict，不静默选择。
    """
    hints = item.correlation_hints.model_dump()
    payload_pid = getattr(item.payload, "producer_call_id", None)
    conflict: dict[str, Any] | None = None
    if payload_pid:
        hinted = hints.get("producer_call_id")
        if not hinted:
            hints["producer_call_id"] = payload_pid
        elif hinted != payload_pid:
            conflict = {
                "field": "producer_call_id",
                "selected": hinted,
                "candidates": [
                    {"value": hinted, "source": "correlation_hints"},
                    {"value": payload_pid, "source": "payload"},
                ],
                "rule": "hints-over-payload",
            }
    return hints, conflict


def _hop_anchor_for(item: Any, seq: int) -> str:
    hints = item.correlation_hints
    if hints.request_id:
        return f"{correlate.ANCHOR_PROXY_REQUEST}:{hints.request_id}"
    return correlate.sequence_anchor(item.source.kind, item.source.instance, seq)


@dataclass
class FinalizeResult:
    records: list[dict] = field(default_factory=list)
    aggregates: list[dict] = field(default_factory=list)
    compaction_hints: list[dict] = field(default_factory=list)
    logical_calls: set[str] = field(default_factory=set)
    unmatched_calls: int = 0
    hops: set[str] = field(default_factory=set)
    conflicts: int = 0
    # capture_event 驱动的 per-source 状态（映射表）：
    # key 为 payload.source_instance（resolved instance，不做 kind 扇出）；
    # value 含事件计数与 cumulative counters（同名 counter 取 max，见
    # evidence.CaptureEventPayload.counters 语义）。
    capture_stats: dict[str, dict[str, Any]] = field(default_factory=dict)


def _map_evidence(
    scans: list[SourceScan], attempt_id: str, cmap: correlate.CorrelationMap
) -> FinalizeResult:
    result = FinalizeResult()
    mcp_frames: list[dict] = []

    # 先收集全部 call anchor 组、统一 union 后再产出 record：
    # 单遍边遍历边发 record 时，桥接 evidence（同时带 producer-call 和
    # provider-response）无法修正已输出记录，会造成 split-brain logical call。
    call_groups: list[list[str]] = []
    group_index: dict[tuple[int, int], int] = {}
    call_conflicts: dict[tuple[int, int], dict[str, Any]] = {}
    for si, scan in enumerate(scans):
        for seq, item in enumerate(scan.records):
            if item.evidence_type == "native_llm_call":
                hints, conflict = _native_call_hints(item)
                if conflict is not None:
                    call_conflicts[(si, seq)] = conflict
                anchors = correlate.explicit_anchors(hints)
                if not anchors:
                    # 无显式 ID 的 native call：优先用 normalizer 在 hints.sequence
                    # 里固定的稳定序号（与它写 trajectory step 时同一 anchor），
                    # 否则退回 scan 记录位置。二者一致才能让 step↔call 对上。
                    seq_no = hints.get("sequence")
                    seq_no = seq_no if isinstance(seq_no, int) else seq
                    anchors = [
                        correlate.sequence_anchor(scan.kind, scan.instance, seq_no)
                    ]
                group_index[(si, seq)] = len(call_groups)
                call_groups.append(anchors)
            elif item.evidence_type == "http_exchange":
                call_anchors = correlate.explicit_anchors(
                    item.correlation_hints.model_dump()
                )
                if call_anchors:
                    group_index[(si, seq)] = len(call_groups)
                    call_groups.append(call_anchors)
    resolutions = cmap.resolve_groups(call_groups) if call_groups else []

    for si, scan in enumerate(scans):
        for seq, item in enumerate(scan.records):
            etype = item.evidence_type
            if etype == "native_llm_call":
                lc, chosen, confidence = resolutions[group_index[(si, seq)]]
                p = item.payload
                record = _base_record(item, "llm_call", attempt_id, {
                    "protocol": None,
                    # call_role 不可得时写 unknown，不伪造 main——main 是
                    # 聚合/compaction 分析的业务事实
                    "call_role": p.call_role if p.call_role is not None else "unknown",
                    "model_requested": None,
                    "model_resolved": p.model,
                    "partial": False,
                    "request": (
                        p.request_summary.model_dump() if p.request_summary else {}
                    ),
                    "response": (
                        p.response_summary.model_dump() if p.response_summary else {}
                    ),
                    "usage": p.usage.model_dump() if p.usage else {},
                    "finish_reason": p.finish_reason,
                    "evidence_refs": [item.evidence_id],
                })
                record["correlation"]["logical_call_id"] = lc
                record["correlation"]["confidence"] = confidence
                conflict = call_conflicts.get((si, seq))
                if conflict is not None:
                    record["conflicts"].append(conflict)
                    result.conflicts += 1
                result.logical_calls.add(lc)
                # unmatched 口径统一：只有 confidence=="unmatched" 才
                # 计入 unmatched_calls；inferred（sequence anchor）是有 lc 的
                # 已匹配 call，进曲线，不算 unmatched。
                if confidence == "unmatched":
                    result.unmatched_calls += 1
                result.records.append(record)
            elif etype == "aggregate_usage":
                # 不伪造 call：进 manifest 对账（映射表）。
                result.aggregates.append({
                    "evidence_id": item.evidence_id,
                    "scope": item.payload.scope,
                    "usage": item.payload.usage.model_dump() if item.payload.usage else None,
                    "producer_event_type": item.payload.producer_event_type,
                })
            elif etype == "http_exchange":
                hop_anchor = _hop_anchor_for(item, seq)
                hop = ids.hop_id(
                    attempt_id=attempt_id,
                    source_instance=scan.instance,
                    hop_anchor=hop_anchor,
                )
                p = item.payload
                # body blob ref 由 source 在 policy=parsed/full 时写进 extensions
                # ；metadata/off 档根本不写 → 这里取到 None。blob
                # 下载另由 wire blob API 按 effective policy 二次门控。
                ext = item.extensions or {}
                record = _base_record(item, "http_exchange", attempt_id, {
                    "hop_id": hop,
                    # direction 取自 evidence：inbound=Env Server
                    # 工具请求，outbound=发往 provider；未声明时默认 outbound
                    # （反代/gateway 的历史语义）。
                    "direction": getattr(p, "direction", None) or "outbound",
                    "method": p.method,
                    "scheme": p.scheme,
                    "authority": p.authority,
                    "path": p.path,
                    "status_code": p.status_code,
                    "request_bytes": p.request_bytes,
                    "response_bytes": p.response_bytes,
                    "streamed": p.streamed,
                    # null 保留：不可观测 ≠ False，不补业务事实
                    "partial": p.partial,
                    "request_body_ref": ext.get("x-octagon.request-body-ref"),
                    "response_body_ref": ext.get("x-octagon.response-body-ref"),
                    # body 截断标记：从 evidence extensions 映射进 canonical，
                    # 否则前端看不到、会把残缺 blob 当完整正文。
                    "request_body_truncated": bool(
                        ext.get("x-octagon.request-body-truncated", False)
                    ),
                    "response_body_truncated": bool(
                        ext.get("x-octagon.response-body-truncated", False)
                    ),
                    # 跨协议 semantic summary：transport source 解析
                    # 明文 body 得到的 hash，落进 canonical 供跨 agent/source 对比。
                    # 无解析能力（如 native http_exchange）时为 None，不伪造。
                    "request_summary": (
                        p.request_summary.model_dump()
                        if getattr(p, "request_summary", None) else None
                    ),
                    "response_summary": (
                        p.response_summary.model_dump()
                        if getattr(p, "response_summary", None) else None
                    ),
                    # 反代只持久化 provider 返回的结构化 token 数字；metadata
                    # policy 下 response body 仍不落盘。无 native usage 的 agent
                    # 可由 aggregate 层用此字段兜底。
                    "usage": (
                        p.usage.model_dump()
                        if getattr(p, "usage", None) else {}
                    ),
                })
                # HTTP source 的耗时属于 canonical time envelope，而非 data。
                # started/finished 继续来自 evidence envelope；producer 实测的
                # duration 无损保留，避免短请求被时间戳精度吞掉。
                if p.timing is not None:
                    record["time"]["duration_ms"] = p.timing.duration_ms
                gi = group_index.get((si, seq))
                if gi is not None:
                    lc, _, confidence = resolutions[gi]
                    record["correlation"]["logical_call_id"] = lc
                    record["correlation"]["confidence"] = confidence
                    result.logical_calls.add(lc)
                else:
                    # 无显式 anchor：unmatched，不按时间强配。
                    # 统一口径：unmatched hop 也计入 unmatched_calls，
                    # 与 UI 的 unmatched 分组一致，banner 数字不再与分组矛盾。
                    record["correlation"]["confidence"] = "unmatched"
                    result.unmatched_calls += 1
                record["correlation"]["hop_id"] = hop
                result.hops.add(hop)
                result.records.append(record)
            elif etype == "stream_chunk":
                p = item.payload
                hop_anchor = p.hop_anchor or _hop_anchor_for(item, seq)
                hop = ids.hop_id(
                    attempt_id=attempt_id,
                    source_instance=scan.instance,
                    hop_anchor=hop_anchor,
                )
                record = _base_record(item, "stream_chunk", attempt_id, {
                    "hop_id": hop,
                    "sequence": p.sequence if p.sequence is not None else seq,
                    "relative_ms": p.relative_ms,
                    "event_type": p.event_type,
                    "bytes": p.bytes,
                    "content_hash": p.content_hash,
                    # null 保留：terminal/dropped_before 不可得时写 null
                    "is_terminal": p.terminal,
                    "dropped_before": p.dropped_before,
                })
                record["correlation"]["hop_id"] = hop
                result.records.append(record)
            elif etype == "mcp_frame":
                p = item.payload
                # tap 已算好类型/方向正确的配对 anchor——直接沿用，不在
                # canonical（丢了 id 类型）里重算。存进 record 供 pair_mcp_frames 分组。
                _mcp_anchor = (item.extensions or {}).get("x-octagon.mcp-paired-anchor")
                record = _base_record(item, "mcp_frame", attempt_id, {
                    # null 保留：direction 不可得时绝不伪造成
                    # client-to-server——那是业务事实不是默认值
                    "direction": p.direction,
                    "jsonrpc_id": p.jsonrpc_id,
                    "message_kind": p.message_kind,
                    "method": p.method,
                    "tool_name": p.tool_name,
                    "bytes": p.bytes,
                    "is_error": p.is_error,
                    "truncated": p.truncated,
                    "paired_record_id": None,
                    "_paired_anchor": _mcp_anchor,  # 内部：pair 后删
                    # 与 trajectory step 的关联（按 tool name/顺序，confidence
                    # 标注；不虚构相同 tool_call_id）。associate_mcp_trajectory 填。
                    "trajectory_step_id": None,
                    "association_confidence": None,
                })
                # jsonrpc_id 只进 MCP 配对空间，不生成 logical call 关联。
                mcp_frames.append(record)
                result.records.append(record)
            elif etype == "capture_event":
                p = item.payload
                instance = p.source_instance or scan.instance
                record = _base_record(item, "capture_event", attempt_id, {
                    "event": p.event,
                    "source_instance": instance,
                    "status": p.status,
                    "reason_code": p.reason_code,
                    "message": p.message,
                    "counters": p.counters or {},
                    "effective_capabilities": p.effective_capabilities or {},
                })
                result.records.append(record)
                # capture_event 驱动 manifest source status。
                # 归属只用 resolved instance，不做 kind fallback——否则一个
                # 实例的错误会扇出污染同 kind 的所有实例。
                stats = result.capture_stats.setdefault(
                    instance, {"errors": 0, "drops": 0, "counters": {}}
                )
                if p.event == "error":
                    stats["errors"] += 1
                elif p.event == "drop":
                    stats["drops"] += 1
                # counters 是 cumulative 语义（同名取 max，不重复相加）
                for name, value in (p.counters or {}).items():
                    prev = stats["counters"].get(name, 0)
                    stats["counters"][name] = max(int(prev), int(value))
            elif etype == "compaction_hint":
                # 相邻 call 分析在后续步骤；证据不足不伪造 context_compaction。
                result.compaction_hints.append({
                    "evidence_id": item.evidence_id,
                    "strategy": item.payload.strategy,
                    "confidence": item.payload.confidence,
                })
    correlate.pair_mcp_frames(mcp_frames)
    return result


def select_agent_run_calls(records: list[dict]) -> list[dict]:
    """agent token 聚合的输入：只取 phase=agent_run 的 llm_call。

    verification/unknown 等 phase 的 evidence 单独保留但不进聚合。
    """
    return [
        r
        for r in records
        if r.get("record_type") == "llm_call" and r.get("phase") == "agent_run"
    ]


# ---------- manifest --------------------------------------------------------


def _read_generation(manifest_path: Path) -> int:
    import json

    try:
        return int(json.loads(manifest_path.read_text()).get("generation", 0))
    except (OSError, ValueError, TypeError):
        return 0


def write_in_progress_manifest(
    *,
    data_path: Path,
    attempt_id: str,
    policy: EffectivePolicy,
    strict: bool,
    started_at: str | None,
    declared_sources: list[dict[str, Any]] | None = None,
    gaps: list[dict[str, str]] | None = None,
    phase_attribution: str = "explicit",
) -> None:
    """prepare 成功后落 in-progress manifest：崩溃后 startup recovery
    以它为扫描锚点，不长期伪装 in-progress。

    declared_sources/gaps/phase_attribution 必须持久化：否则崩溃后
    recovery 只能用空 declared 调 finalizer，「source 在建 spool 前失败」会被
    误判成 not-applicable 而不是 failed。
    """
    manifest_path = paths.manifest_file(data_path, attempt_id)
    writer.atomic_write_json(manifest_path, {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "attempt_id": attempt_id,
        "generation": _read_generation(manifest_path),
        "status": "in-progress",
        "strict": strict,
        "policy": {
            "requested": policy.requested,
            "effective": policy.effective,
            "downgrade_reason": policy.downgrade_reason,
        },
        "phase_attribution": phase_attribution,
        "declared_sources": list(declared_sources or []),
        "gaps": list(gaps or []),
        "started_at": started_at,
        "finished_at": None,
    })


def finalize_attempt(
    *,
    data_path: Path,
    attempt_id: str,
    policy: EffectivePolicy,
    strict: bool = False,
    declared_sources: list[dict[str, Any]] | None = None,
    gaps: list[dict[str, str]] | None = None,
    phase_attribution: str = "explicit",
    started_at: str | None = None,
    finished_at: str | None = None,
    recovered: bool = False,
) -> dict[str, Any]:
    """扫描 wire-sources → canonical wire.jsonl + correlation-map + manifest。

    ``declared_sources``：lifecycle 声明启用过的 source（kind/instance），
    用于区分「声明了但没有 spool」（source 没工作）与「压根没启用」。
    """
    data_path = Path(data_path)
    gaps = list(gaps or [])
    declared = list(declared_sources or [])

    with _finalize_lock(attempt_id):
        return _finalize_attempt_locked(
            data_path=data_path,
            attempt_id=attempt_id,
            policy=policy,
            strict=strict,
            declared=declared,
            gaps=gaps,
            phase_attribution=phase_attribution,
            started_at=started_at,
            finished_at=finished_at,
            recovered=recovered,
        )


def _finalize_attempt_locked(
    *,
    data_path: Path,
    attempt_id: str,
    policy: EffectivePolicy,
    strict: bool,
    declared: list[dict[str, Any]],
    gaps: list[dict[str, str]],
    phase_attribution: str,
    started_at: str | None,
    finished_at: str | None,
    recovered: bool,
) -> dict[str, Any]:
    scans = _scan_sources(data_path, attempt_id)
    cmap = correlate.CorrelationMap.load(
        paths.sources_dir(data_path, attempt_id) / "correlation-map.json", attempt_id
    )
    result = _map_evidence(scans, attempt_id, cmap)

    # not-applicable：policy off 或既无声明 source 也无任何 spool。
    if policy.effective == "off" or (not scans and not declared):
        manifest = _build_manifest(
            data_path, attempt_id, policy, strict, [], result, gaps,
            phase_attribution, started_at, finished_at,
            status_override="not-applicable",
        )
        _write_outputs(data_path, attempt_id, [], cmap, manifest, scans)
        return manifest

    # per-source 状态
    scanned_keys = {(s.kind, s.instance) for s in scans}
    source_entries: list[dict[str, Any]] = []
    failed_reasons: dict[str, str] = {}
    for g in gaps:
        if g.get("reason") == "source_start_failed":
            failed_reasons[g.get("instance") or g["field"]] = g["reason"]
            failed_reasons.setdefault(g["field"], g["reason"])
    # capture_event error/drop/counters 驱动 source status。
    # 严格按 resolved instance 归属，禁止 kind fallback 扇出。
    _empty = {"errors": 0, "drops": 0, "counters": {}}

    for scan in scans:
        stats = result.capture_stats.get(scan.instance, _empty)
        counters = stats["counters"]
        # counters 的 cumulative 汇报并入 completeness
        counter_dropped = int(counters.get("records_dropped", 0))
        counter_parse_errors = int(counters.get("parse_errors", 0))
        dropped = scan.dropped + stats["drops"] + counter_dropped
        parse_errors = scan.parse_errors + counter_parse_errors
        if scan.partial or scan.truncated_tail:
            status = "partial"
        elif parse_errors or dropped or stats["errors"]:
            status = "partial"
        else:
            # 干净关闭：records=0 即「零通信」，与 source 未工作可区分
            status = "complete"
        source_entries.append({
            "kind": scan.kind,
            "instance": scan.instance,
            "status": status,
            "capabilities": scan.capabilities,
            "records": len(scan.records),
            "dropped": dropped,
            "parse_errors": parse_errors,
            "errors": stats["errors"],
            "counters": counters,
            "truncated_tail": scan.truncated_tail,
            "failure_reason": None,
        })
    for d in declared:
        key = (d.get("kind", ""), d.get("instance", d.get("kind", "")))
        if key in scanned_keys:
            continue
        stats = result.capture_stats.get(key[1], _empty)
        source_entries.append({
            "kind": key[0],
            "instance": key[1],
            "status": "failed",
            "capabilities": {},
            "records": 0,
            "dropped": stats["drops"],
            "parse_errors": 0,
            "errors": stats["errors"],
            "counters": stats["counters"],
            "truncated_tail": False,
            "failure_reason": failed_reasons.get(key[1])
            or failed_reasons.get(key[0], "no-spool"),
        })

    # trajectory referential-integrity 必须在 manifest 状态计算之前完成，
    # 否则 dangling step 追加的 gap 赶不上 complete/partial 判定。
    traj_gaps = _reconcile_trajectory(data_path, attempt_id, cmap)
    gaps.extend(traj_gaps)
    # mcp_frame ↔ trajectory step 关联（按 tool name/顺序，标 confidence；
    # 不虚构相同 tool_call_id）。在 trajectory reconcile 之后，用最终 trajectory。
    _associate_mcp_trajectory(data_path, attempt_id, result.records)
    # 补充：把仍无 logical_call_id 的工具帧按时间就近挂到 provider 调用（codex 无
    # native call、mcp jsonrpc_id≠agent tool_call_id，union-find 关联不上时的 fallback）。
    # 只补空缺、不覆盖已有 lc，标 confidence=time-proximity 以示是就近推断非精确锚定。
    _associate_orphan_mcp_calls(result.records)
    # turn correlation。explicit（turn header extension）已在 _base_record
    # 投影；这里补 inferred（conversation.jsonl 时间窗口）兜底并按 logical call
    # 合并 turn。在 lc 全部确定后跑（logical-call 合并依赖最终 lc），在 compaction
    # 检测前跑（compaction record 需要 before/after turn，见 detect_compactions）。
    windows = correlate_turn.load_turn_windows(
        paths.attempt_dir(data_path, attempt_id)
    )
    correlate_turn.project_turn_correlation(result.records, windows)
    # compaction 检测——被动分型 + 显式 hint 融合去重。在
    # llm_call 有最终 lc 之后跑（before/after_call_id 引用 canonical lc）。
    # detect_compactions **消费**输入里已有的 explicit-hint context_compaction 并
    # 融合，返回**完整**的 context_compaction 集合——所以这里先剔除旧的
    # context_compaction、再用检测输出替换，避免显式 record 与融合结果重复。
    try:
        from backend.wire.compaction import detect_compactions

        detected = detect_compactions(result.records)
        result.records = [
            r for r in result.records if r.get("record_type") != "context_compaction"
        ]
        result.records.extend(detected)
    except Exception:
        logger.exception("compaction 检测失败（忽略，不影响 finalize）")
    # adapter 累计 usage 对账：native 聚合 vs adapter aggregate 差异写 conflict。
    recon = _reconcile_adapter_usage(result)
    if recon is not None:
        result.aggregates.append(recon)
        result.conflicts += 1
        gaps.append({"field": "token_usage", "reason": "adapter_native_mismatch"})

    # phase_attribution 从 canonical evidence 自动推导（-6）：
    # 独立进程（Env Server）写 phase=unknown 时 lifecycle 无从得知，finalizer
    # 见到任何非 capture_event 的 unknown-phase record 就降 degraded，不能只
    # 相信 lifecycle 传入的 explicit。
    if phase_attribution != "degraded" and any(
        r.get("phase") == "unknown" and r.get("record_type") != "capture_event"
        for r in result.records
    ):
        phase_attribution = "degraded"
        gaps.append({"field": "phase", "reason": "unknown_phase_evidence"})

    manifest = _build_manifest(
        data_path, attempt_id, policy, strict, source_entries, result, gaps,
        phase_attribution, started_at, finished_at,
        status_override="recovered" if recovered else None,
    )
    _write_outputs(data_path, attempt_id, result.records, cmap, manifest, scans)
    return manifest


def _reconcile_adapter_usage(result: "FinalizeResult") -> dict[str, Any] | None:
    """比较 native call 聚合与 adapter 累计 usage；不一致返回 conflict 记录。

    native 聚合 = phase=agent_run 的 llm_call usage 求和；adapter aggregate =
    scope="adapter" 的证据。二者 input/output 不一致时双保留（不静默
    修正）。无 adapter aggregate 或无 native call 时返回 None。
    """
    adapter_agg = next(
        (a for a in result.aggregates if a.get("scope") == "adapter"), None
    )
    if adapter_agg is None or not (adapter_agg.get("usage") or {}):
        return None
    native_in = native_out = 0
    seen = False
    for rec in result.records:
        if rec.get("record_type") == "llm_call" and rec.get("phase") == "agent_run":
            u = (rec.get("data") or {}).get("usage") or {}
            if isinstance(u.get("input_tokens"), int):
                native_in += u["input_tokens"]
                seen = True
            if isinstance(u.get("output_tokens"), int):
                native_out += u["output_tokens"]
                seen = True
    if not seen:
        return None
    a_usage = adapter_agg["usage"]
    a_in, a_out = a_usage.get("input_tokens"), a_usage.get("output_tokens")
    # 零与 null 区分：只有显式数值才参与比较（同源问题）。
    if (isinstance(a_in, int) and a_in != native_in) or (
        isinstance(a_out, int) and a_out != native_out
    ):
        return {
            "evidence_id": adapter_agg.get("evidence_id"),
            "scope": "reconciliation",
            "conflict": {
                "adapter": {"input_tokens": a_in, "output_tokens": a_out},
                "native": {"input_tokens": native_in, "output_tokens": native_out},
            },
            "producer_event_type": "usage_reconciliation",
        }
    return None


def _reconcile_trajectory(
    data_path: Path,
    attempt_id: str,
    cmap: correlate.CorrelationMap,
) -> list[dict[str, str]]:
    """两阶段 trajectory第二阶段：把 normalizer 先写的
    step.logical_call_id 更新为 correlator union 后的 canonical lc，并做
    referential-integrity check。返回需并入 manifest 的 gap（状态计算前）。

    normalizer 用「anchor 未桥接前的 lc」标记 step；finalizer union-find 合并
    split 集合后 canonical lc 可能变化——这里按 correlation-map 把 step 重指到
    最终 lc。trajectory 缺失时静默跳过（native source 未产出）。
    """
    traj_path = paths.attempt_dir(data_path, attempt_id) / "trajectory.json"
    if not traj_path.exists():
        return []
    try:
        traj = json.loads(traj_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    # 旧 lc → canonical lc：correlation-map 里同 anchor 现在指向的 lc。
    # normalizer 的 lc 由 anchor 直接派生，因此用 anchor 反查最终 lc。
    lc_remap: dict[str, str] = {}
    for anchor, final_lc in cmap.anchors.items():
        stale_lc = ids.logical_call_id(attempt_id=attempt_id, call_anchor=anchor)
        if stale_lc != final_lc:
            lc_remap[stale_lc] = final_lc
    unresolved = 0
    valid_lcs = set(cmap.anchors.values())
    for step in traj.get("steps", []):
        lc = step.get("logical_call_id")
        if lc is None:
            continue
        lc = lc_remap.get(lc, lc)
        step["logical_call_id"] = lc
        if lc not in valid_lcs:
            unresolved += 1
    writer.atomic_write_json(traj_path, traj)
    if unresolved:
        return [{"field": "trajectory", "reason": f"unresolved_step_lc:{unresolved}"}]
    return []


def _normalize_tool_name(name: Any) -> str | None:
    """归一工具名用于 MCP frame ↔ trajectory 关联。

    Claude/Codex 在 agent 侧把 MCP 工具存成 ``mcp__<server>__<tool>``，而 MCP 协议的
    ``tools/call.params.name`` 是裸 ``<tool>``。剥掉 ``mcp__<server>__`` 前缀取裸名，
    两侧才能对齐。非 mcp__ 前缀（普通内置工具）原样返回。"""
    if not isinstance(name, str) or not name:
        return None
    if name.startswith("mcp__"):
        # mcp__<server>__<tool>：取最后一个 __ 之后的裸工具名。
        parts = name.split("__")
        if len(parts) >= 3:
            return "__".join(parts[2:])  # tool 名本身可能含 __，保留其余全部
    return name


def _associate_mcp_trajectory(
    data_path: Path, attempt_id: str, records: list[dict[str, Any]]
) -> None:
    """把 mcp_frame 的 ``tools/call`` request 关联到 trajectory tool 步骤。

    规则：
    - 只关联 ``message_kind==request`` 且有 ``tool_name`` 的 mcp_frame（工具调用）；
    - **显式 tool ID 对齐**：mcp jsonrpc_id 不是 agent 的 tool_call_id，二者天然不同
      namespace，不虚构相同 ID——因此这里按 **tool name + 顺序**关联，confidence
      标 ``tool-name-order``；同名工具多次调用按出现顺序一一对应；
    - 无匹配 step → 不关联（confidence=None），不猜。

    原地在 mcp_frame record 写 ``trajectory_step_id`` / ``association_confidence``。
    trajectory 缺失时静默跳过。
    """
    traj_path = paths.attempt_dir(data_path, attempt_id) / "trajectory.json"
    if not traj_path.exists():
        return
    try:
        traj = json.loads(traj_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    # 按**归一后**的 tool_name 归组 trajectory tool 步骤，保序（同名多次调用一一
    # 对应）。归一：agent 侧 trajectory 存 mcp__<server>__<tool>，MCP
    # tools/call.params.name 是裸 <tool>——剥前缀后才对得上，否则真实场景永不关联。
    # 同时记 step 的 logical_call_id：CC 的 tool_use step 有 lc（与 native call 同源），
    # 可精确关联到 mcp_frame；codex 的 step 无 lc（aggregate-only）则留给时间就近兜底。
    steps_by_tool: dict[str, list[tuple[str, str | None]]] = {}
    for step in traj.get("steps", []):
        tname = _normalize_tool_name(step.get("tool_name"))
        sid = step.get("step_id")
        if tname and sid:
            steps_by_tool.setdefault(tname, []).append((sid, step.get("logical_call_id")))
    if not steps_by_tool:
        return
    # 消费指针：同名工具第 k 次 mcp 调用挂到第 k 个同名 step。
    cursor: dict[str, int] = {}
    for rec in records:
        if rec.get("record_type") != "mcp_frame":
            continue
        data = rec.get("data", {})
        if data.get("message_kind") != "request":
            continue
        tname = _normalize_tool_name(data.get("tool_name"))
        if not tname:
            continue
        candidates = steps_by_tool.get(tname)
        if not candidates:
            continue
        idx = cursor.get(tname, 0)
        if idx >= len(candidates):
            continue  # mcp 调用比 trajectory step 多：多出的不强配
        sid, step_lc = candidates[idx]
        data["trajectory_step_id"] = sid
        data["association_confidence"] = "tool-name-order"
        # step 有 lc（CC）→ 精确关联到 logical call（比时间就近更准，优先）。
        if step_lc:
            rec.setdefault("correlation", {})["logical_call_id"] = step_lc
        cursor[tname] = idx + 1


# provider LLM 端点（时间就近关联的锚点来源）。
_LLM_ENDPOINT_RE = re.compile(r"/(responses|messages|chat/completions)\b")


def _associate_orphan_mcp_calls(records: list[dict[str, Any]]) -> None:
    """把仍无 logical_call_id 的工具帧（mcp_frame request）按时间就近挂到最近的
    provider 调用。

    动机：codex 无 native llm_call，mcp jsonrpc_id 又与 agent tool_call_id 不同
    namespace，union-find 关联不上 → 工具帧 lc 全 None，UI 上工具调用与触发它的
    provider 调用割裂。这里做 fail-safe 的**时间就近**兜底：以 provider 调用
    （http_exchange 到 /responses|/messages|/chat）的时间点为锚，把每个 orphan 工具帧
    关联到时间距离最近的那次调用。

    严格边界（不误伤已有正确关联）：
    - **只补空缺**：mcp_frame 已有 logical_call_id 的一律不动（CC 经 union-find 已
      正确关联的帧不受影响）；
    - 只处理 ``message_kind==request`` 且有 ``tool_name`` 的工具帧；
    - 无任何 provider 调用锚点时什么都不做；
    - 标 ``association_confidence`` 为 ``time-proximity``（若该帧尚无 confidence），
      明示这是就近推断、非精确锚定——UI 可据此弱化展示。
    """
    # 收集 provider 调用锚点：(timestamp_ms, logical_call_id)。
    anchors: list[tuple[float, str]] = []
    for rec in records:
        if rec.get("record_type") != "http_exchange":
            continue
        path = (rec.get("data") or {}).get("path") or ""
        if not _LLM_ENDPOINT_RE.search(path):
            continue
        lc = (rec.get("correlation") or {}).get("logical_call_id")
        ts = _epoch_ms((rec.get("time") or {}).get("timestamp"))
        if lc and ts is not None:
            anchors.append((ts, lc))
    if not anchors:
        return
    anchors.sort()

    # 判定一条 record 是否是"孤儿工具调用"（可就近关联的对象）：
    # - mcp_frame 的 tools/call request（有 tool_name）；
    # - env-inbound 的工具回调 http_exchange（path 含 /tools/）——同一次工具调用的
    #   另一面（MCP server 的 tools.py 回调 Env Attempt Server），否则它会单独成一条
    #   unmatched 泳道，与对应的 provider 调用割裂。
    def _is_orphan_tool(rec: dict[str, Any]) -> bool:
        rt = rec.get("record_type")
        data = rec.get("data") or {}
        if rt == "mcp_frame":
            return data.get("message_kind") == "request" and bool(data.get("tool_name"))
        if rt == "http_exchange":
            return "/tools/" in (data.get("path") or "")
        return False

    for rec in records:
        if not _is_orphan_tool(rec):
            continue
        corr = rec.setdefault("correlation", {})
        if corr.get("logical_call_id"):
            continue  # 已关联：绝不覆盖
        ts = _epoch_ms((rec.get("time") or {}).get("timestamp"))
        if ts is None:
            continue
        # 取时间距离最近的 provider 调用。
        nearest = min(anchors, key=lambda a: abs(a[0] - ts))
        corr["logical_call_id"] = nearest[1]
        data = rec.get("data") or {}
        if not data.get("association_confidence"):
            data["association_confidence"] = "time-proximity"


def _epoch_ms(ts: str | None) -> float | None:
    """ISO8601 → epoch 毫秒；无效返回 None。"""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000.0
    except (ValueError, AttributeError):
        return None



def _axis_status(
    entries: list[dict[str, Any]],
    kind_prefixes: tuple[str, ...],
    *,
    absent: str = "not-observed",
) -> str:
    matched = [
        e for e in entries if str(e.get("kind", "")).startswith(kind_prefixes)
    ]
    if not matched:
        return absent
    if all(e["status"] == "failed" for e in matched):
        return "failed"
    if all(e["status"] == "complete" for e in matched):
        return "complete"
    return "partial"


def _build_manifest(
    data_path: Path,
    attempt_id: str,
    policy: EffectivePolicy,
    strict: bool,
    source_entries: list[dict[str, Any]],
    result: FinalizeResult,
    gaps: list[dict[str, str]],
    phase_attribution: str,
    started_at: str | None,
    finished_at: str | None,
    status_override: str | None = None,
) -> dict[str, Any]:
    blobs_dir = paths.blobs_dir(data_path, attempt_id)
    blob_files = list(blobs_dir.glob("sha256-*")) if blobs_dir.is_dir() else []

    if source_entries and all(s["status"] == "failed" for s in source_entries):
        status = "failed"
    elif (
        any(s["status"] != "complete" for s in source_entries)
        or gaps
        or phase_attribution == "degraded"
    ):
        status = "partial"
    else:
        status = "complete"
    if status_override == "not-applicable":
        status = status_override
    elif status_override == "recovered" and status in ("complete", "partial"):
        # recovered 只覆盖成功恢复的情形；全 source failed 仍如实报 failed，
        # 不能用 recovered 掩盖「capture 启动了但没工作」。
        status = status_override

    manifest_path = paths.manifest_file(data_path, attempt_id)
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "attempt_id": attempt_id,
        # generation 每次成功原子 finalize 单调递增（it5， API 依赖）
        "generation": _read_generation(manifest_path) + 1,
        "status": status,
        "strict": strict,
        "policy": {
            "requested": policy.requested,
            "effective": policy.effective,
            "downgrade_reason": policy.downgrade_reason,
        },
        "phase_attribution": phase_attribution,
        "sources": source_entries,
        "coverage": {
            # 四轴：按 source kind 前缀归轴聚合；该轴没有任何 source
            # 时如实报 not-observed / not-applicable，不冒充 complete。
            "agent_semantics": _axis_status(source_entries, ("native",)),
            "llm_transport": _axis_status(
                source_entries,
                ("http-proxy", "octagon-http", "llm-gateway", "responses-compat")
            ),
            "mcp": _axis_status(source_entries, ("mcp",)),
            "blade_hops": _axis_status(
                source_entries, ("blade",), absent="not-applicable"
            ),
            "correlated_calls": len(result.logical_calls),
            "unmatched_calls": result.unmatched_calls,
        },
        "totals": {
            "records": len(result.records),
            "logical_calls": len(result.logical_calls),
            "hops": len(result.hops),
            "blobs": len(blob_files),
            "bytes": sum(f.stat().st_size for f in blob_files),
            "conflicts": result.conflicts,
        },
        "aggregates": result.aggregates,
        "compaction_hints": result.compaction_hints,
        "gaps": gaps,
        "started_at": started_at,
        "finished_at": finished_at,
    }


def update_db_summary(db_path: Path, attempt_id: str, manifest: dict[str, Any]) -> None:
    """attempts 表 wire 摘要列：只写摘要，不搬 payload。幂等。"""
    import sqlite3

    sources = manifest.get("sources", [])
    errors = sum(
        int(s.get("parse_errors", 0))
        + int(s.get("dropped", 0))
        + int(s.get("errors", 0))
        for s in sources
    ) + len(manifest.get("gaps", []))
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE attempts SET wire_status=?, wire_record_count=?,"
            " wire_call_count=?, wire_error_count=?, wire_manifest_version=?"
            " WHERE id=?",
            (
                manifest.get("status", "not_available"),
                int(manifest.get("totals", {}).get("records", 0)),
                int(manifest.get("totals", {}).get("logical_calls", 0)),
                errors,
                manifest.get("schema_version"),
                attempt_id,
            ),
        )
        conn.commit()


def _write_outputs(
    data_path: Path,
    attempt_id: str,
    records: list[dict],
    cmap: correlate.CorrelationMap,
    manifest: dict[str, Any],
    scans: list[SourceScan],
) -> None:
    """写出顺序 wire → map → manifest；manifest 内嵌 wire 文件指纹。

    API 读取时用指纹（bytes/sha256）识别「新 wire + 旧 manifest」的中间窗口
    ：扫描前后任一指纹不匹配即 409，客户端从头刷新。
    """
    lines = [
        json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in records
    ]
    payload = ("\n".join(lines) + "\n").encode("utf-8") if lines else b""
    manifest["wire_file"] = {
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "records": len(records),
    }
    writer.atomic_write_bytes(paths.wire_file(data_path, attempt_id), payload)
    if scans:
        cmap.save(paths.sources_dir(data_path, attempt_id) / "correlation-map.json")
    writer.atomic_write_json(paths.manifest_file(data_path, attempt_id), manifest)
