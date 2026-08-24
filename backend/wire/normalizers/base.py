"""normalizer 公共类型（Phase 1）。

`NormalizeResult` 原定义在 `claude_code.py`，codex 靠
``from ...claude_code import NormalizeResult`` 复用——那是"Codex 依赖
Claude Code 模块"隐式耦合的一部分。搬到本模块后 CC/Codex/Blade 三个
normalizer 统一从这里 import，`codex.py` 不再有任何一行从
`claude_code.py` import。

`NormalizeResult.trajectory` 的类型是**序列化后的 dict**，不是
`trajectory_schema.Trajectory` 对象——`Trajectory` 的构造/校验/序列化全部
是 normalizer 内部步骤，`normalize()` 返回前必须已完成
`trajectory_to_dict()`；`runner.py` 对 `result.trajectory` 的两处用法
（`.get("steps")`、直接交给 `atomic_write_json`）因此不需要改。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from backend.wire import ids
from backend.wire.evidence import (
    CaptureEventEvidence,
    CaptureEventPayload,
    CorrelationHints,
    EvidenceProducer,
    EvidenceRawRef,
    EvidenceRedaction,
    EvidenceSource,
    EvidenceTime,
)


@dataclass
class NormalizeResult:
    evidence: list[Any] = field(default_factory=list)
    trajectory: dict[str, Any] = field(default_factory=dict)
    parse_errors: int = 0
    # 出错 raw event 的行号（精确定位；进 parse-error evidence 供 debug）
    error_lines: list[int] = field(default_factory=list)
    # raw 里见到的最后一个 timestamp（确定性 UTC，供派生 evidence 的 observed_at；
    # 用数据自身的时间既满足 UTC 约定又保持 rebuild 幂等）
    last_ts: str | None = None
    # 本次 normalize 实际读取的输入文件名（供 runner 的 parse-error evidence
    # raw_ref 指向正确来源——blade 走 blade_history.json 降级时不能再指
    # events.jsonl）。None 时 runner 回落 normalizer.raw_file 静态声明。
    raw_file: str | None = None

    def record_error(self, lineno: int) -> None:
        self.parse_errors += 1
        # 上限避免病态文件把行号列表撑爆
        if len(self.error_lines) < 100:
            self.error_lines.append(lineno)


class Normalizer(Protocol):
    """native normalizer 的公共契约（runner 按此调用）。

    `has_input()` 由各 normalizer 自己声明"这个 attempt_dir 有没有可解析
    输入"——runner 不再硬编码单一 RAW_FILE 文件名（那会让 blade 的
    blade_history.json 降级路径永远不可达）。
    """

    producer: str
    parser_version: str
    raw_file: str

    def has_input(self, attempt_dir: Path) -> bool: ...

    def normalize(self, *, attempt_id: str, attempt_dir: Path) -> NormalizeResult: ...


def trajectory_validation_evidence(
    *,
    attempt_id: str,
    producer: str,
    parser_version: str | None,
    errors: list[str],
    last_ts: str | None,
    raw_file: str,
) -> CaptureEventEvidence:
    """Trajectory.validate() 结构校验失败 → capture_event（fail-open）。

    校验错误不阻止 trajectory.json 写盘（照常写出全部已产出 step），但必须
    留痕进 spool——不静默丢弃。message 带前若干条错误内容供 debug。
    """
    preview = "; ".join(errors[:10])
    if len(errors) > 10:
        preview += f"; ...(+{len(errors) - 10})"
    return CaptureEventEvidence(
        evidence_id=ids.evidence_id(
            attempt_id=attempt_id,
            source_kind="native-event",
            source_instance="native-event",
            raw_ref=f"{raw_file}:trajectory-validation",
            producer_id="normalizer",
        ),
        attempt_id=attempt_id,
        phase="agent_run",
        source=EvidenceSource(kind="native-event", instance="native-event"),
        producer=EvidenceProducer(name=producer, version=parser_version),
        time=EvidenceTime(observed_at=last_ts or "", started_at=None, finished_at=None),
        raw_ref=EvidenceRawRef(kind="events-jsonl", file=raw_file, line=None),
        correlation_hints=CorrelationHints(),
        capabilities={},
        redaction=EvidenceRedaction(policy="metadata", status="applied"),
        errors=[],
        extensions={},
        payload=CaptureEventPayload(
            event="error",
            source_instance="native-event",
            status=None,
            reason_code="trajectory_validation_failed",
            message=preview,
            counters={"validation_errors": len(errors)},
            effective_capabilities=None,
        ),
    )
