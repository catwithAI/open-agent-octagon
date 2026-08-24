"""Codex native normalizer。

输入：attempt 的 ``events.jsonl``（``codex exec --json`` stdout 逐行）。
输出：``aggregate_usage`` WireEvidence + ``trajectory.json``。

spike 决议：Octagon 现状用 ``--ephemeral``，不落 internal
session rollout，逐调用 ``token_count.last_token_usage`` 不可得；stdout 每次
exec 只有 1 个 ``turn.completed``（整 turn 累计 usage），逐次 agent_message 无
per-call usage。因此**首期只产 attempt 级 aggregate，不伪造逐调用曲线**——
normalizer 标 ``call_boundary=aggregate-only``（capabilities），caller/finalizer
把它落进 manifest。

若未来接入去-ephemeral 的 rollout（含 ``event_msg/token_count``），逐调用切分
在后续增强里做；本模块只吃 stdout。

保留 producer event type：aggregate 证据的 ``producer_event_type``
记为 ``turn.completed``，不把 stdout 的累计 usage 伪装成调用边界。

stdout 事件（codex-cli 0.144.1）：
- ``thread.started``：thread_id（→ producer_session_id）；
- ``turn.started`` / ``turn.completed``：turn 边界，completed 带累计 usage；
- ``item.completed`` (agent_message)：一次可见输出，≈一次 API call（无 usage）；
- ``item.completed`` (mcp_tool_call / command_execution)：工具/命令，记 trajectory。
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

PRODUCER_NAME = "codex"
PARSER_VERSION = "codex-normalizer-v1"
SOURCE_KIND = "native-event"
SOURCE_INSTANCE = "native-event"
RAW_FILE = "events.jsonl"

# stdout 只能给 attempt 级累计——逐调用边界不可得。
CALL_BOUNDARY = "aggregate-only"

# normalizer 声明的 capability（manifest 必须明确报 gap）。
#
# `subagent_identity=false` 是**可执行的**能力声明，不只写在文档里——评测/
# manifest 据此判定子 agent 压缩 unsupported，而不是靠人读文档。
#
# 措辞限定到被测版本：在 codex-cli 0.144.5 上观察到无子 agent
# 能力（CLI 无相关命令、agent 自述无此工具、事件 schema 零归属字段），
# 这**不等于**产品永久不会有。schema 变化时需重新评估——
# `test_codex_multiturn_normalizer.py` 的 schema 断言会在那时失败提醒。
OBSERVED_CLI_VERSION = "codex-cli 0.144.5"
CAPABILITIES: dict[str, Any] = {
    "call_boundary": CALL_BOUNDARY,
    "subagent_identity": False,
    "subagent_identity_basis": (
        f"not observed in {OBSERVED_CLI_VERSION}"
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


def _usage_from_turn(usage: dict[str, Any] | None) -> UsagePayload:
    """codex turn.completed.usage → 统一 UsagePayload。

    codex 字段：input_tokens / cached_input_tokens / output_tokens /
    reasoning_output_tokens。cached_input_tokens 映射 cache_read；codex 无
    cache_write 概念，写 null（区分零与不可得）。
    """
    u = usage or {}
    output = u.get("output_tokens")
    reasoning = u.get("reasoning_output_tokens")
    if isinstance(output, int) and isinstance(reasoning, int):
        output += reasoning
    elif output is None and isinstance(reasoning, int):
        output = reasoning
    return UsagePayload(
        input_tokens=u.get("input_tokens"),
        output_tokens=output,
        cache_read_tokens=u.get("cached_input_tokens"),
        cache_write_tokens=None,
        reasoning_tokens=reasoning,
        estimated=False,
    )


@dataclass
class _State:
    steps: list[TrajectoryStep] = field(default_factory=list)
    step_seq: int = 0
    session_id: str | None = None
    turn_usage: dict[str, Any] | None = None
    turn_line: int | None = None
    turn_ts: str | None = None
    last_ts: str | None = None
    # 见过的所有 thread ID（按出现顺序去重）。多轮 attempt 正常情况下只有
    # 一个；出现多个说明 resume 落到了不同 thread，上下文已断裂。
    session_ids: list[str] = field(default_factory=list)


class CodexNormalizer:
    producer = PRODUCER_NAME
    parser_version = PARSER_VERSION
    raw_file = RAW_FILE

    def has_input(self, attempt_dir: Path) -> bool:
        """runner 的输入存在性检查（取代硬编码 RAW_FILE 判断）。"""
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
            etype = event.get("type")
            ts = event.get("timestamp")
            if isinstance(ts, str) and ts:
                st.last_ts = ts
            # adapter 把无法解析的 stdout 包成 {"raw_line": ...}（见 codex adapter）：
            # 合法 JSON 但非已知事件，计 parse error（对齐 claude）。
            if "raw_line" in event and etype is None:
                result.record_error(lineno)
                continue
            try:
                self._apply_event(event, etype, lineno, attempt_id, st, ts)
            except Exception:
                result.record_error(lineno)
                continue

        # aggregate usage evidence（只此一条，不伪造逐调用）
        if st.turn_usage is not None:
            # 跨 thread 的 aggregate 不标单一 session ID：那会把先前 thread 的
            # 消耗错误归给某一个（session continuity broken）。此时置
            # None 并另发 capability gap，让下游知道"这份 aggregate 跨了会话"。
            spans_multiple = len(st.session_ids) > 1
            result.evidence.append(
                self._aggregate_evidence(
                    attempt_id, st.turn_usage, st.turn_line, st.turn_ts,
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
            # 观察到 item 但无 turn.completed（如中断）：写明确的 usage gap，
            # 不伪造 aggregate。trajectory + capability 仍产出。
            result.evidence.append(
                self._usage_gap_evidence(attempt_id, st.last_ts, st.session_id)
            )
        # Trajectory 独立模型：构造 + 结构校验 + 序列化。codex 的
        # kind 集合无 tool_result，配对规则天然跳过（trajectory_schema 按
        # producer 分发）。构造期 ValueError 向上传播；validate() 走 fail-open。
        trajectory = Trajectory(
            schema_version=TRAJECTORY_SCHEMA_VERSION,
            attempt_id=attempt_id,
            steps=tuple(st.steps),
            producer=PRODUCER_NAME,
        )
        v_errors = trajectory.validate()
        if v_errors:
            result.evidence.append(trajectory_validation_evidence(
                attempt_id=attempt_id, producer=PRODUCER_NAME,
                parser_version=PARSER_VERSION, errors=v_errors,
                last_ts=st.last_ts, raw_file=RAW_FILE,
            ))
        result.trajectory = trajectory_to_dict(trajectory)
        return result

    def _apply_event(
        self, event: dict[str, Any], etype: Any, lineno: int, attempt_id: str,
        st: "_State", ts: Any,
    ) -> None:
        if etype == "thread.started":
            tid = event.get("thread_id")
            if isinstance(tid, str) and tid and tid not in st.session_ids:
                st.session_ids.append(tid)
            # session_id 保留**首个** thread ID：它是这个 attempt 的身份。
            # 用最后一个会把先前 thread 的消耗错误地归给新 thread。
            st.session_id = st.session_id or tid
            return
        if etype == "turn.completed":
            usage = event.get("usage")
            if usage is None:
                # 已知事件但缺 usage：schema drift，计 parse error 而非静默
                # 变「无 usage」。
                raise ValueError("turn.completed 缺 usage")
            if not isinstance(usage, dict):
                raise ValueError("turn.completed.usage 非 object")
            # 多 turn 时累加（同一 attempt 语义上仍是 attempt 级 aggregate）
            st.turn_usage = _merge_usage(st.turn_usage, usage)
            st.turn_line = lineno
            st.turn_ts = ts
            return
        if etype == "item.completed":
            item = event.get("item")
            if not isinstance(item, dict):
                raise ValueError("item.completed.item 非 object")
            self._item_step(item, lineno, attempt_id, st, ts)
            return
        # thread.started/turn.started/item.started/item.updated 等：无聚合语义

    def _item_step(
        self, item: dict[str, Any], lineno: int, attempt_id: str,
        st: "_State", ts: Any,
    ) -> None:
        itype = item.get("type")
        kind_map = {
            "agent_message": "assistant",
            "mcp_tool_call": "tool_call",
            "command_execution": "tool_call",
        }
        kind = kind_map.get(itype)
        if kind is None:
            return  # todo_list/file_change 等非调用/工具语义，不建 step
        item_id = item.get("id")
        tool_id = item_id if kind == "tool_call" else None
        # tool_name：关联依赖 step.tool_name。mcp_tool_call 的
        # item.tool 已是**裸**工具名（与 MCP tools/call.params.name 同形，无需归一）；
        # command_execution 用固定名。
        if itype == "mcp_tool_call":
            _tool_name = item.get("tool") or None
        elif itype == "command_execution":
            _tool_name = "command_execution"
        else:
            _tool_name = None
        # 可见 payload → 公共 semantic IR + hash：
        # agent_message.text → messages IR；tool call → tools/args IR。
        content_hash, content_bytes = _item_semantic_hash(itype, item)
        st.step_seq += 1
        st.steps.append(TrajectoryStep(
            step_id=ids.trajectory_step_id(
                attempt_id=attempt_id,
                step_anchor=f"{RAW_FILE}:{lineno}:{itype}:{item_id}",
            ),
            sequence=st.step_seq, timestamp=ts, kind=kind,
            producer_event_refs=({"file": RAW_FILE, "line": lineno},),
            tool_call_id=tool_id,
            tool_name=_tool_name,
            # aggregate-only：无逐调用 lc，trajectory step 不挂 logical_call_id
            logical_call_id=None,
            content_hash=content_hash,
            content_bytes=content_bytes,
            # codex --ephemeral 无 sub-agent 语义，恒 main（与迁移前一致）
            agent_id="main", parent_agent_id=None,
        ))

    def _usage_gap_evidence(
        self, attempt_id: str, ts: str | None, session_id: str | None
    ):
        """观察到 item 但无 turn.completed：明确 usage gap capture_event。"""
        from backend.wire.evidence import CaptureEventEvidence, CaptureEventPayload

        return CaptureEventEvidence(
            evidence_id=ids.evidence_id(
                attempt_id=attempt_id, source_kind=SOURCE_KIND,
                source_instance=SOURCE_INSTANCE,
                raw_ref="codex:usage-gap", producer_id="normalizer",
            ),
            attempt_id=attempt_id,
            phase="agent_run",
            source=EvidenceSource(kind=SOURCE_KIND, instance=SOURCE_INSTANCE),
            producer=EvidenceProducer(name=PRODUCER_NAME, version=PARSER_VERSION),
            time=EvidenceTime(observed_at=ts or "", started_at=None, finished_at=None),
            raw_ref=EvidenceRawRef(kind="events-jsonl", file=RAW_FILE, line=None),
            correlation_hints=CorrelationHints(producer_session_id=session_id),
            capabilities={**CAPABILITIES, "usage": "not-observed"},
            redaction=EvidenceRedaction(policy="metadata", status="applied"),
            errors=[],
            extensions={},
            payload=CaptureEventPayload(
                event="error", source_instance=SOURCE_INSTANCE, status=None,
                reason_code="usage_not_observed", message="观察到工具/消息但无 turn.completed usage",
                counters=None, effective_capabilities=None,
            ),
        )

    def _session_broken_evidence(
        self, attempt_id: str, session_ids: list[str], ts: str | None
    ):
        """一个 attempt 里出现多个 thread ID：session 连续性断裂。

        resume 落到了不同 thread，上下文已断——aggregate 跨了多个会话，不能
        标单一 session ID，压缩/保真度结论也不成立。留痕供 evaluation summary
        判 `incomplete`，不静默把消耗归给其中一个。
        """
        from backend.wire.evidence import CaptureEventEvidence, CaptureEventPayload

        return CaptureEventEvidence(
            evidence_id=ids.evidence_id(
                attempt_id=attempt_id, source_kind=SOURCE_KIND,
                source_instance=SOURCE_INSTANCE,
                raw_ref="codex:session-continuity", producer_id="normalizer",
            ),
            attempt_id=attempt_id,
            phase="agent_run",
            source=EvidenceSource(kind=SOURCE_KIND, instance=SOURCE_INSTANCE),
            producer=EvidenceProducer(name=PRODUCER_NAME, version=PARSER_VERSION),
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
                    "一个 attempt 出现多个 thread ID："
                    + ", ".join(session_ids[:5])
                ),
                counters={"session_count": len(session_ids)},
                effective_capabilities=None,
            ),
        )

    def _aggregate_evidence(
        self, attempt_id: str, usage: dict[str, Any],
        line: int | None, ts: str | None, session_id: str | None,
    ) -> AggregateUsageEvidence:
        return AggregateUsageEvidence(
            evidence_id=ids.evidence_id(
                attempt_id=attempt_id, source_kind=SOURCE_KIND,
                source_instance=SOURCE_INSTANCE,
                raw_ref=f"{RAW_FILE}:{line}", producer_id="turn-aggregate",
            ),
            attempt_id=attempt_id,
            phase="agent_run",
            source=EvidenceSource(kind=SOURCE_KIND, instance=SOURCE_INSTANCE),
            producer=EvidenceProducer(name=PRODUCER_NAME, version=PARSER_VERSION),
            time=EvidenceTime(observed_at=ts or "", started_at=None, finished_at=None),
            raw_ref=EvidenceRawRef(kind="events-jsonl", file=RAW_FILE, line=line),
            correlation_hints=CorrelationHints(producer_session_id=session_id),
            # capabilities 声明逐调用边界不可得——finalizer/manifest 据此标
            # call_boundary=aggregate-only（不伪装边界）。
            capabilities=dict(CAPABILITIES),
            redaction=EvidenceRedaction(policy="metadata", status="applied"),
            errors=[],
            extensions={},
            payload=AggregateUsagePayload(
                scope="attempt",
                usage=_usage_from_turn(usage),
                # 保留 producer event type
                producer_event_type="turn.completed",
            ),
        )


def _item_semantic_hash(itype: str, item: dict[str, Any]) -> tuple[str | None, int | None]:
    """可见 item payload → 公共 semantic IR + hash。

    **跨 source 可比**：与 Claude 的 `_content_to_ir_parts` 用同一 `messages` IR
    形状（`tools` kind 是工具**声明**，不是工具**调用**，故这里工具调用
    走 messages 里的 `tool_call` content part，而非 `tools`）：

    - agent_message.text → `{type:text, text}`；
    - mcp_tool_call → `{type:tool_call, name, arguments}`；
    - command_execution → `{type:tool_call, name:"command_execution", arguments:cmd}`。

    返回 (content_hash, content_bytes)；无法取内容时 (None, None)，不伪造 hash。
    """
    if itype == "agent_message":
        text = item.get("text")
        if not isinstance(text, str):
            return None, None
        return hashing.part_semantic_hash([{"type": "text", "text": text}])
    if itype == "mcp_tool_call":
        name = f"{item.get('server', '')}.{item.get('tool', '')}"
        return hashing.part_semantic_hash([{
            "type": "tool_call", "name": name, "arguments": item.get("arguments"),
        }])
    if itype == "command_execution":
        cmd = item.get("command")
        if not isinstance(cmd, str):
            return None, None
        return hashing.part_semantic_hash([{
            "type": "tool_call", "name": "command_execution", "arguments": cmd,
        }])
    return None, None


def _merge_usage(
    acc: dict[str, Any] | None, new: dict[str, Any]
) -> dict[str, Any]:
    """多 turn 累加（数值字段求和）；acc 为 None 时取 new 的浅拷贝。"""
    if acc is None:
        return dict(new)
    out = dict(acc)
    for k, v in new.items():
        if isinstance(v, (int, float)) and isinstance(out.get(k), (int, float)):
            out[k] = out[k] + v
        else:
            out[k] = v
    return out
