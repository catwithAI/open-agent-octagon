"""Claude Code native normalizer。

输入：attempt 的 ``events.jsonl``（claude CLI stream-json 逐行）。
输出：``native_llm_call`` / ``aggregate_usage`` WireEvidence + ``trajectory.json``。

状态机：

1. ``system/init``：记录 session/model/version，不生成 call；
2. ``assistant.message.id`` 首次出现建 candidate call；相同 ID 的重复/增量
   事件合并（流式先发 partial、后发 final，usage 取信息更全的一版）；
3. assistant usage 作为该 call 的 producer-reported usage；
4. tool_use blocks 记 adjacency（trajectory step），不当额外 LLM call；
5. ``result`` usage 作为 attempt aggregate evidence，不生成额外 call；
6. 无 message ID 时按 assistant event sequence 建 call，confidence=inferred；
7. timestamp 是 stdout 到达时间，只作完成时间近似，request start/duration=null。

trajectory 两阶段：normalizer 先按 producer event ref 生成稳定 step
ID 与邻接，logical_call_id 用 message ID 作为 producer_call_id anchor（与
finalizer 的 lc 生成同源，因此重跑幂等、correlation 前后 step ID 不变）。

解析失败保留 raw + parser version，不中断整体（幂等）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from backend.wire import correlate, hashing, ids
from backend.wire import turn_correlation as _turn

# events.jsonl 行级 turn 归属键（adapter with_turn_ext 写入，与 canonical evidence
# 的 turn extension 同名）。用 wire 层常量避免 wire→conversation 反向依赖。
_TURN_ID_KEY = _turn.EXT_TURN_ID
_TURN_INDEX_KEY = _turn.EXT_TURN_INDEX
from backend.wire.evidence import (
    CaptureEventEvidence,
    CaptureEventPayload,
    CorrelationHints,
    EvidenceProducer,
    EvidenceRawRef,
    EvidenceRedaction,
    EvidenceSource,
    EvidenceTime,
    NativeLlmCallEvidence,
    NativeLlmCallPayload,
    RequestSummary,
    ResponseSummary,
    UsagePayload,
    AggregateUsageEvidence,
    AggregateUsagePayload,
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

PRODUCER_NAME = "claude-code"
PARSER_VERSION = "claude-code-normalizer-v1"
SOURCE_KIND = "native-event"
# spool 文件名 native-event.jsonl 无 @instance 后缀，finalize 读到的 instance
# 等于 kind——因此 sequence anchor 的 instance 段也必须是 "native-event"，
# 否则 normalizer 与 finalize 对 orphan call 算出不同 lc。producer 身份
# （claude-code）另由 producer.name/source.version 记录。
SOURCE_INSTANCE = "native-event"
RAW_FILE = "events.jsonl"
# observed_at 不可得时的固定值（与 runner/blade normalizer 一致，不写空串）
_EPOCH_TS = "1970-01-01T00:00:00.000Z"

# 派生子 agent 的工具名（owner 索引用）。CC 各版本命名不一致：
# 2.1.215 实测是 "Agent"，文档与旧版本是 "Task"。两者都认——只认一个会让
# 另一种情况静默退化成"父 agent 未解析"，嵌套拓扑就断了。
_SUBAGENT_TOOL_NAMES = frozenset({"Task", "Agent"})

# owner 索引里表示"该 tool_use_id 在多个作用域被复用，父归属歧义"的哨兵。
# 用哨兵而不是删除条目，是为了区分"没见过这个 ID"（孤儿）与"见过但有歧义"
# ——两者的 gap 原因不同，排查路径也不同。
_AMBIGUOUS_OWNER = "\x00ambiguous"

# CLI role → semantic IR role。
_ROLE_MAP = {
    "user": "user",
    "assistant": "assistant",
    "system": "system",
    "tool": "tool",
}


@dataclass
class _Call:
    """一次 assistant turn 聚成的 candidate call。"""

    message_id: str | None
    anchor: str            # producer-call:<id> 或 source-seq:...
    confidence: str        # explicit | inferred
    first_line: int
    last_line: int
    seq_no: int | None = None  # inferred call 的稳定序号（进 hints.sequence）
    model: str | None = None
    usage: dict[str, Any] | None = None
    stop_reason: str | None = None
    content_parts: list[dict[str, Any]] = field(default_factory=list)
    # 同一 message 的流式事件可能是「分离 blocks」或「累计快照」。记录每种
    # block identity 已保留的最大出现次数，合并时既不覆盖前序 block，也不把
    # 累计快照重复追加；同一事件内两个相同文本仍可保留两次。
    content_identity_counts: dict[tuple[str, str], int] = field(default_factory=dict)
    output_blocks: int = 0
    observed_at: str | None = None
    # sub-agent 拓扑：CC 的 Task 子 agent 事件带 parent_tool_use_id；
    # 该 call 归属的 agent_id（main 或 sub-<parent_tool_use_id>）与父 agent。
    agent_id: str = "main"
    parent_agent_id: str | None = None
    # 该 call 所属的 conversation turn（adapter 用 with_turn_ext 写进
    # events.jsonl 行的 x-octagon.turn-id/-index）。多轮时非空，单轮 legacy 为 None。
    turn_id: str | None = None
    turn_index: int | None = None


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


def _content_to_ir_parts(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """assistant/user content[] → semantic IR parts。"""
    parts: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append({"type": "text", "text": block.get("text", "")})
        elif btype == "thinking":
            parts.append({"type": "text", "text": block.get("thinking", "")})
        elif btype == "tool_use":
            parts.append({
                "type": "tool_call",
                "name": block.get("name", ""),
                "arguments": block.get("input", {}),
            })
        elif btype == "tool_result":
            parts.append({
                "type": "tool_result",
                "content": block.get("content", ""),
            })
    return parts


def _usage_payload(usage: dict[str, Any] | None) -> UsagePayload:
    u = usage or {}
    # cache_creation_input_tokens 是 Anthropic 的 cache write。
    return UsagePayload(
        input_tokens=u.get("input_tokens"),
        output_tokens=u.get("output_tokens"),
        cache_read_tokens=u.get("cache_read_input_tokens"),
        cache_write_tokens=u.get("cache_creation_input_tokens"),
        reasoning_tokens=None,
        estimated=False,
    )


def _call_extensions(call: "_Call") -> dict[str, Any]:
    """一个 call 的 evidence extensions：sub-agent 拓扑 + turn 归属。

    - 非 main agent → agent-id/parent-agent-id；
    - 有 turn（多轮）→ turn-id/turn-index，finalizer 投影成 canonical
      correlation.turn_*（explicit）。
    main、单轮、无 turn 的 call 返回空 dict，产物逐字节与改造前一致。
    """
    ext: dict[str, Any] = {}
    if call.agent_id != "main":
        ext["x-octagon.agent-id"] = call.agent_id
        ext["x-octagon.parent-agent-id"] = call.parent_agent_id
    if call.turn_id is not None:
        ext[_TURN_ID_KEY] = call.turn_id
        if call.turn_index is not None:
            ext[_TURN_INDEX_KEY] = call.turn_index
    return ext


def _usage_completeness(usage: dict[str, Any] | None) -> tuple[int, int]:
    """流式合并 usage 的信息量排序。

    先比较可用数值字段数；字段数相同时，非零字段更多的一版更接近完成快照。
    新版 CC 的分离 block 事件常先报全 0，后续同 message ID 才报真实 usage，
    不能因为两版字段数相同就永久保留首个零快照。
    """
    if not usage:
        return -1, -1
    numeric = [v for v in usage.values() if isinstance(v, (int, float))]
    return len(numeric), sum(1 for v in numeric if v != 0)


def _content_part_identity(
    part: dict[str, Any], raw_block: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Claude stream block 的稳定去重键。

    tool block 优先使用 producer ID，避免同一 message 内两个同名同参的独立
    调用被错误折叠；文本类 block 使用语义内容，识别累计快照中的重发。
    """
    ptype = str(part.get("type") or "")
    raw_block = raw_block or {}
    if ptype == "tool_call":
        tool_id = raw_block.get("id")
        if isinstance(tool_id, str) and tool_id:
            return ptype, f"id:{tool_id}"
        args = json.dumps(
            part.get("arguments"), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), default=str,
        )
        return ptype, f"value:{part.get('name') or ''}\x00{args}"
    if ptype == "tool_result":
        tool_id = raw_block.get("tool_use_id")
        if isinstance(tool_id, str) and tool_id:
            return ptype, f"id:{tool_id}"
        value = json.dumps(
            part.get("content"), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), default=str,
        )
        return ptype, value
    return ptype, str(part.get("text") or "")


def _merge_content_parts(
    call: "_Call", new_parts: list[dict[str, Any]], raw_blocks: list[dict[str, Any]],
) -> None:
    """按首次观察顺序合并同一 message ID 的分离 blocks/累计快照。"""
    event_counts: dict[tuple[str, str], int] = {}
    for index, part in enumerate(new_parts):
        raw = raw_blocks[index] if index < len(raw_blocks) else None
        identity = _content_part_identity(part, raw)
        occurrence = event_counts.get(identity, 0) + 1
        event_counts[identity] = occurrence
        if occurrence <= call.content_identity_counts.get(identity, 0):
            continue
        call.content_parts.append(part)
    for identity, count in event_counts.items():
        call.content_identity_counts[identity] = max(
            call.content_identity_counts.get(identity, 0), count,
        )
    call.output_blocks = len(call.content_parts)


@dataclass
class _ParseState:
    calls: dict[str, _Call] = field(default_factory=dict)
    call_order: list[str] = field(default_factory=list)
    steps: list[TrajectoryStep] = field(default_factory=list)
    model_hint: str | None = None
    session_id: str | None = None
    cli_version: str | None = None
    result_usage: dict[str, Any] | None = None
    result_line: int | None = None
    result_ts: str | None = None
    step_seq: int = 0
    # Task tool_use 归属索引（嵌套拓扑）：tool_use_id → 发起它的 agent_id。
    # 事件流里 tool_use 必然先于该子 agent 的事件出现（父先调用、子才开始），
    # 所以边解析边建索引即可，无需两遍扫描。
    #
    # 一级子 agent 的 owner 是 main；二级的 owner 是那个一级子 agent——不查索引
    # 就只能一律写 main，嵌套关系被压平（明确禁止）。
    task_owner: dict[str, str] = field(default_factory=dict)
    # 解析不出 owner 的 tool_use_id → 原因，进 capability gap（不静默归 main）
    unresolved_parents: dict[str, str] = field(default_factory=dict)


class ClaudeCodeNormalizer:
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

        st = _ParseState()
        for lineno, event in _iter_events(events_path):
            if event is None or not isinstance(event, dict):
                # 非法 JSON 或顶层非 object（schema drift）：计 parse error
                result.record_error(lineno)
                continue
            etype = event.get("type")
            _ev_ts = event.get("timestamp")
            if isinstance(_ev_ts, str) and _ev_ts:
                result.last_ts = _ev_ts

            # adapter 把无法解析的 CLI 输出包成 {"raw_line": ...}（见
            # claude_code adapter）：它是合法 JSON 但不是已知事件，计 parse error
            # 而非静默当未知事件吞掉。
            if "raw_line" in event and etype is None:
                result.record_error(lineno)
                continue

            # 单个事件的解析异常（message 变 list/string 等 schema drift）不能
            # 让整次 normalizer fail-open——per-event 兜住并计 parse error 继续
            # 已知 type 但 payload 畸形也走这条路。
            try:
                self._apply_event(event, etype, lineno, attempt_id, st)
            except Exception:
                result.record_error(lineno)
                continue

        # 产出 call evidence
        for anchor in st.call_order:
            call = st.calls[anchor]
            result.evidence.append(
                self._call_evidence(
                    attempt_id, call, st.model_hint, st.session_id, st.cli_version
                )
            )
        # aggregate usage evidence
        if st.result_usage is not None:
            result.evidence.append(
                self._aggregate_evidence(
                    attempt_id, st.result_usage, st.result_line, st.result_ts,
                    st.session_id,
                )
            )
        # 父 agent 解析失败留痕：不静默归 main，capability gap 可见
        if st.unresolved_parents:
            result.evidence.append(
                _parent_unresolved_evidence(
                    attempt_id, st.unresolved_parents, result.last_ts,
                )
            )
        # Trajectory 独立模型：构造 + 结构校验 + 序列化。
        # 构造期 ValueError（programmer error）不捕获，向上传播（第一类）；
        # validate() 的结构问题走 fail-open：记 capture_event、照常写盘。
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
                last_ts=result.last_ts, raw_file=RAW_FILE,
            ))
        result.trajectory = trajectory_to_dict(trajectory)
        return result

    def _apply_event(
        self, event: dict[str, Any], etype: Any, lineno: int,
        attempt_id: str, st: "_ParseState",
    ) -> None:
        """处理单个 raw event，更新 st。异常由调用方计 parse error。"""
        ts = event.get("timestamp")

        if etype == "system" and event.get("subtype") == "init":
            st.session_id = event.get("session_id") or st.session_id
            st.model_hint = event.get("model") or st.model_hint
            st.cli_version = event.get("version") or st.cli_version
            return

        # sub-agent 归属：CC 的 Task 子 agent 事件带 parent_tool_use_id
        # （顶层字段）。有则该事件属于以此 tool_use 为父的子 agent；agent_id 由
        # parent_tool_use_id 稳定派生，parent_agent_id=main。无则 main。
        parent_tuid = event.get("parent_tool_use_id")
        cur_agent_id, cur_parent = _agent_of(parent_tuid, st)

        if etype == "assistant":
            message = event.get("message")
            if not isinstance(message, dict):
                raise ValueError("assistant.message 非 object")
            msg_id = message.get("id")
            if msg_id:
                anchor = f"{correlate.ANCHOR_PRODUCER_CALL}:{msg_id}"
                confidence, seq_no = "explicit", None
            else:
                seq_no = len([a for a in st.call_order if not st.calls[a].message_id])
                anchor = correlate.sequence_anchor(SOURCE_KIND, SOURCE_INSTANCE, seq_no)
                confidence = "inferred"

            # 该行的 turn 归属（adapter with_turn_ext 写在行级）。
            row_turn_id = event.get(_TURN_ID_KEY)
            row_turn_index = event.get(_TURN_INDEX_KEY)
            call = st.calls.get(anchor)
            if call is None:
                call = _Call(
                    message_id=msg_id, anchor=anchor, confidence=confidence,
                    first_line=lineno, last_line=lineno,
                    agent_id=cur_agent_id, parent_agent_id=cur_parent,
                    turn_id=row_turn_id if isinstance(row_turn_id, str) else None,
                    turn_index=(
                        row_turn_index if isinstance(row_turn_index, int) else None
                    ),
                )
                call.seq_no = seq_no
                st.calls[anchor] = call
                st.call_order.append(anchor)
            elif call.turn_id is None and isinstance(row_turn_id, str):
                # 同一 call 跨多行（assistant 分片）：首个带 turn 的行定 turn。
                call.turn_id = row_turn_id
                if isinstance(row_turn_index, int):
                    call.turn_index = row_turn_index
            call.last_line = lineno
            call.observed_at = ts or call.observed_at
            call.model = message.get("model") or call.model
            call.stop_reason = message.get("stop_reason") or call.stop_reason
            content = message.get("content")
            content = content if isinstance(content, list) else []
            raw_blocks = [c for c in content if isinstance(c, dict)]
            new_parts = _content_to_ir_parts(raw_blocks)
            _merge_content_parts(call, new_parts, raw_blocks)
            usage = message.get("usage")
            usage = usage if isinstance(usage, dict) else None
            if _usage_completeness(usage) > _usage_completeness(call.usage):
                call.usage = usage

            # assistant step 的 content hash：该 message 的 text parts（与 Codex
            # agent_message 用同一 per-part messages IR，跨 source 可比）。
            text_parts = [p for p in _content_to_ir_parts(content) if p.get("type") == "text"]
            a_hash, a_bytes = hashing.part_semantic_hash(text_parts)
            st.step_seq += 1
            st.steps.append(TrajectoryStep(
                step_id=ids.trajectory_step_id(
                    attempt_id=attempt_id, step_anchor=f"{RAW_FILE}:{lineno}:assistant",
                ),
                sequence=st.step_seq, timestamp=ts, kind="assistant",
                producer_event_refs=({"file": RAW_FILE, "line": lineno},),
                logical_call_id=_lc(attempt_id, anchor),
                content_hash=a_hash, content_bytes=a_bytes,
                # 沿用迁移前行为：assistant step 恒记 main（sub-agent 归属
                # 由 call evidence 的 extensions 表达），保持产物逐字节一致。
                agent_id="main", parent_agent_id=None,
            ))
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    # 子 agent 派生工具的 owner 索引：这个 tool_use 由
                    # cur_agent_id 发起，将来带 parent_tool_use_id=<它的 id> 的
                    # 事件就属于它派生的子 agent，父即 cur_agent_id。
                    #
                    # 工具名在不同 CC 版本/配置下不一致（实测 2.1.215 用
                    # "Agent"，文档与旧版本用 "Task"），因此匹配一组名字而不是
                    # 单个字面量——写死一个会让另一种情况静默退化成"父未解析"。
                    if block.get("name") in _SUBAGENT_TOOL_NAMES and block.get("id"):
                        _register_task_owner(st, str(block["id"]), cur_agent_id)
                    tc_hash, tc_bytes = hashing.part_semantic_hash([{
                        "type": "tool_call", "name": block.get("name", ""),
                        "arguments": block.get("input"),
                    }])
                    st.step_seq += 1
                    st.steps.append(TrajectoryStep(
                        step_id=ids.trajectory_step_id(
                            attempt_id=attempt_id,
                            step_anchor=f"{RAW_FILE}:{lineno}:tool_use:{block.get('id')}",
                        ),
                        sequence=st.step_seq, timestamp=ts, kind="tool_call",
                        producer_event_refs=({"file": RAW_FILE, "line": lineno},),
                        tool_call_id=block.get("id"),
                        tool_name=block.get("name"),
                        logical_call_id=_lc(attempt_id, anchor),
                        content_hash=tc_hash, content_bytes=tc_bytes,
                        agent_id=cur_agent_id, parent_agent_id=cur_parent,
                    ))
            return

        if etype == "user":
            message = event.get("message")
            if not isinstance(message, dict):
                raise ValueError("user.message 非 object")
            content = message.get("content")
            content = content if isinstance(content, list) else []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    st.step_seq += 1
                    st.steps.append(TrajectoryStep(
                        step_id=ids.trajectory_step_id(
                            attempt_id=attempt_id,
                            step_anchor=f"{RAW_FILE}:{lineno}:tool_result:{block.get('tool_use_id')}",
                        ),
                        sequence=st.step_seq, timestamp=ts, kind="tool_result",
                        producer_event_refs=({"file": RAW_FILE, "line": lineno},),
                        tool_call_id=block.get("tool_use_id"),
                        agent_id=cur_agent_id, parent_agent_id=cur_parent,
                    ))
            return

        if etype == "result":
            # result usage 作为 attempt aggregate，不生成额外 call
            usage = event.get("usage")
            st.result_usage = (usage if isinstance(usage, dict) else None) or st.result_usage
            st.result_line = lineno
            st.result_ts = ts
            return
        # 其他已知/未知 type（rate_limit_event 等）：无 call 语义，忽略但不计错

    # ---- evidence 构造 -----------------------------------------------

    def _call_evidence(
        self, attempt_id: str, call: _Call,
        model_hint: str | None, session_id: str | None, cli_version: str | None,
    ) -> NativeLlmCallEvidence:
        model = call.model or model_hint
        # response_summary：对 assistant content parts 算 semantic hash——用
        # 与 trajectory 同一 _part_semantic_hash（[{role,content}] IR，
        # canonical response hash 不能再用裸 parts）。
        content_hash, _ = hashing.part_semantic_hash(call.content_parts)
        hash_domain = hashing.DOMAIN_SEMANTIC if content_hash else None
        response_summary = ResponseSummary(
            content_hash=content_hash,
            hash_domain=hash_domain,
            message_bytes=None,
            output_blocks=call.output_blocks,
        )
        request_summary = RequestSummary(
            model=model, message_count=None, message_bytes=None,
            system_hash=None, messages_hash=None, tools_hash=None, hash_domain=None,
        )
        payload = NativeLlmCallPayload(
            producer_call_id=call.message_id,
            model=model,
            # 子 agent 的 call 必须标 subagent：压缩 detector 只对
            # call_role=="main" 的相邻调用算 token delta（wire/compaction.py），
            # 把子 agent 混进 main 段会污染主 agent 的 token 曲线，并在
            # main→subagent→main 的边界上造出虚假 token drop（禁止
            # 跨 agent 比较相邻 calls）。blade normalizer 有同款修复。
            call_role="main" if call.agent_id == "main" else "subagent",
            request_summary=request_summary,
            response_summary=response_summary,
            usage=_usage_payload(call.usage),
            finish_reason=call.stop_reason,
        )
        return NativeLlmCallEvidence(
            evidence_id=ids.evidence_id(
                attempt_id=attempt_id, source_kind=SOURCE_KIND,
                source_instance=SOURCE_INSTANCE,
                raw_ref=f"{RAW_FILE}:{call.first_line}", producer_id=call.anchor,
            ),
            attempt_id=attempt_id,
            phase="agent_run",
            source=EvidenceSource(kind=SOURCE_KIND, instance=SOURCE_INSTANCE, version=cli_version),
            producer=EvidenceProducer(name=PRODUCER_NAME, version=PARSER_VERSION, event_id=call.message_id),
            time=EvidenceTime(observed_at=call.observed_at or "", started_at=None, finished_at=call.observed_at),
            raw_ref=EvidenceRawRef(kind="events-jsonl", file=RAW_FILE, line=call.first_line),
            correlation_hints=CorrelationHints(
                producer_session_id=session_id,
                producer_call_id=call.message_id,
                model=model,
                sequence=call.seq_no,
            ),
            capabilities={"call_boundary": True},
            redaction=EvidenceRedaction(policy="metadata", status="applied", hash_algorithm="sha256", hash_domain=hash_domain),
            errors=[],
            # sub-agent 拓扑：非 main agent 的 call 带 agent_id/parent（走
            # namespaced 扩展，CorrelationHints 无 agent_id 字段）。finalizer 读它填
            # canonical llm_call.correlation.agent_id。main call 不写（默认 main）。
            # 多轮时附 turn 归属（x-octagon.turn-id/-index），finalizer 的
            # _base_record 投影成 canonical correlation.turn_*（confidence=explicit）。
            extensions=_call_extensions(call),
            payload=payload,
        )

    def _aggregate_evidence(
        self, attempt_id: str, usage: dict[str, Any],
        line: int | None, ts: str | None, session_id: str | None,
    ) -> AggregateUsageEvidence:
        return AggregateUsageEvidence(
            evidence_id=ids.evidence_id(
                attempt_id=attempt_id, source_kind=SOURCE_KIND,
                source_instance=SOURCE_INSTANCE,
                raw_ref=f"{RAW_FILE}:{line}", producer_id="result-aggregate",
            ),
            attempt_id=attempt_id,
            phase="agent_run",
            source=EvidenceSource(kind=SOURCE_KIND, instance=SOURCE_INSTANCE),
            producer=EvidenceProducer(name=PRODUCER_NAME, version=PARSER_VERSION),
            time=EvidenceTime(observed_at=ts or "", started_at=None, finished_at=None),
            raw_ref=EvidenceRawRef(kind="events-jsonl", file=RAW_FILE, line=line),
            correlation_hints=CorrelationHints(producer_session_id=session_id),
            capabilities={},
            redaction=EvidenceRedaction(policy="metadata", status="applied", hash_algorithm=None, hash_domain=None),
            errors=[],
            extensions={},
            payload=AggregateUsagePayload(
                scope="attempt",
                usage=_usage_payload(usage),
                producer_event_type="result",
            ),
        )


def _lc(attempt_id: str, anchor: str) -> str:
    """trajectory step 的 logical_call_id：与 finalizer 的 lc 生成同源
    （anchor 未桥接前的确定值）。"""
    return ids.logical_call_id(attempt_id=attempt_id, call_anchor=anchor)


def _register_task_owner(st: "_ParseState", tool_use_id: str, owner: str) -> None:
    """登记 Task/Agent tool_use 的发起者（owner 索引）。

    同一个 tool_use_id 二次出现时**不能静默覆盖**：
    - owner 相同 → 幂等，保持（同一 invocation 的重复投影/断线重放）；
    - owner 不同 → 该 ID 在不同作用域被复用，父归属歧义。标记 ambiguous、
      把 owner 置为哨兵，后续 `_agent_of` 查到哨兵就返回"父未解析"并计入
      capability gap（不猜测、不静默归 main）。

    注意这只解决**父归属**的歧义。`agent_id` 仍由 tool_use_id 派生，所以两个
    复用同一 ID 的 invocation 会落进同一个 detector segment——那是更深的
    identity 问题（需要 scoped identity），当前 producer 未提供可用的作用域
    标识，因此在 gap 里如实标注 `duplicate_tool_use_id`，不假装已解决。
    """
    existing = st.task_owner.get(tool_use_id)
    if existing is None:
        st.task_owner[tool_use_id] = owner
        return
    if existing == owner:
        return  # 幂等
    st.task_owner[tool_use_id] = _AMBIGUOUS_OWNER
    st.unresolved_parents[tool_use_id] = "duplicate_tool_use_id"


def _parent_unresolved_evidence(
    attempt_id: str, unresolved: dict[str, str], last_ts: str | None,
) -> CaptureEventEvidence:
    """父 agent 解析失败 → capture_event（不静默归 main，留痕可追溯）。

    与 blade normalizer 的 `agent_parent_unresolved` 同款约定，便于 UI/分析
    统一消费两个 producer 的同类 gap。
    """
    detail = "; ".join(
        f"{tuid}:{reason}" for tuid, reason in list(unresolved.items())[:10]
    )
    return CaptureEventEvidence(
        evidence_id=ids.evidence_id(
            attempt_id=attempt_id, source_kind=SOURCE_KIND,
            source_instance=SOURCE_INSTANCE,
            raw_ref=f"{RAW_FILE}:parent-agent-resolution",
            producer_id="normalizer",
        ),
        attempt_id=attempt_id,
        phase="agent_run",
        source=EvidenceSource(kind=SOURCE_KIND, instance=SOURCE_INSTANCE),
        producer=EvidenceProducer(name=PRODUCER_NAME, version=PARSER_VERSION),
        time=EvidenceTime(
            observed_at=last_ts or _EPOCH_TS, started_at=None, finished_at=None
        ),
        raw_ref=EvidenceRawRef(kind="events-jsonl", file=RAW_FILE, line=None),
        correlation_hints=CorrelationHints(),
        capabilities={},
        redaction=EvidenceRedaction(policy="metadata", status="applied"),
        errors=[],
        extensions={},
        payload=CaptureEventPayload(
            event="error",
            source_instance=SOURCE_INSTANCE,
            status=None,
            reason_code="agent_parent_unresolved",
            message=detail,
            counters={"unresolved_parents": len(unresolved)},
            effective_capabilities=None,
        ),
    )


def _agent_of(
    parent_tool_use_id: Any, st: "_ParseState | None" = None
) -> tuple[str, str | None]:
    """事件的 agent 归属（嵌套拓扑）。

    CC 的 Task 子 agent 事件带 `parent_tool_use_id`（顶层字段）：
    - 无 → main；
    - 有 → 子 agent，`agent_id` 由该 ID 稳定派生（`sub-<tool_use_id>`，与
      迁移前一致）；父 agent 从 `st.task_owner` 索引查——**发起这个 Task 的
      agent 才是父**。一级子 agent 的 owner 是 main；二级的 owner 是那个一级
      子 agent。不查索引就只能一律写 main，二级及更深的嵌套关系被压平
      （明确禁止"不能一律挂到 main"）。

    索引里查不到（孤儿事件：父 tool_use 未出现在流里，可能被截断或跨文件）
    时保留 identity 但父置 None，并记 unresolved 原因供 capability gap——
    不猜测、不静默归 main（与 blade normalizer 的
    `parent_agent_resolution` 同款约定）。

    `st=None` 时退化为一级行为（父恒为 main），供不关心嵌套的调用点使用。
    """
    if not (isinstance(parent_tool_use_id, str) and parent_tool_use_id):
        return "main", None
    agent_id = f"sub-{parent_tool_use_id}"
    if st is None:
        return agent_id, "main"
    owner = st.task_owner.get(parent_tool_use_id)
    if owner is None:
        st.unresolved_parents.setdefault(
            parent_tool_use_id, "task_tool_use_not_seen"
        )
        return agent_id, None
    if owner == _AMBIGUOUS_OWNER:
        # 该 ID 在多个作用域被复用，父归属歧义（reason 已在登记时写入）。
        # 保留 identity、父置 None——不在两个候选里猜一个。
        return agent_id, None
    return agent_id, owner


