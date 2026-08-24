"""dsh native normalizer（DeepSeek Harness SDK 事件流）。

输入：attempt 的 ``events.jsonl``（`RunResult.events` 逐行 + turn 扩展）。
输出：``aggregate_usage`` WireEvidence + ``trajectory.json``。

事件形状（实测 2026-08-13，`deepseek-harness-sdk==0.1.0rc6`，本机 macOS
arm64，上游 OpenRouter；样本见 ``tests/fixtures/dsh/``）：

    {"type":"tool/call","seq":7,"time":1786636455579,
     "data":{"turn":1,"step":1,"callId":"call_…","name":"write",
             "arguments":"{\\"file_path\\":…}"}}
    {"type":"tool/result","data":{"turn":1,"step":1,
     "message":{"content":[{"type":"tool-result","toolCallId":"call_…",
                            "isError":false,"content":[…]}]},
     "error":{"name":"FsError","code":"FS_NOT_FOUND"}}}
    {"type":"assistant/message","data":{"turn":1,"step":1,
     "message":{"content":[{"type":"text"|"reasoning"|"tool-call",…}]},
     "usage":{"inputTokens":N,"outputTokens":N,…}}}

与其余五家的三处关键差异：

1. **usage 挂在 ``assistant/message`` 上**，没有独立的 usage 事件——模型输出
   与它的计费同条记录。逐条求和（每条是该 step 的量，不是累计快照）。
2. **``tool/result`` 是独立事件且带 ``toolCallId``**，与 CC 同构、与
   codex/opencode 不同（后者把调用与结果打包在一个事件里）。故这里**分别**
   建 ``tool_call`` 与 ``tool_result`` 两个 step，配对校验走
   ``_check_cc_tool_call_pairing``（见 trajectory_schema）。
3. **命名带斜杠命名空间**（``tool/call``），且 block 层用连字符
   （``tool-call``/``tool-result``）。两层字段名不同：事件层是 ``callId``，
   block 层是 ``id``/``toolCallId``。

⚠️ 走 ``llm-pi-ai`` route 时 usage **只有 input/output 两维**——pi-ai 的
``mapUsage()`` 把 reasoning 折进 output，且零值 cache 直接省略
（实测对照见 ``tests/fixtures/dsh/events_piai_usage.jsonl``）。缺的维度写
None（不可得）而非 0。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from backend.adapters.dsh_events import (
    merge_usage,
    tool_result_call_id,
    usage_from_event,
)
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

PARSER_VERSION = "dsh-normalizer-v1"
PRODUCER = "dsh"
SOURCE_KIND = "native-event"
SOURCE_INSTANCE = "native-event"
RAW_FILE = "events.jsonl"

# usage 逐 step 出现（每条 assistant/message 一份），但 step 边界不等价于
# API 调用边界，故仍声明 aggregate-only，不伪装成逐调用曲线。
CALL_BOUNDARY = "aggregate-only"

OBSERVED_SDK_VERSION = "deepseek-harness-sdk 0.1.0rc6"
CAPABILITIES: dict[str, Any] = {
    "call_boundary": CALL_BOUNDARY,
    # subagent 工具存在且可用（实测派生成功），但**子 session 的事件只在
    # RunResult.notifications 里，root events 看不到**——adapter 落盘的
    # events.jsonl 只含 root session，故这层拿不到子 agent 身份。
    "subagent_identity": False,
    "subagent_identity_basis": (
        f"root events 不含子 session 事件（子 agent 只在 notifications；"
        f"{OBSERVED_SDK_VERSION} 实测）"
    ),
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


def _to_iso(value: Any) -> str | None:
    """dsh 的 `time` 是 Unix epoch **毫秒整数**；evidence 要 ISO。"""
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


@dataclass
class _State:
    steps: list[TrajectoryStep] = field(default_factory=list)
    step_seq: int = 0
    usage: dict[str, Any] | None = None
    usage_line: int | None = None
    usage_ts: str | None = None
    last_ts: str | None = None
    saw_behaviour: bool = False


class DshNormalizer:
    """dsh 的 native normalizer。"""

    parser_version = PARSER_VERSION
    raw_file = RAW_FILE
    producer = PRODUCER

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
            ts = _to_iso(event.get("time"))
            if ts:
                st.last_ts = ts
            try:
                self._apply_event(event, lineno, attempt_id, st, ts)
            except Exception:  # noqa: BLE001 - 单行坏数据不该毁掉整份 trajectory
                result.record_error(lineno)
                continue

        if st.usage is not None:
            result.evidence.append(
                self._aggregate_evidence(
                    attempt_id, st.usage, st.usage_line, st.usage_ts
                )
            )
        elif st.saw_behaviour:
            # 有行为但没拿到 usage（超时截断等）：明确报 gap，不伪造 aggregate。
            result.evidence.append(self._usage_gap_evidence(attempt_id, st.last_ts))

        trajectory = Trajectory(
            schema_version=TRAJECTORY_SCHEMA_VERSION,
            attempt_id=attempt_id,
            steps=tuple(st.steps),
            producer=PRODUCER,
        )
        v_errors = trajectory.validate()
        if v_errors:
            result.evidence.append(trajectory_validation_evidence(
                attempt_id=attempt_id, producer=PRODUCER,
                parser_version=PARSER_VERSION, errors=v_errors,
                last_ts=st.last_ts, raw_file=RAW_FILE,
            ))
        result.trajectory = trajectory_to_dict(trajectory)
        result.last_ts = st.last_ts
        return result

    # ---------- 事件分派 ----------

    def _apply_event(
        self, event: dict[str, Any], lineno: int, attempt_id: str,
        st: _State, ts: str | None,
    ) -> None:
        etype = event.get("type")
        data = event.get("data")
        data = data if isinstance(data, dict) else {}

        if etype == "tool/call":
            self._push_tool_call(data, lineno, attempt_id, st, ts)
            return

        if etype == "tool/result":
            self._push_tool_result(data, lineno, attempt_id, st, ts)
            return

        if etype == "assistant/message":
            usage = data.get("usage")
            if isinstance(usage, dict):
                # 与 adapter 共用同一套取值/累计规则（backend/adapters/dsh_events）
                # ——两边各写一份的结果是同一事件被算出两个数。
                st.usage = merge_usage(st.usage, usage_from_event(usage))
                st.usage_line = lineno
                st.usage_ts = ts
            self._push_message_blocks(data, lineno, attempt_id, st, ts)
            return

        # turn/step 边界、chunk、session/title 等：框架事件，无行为语义。

    def _push_message_blocks(
        self, data: dict[str, Any], lineno: int, attempt_id: str,
        st: _State, ts: str | None,
    ) -> None:
        """assistant/message 的 content 块 → assistant / thinking step。

        `tool-call` 块**跳过**——工具调用统一由 `tool/call` 事件建 step，
        两处都建会让同一次调用在 trajectory 里出现两遍。
        """
        message = data.get("message")
        blocks = message.get("content") if isinstance(message, dict) else None
        if not isinstance(blocks, list):
            return
        for index, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            # dsh 的思考块是 `reasoning`（CC 是 `thinking`），文本在 `text`。
            kind = (
                "assistant" if btype == "text"
                else "thinking" if btype == "reasoning"
                else None
            )
            if kind is None:
                continue
            text = block.get("text")
            if not isinstance(text, str) or not text:
                continue
            content_hash, content_bytes = hashing.part_semantic_hash(
                [{"type": "text", "text": text}]
            )
            st.saw_behaviour = True
            st.step_seq += 1
            st.steps.append(TrajectoryStep(
                step_id=ids.trajectory_step_id(
                    attempt_id=attempt_id,
                    step_anchor=f"{RAW_FILE}:{lineno}:{btype}:{index}",
                ),
                sequence=st.step_seq,
                timestamp=ts,
                agent_id=PRODUCER,
                parent_agent_id=None,
                kind=kind,
                producer_event_refs=({"file": RAW_FILE, "line": lineno},),
                content_hash=content_hash,
                content_bytes=content_bytes,
            ))

    def _push_tool_call(
        self, data: dict[str, Any], lineno: int, attempt_id: str,
        st: _State, ts: str | None,
    ) -> None:
        name = data.get("name")
        call_id = data.get("callId")
        # `arguments` 是模型原样产出的 JSON 字符串（未解析），可能非法——
        # 解析失败就把原串喂给 hash，不伪造结构。
        raw_args = data.get("arguments")
        try:
            arguments: Any = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except json.JSONDecodeError:
            arguments = raw_args
        content_hash, content_bytes = hashing.part_semantic_hash([{
            "type": "tool_call",
            "name": name if isinstance(name, str) else "tool",
            "arguments": arguments,
        }])
        st.saw_behaviour = True
        st.step_seq += 1
        st.steps.append(TrajectoryStep(
            step_id=ids.trajectory_step_id(
                attempt_id=attempt_id,
                step_anchor=f"{RAW_FILE}:{lineno}:tool/call:{call_id}",
            ),
            sequence=st.step_seq,
            timestamp=ts,
            agent_id=PRODUCER,
            parent_agent_id=None,
            kind="tool_call",
            producer_event_refs=({"file": RAW_FILE, "line": lineno},),
            tool_call_id=call_id if isinstance(call_id, str) else None,
            tool_name=name if isinstance(name, str) else None,
            content_hash=content_hash,
            content_bytes=content_bytes,
        ))

    def _push_tool_result(
        self, data: dict[str, Any], lineno: int, attempt_id: str,
        st: _State, ts: str | None,
    ) -> None:
        """tool/result → 独立的 tool_result step（与 CC 同构）。

        配对键在 `message.content[0].toolCallId`，不在顶层。
        `error: {name, code}` 存在时记进 attributes——注意 `isError` 只反映
        **工具框架层**失败，bash 的非零退出码是 isError=False。
        """
        call_id = tool_result_call_id(data)
        message = data.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        block = content[0] if isinstance(content, list) and content else None

        attributes: dict[str, Any] = {}
        error = data.get("error")
        if isinstance(error, dict):
            attributes["error_name"] = error.get("name")
            attributes["error_code"] = error.get("code")
        if isinstance(block, dict) and isinstance(block.get("isError"), bool):
            attributes["is_error"] = block["isError"]

        st.step_seq += 1
        st.steps.append(TrajectoryStep(
            step_id=ids.trajectory_step_id(
                attempt_id=attempt_id,
                step_anchor=f"{RAW_FILE}:{lineno}:tool/result:{call_id}",
            ),
            sequence=st.step_seq,
            timestamp=ts,
            agent_id=PRODUCER,
            parent_agent_id=None,
            kind="tool_result",
            producer_event_refs=({"file": RAW_FILE, "line": lineno},),
            tool_call_id=call_id,
            attributes=attributes or None,
        ))

    # ---------- evidence ----------

    def _aggregate_evidence(
        self, attempt_id: str, usage: dict[str, Any],
        line: int | None, ts: str | None,
    ) -> AggregateUsageEvidence:
        return AggregateUsageEvidence(
            evidence_id=ids.evidence_id(
                attempt_id=attempt_id, source_kind=SOURCE_KIND,
                source_instance=SOURCE_INSTANCE,
                raw_ref=f"{RAW_FILE}:{line}", producer_id="message-aggregate",
            ),
            attempt_id=attempt_id,
            phase="agent_run",
            source=EvidenceSource(kind=SOURCE_KIND, instance=SOURCE_INSTANCE),
            producer=EvidenceProducer(name=PRODUCER, version=PARSER_VERSION),
            time=EvidenceTime(observed_at=ts or "", started_at=None, finished_at=None),
            raw_ref=EvidenceRawRef(kind="events-jsonl", file=RAW_FILE, line=line),
            correlation_hints=CorrelationHints(producer_session_id=None),
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
                # 保留 producer 原始事件类型：这份总计确实来自多条
                # assistant/message 的 usage 求和，不伪装成某个"总计"事件。
                producer_event_type="assistant/message",
            ),
        )

    def _usage_gap_evidence(self, attempt_id: str, ts: str | None):
        from backend.wire.evidence import CaptureEventEvidence, CaptureEventPayload

        return CaptureEventEvidence(
            evidence_id=ids.evidence_id(
                attempt_id=attempt_id, source_kind=SOURCE_KIND,
                source_instance=SOURCE_INSTANCE,
                raw_ref=f"{PRODUCER}:usage-gap", producer_id="normalizer",
            ),
            attempt_id=attempt_id,
            phase="agent_run",
            source=EvidenceSource(kind=SOURCE_KIND, instance=SOURCE_INSTANCE),
            producer=EvidenceProducer(name=PRODUCER, version=PARSER_VERSION),
            time=EvidenceTime(observed_at=ts or "", started_at=None, finished_at=None),
            raw_ref=EvidenceRawRef(kind="events-jsonl", file=RAW_FILE, line=None),
            correlation_hints=CorrelationHints(producer_session_id=None),
            capabilities={**CAPABILITIES, "usage": "not-observed"},
            redaction=EvidenceRedaction(policy="metadata", status="applied"),
            errors=[],
            extensions={},
            payload=CaptureEventPayload(
                event="error", source_instance=SOURCE_INSTANCE, status=None,
                reason_code="usage_not_observed",
                message="观察到行为但 assistant/message 无 usage",
                counters=None, effective_capabilities=None,
            ),
        )
