"""opencode / mimo-code native normalizer（token_cost_accounting）。

输入：attempt 的 ``events.jsonl``（``<cli> run --format json`` stdout 逐行）。
输出：``aggregate_usage`` WireEvidence + ``trajectory.json``。

**一个 normalizer 服务两个 producer**：mimo-code 是 opencode 的下游 fork，
事件流逐字段同构（同 `part` 结构、同 `sessionID`、同 `tokens` 形状），差异只在
可执行文件名与环境变量前缀——那些在 adapter 层已消化，到 events.jsonl 这层
完全一致。故用 `producer` 构造参数区分身份，解析逻辑共用（与
`backend/adapters/opencode_family.py` 同一取舍）。

事件形状（实测 49，opencode 1.18.5 / mimo 0.1.9，2026-07-27）：

    {"type":"step_start","sessionID":"ses_…","part":{"type":"step-start",…}}
    {"type":"text","sessionID":"ses_…","part":{"type":"text","text":"…"}}
    {"type":"reasoning","sessionID":"ses_…","part":{"type":"reasoning","text":"…"}}
    {"type":"tool_use","sessionID":"ses_…","part":{"type":"tool","tool":"bash",
      "callID":"call_…","state":{"status":"completed","input":{…},"output":"…"}}}
    {"type":"step_finish","sessionID":"ses_…","part":{"type":"step-finish",
      "tokens":{"total":N,"input":N,"output":N,"reasoning":N,
                "cache":{"read":N,"write":N}}}}

与 codex 的关键差异：**usage 是逐 step 的**（每个 `step_finish` 带一份
`tokens`），不是整个 attempt 一条。但 `tokens` 是**该 step 的量**而非累计
快照，故这里逐条求和；`call_boundary` 仍声明 `aggregate-only`——step 边界不
等价于 API 调用边界（一个 step 可能含多次工具往返），不伪装成逐调用曲线。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from backend.wire import hashing, ids
from backend.wire.evidence import (
    AggregateUsageEvidence,
    AggregateUsagePayload,
    CorrelationHints,
    EvidenceProducer,
    EvidenceRawRef,
    EvidenceRedaction,
    EvidenceSource,
    EvidenceTime,
    UsagePayload,
)
from backend.wire.normalizers.base import (
    NormalizeResult,
    trajectory_validation_evidence,
)
from backend.wire.trajectory_schema import (
    TRAJECTORY_SCHEMA_VERSION,
    Trajectory,
    TrajectoryStep,
    empty_trajectory,
    trajectory_to_dict,
)

PARSER_VERSION = "opencode-family-normalizer-v1"
SOURCE_KIND = "native-event"
SOURCE_INSTANCE = "native-event"
RAW_FILE = "events.jsonl"

# step 边界 ≠ API 调用边界（一个 step 可含多次工具往返），不伪装逐调用曲线。
CALL_BOUNDARY = "aggregate-only"

OBSERVED_CLI_VERSIONS = "opencode 1.18.5 / mimo 0.1.9"
CAPABILITIES: dict[str, Any] = {
    "call_boundary": CALL_BOUNDARY,
    # 两者都有 `task` 工具可派生子 agent，但事件流**不带**子 agent 归属字段
    # （实测 mimo 跑出 3 次 task 调用，事件里无 parent/child 标识），故子
    # agent 身份不可得。schema 变化时下面的断言测试会失败提醒。
    "subagent_identity": False,
    "subagent_identity_basis": (
        f"事件流无子 agent 归属字段（{OBSERVED_CLI_VERSIONS} 实测）"
    ),
}

# part.type → trajectory step kind。step-start/step-finish 是框架边界，
# 不建 step（它们不是 agent 行为）。
_PART_KIND = {
    "text": "assistant",
    "reasoning": "thinking",
    "tool": "tool_call",
}


def _iter_events(path: Path) -> Iterator[tuple[int, dict[str, Any] | None]]:
    with path.open("r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                yield lineno, json.loads(raw)
            except json.JSONDecodeError:
                yield lineno, None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _usage_from_tokens(tokens: dict[str, Any]) -> UsagePayload:
    """`part.tokens` → 统一 UsagePayload。

    形状与 OpenAI/Anthropic 都不同：裸 `input`/`output`，cache 是嵌套对象。
    缺字段写 None（不可得）而非 0——区分零与不可得是计价正确性的前提。
    """
    cache = tokens.get("cache")
    cache = cache if isinstance(cache, dict) else {}
    output = _int_or_none(tokens.get("output"))
    reasoning = _int_or_none(tokens.get("reasoning"))
    return UsagePayload(
        input_tokens=_int_or_none(tokens.get("input")),
        output_tokens=(
            output + reasoning
            if output is not None and reasoning is not None
            else output if output is not None
            else reasoning
        ),
        cache_read_tokens=_int_or_none(cache.get("read")),
        cache_write_tokens=_int_or_none(cache.get("write")),
        reasoning_tokens=reasoning,
        estimated=False,
    )


def _merge_usage(base: dict[str, Any] | None, delta: dict[str, Any]) -> dict[str, Any]:
    """逐 step 求和。None 视作"不可得"：None + N = N，None + None = None。

    `tokens` 是**该 step 的量**（实测两轮各 10556 而非 10556→21112 的累计
    快照），故求和而非取 max——取 max 会把多 step 的消耗压成最大的那一个。
    """
    if base is None:
        return dict(delta)
    out: dict[str, Any] = {}
    for key in set(base) | set(delta):
        a, b = base.get(key), delta.get(key)
        out[key] = b if a is None else (a if b is None else a + b)
    return out


@dataclass
class _State:
    steps: list[TrajectoryStep] = field(default_factory=list)
    step_seq: int = 0
    session_id: str | None = None
    session_ids: list[str] = field(default_factory=list)
    usage: dict[str, Any] | None = None
    usage_line: int | None = None
    usage_ts: str | None = None
    last_ts: str | None = None


class OpencodeFamilyNormalizer:
    """opencode 与其 fork 共用的 normalizer；`producer` 区分身份。"""

    parser_version = PARSER_VERSION
    raw_file = RAW_FILE

    def __init__(self, producer: str = "opencode") -> None:
        self.producer = producer

    def has_input(self, attempt_dir: Path) -> bool:
        return (Path(attempt_dir) / RAW_FILE).exists()

    def normalize(self, *, attempt_id: str, attempt_dir: Path) -> NormalizeResult:
        attempt_dir = Path(attempt_dir)
        events_path = attempt_dir / RAW_FILE
        result = NormalizeResult()
        if not events_path.exists():
            result.trajectory = empty_trajectory(attempt_id)
            return result

        st = _State()
        for lineno, event in _iter_events(events_path):
            if event is None or not isinstance(event, dict):
                result.record_error(lineno)
                continue
            # adapter 把无法解析的 stdout 包成 {"raw_line": …}：合法 JSON 但
            # 非已知事件，计 parse error（与 codex/claude 对齐）。
            if "raw_line" in event and event.get("type") is None:
                result.record_error(lineno)
                continue
            ts = event.get("timestamp")
            # 这两家的 timestamp 是**毫秒 epoch 整数**，不是 ISO 串；
            # evidence 的 observed_at 要 ISO，这里统一转换。
            ts = _to_iso(ts)
            if ts:
                st.last_ts = ts
            try:
                self._apply_event(event, lineno, attempt_id, st, ts)
            except Exception:
                result.record_error(lineno)
                continue

        if st.usage is not None:
            spans_multiple = len(st.session_ids) > 1
            result.evidence.append(
                self._aggregate_evidence(
                    attempt_id, st.usage, st.usage_line, st.usage_ts,
                    None if spans_multiple else st.session_id,
                )
            )
            if spans_multiple:
                result.evidence.append(
                    self._session_broken_evidence(
                        attempt_id, st.session_ids, st.last_ts,
                    )
                )
        elif st.steps:
            # 有行为但没 step_finish（被 timeout 截断等）：明确报 usage gap，
            # 不伪造 aggregate。
            result.evidence.append(
                self._usage_gap_evidence(attempt_id, st.last_ts, st.session_id)
            )

        trajectory = Trajectory(
            schema_version=TRAJECTORY_SCHEMA_VERSION,
            attempt_id=attempt_id,
            steps=tuple(st.steps),
            producer=self.producer,
        )
        v_errors = trajectory.validate()
        if v_errors:
            result.evidence.append(trajectory_validation_evidence(
                attempt_id=attempt_id, producer=self.producer,
                parser_version=PARSER_VERSION, errors=v_errors,
                last_ts=st.last_ts, raw_file=RAW_FILE,
            ))
        result.trajectory = trajectory_to_dict(trajectory)
        return result

    # ---------- 事件分派 ----------

    def _apply_event(
        self, event: dict[str, Any], lineno: int, attempt_id: str,
        st: "_State", ts: str | None,
    ) -> None:
        sid = event.get("sessionID")
        if isinstance(sid, str) and sid:
            if sid not in st.session_ids:
                st.session_ids.append(sid)
            # 保留**首个** session ID：它是这个 attempt 的身份，用最后一个
            # 会把先前会话的消耗错误归给新会话（与 codex 同）。
            st.session_id = st.session_id or sid

        part = event.get("part")
        if not isinstance(part, dict):
            return
        ptype = part.get("type")

        if ptype == "step-finish":
            tokens = part.get("tokens")
            if tokens is None:
                return  # 无 usage 的 step_finish：不是错误，跳过
            if not isinstance(tokens, dict):
                raise ValueError("step-finish.tokens 非 object")
            payload = _usage_from_tokens(tokens)
            st.usage = _merge_usage(st.usage, {
                "input_tokens": payload.input_tokens,
                "output_tokens": payload.output_tokens,
                "cache_read_tokens": payload.cache_read_tokens,
                "cache_write_tokens": payload.cache_write_tokens,
                "reasoning_tokens": payload.reasoning_tokens,
            })
            st.usage_line = lineno
            st.usage_ts = ts
            return

        kind = _PART_KIND.get(ptype)
        if kind is None:
            return  # step-start / patch / snapshot 等：无行为语义
        self._push_step(part, kind, lineno, attempt_id, st, ts)

    def _push_step(
        self, part: dict[str, Any], kind: str, lineno: int, attempt_id: str,
        st: "_State", ts: str | None,
    ) -> None:
        tool_id = None
        tool_name = None
        if kind == "tool_call":
            raw_id = part.get("callID")
            raw_name = part.get("tool")
            tool_id = raw_id if isinstance(raw_id, str) else None
            tool_name = raw_name if isinstance(raw_name, str) else None
        content_hash, content_bytes = _part_semantic_hash(part, kind, tool_name)

        st.step_seq += 1
        st.steps.append(TrajectoryStep(
            step_id=ids.trajectory_step_id(
                attempt_id=attempt_id,
                step_anchor=f"{RAW_FILE}:{lineno}:{part.get('type')}:{part.get('id')}",
            ),
            sequence=st.step_seq,
            timestamp=ts,
            kind=kind,
            producer_event_refs=({"file": RAW_FILE, "line": lineno},),
            tool_call_id=tool_id,
            tool_name=tool_name,
            # aggregate-only：无逐调用 lc，step 不挂 logical_call_id
            logical_call_id=None,
            content_hash=content_hash,
            content_bytes=content_bytes,
            # 事件流无子 agent 归属字段（见 CAPABILITIES），恒 main
            agent_id="main", parent_agent_id=None,
        ))

    # ---------- evidence ----------

    def _aggregate_evidence(
        self, attempt_id: str, usage: dict[str, Any],
        line: int | None, ts: str | None, session_id: str | None,
    ) -> AggregateUsageEvidence:
        return AggregateUsageEvidence(
            evidence_id=ids.evidence_id(
                attempt_id=attempt_id, source_kind=SOURCE_KIND,
                source_instance=SOURCE_INSTANCE,
                raw_ref=f"{RAW_FILE}:{line}", producer_id="step-aggregate",
            ),
            attempt_id=attempt_id,
            phase="agent_run",
            source=EvidenceSource(kind=SOURCE_KIND, instance=SOURCE_INSTANCE),
            producer=EvidenceProducer(name=self.producer, version=PARSER_VERSION),
            time=EvidenceTime(observed_at=ts or "", started_at=None, finished_at=None),
            raw_ref=EvidenceRawRef(kind="events-jsonl", file=RAW_FILE, line=line),
            correlation_hints=CorrelationHints(producer_session_id=session_id),
            capabilities=dict(CAPABILITIES),
            redaction=EvidenceRedaction(policy="metadata", status="applied"),
            errors=[],
            extensions={},
            payload=AggregateUsagePayload(
                scope="attempt",
                usage=UsagePayload(
                    input_tokens=usage.get("input_tokens"),
                    output_tokens=usage.get("output_tokens"),
                    cache_read_tokens=usage.get("cache_read_tokens"),
                    cache_write_tokens=usage.get("cache_write_tokens"),
                    reasoning_tokens=usage.get("reasoning_tokens"),
                    estimated=False,
                ),
                # 保留 producer 原始事件类型：不把逐 step 求和
                # 伪装成某个"总计"事件——它确实来自多条 step-finish。
                producer_event_type="step-finish",
            ),
        )

    def _usage_gap_evidence(
        self, attempt_id: str, ts: str | None, session_id: str | None
    ):
        from backend.wire.evidence import CaptureEventEvidence, CaptureEventPayload

        return CaptureEventEvidence(
            evidence_id=ids.evidence_id(
                attempt_id=attempt_id, source_kind=SOURCE_KIND,
                source_instance=SOURCE_INSTANCE,
                raw_ref=f"{self.producer}:usage-gap", producer_id="normalizer",
            ),
            attempt_id=attempt_id,
            phase="agent_run",
            source=EvidenceSource(kind=SOURCE_KIND, instance=SOURCE_INSTANCE),
            producer=EvidenceProducer(name=self.producer, version=PARSER_VERSION),
            time=EvidenceTime(observed_at=ts or "", started_at=None, finished_at=None),
            raw_ref=EvidenceRawRef(kind="events-jsonl", file=RAW_FILE, line=None),
            correlation_hints=CorrelationHints(producer_session_id=session_id),
            capabilities={**CAPABILITIES, "usage": "not-observed"},
            redaction=EvidenceRedaction(policy="metadata", status="applied"),
            errors=[],
            extensions={},
            payload=CaptureEventPayload(
                event="error", source_instance=SOURCE_INSTANCE, status=None,
                reason_code="usage_not_observed",
                message="观察到行为但无 step-finish tokens",
                counters=None, effective_capabilities=None,
            ),
        )

    def _session_broken_evidence(
        self, attempt_id: str, session_ids: list[str], ts: str | None
    ):
        """一个 attempt 出现多个 sessionID：resume 落到了别的会话。"""
        from backend.wire.evidence import CaptureEventEvidence, CaptureEventPayload

        return CaptureEventEvidence(
            evidence_id=ids.evidence_id(
                attempt_id=attempt_id, source_kind=SOURCE_KIND,
                source_instance=SOURCE_INSTANCE,
                raw_ref=f"{self.producer}:session-continuity",
                producer_id="normalizer",
            ),
            attempt_id=attempt_id,
            phase="agent_run",
            source=EvidenceSource(kind=SOURCE_KIND, instance=SOURCE_INSTANCE),
            producer=EvidenceProducer(name=self.producer, version=PARSER_VERSION),
            time=EvidenceTime(observed_at=ts or "", started_at=None, finished_at=None),
            raw_ref=EvidenceRawRef(kind="events-jsonl", file=RAW_FILE, line=None),
            correlation_hints=CorrelationHints(producer_session_id=None),
            capabilities=dict(CAPABILITIES),
            redaction=EvidenceRedaction(policy="metadata", status="applied"),
            errors=[],
            extensions={},
            payload=CaptureEventPayload(
                event="error", source_instance=SOURCE_INSTANCE, status=None,
                reason_code="session_continuity_broken",
                message=(
                    "一个 attempt 出现多个 sessionID："
                    + ", ".join(session_ids[:5])
                ),
                counters={"session_count": len(session_ids)},
                effective_capabilities=None,
            ),
        )


def _part_semantic_hash(
    part: dict[str, Any], kind: str, tool_name: str | None
) -> tuple[str | None, int | None]:
    """part → 公共 semantic IR + hash。

    **跨 producer 可比**：与 CC / codex 用同一 `messages` IR 形状，这样同一
    份工作在不同 agent 上的内容哈希才有可比性。取不到内容时返回
    (None, None)，不伪造 hash。
    """
    if kind == "tool_call":
        state = part.get("state")
        state = state if isinstance(state, dict) else {}
        return hashing.part_semantic_hash([{
            "type": "tool_call",
            "name": tool_name or "tool",
            "arguments": state.get("input"),
        }])
    text = part.get("text")
    if not isinstance(text, str) or not text:
        return None, None
    return hashing.part_semantic_hash([{"type": "text", "text": text}])


def _to_iso(value: Any) -> str | None:
    """opencode 系的 timestamp 是毫秒 epoch 整数 → ISO UTC。

    evidence 的 observed_at 约定是 ISO UTC 串；直接塞整数会让下游时间比较
    与 rebuild 幂等性失效。已是字符串的原样返回（防御未来 schema 变化）。
    """
    if isinstance(value, str):
        return value or None
    if isinstance(value, (int, float)):
        from datetime import datetime, timezone

        try:
            return (
                datetime.fromtimestamp(value / 1000, tz=timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
        except (OSError, OverflowError, ValueError):
            return None
    return None
