"""blade-agent native normalizer。

输入优先级：

1. ``events.jsonl``（主数据源）——adapter 落盘的行，按每行 ``source`` 分流：
   - ``"socket"``：SDK RawEvent，形状 ``{"kind", "timestamp", "raw", "source"}``，
     ``raw = {"type", "payload", "stream_sequence"}``——**loop_name/tool_call_id
     都在 raw.payload 里**（Phase 0.5 样本 810 行实证零例外）；
   - ``"messages_poll"``：断线恢复轮询 ``/messages`` 的原始 entry，``raw`` 就是
     entry 本身 ``{id, kind, loop_name, message, parent_id, timestamp}``，
     **没有 payload 包装层**，``message.tool_call_id`` 在 assistant/tool 两侧
     都精确存在——不需要 FIFO。
2. ``blade_history.json``（降级来源，仅 events.jsonl 缺失时）——``get_history()``
   投影 node，tool 节点**不带** tool_call_id，唯一允许 FIFO 猜配的场景，
   产出的 step 标 ``attributes.tool_call_id_source="fifo_inferred"``。
3. ``trace.jsonl`` 不作为输入（已是 FIFO 猜配过的有损投影）。

sub-agent 拓扑：``loop_name`` 是执行单元标识（root=主循环，fork 出的
子 agent 形如 ``agent:<8hex>``）。父子关系用 **索引查找**：
``agent:start.payload.parent_fork_tool_call_id`` 查
``tool_call_id → {候选 loop_name}`` 索引，候选恰好 1 个才采用（root 转写为
main）；0 个或多个都保持 ``parent_agent_id=None``，原因写入
``attributes["parent_agent_resolution"]`` 并计一条 capture_event——不猜测、
不静默归 main。``agent_id`` 永远是真实 loop_name，不因父级未知被改写。

去重语义（socket/poll 双写是**常态**——monitor 在 socket 健康时也把每个
终态 turn 写成 poll 行）：

- socket↔socket 重放：``stream_sequence``（raw 顶层）精确去重；
- poll↔poll 重复：history entry ``id`` 精确去重；
- 跨 envelope（socket↔poll 同一 turn 双写）：
  - 带工具调用的 turn 以 ``(agent_id, tool_call_id)`` 锚定——assistant step
    只在"该 turn 的所有 call 都未见过"时产出（对称适用于两个方向），
    tool_call step 逐个补缺（不因部分已见而整行丢弃）；
  - 纯文本 turn 用**跨 envelope 计数对消**（每侧一个 unmatched 计数器，
    另一侧同 (agent, hash) 的行先对消再产出）——不是永久 set：同一 agent
    两次合法说出相同文本是两个 turn，都必须保留。
- 已知残余：若同一文本 turn 的 socket/poll 两侧内容不一致（如 poll 侧被
  截断），对消失败会多出一个 step——按 fail-open 宁多勿删。

异常契约：原始行形状非法（缺 raw/message 等**数据问题**）抛
``_RowParseError`` 逐行兜住计 parse error；``TrajectoryStep`` 构造错误、
hash helper 崩溃等 **programmer error 不捕获**，穿过 runner 向上传播
（在线由 lifecycle 兜底，离线 rebuild fail-fast）。

本模块 attributes 的约定 key：

- ``skill_id``/``skill_tool``/``skill_args``：Bash 调 ``blade skill run`` 的
  业务语义（复用 adapter 的 ``_parse_blade_skill_run``，tool_name 仍如实记
  ``Bash``，不改写证据）；
- ``tool_call_id_source="fifo_inferred"``：仅 history 降级路径；
- ``parent_agent_resolution``：父 agent 解析失败原因（``agent_start_not_found``
  / ``parent_fork_tool_call_id_unresolved`` / ``tool_call_id_ambiguous``）；
- ``deprecated=True``：history 降级路径里 ``is_deprecated`` 的节点
  （保留不裁剪，下游自行决定过滤）。

已知覆盖缺口（不隐瞒）：

- **headless 屏蔽 fork**：Octagon 评测走 ``headless=True``，blade 的
  ``fork:Agent`` 被 ``headless_setup.py`` 硬编码屏蔽——正常评测 attempt 的
  trajectory 预期恒为单 agent（main）；
- **旧 SDK 残缺形态**：blade-agent-kit ≤1.0.30（PyPI 全部版本）不支持
  turn:events 新协议，旧 SDK 采集的 events.jsonl 几乎只有 messages_poll
  兜底行——本 normalizer 对这种文件自然退化为纯 polling 解析；
- polling（``entries()`` 沿分支链）与 history 降级（``list_history()`` 全量
  含 deprecated）对"分支"的覆盖不同；
- **per-call evidence（方案1，2026-07-17 加）**：每个去重后的 socket
  ``llm:response:done`` 产一条 ``native_llm_call`` evidence（usage/model/
  finish_reason，anchor 用 sequence anchor 与 finalizer 的 lc 派生同源），
  该回合的 assistant/tool_call step 填同源 ``logical_call_id``——Wire 页
  泳道与轨迹面板因此可双向跳转。信息密度低于 CC（无 HTTP hop、时间是
  socket 到达近似、无 request summary），poll-only 残缺文件无 per-call
  evidence（usage 不可得，不伪造）。

``llm:tool_call:created`` 是流式分片（Phase 0.5：326 事件仅 9 唯一 id），
不用于建 step；``llm:response:done.payload.tool_calls[]`` 是权威完整列表。
新协议 socket 流没有 turn:end——``kind=="turn:end"`` 的行只可能来自
messages_poll。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from backend.adapters.blade_service import _parse_blade_skill_run
from backend.wire import correlate, hashing, ids
from backend.wire import turn_correlation as _turn

# events.jsonl 行级 turn 归属键（adapter with_turn_ext 写入，与 canonical evidence
# 的 turn extension 同名）。
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

PRODUCER_NAME = "blade-agent"
PARSER_VERSION = "blade-normalizer-v1"
SOURCE_KIND = "native-event"
SOURCE_INSTANCE = "native-event"
RAW_FILE = "events.jsonl"
HISTORY_FILE = "blade_history.json"

# 与 runner._derived_ts 相同的确定性回退（观测时间不可得时不写空串）
_EPOCH_TS = "1970-01-01T00:00:00.000Z"

_MAIN = "main"
_ROOT = "root"


class _RowParseError(ValueError):
    """单行原始数据形状非法（**数据问题**，非编程错误）。

    只有这个异常会被逐行兜住计 parse error；TrajectoryStep 构造失败等
    programmer error 不捕获，向上传播（两类失败区分）。
    """


def _iter_rows(path: Path) -> Iterator[tuple[int, dict[str, Any] | None]]:
    with path.open("r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                yield lineno, json.loads(raw)
            except json.JSONDecodeError:
                yield lineno, None


def _parse_tool_arguments(raw_args: Any) -> Any:
    """OpenAI 格式 tool_call 的 function.arguments 是 JSON 字符串；解析失败
    保留原文（不伪造结构）。"""
    if isinstance(raw_args, str):
        try:
            return json.loads(raw_args)
        except ValueError:
            return raw_args
    return raw_args


def _skill_attributes(tool_name: str | None, arguments: Any) -> dict[str, Any] | None:
    """blade skill 业务语义：只在 Bash 调 `blade skill run` 时返回。"""
    if tool_name != "Bash" or not isinstance(arguments, dict):
        return None
    skill_call = _parse_blade_skill_run(arguments)
    if skill_call is None:
        return None
    skill_id, skill_tool, skill_args = skill_call
    return {"skill_id": skill_id, "skill_tool": skill_tool, "skill_args": skill_args}


def _agent_of(loop_name: Any) -> str:
    """loop_name → agent_id：root 对外统一为 main，其余保留原值。"""
    if not isinstance(loop_name, str) or not loop_name or loop_name == _ROOT:
        return _MAIN
    return loop_name


@dataclass
class _Topology:
    """两遍扫描的第一遍产物：agent:start 表 + tool_call_id 候选集合索引。"""

    # loop_name -> agent:start payload
    agent_starts: dict[str, dict[str, Any]] = field(default_factory=dict)
    # tool_call_id -> {候选 loop_name}（集合语义：不猜测归属，候选 !=1 即未知）
    tool_call_index: dict[str, set[str]] = field(default_factory=dict)

    def add_call(self, tool_call_id: Any, loop_name: Any) -> None:
        if isinstance(tool_call_id, str) and tool_call_id:
            self.tool_call_index.setdefault(tool_call_id, set()).add(
                loop_name if isinstance(loop_name, str) and loop_name else _ROOT
            )

    def resolve_parent(self, loop_name: str) -> tuple[str | None, str | None]:
        """(parent_agent_id, unresolved_reason)。成功时 reason 为 None。"""
        start = self.agent_starts.get(loop_name)
        if start is None:
            return None, "agent_start_not_found"
        pftc = start.get("parent_fork_tool_call_id")
        if not isinstance(pftc, str) or not pftc:
            return None, "parent_fork_tool_call_id_unresolved"
        candidates = self.tool_call_index.get(pftc, set())
        if len(candidates) == 1:
            return _agent_of(next(iter(candidates))), None
        if len(candidates) > 1:
            return None, "tool_call_id_ambiguous"
        return None, "parent_fork_tool_call_id_unresolved"


@dataclass
class _State:
    steps: list[TrajectoryStep] = field(default_factory=list)
    step_seq: int = 0
    last_ts: str | None = None
    # socket↔socket 重放去重（断线重连回放同一事件）
    seen_stream_seqs: set[int] = field(default_factory=set)
    # poll↔poll 重复去重（history entry id 是精确身份）
    seen_poll_entry_ids: set[str] = field(default_factory=set)
    # 跨 envelope：带工具调用的 turn 以 (agent_id, tool_call_id) 锚定
    seen_tool_calls: set[tuple[str, str]] = field(default_factory=set)
    seen_tool_results: set[tuple[str, str]] = field(default_factory=set)
    # 跨 envelope：纯文本 turn 计数对消（每侧一个 unmatched 计数，另一侧
    # 同 key 先对消再产出）。key = (agent_id, content_hash)。
    # 不用永久 set：同 agent 两次合法说相同文本是两个 turn，都要保留。
    sock_text_unmatched: dict[tuple[str, str | None], int] = field(default_factory=dict)
    poll_text_unmatched: dict[tuple[str, str | None], int] = field(default_factory=dict)
    # 父 agent 解析失败聚合：loop_name -> reason（进 capture_event）
    unresolved_parents: dict[str, str] = field(default_factory=dict)
    # per-call evidence（方案1）：去重后的 response:done 计数（sequence anchor
    # 的确定性序号，重跑幂等）与产出的 evidence
    call_seq: int = 0
    call_evidence: list[Any] = field(default_factory=list)


class BladeNormalizer:
    producer = PRODUCER_NAME
    parser_version = PARSER_VERSION
    raw_file = RAW_FILE

    def has_input(self, attempt_dir: Path) -> bool:
        """events.jsonl 或 blade_history.json 任一存在即有输入——这是
        blade_history.json 降级分支真正可达的前提（runner 不再硬编码单一
        文件名）。"""
        attempt_dir = Path(attempt_dir)
        return (attempt_dir / RAW_FILE).exists() or (
            attempt_dir / HISTORY_FILE
        ).exists()

    def normalize(self, *, attempt_id: str, attempt_dir: Path) -> NormalizeResult:
        attempt_dir = Path(attempt_dir)
        events_path = attempt_dir / RAW_FILE
        if not events_path.exists():
            return self._normalize_from_history_snapshot(attempt_id, attempt_dir)

        result = NormalizeResult()
        result.raw_file = RAW_FILE
        rows: list[tuple[int, dict[str, Any]]] = []
        for lineno, row in _iter_rows(events_path):
            if row is None or not isinstance(row, dict):
                result.record_error(lineno)
                continue
            ts = row.get("timestamp")
            if isinstance(ts, str) and ts:
                result.last_ts = ts
            rows.append((lineno, row))

        # 第一遍：拓扑（agent:start + tool_call 候选索引）。索引同时吃 socket
        # 权威列表与 poll 行的 assistant tool_calls——poll-only 文件（旧 SDK
        # 残缺形态）也能建索引。
        topo = _Topology()
        for _, row in rows:
            source = row.get("source")
            raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
            if source == "socket":
                payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
                etype = raw.get("type")
                loop = payload.get("loop_name")
                if etype == "agent:start" and isinstance(payload.get("loop_name"), str):
                    topo.agent_starts.setdefault(payload["loop_name"], payload)
                elif etype == "llm:response:done":
                    for tc in payload.get("tool_calls") or []:
                        if isinstance(tc, dict):
                            topo.add_call(tc.get("id"), loop)
                elif etype == "llm:tool_call:created":
                    topo.add_call(payload.get("id") or payload.get("tool_call_id"), loop)
            elif source == "messages_poll":
                message = raw.get("message") if isinstance(raw.get("message"), dict) else {}
                if message.get("role") == "assistant":
                    for tc in message.get("tool_calls") or []:
                        if isinstance(tc, dict):
                            topo.add_call(tc.get("id"), raw.get("loop_name"))

        # 第二遍：建 step。只兜 _RowParseError（数据形状非法）；
        # TrajectoryStep 构造错误等 programmer error 向上传播。
        st = _State()
        st.last_ts = result.last_ts
        for lineno, row in rows:
            source = row.get("source")
            try:
                if source == "socket":
                    self._apply_socket_row(row, lineno, attempt_id, st, topo)
                elif source == "messages_poll":
                    self._apply_polling_row(row, lineno, attempt_id, st, topo)
                else:
                    raise _RowParseError(f"未知 source: {source!r}")
            except _RowParseError:
                result.record_error(lineno)

        # per-call evidence（方案1）在前，capture_event 在后
        result.evidence.extend(st.call_evidence)

        # 父 agent 解析失败 → capture_event（fail-open：留痕不阻断）
        if st.unresolved_parents:
            result.evidence.append(
                _parent_unresolved_evidence(
                    attempt_id, st.unresolved_parents, result.last_ts, RAW_FILE
                )
            )

        return self._finish(result, attempt_id, st.steps, RAW_FILE)

    # ---- socket 行 ---------------------------------------------------------

    def _apply_socket_row(
        self, row: dict[str, Any], lineno: int, attempt_id: str,
        st: _State, topo: _Topology,
    ) -> None:
        raw = row.get("raw")
        if not isinstance(raw, dict):
            raise _RowParseError("socket 行缺 raw")
        etype = raw.get("type")
        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
        seq = raw.get("stream_sequence")
        if isinstance(seq, int):
            if seq in st.seen_stream_seqs:
                return  # 断线重放的重复事件
            st.seen_stream_seqs.add(seq)

        ts = row.get("timestamp")
        agent_id = _agent_of(payload.get("loop_name"))
        parent_id, reason = self._parent_of(agent_id, topo, st)
        # 行级 turn 归属（adapter with_turn_ext 写入）。blade 走时间窗口
        # inferred 为主，但若 events 行带显式 turn（多轮 driver 打标），也投影成
        # explicit——两条路径互补，显式优先。
        turn_id = row.get(_TURN_ID_KEY)
        turn_index = row.get(_TURN_INDEX_KEY)

        if etype == "llm:response:done":
            content = payload.get("content")
            tool_calls = [
                tc for tc in (payload.get("tool_calls") or []) if isinstance(tc, dict)
            ]
            # per-call evidence（方案1）：每个去重后的 response:done 是一次
            # LLM 调用。anchor 用 sequence anchor（确定性序号，重跑幂等），
            # 与 finalizer 的 lc 派生同源——step 与泳道调用因此可双向跳转。
            anchor = correlate.sequence_anchor(
                SOURCE_KIND, SOURCE_INSTANCE, st.call_seq
            )
            lc = ids.logical_call_id(attempt_id=attempt_id, call_anchor=anchor)
            st.call_evidence.append(_call_evidence(
                attempt_id=attempt_id, payload=payload, lineno=lineno,
                seq_no=st.call_seq, ts=ts if isinstance(ts, str) else None,
                agent_id=agent_id, parent_id=parent_id,
                turn_id=turn_id if isinstance(turn_id, str) else None,
                turn_index=turn_index if isinstance(turn_index, int) else None,
            ))
            st.call_seq += 1
            self._emit_turn(
                envelope="socket", raw_file=RAW_FILE, lineno=lineno,
                attempt_id=attempt_id, st=st, ts=ts,
                agent_id=agent_id, parent_id=parent_id, reason=reason,
                content=content, tool_calls=tool_calls,
                logical_call_id=lc,
            )
            return

        if etype == "tool:result:done":
            tool_call_id = payload.get("tool_call_id")
            self._emit_tool_result(
                raw_file=RAW_FILE, lineno=lineno, attempt_id=attempt_id, st=st,
                ts=ts, agent_id=agent_id, parent_id=parent_id, reason=reason,
                tool_call_id=tool_call_id,
            )
            return
        # llm:tool_call:created（流式分片，权威列表在 response:done）、
        # llm:*:delta、loop:turn、agent:start/end、chat:end 等：不建 step

    # ---- messages_poll 行 --------------------------------------------------

    def _apply_polling_row(
        self, row: dict[str, Any], lineno: int, attempt_id: str,
        st: _State, topo: _Topology,
    ) -> None:
        """poll 行的 raw 是 entries() 原始 entry：{id, kind, loop_name,
        message, parent_id, timestamp}——没有 payload 包装层，不要照搬
        socket 的解析路径。"""
        raw = row.get("raw")
        if not isinstance(raw, dict):
            raise _RowParseError("poll 行缺 raw")
        entry_id = raw.get("id")
        if isinstance(entry_id, str) and entry_id:
            if entry_id in st.seen_poll_entry_ids:
                return  # poll↔poll 精确重复（如 recovery 重复回放）
            st.seen_poll_entry_ids.add(entry_id)
        message = raw.get("message") if isinstance(raw.get("message"), dict) else {}
        role = message.get("role")
        ts = row.get("timestamp")
        agent_id = _agent_of(raw.get("loop_name"))
        parent_id, reason = self._parent_of(agent_id, topo, st)

        if role == "assistant":
            tool_calls = [
                tc for tc in (message.get("tool_calls") or []) if isinstance(tc, dict)
            ]
            content = message.get("content")
            self._emit_turn(
                envelope="poll", raw_file=RAW_FILE, lineno=lineno,
                attempt_id=attempt_id, st=st, ts=ts,
                agent_id=agent_id, parent_id=parent_id, reason=reason,
                content=content, tool_calls=tool_calls,
            )
            return

        if role == "tool":
            # entries() 的 tool message 精确带 tool_call_id（loop.py:927 写入）
            # ——直接映射，不需要 FIFO
            self._emit_tool_result(
                raw_file=RAW_FILE, lineno=lineno, attempt_id=attempt_id, st=st,
                ts=ts, agent_id=agent_id, parent_id=parent_id, reason=reason,
                tool_call_id=message.get("tool_call_id"),
            )
        # role == "user" / 其他 kind：不建 step（与 CC/Codex 的 step 语义一致）

    # ---- turn/step 产出（socket 与 poll 共用，对称去重） ---------------------

    def _emit_turn(
        self, *, envelope: str, raw_file: str, lineno: int, attempt_id: str,
        st: _State, ts: Any, agent_id: str, parent_id: str | None,
        reason: str | None, content: Any, tool_calls: list[dict[str, Any]],
        extra_attrs: dict[str, Any] | None = None,
        logical_call_id: str | None = None,
    ) -> None:
        """一个 assistant turn（可带工具调用）→ assistant step + tool_call
        steps。跨 envelope 去重对两个方向对称：

        - 带工具调用：该 turn 任一 call 已见 → assistant 不再产出（turn 已由
          另一 envelope 表达过）；tool_call **逐个补缺**，不整行丢弃；
        - 纯文本：跨 envelope 计数对消（见 _State 注释）。
        """
        a_hash, a_bytes = (None, None)
        if isinstance(content, str) and content:
            a_hash, a_bytes = hashing.part_semantic_hash(
                [{"type": "text", "text": content}]
            )

        emit_assistant = True
        if tool_calls:
            emit_assistant = not any(
                (agent_id, tc.get("id")) in st.seen_tool_calls
                for tc in tool_calls if isinstance(tc.get("id"), str)
            )
        else:
            key = (agent_id, a_hash)
            mine, theirs = (
                (st.sock_text_unmatched, st.poll_text_unmatched)
                if envelope == "socket"
                else (st.poll_text_unmatched, st.sock_text_unmatched)
            )
            if theirs.get(key, 0) > 0:
                theirs[key] -= 1
                emit_assistant = False  # 另一 envelope 已产出，本行是双写对消
            else:
                mine[key] = mine.get(key, 0) + 1

        if emit_assistant:
            st.step_seq += 1
            st.steps.append(TrajectoryStep(
                step_id=ids.trajectory_step_id(
                    attempt_id=attempt_id,
                    step_anchor=f"{raw_file}:{lineno}:assistant",
                ),
                sequence=st.step_seq, timestamp=ts, kind="assistant",
                producer_event_refs=({"file": raw_file, "line": lineno},),
                logical_call_id=logical_call_id,
                content_hash=a_hash, content_bytes=a_bytes,
                agent_id=agent_id, parent_agent_id=parent_id,
                attributes=_merge_attrs(_resolution_attr(reason), extra_attrs),
            ))
        for tc in tool_calls:
            self._append_tool_call(
                tc, raw_file, lineno, attempt_id, st,
                ts=ts, agent_id=agent_id, parent_id=parent_id, reason=reason,
                extra_attrs=extra_attrs, logical_call_id=logical_call_id,
            )

    def _emit_tool_result(
        self, *, raw_file: str, lineno: int, attempt_id: str, st: _State,
        ts: Any, agent_id: str, parent_id: str | None, reason: str | None,
        tool_call_id: Any, extra_attrs: dict[str, Any] | None = None,
    ) -> None:
        if isinstance(tool_call_id, str) and tool_call_id:
            key = (agent_id, tool_call_id)
            if key in st.seen_tool_results:
                return
            st.seen_tool_results.add(key)
        st.step_seq += 1
        st.steps.append(TrajectoryStep(
            step_id=ids.trajectory_step_id(
                attempt_id=attempt_id,
                step_anchor=f"{raw_file}:{lineno}:tool_result:{tool_call_id}",
            ),
            sequence=st.step_seq, timestamp=ts, kind="tool_result",
            producer_event_refs=({"file": raw_file, "line": lineno},),
            tool_call_id=tool_call_id if isinstance(tool_call_id, str) else None,
            agent_id=agent_id, parent_agent_id=parent_id,
            attributes=_merge_attrs(_resolution_attr(reason), extra_attrs),
        ))

    def _append_tool_call(
        self, tc: dict[str, Any], raw_file: str, lineno: int, attempt_id: str,
        st: _State, *, ts: Any, agent_id: str, parent_id: str | None,
        reason: str | None, extra_attrs: dict[str, Any] | None = None,
        logical_call_id: str | None = None,
    ) -> None:
        tc_id = tc.get("id")
        if isinstance(tc_id, str) and tc_id:
            key = (agent_id, tc_id)
            if key in st.seen_tool_calls:
                return
            st.seen_tool_calls.add(key)
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
        tool_name = fn.get("name") if isinstance(fn.get("name"), str) else None
        arguments = _parse_tool_arguments(fn.get("arguments"))
        tc_hash, tc_bytes = hashing.part_semantic_hash([{
            "type": "tool_call", "name": tool_name or "", "arguments": arguments,
        }])
        attributes = _resolution_attr(reason) or {}
        skill = _skill_attributes(tool_name, arguments)
        if skill:
            attributes.update(skill)
        if extra_attrs:
            attributes.update(extra_attrs)
        st.step_seq += 1
        st.steps.append(TrajectoryStep(
            step_id=ids.trajectory_step_id(
                attempt_id=attempt_id,
                step_anchor=f"{raw_file}:{lineno}:tool_call:{tc_id}",
            ),
            sequence=st.step_seq, timestamp=ts, kind="tool_call",
            producer_event_refs=({"file": raw_file, "line": lineno},),
            tool_call_id=tc_id if isinstance(tc_id, str) else None,
            tool_name=tool_name,
            logical_call_id=logical_call_id,
            content_hash=tc_hash, content_bytes=tc_bytes,
            agent_id=agent_id, parent_agent_id=parent_id,
            attributes=attributes or None,
        ))

    def _parent_of(
        self, agent_id: str, topo: _Topology, st: _State
    ) -> tuple[str | None, str | None]:
        """agent_id 的父级解析。main 无父；子 loop 走索引查找。失败原因
        记入 st.unresolved_parents（每个 loop 只记一次）。"""
        if agent_id == _MAIN:
            return None, None
        parent, reason = topo.resolve_parent(agent_id)
        if reason is not None:
            st.unresolved_parents.setdefault(agent_id, reason)
        return parent, reason

    # ---- blade_history.json 降级 -------------------------------------------

    def _normalize_from_history_snapshot(
        self, attempt_id: str, attempt_dir: Path
    ) -> NormalizeResult:
        """events.jsonl 缺失时读 get_history() 快照。/history 投影 node 的
        tool 节点不带 tool_call_id（phase0 实测 tool_call_id_in_tool_nodes:
        false）——唯一允许 FIFO 猜配的场景，标 fifo_inferred。"""
        result = NormalizeResult()
        result.raw_file = HISTORY_FILE
        history_path = attempt_dir / HISTORY_FILE
        if not history_path.exists():
            result.trajectory = empty_trajectory(attempt_id)
            return result
        try:
            data = json.loads(history_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            result.record_error(1)
            result.trajectory = empty_trajectory(attempt_id)
            return result
        nodes = data.get("nodes") if isinstance(data, dict) else None
        if not isinstance(nodes, list):
            result.trajectory = empty_trajectory(attempt_id)
            return result

        st = _State()
        # 无 agent:start 事件可用：非 root loop 的父级一律 unresolved
        topo = _Topology()
        # loop -> 待配对 tool_call FIFO 队列（_extract_trace_from_history 先例）
        pending: dict[str, list[dict[str, Any]]] = {}

        for idx, node in enumerate(nodes, start=1):
            if not isinstance(node, dict) or node.get("kind") != "message":
                continue
            role = node.get("role")
            ts = node.get("timestamp")
            if isinstance(ts, str) and ts:
                result.last_ts = ts
            agent_id = _agent_of(node.get("loop_name"))
            parent_id, reason = self._parent_of(agent_id, topo, st)
            base_attrs: dict[str, Any] = {}
            if node.get("is_deprecated"):
                base_attrs["deprecated"] = True  # 保留并标记，不裁剪

            if role == "assistant":
                tool_calls = [
                    tc for tc in (node.get("tool_calls") or []) if isinstance(tc, dict)
                ]
                for tc in tool_calls:
                    pending.setdefault(agent_id, []).append(tc)
                self._emit_turn(
                    envelope="poll", raw_file=HISTORY_FILE, lineno=idx,
                    attempt_id=attempt_id, st=st, ts=ts,
                    agent_id=agent_id, parent_id=parent_id, reason=reason,
                    content=node.get("content"), tool_calls=tool_calls,
                    extra_attrs=base_attrs or None,
                )
                continue

            if role == "tool":
                queue = pending.get(agent_id) or []
                tc = queue.pop(0) if queue else {}
                tool_call_id = tc.get("id") if isinstance(tc, dict) else None
                attrs = dict(base_attrs)
                # FIFO 猜配的引用必须与精确引用可区分
                attrs["tool_call_id_source"] = "fifo_inferred"
                self._emit_tool_result(
                    raw_file=HISTORY_FILE, lineno=idx, attempt_id=attempt_id,
                    st=st, ts=ts, agent_id=agent_id, parent_id=parent_id,
                    reason=reason, tool_call_id=tool_call_id,
                    extra_attrs=attrs,
                )

        if st.unresolved_parents:
            result.evidence.append(
                _parent_unresolved_evidence(
                    attempt_id, st.unresolved_parents, result.last_ts, HISTORY_FILE
                )
            )
        return self._finish(result, attempt_id, st.steps, HISTORY_FILE)

    # ---- 收尾 --------------------------------------------------------------

    def _finish(
        self, result: NormalizeResult, attempt_id: str,
        steps: list[TrajectoryStep], raw_file: str,
    ) -> NormalizeResult:
        """构造 Trajectory + 结构校验 + 序列化。构造期 ValueError（编程错误）
        不捕获向上传播（第一类）；validate() 走 fail-open。"""
        trajectory = Trajectory(
            schema_version=TRAJECTORY_SCHEMA_VERSION,
            attempt_id=attempt_id,
            steps=tuple(steps),
            producer=PRODUCER_NAME,
        )
        v_errors = trajectory.validate()
        if v_errors:
            result.evidence.append(trajectory_validation_evidence(
                attempt_id=attempt_id, producer=PRODUCER_NAME,
                parser_version=PARSER_VERSION, errors=v_errors,
                last_ts=result.last_ts, raw_file=raw_file,
            ))
        result.trajectory = trajectory_to_dict(trajectory)
        return result


def _resolution_attr(reason: str | None) -> dict[str, Any] | None:
    return {"parent_agent_resolution": reason} if reason else None


def _merge_attrs(
    a: dict[str, Any] | None, b: dict[str, Any] | None
) -> dict[str, Any] | None:
    if not a and not b:
        return None
    merged: dict[str, Any] = {}
    if a:
        merged.update(a)
    if b:
        merged.update(b)
    return merged


def _usage_payload_blade(usage: Any) -> UsagePayload:
    """blade 的 usage 是 OpenAI 形状（prompt/completion_tokens + details）。
    缺失字段写 None（区分零与不可得），不伪造。"""
    u = usage if isinstance(usage, dict) else {}
    p_details = u.get("prompt_tokens_details")
    c_details = u.get("completion_tokens_details")
    p_details = p_details if isinstance(p_details, dict) else {}
    c_details = c_details if isinstance(c_details, dict) else {}
    return UsagePayload(
        input_tokens=u.get("prompt_tokens", u.get("input_tokens")),
        output_tokens=u.get("completion_tokens", u.get("output_tokens")),
        cache_read_tokens=p_details.get("cached_tokens"),
        cache_write_tokens=p_details.get("cache_write_tokens"),
        reasoning_tokens=c_details.get("reasoning_tokens"),
        estimated=False,
    )


def _blade_call_extensions(
    agent_id: str, parent_id: str | None,
    turn_id: str | None, turn_index: int | None,
) -> dict[str, Any]:
    """blade call 的 evidence extensions：sub-agent 拓扑 + 可选 turn 归属。

    main + 无 turn → 空 dict（产物逐字节与改造前一致）。
    """
    ext: dict[str, Any] = {}
    if agent_id != _MAIN:
        ext["x-octagon.agent-id"] = agent_id
        ext["x-octagon.parent-agent-id"] = parent_id
    if turn_id is not None:
        ext[_TURN_ID_KEY] = turn_id
        if turn_index is not None:
            ext[_TURN_INDEX_KEY] = turn_index
    return ext


def _call_evidence(
    *, attempt_id: str, payload: dict[str, Any], lineno: int, seq_no: int,
    ts: str | None, agent_id: str, parent_id: str | None,
    turn_id: str | None = None, turn_index: int | None = None,
) -> NativeLlmCallEvidence:
    """socket llm:response:done → native_llm_call evidence（方案1）。

    blade 事件无 producer 侧调用 ID，anchor 走 sequence anchor
    （confidence=inferred，与 CC 的无 message-id 路径同款）；instance 段必须
    与 spool 文件名一致（native-event），否则 finalizer 对 orphan call 算出
    不同 lc（CC normalizer 同注释）。请求侧信息不可得（blade server 内部
    组装 prompt），request_summary 全 None——不伪造。
    """
    content = payload.get("content")
    parts = (
        [{"type": "text", "text": content}]
        if isinstance(content, str) and content else []
    )
    content_hash, _ = hashing.part_semantic_hash(parts)
    hash_domain = hashing.DOMAIN_SEMANTIC if content_hash else None
    model = payload.get("model") if isinstance(payload.get("model"), str) else None
    finish = payload.get("finish_reason")
    tool_calls = [tc for tc in (payload.get("tool_calls") or []) if isinstance(tc, dict)]
    return NativeLlmCallEvidence(
        evidence_id=ids.evidence_id(
            attempt_id=attempt_id, source_kind=SOURCE_KIND,
            source_instance=SOURCE_INSTANCE,
            raw_ref=f"{RAW_FILE}:{lineno}",
            producer_id=f"response-done-{seq_no}",
        ),
        attempt_id=attempt_id,
        phase="agent_run",
        source=EvidenceSource(kind=SOURCE_KIND, instance=SOURCE_INSTANCE),
        producer=EvidenceProducer(name=PRODUCER_NAME, version=PARSER_VERSION),
        time=EvidenceTime(observed_at=ts or _EPOCH_TS, started_at=None, finished_at=ts),
        raw_ref=EvidenceRawRef(kind="events-jsonl", file=RAW_FILE, line=lineno),
        correlation_hints=CorrelationHints(
            producer_call_id=None,
            model=model,
            sequence=seq_no,
        ),
        capabilities={"call_boundary": True},
        redaction=EvidenceRedaction(
            policy="metadata", status="applied",
            hash_algorithm="sha256" if content_hash else None,
            hash_domain=hash_domain,
        ),
        errors=[],
        # sub-agent 拓扑：非 main 的调用带 agent 归属扩展（与 CC 同款约定）。
        # 有显式 turn 时附 turn 归属（finalizer 投成 explicit）；无则留给
        # 时间窗口 inferred 兜底（blade 主路径）。
        extensions=_blade_call_extensions(agent_id, parent_id, turn_id, turn_index),
        payload=NativeLlmCallPayload(
            producer_call_id=None,
            model=model,
            # fork 出的子 agent 必须标 subagent：压缩 detector 只对
            # call_role=="main" 的相邻调用算 token delta（compaction.py），
            # 把子 agent 的 call 混进 main 段会同时造成两种错误——污染主 agent
            # 的 token 曲线，以及在 main→subagent→main 的边界上产生虚假
            # token drop。非 headless 起 fork 真实可达，此路径不再是死代码。
            call_role="main" if agent_id == _MAIN else "subagent",
            request_summary=RequestSummary(
                model=model, message_count=None, message_bytes=None,
                system_hash=None, messages_hash=None, tools_hash=None,
                hash_domain=None,
            ),
            response_summary=ResponseSummary(
                content_hash=content_hash,
                hash_domain=hash_domain,
                message_bytes=None,
                output_blocks=(1 if parts else 0) + len(tool_calls),
            ),
            usage=_usage_payload_blade(payload.get("usage")),
            finish_reason=finish if isinstance(finish, str) else None,
        ),
    )


def _parent_unresolved_evidence(
    attempt_id: str, unresolved: dict[str, str], last_ts: str | None,
    raw_file: str,
) -> CaptureEventEvidence:
    """父 agent 解析失败 → capture_event（不静默归 main，留痕可追溯）。

    raw_file 指向本次实际解析的输入（events.jsonl 或 blade_history.json 降级），
    observed_at 不可得时用与 runner 相同的固定 epoch，不写空串。"""
    detail = "; ".join(f"{loop}:{reason}" for loop, reason in list(unresolved.items())[:10])
    return CaptureEventEvidence(
        evidence_id=ids.evidence_id(
            attempt_id=attempt_id, source_kind=SOURCE_KIND,
            source_instance=SOURCE_INSTANCE,
            raw_ref=f"{raw_file}:parent-agent-resolution", producer_id="normalizer",
        ),
        attempt_id=attempt_id,
        phase="agent_run",
        source=EvidenceSource(kind=SOURCE_KIND, instance=SOURCE_INSTANCE),
        producer=EvidenceProducer(name=PRODUCER_NAME, version=PARSER_VERSION),
        time=EvidenceTime(
            observed_at=last_ts or _EPOCH_TS, started_at=None, finished_at=None
        ),
        raw_ref=EvidenceRawRef(kind="events-jsonl", file=raw_file, line=None),
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
