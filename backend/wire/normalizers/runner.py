"""Native normalizer 运行入口。

把某个 normalizer 的输出写成 source spool（``wire-sources/native-event.jsonl``）
+ 原子写 ``trajectory.json``。lifecycle 的 ``agent_result`` 与离线 rebuild
共用它，因此 normalize 逻辑只有一处。

agent registry：按 agent_name 选 normalizer。未知 agent 返回 None（无 native
source，不是错误）。

契约：

- raw events 缺失/无内容 → 不产出、**保留**已有派生数据（返回 False），
  绝不把已有 wire 重建成「完整但零调用」；
- parse_errors 写成 ``capture_event`` 进 spool（进 manifest source status /
  completeness），不只留在内存；
- staging：先写 ``.rebuild`` 临时文件校验，再原子替换（rebuild 侧）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.wire import ids, paths, spool, writer
from backend.wire.evidence import (
    AggregateUsageEvidence,
    AggregateUsagePayload,
    CaptureEventEvidence,
    CaptureEventPayload,
    CorrelationHints,
    EvidenceProducer,
    EvidenceRawRef,
    EvidenceRedaction,
    EvidenceSource,
    EvidenceTime,
    UsagePayload,
)
from backend.wire.normalizers.blade import BladeNormalizer
from backend.wire.normalizers.claude_code import ClaudeCodeNormalizer
from backend.wire.normalizers.codex import CodexNormalizer
from backend.wire.normalizers.dsh import DshNormalizer
from backend.wire.normalizers.opencode_family import OpencodeFamilyNormalizer
from backend.wire.trajectory_schema import TRAJECTORY_SCHEMA_VERSION

_NORMALIZERS = {
    "claude-code": ClaudeCodeNormalizer,
    "codex": CodexNormalizer,
    "blade-agent": BladeNormalizer,
    "dsh": DshNormalizer,
}

# opencode 与其 fork mimo 事件流逐字段同构，共用一个 normalizer 实现，
# 只用 producer 区分身份（与 adapter 层同一取舍）。
_FAMILY_NORMALIZERS = {
    "opencode": "opencode",
    "mimo-code": "mimo-code",
}


def normalizer_for(agent_name: str):
    cls = _NORMALIZERS.get(agent_name)
    if cls:
        return cls()
    producer = _FAMILY_NORMALIZERS.get(agent_name)
    if producer:
        return OpencodeFamilyNormalizer(producer=producer)
    return None


def _derived_ts(last_ts: str | None) -> str:
    """派生 evidence 的 observed_at：优先用 raw 数据里最后一个真实 UTC 时间戳
    （确定性、满足 UTC 约定、保持 rebuild 幂等）；raw 无时间戳时回退固定 epoch
    UTC（有效 ISO，而非空串）。"""
    if last_ts:
        return last_ts
    return "1970-01-01T00:00:00.000Z"


def _parse_error_evidence(
    attempt_id: str, parse_errors: int, error_lines: list[int], last_ts: str | None,
    producer: tuple[str, str | None], raw_file: str,
) -> CaptureEventEvidence:
    """把 normalizer 的 parse-error 数汇报成 capture_event（cumulative counter）。

    finalizer 据此把 native-event source 标 partial 并计入 wire_error_count——
    parse-error completeness 落进持久化契约。message 带出错行号供
    精确定位；observed_at 用 raw 最后时间戳（确定性 UTC）。
    """
    line_hint = ",".join(str(n) for n in error_lines[:20]) if error_lines else ""
    return CaptureEventEvidence(
        evidence_id=ids.evidence_id(
            attempt_id=attempt_id, source_kind="native-event",
            source_instance="native-event",
            raw_ref="native-normalize:parse-errors", producer_id="normalizer",
        ),
        attempt_id=attempt_id,
        phase="agent_run",
        source=EvidenceSource(kind="native-event", instance="native-event"),
        producer=EvidenceProducer(name=producer[0], version=producer[1]),
        time=EvidenceTime(observed_at=_derived_ts(last_ts), started_at=None, finished_at=None),
        raw_ref=EvidenceRawRef(
            kind="events-jsonl", file=raw_file,
            line=error_lines[0] if error_lines else None,
        ),
        correlation_hints=CorrelationHints(),
        capabilities={},
        redaction=EvidenceRedaction(policy="metadata", status="applied"),
        errors=[],
        extensions={},
        payload=CaptureEventPayload(
            event="error",
            source_instance="native-event",
            status=None,
            reason_code="parse_failed",
            message=f"parse errors at lines: {line_hint}" if line_hint else None,
            counters={"parse_errors": parse_errors},
            effective_capabilities=None,
        ),
    )


def _adapter_aggregate_evidence(
    attempt_id: str, usage: dict[str, Any], last_ts: str | None,
    producer: tuple[str, str | None],
) -> AggregateUsageEvidence:
    """adapter 累计 usage 作为 ``scope="adapter"`` 的 aggregate evidence：
    finalize 拿它与 native 聚合对账，差异写 manifest conflict。"""
    return AggregateUsageEvidence(
        evidence_id=ids.evidence_id(
            attempt_id=attempt_id, source_kind="native-event",
            source_instance="native-event",
            raw_ref="native-normalize:adapter-usage", producer_id="adapter",
        ),
        attempt_id=attempt_id,
        phase="agent_run",
        source=EvidenceSource(kind="native-event", instance="native-event"),
        producer=EvidenceProducer(name=producer[0], version=producer[1]),
        time=EvidenceTime(observed_at=_derived_ts(last_ts), started_at=None, finished_at=None),
        raw_ref=None,
        correlation_hints=CorrelationHints(),
        capabilities={},
        redaction=EvidenceRedaction(policy="metadata", status="applied"),
        errors=[],
        extensions={},
        payload=AggregateUsagePayload(
            scope="adapter",
            usage=UsagePayload(
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
                cache_read_tokens=usage.get("cache_read_tokens"),
                cache_write_tokens=usage.get("cache_write_tokens"),
                reasoning_tokens=usage.get("reasoning_tokens"),
                estimated=None,
            ),
            producer_event_type="adapter_result",
        ),
    )


def run_native_normalizer(
    *, agent_name: str, attempt_id: str, data_path: Path,
    adapter_usage: dict[str, Any] | None = None,
) -> bool:
    """跑 native normalizer，写 native-event spool + trajectory.json。

    返回 True 表示产出了 native source。无对应 normalizer、或 raw events 缺失/
    无任何 call evidence 时返回 False 且**不触碰**已有派生数据。
    幂等：evidence ID 由 raw ref 派生；spool/trajectory 走临时文件 + 原子替换。
    """
    normalizer = normalizer_for(agent_name)
    if normalizer is None:
        return False
    data_path = Path(data_path)
    attempt_dir = paths.attempt_dir(data_path, attempt_id)
    # 输入存在性由 normalizer 自己声明：
    # 不再硬编码单一 RAW_FILE 文件名——那会让 blade 的 blade_history.json
    # 降级路径永远不可达。CC/Codex 的 has_input 与原判断等价。
    if not normalizer.has_input(attempt_dir):
        # raw 缺失：保留旧产物，不重建成零调用
        return False

    result = normalizer.normalize(attempt_id=attempt_id, attempt_dir=attempt_dir)
    step_count = len(result.trajectory.get("steps", []))
    if not result.evidence and result.parse_errors == 0 and step_count == 0:
        # 无证据、无 parse error、无 trajectory step：raw 为空/无可解析事件，
        # 保留旧产物。只要观察到 trajectory step（如 Codex 中断，有
        # item 但无 turn.completed）就要写 trajectory + spool，不整体丢弃。
        return False
    # 派生 evidence（parse-error/adapter aggregate）的 producer 反映实际 agent，
    # 不硬编码 claude-code（溯源正确）。
    producer = (
        getattr(normalizer, "producer", "unknown"),
        getattr(normalizer, "parser_version", None),
    )

    # native-event spool：staging（.rebuild）→ 校验 → 原子替换（不先删正式档）。
    spool_final = paths.source_spool_file(data_path, attempt_id, "native-event")
    staging = spool_final.with_name(spool_final.name + ".rebuild")
    staging_partial = staging.with_name(staging.name + ".partial")
    # 启动前清理上次失败留下的 staging final+partial：SpoolWriter
    # 以 append 打开 .partial，残留会被继续追加导致重复行/重复计 token。
    staging.unlink(missing_ok=True)
    staging_partial.unlink(missing_ok=True)

    n_evidence = len(result.evidence)
    if result.parse_errors:
        n_evidence += 1
    if adapter_usage:
        n_evidence += 1
    try:
        writer_ = spool.SpoolWriter(staging, expected_attempt_id=attempt_id)
        try:
            for ev in result.evidence:
                writer_.append(ev)
            # parse-error completeness 进 spool
            if result.parse_errors:
                writer_.append(_parse_error_evidence(
                    attempt_id, result.parse_errors, result.error_lines,
                    result.last_ts, producer,
                    # 实际读取的输入文件优先（blade 降级读 blade_history.json
                    # 时不能指向 events.jsonl），未声明回落静态 raw_file
                    result.raw_file or getattr(normalizer, "raw_file", "events.jsonl"),
                ))
            # adapter 累计 usage 对账证据
            if adapter_usage:
                writer_.append(
                    _adapter_aggregate_evidence(
                        attempt_id, adapter_usage, result.last_ts, producer
                    )
                )
            staged_path = writer_.close()  # .rebuild.partial → .rebuild
        except BaseException:
            # 异常路径：关闭句柄并删除半成品 staging，绝不留残留
            writer_.abandon()
            raise
        # 校验：可完整读回、行数吻合、evidence ID 无重复。
        read_back = spool.read_spool(staged_path)
        ids_seen = [r.get("evidence_id") for r in read_back.records]
        if (
            read_back.partial
            or read_back.truncated_tail
            or read_back.parse_errors
            or len(read_back.records) != n_evidence
            or len(set(ids_seen)) != len(ids_seen)
        ):
            raise RuntimeError(
                f"native spool staging 校验失败: {attempt_id}"
                f"（records={len(read_back.records)} expected={n_evidence}"
                f" dup_ids={len(ids_seen) - len(set(ids_seen))}）"
            )
        # 正式档的旧 partial 也清掉，再原子替换。
        spool_final.with_name(spool_final.name + ".partial").unlink(missing_ok=True)
        staged_path.replace(spool_final)
    except BaseException:
        staging.unlink(missing_ok=True)
        staging_partial.unlink(missing_ok=True)
        raise

    # trajectory.json 原子写（writer.atomic_write_json 已是临时+rename）
    writer.atomic_write_json(
        attempt_dir / "trajectory.json",
        result.trajectory
        or {
            "schema_version": TRAJECTORY_SCHEMA_VERSION,
            "attempt_id": attempt_id,
            "steps": [],
        },
    )
    return True
