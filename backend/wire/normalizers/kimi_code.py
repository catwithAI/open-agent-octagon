"""kimi-code native normalizer。

输入优先级（与 ``backend/atif/converters/kimi_code.py`` 同源）：

1. ``<home>/.kimi/sessions/<workdir-hash>/<session-uuid>/wire.jsonl``
   （``sandbox_home`` 或 ``.kimi-iso-home``，优先按 ``.kimi/kimi.json`` 的
   ``work_dirs[].last_session_id`` 定位，否则回落最新 mtime）：
   带时间戳的完整事件流，包含 ``TurnBegin``（用户输入）、``StepBegin``
   （推理步边界）、``ContentPart``（``think`` / ``text``）、``ToolCall``、
   ``ToolResult`` 以及分步 ``StatusUpdate.payload.token_usage``。
2. ``events.jsonl``（降级来源，当 ``wire.jsonl`` 缺失或为空时）：
   ``kimi -p --print --output-format stream-json`` 逐行落盘的事件流，
   包含 ``role in {"user", "assistant", "thinking", "tool"}`` 与
   ``session.resume_hint``。

输出：
- ``native_llm_call`` WireEvidence（每次推理步一条，使用 sequence anchor，
  ``call_boundary=True``，``logical_call_id`` 与 step 同源可双向跳转）；
- ``trajectory.json``（``user`` / ``thinking`` / ``assistant`` /
  ``tool_call`` / ``tool_result``，在可观测时于 step attributes 记录分步用量）。

关于 ``StatusUpdate.payload.token_usage`` 的语义对齐：
Kimi 的 ``wire.jsonl`` 把输入拆成 ``input_other`` / ``input_cache_read`` /
``input_cache_creation``，而 Kimi CLI 走 ``openai_legacy``（OpenAI Chat
Completions）协议通过 Octagon 反代发出请求，反代 ``http_exchange`` 采到的
``prompt_tokens`` 是含缓存的总输入。这里把 ``input_tokens`` 归一化为
``input_other + cache_read + cache_creation``，使 ``native_llm_call`` 与
``http_exchange``、以及 ``backend/cost/estimate.py`` 中 ``usage_source="wire"``
的 inclusive 口径完全一致，避免重复计费或对账假冲突。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from backend.wire import correlate, hashing, ids
from backend.wire import turn_correlation as _turn
from backend.wire.evidence import (
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
    _VALID_PRODUCERS,
    _check_cc_tool_call_pairing,
    _check_step_sequence,
    empty_trajectory,
    trajectory_step_to_dict,
    trajectory_to_dict,
)

PRODUCER_NAME = "kimi-code"
PARSER_VERSION = "kimi-code-normalizer-v1"
SOURCE_KIND = "native-event"
SOURCE_INSTANCE = "native-event"
RAW_FILE = "events.jsonl"

_EPOCH_TS = "1970-01-01T00:00:00.000Z"
_MAIN = "main"
_TURN_ID_KEY = _turn.EXT_TURN_ID
_TURN_INDEX_KEY = _turn.EXT_TURN_INDEX


def _iter_jsonl(path: Path) -> Iterator[tuple[int, dict[str, Any] | None]]:
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for lineno, raw in enumerate(fh, start=1):
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                value = json.loads(stripped)
            except json.JSONDecodeError:
                yield lineno, None
                continue
            yield lineno, value if isinstance(value, dict) else None


def _iso_from_seconds(seconds: Any) -> str | None:
    if isinstance(seconds, str):
        return seconds.strip() or None
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
        return None
    try:
        return (
            datetime.fromtimestamp(seconds, tz=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    except (OverflowError, OSError, ValueError):
        return None


def _nonneg_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        iv = int(value)
    except (TypeError, ValueError):
        return None
    return iv if iv >= 0 else None


def _parse_arguments(raw_args: Any) -> Any:
    if isinstance(raw_args, str):
        try:
            return json.loads(raw_args)
        except ValueError:
            return raw_args
    return raw_args


def _tool_output_text(return_value: Any) -> str:
    if isinstance(return_value, str):
        return return_value
    if not isinstance(return_value, dict):
        return ""
    for key in ("output", "message", "display"):
        value = return_value.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _find_session_dir_under(kimi_home: Path) -> Path | None:
    root = kimi_home / ".kimi" / "sessions"
    if not root.is_dir():
        return None

    wanted: set[str] = set()
    manifest = kimi_home / ".kimi" / "kimi.json"
    if manifest.is_file():
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        for entry in payload.get("work_dirs") or []:
            if isinstance(entry, dict) and entry.get("last_session_id"):
                wanted.add(str(entry["last_session_id"]))

    candidates = [p.parent for p in root.glob("*/*/wire.jsonl") if p.is_file()]
    if not candidates:
        return None
    for directory in candidates:
        if directory.name in wanted:
            return directory
    return max(candidates, key=lambda d: (d / "wire.jsonl").stat().st_mtime)


def _find_wire_file(attempt_dir: Path) -> Path | None:
    """按 sandbox_home → .kimi-iso-home → attempt_dir 顺序定位会话 wire.jsonl。"""
    for home in (
        attempt_dir / "sandbox_home",
        attempt_dir / ".kimi-iso-home",
        attempt_dir,
    ):
        session_dir = _find_session_dir_under(home)
        if session_dir is not None:
            wire_path = session_dir / "wire.jsonl"
            if wire_path.is_file():
                return wire_path
    return None


def _usage_from_wire(usage: Any) -> UsagePayload | None:
    """Kimi wire.jsonl ``StatusUpdate.payload.token_usage`` → UsagePayload。

    ``input_other`` 与 ``input_cache_read`` / ``input_cache_creation`` 在 wire.jsonl
    里是分列上报的；统一折算成含缓存的总 ``input_tokens``（与 OpenAI proxy 的
    ``prompt_tokens`` 口径一致）。
    """
    if not isinstance(usage, dict) or not usage:
        return None
    input_other = _nonneg_int(usage.get("input_other"))
    cache_read = _nonneg_int(usage.get("input_cache_read"))
    cache_write = _nonneg_int(usage.get("input_cache_creation"))
    output = _nonneg_int(usage.get("output"))
    reasoning = _nonneg_int(usage.get("reasoning"))

    if input_other is not None:
        total_input: int | None = (
            input_other + (cache_read or 0) + (cache_write or 0)
        )
    elif cache_read is not None or cache_write is not None:
        total_input = (cache_read or 0) + (cache_write or 0)
    else:
        total_input = _nonneg_int(
            usage.get("prompt_tokens", usage.get("input_tokens"))
        )

    if (
        total_input is None
        and output is None
        and cache_read is None
        and cache_write is None
        and reasoning is None
    ):
        return None
    return UsagePayload(
        input_tokens=total_input,
        output_tokens=output,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        reasoning_tokens=reasoning,
        estimated=False,
    )


def _usage_from_events_row(usage: Any) -> UsagePayload | None:
    if not isinstance(usage, dict) or not usage:
        return None
    if "input_other" in usage or "input_cache_read" in usage:
        return _usage_from_wire(usage)
    p_details = usage.get("prompt_tokens_details")
    p_details = p_details if isinstance(p_details, dict) else {}
    c_details = usage.get("completion_tokens_details")
    c_details = c_details if isinstance(c_details, dict) else {}

    in_tok = _nonneg_int(usage.get("prompt_tokens", usage.get("input_tokens")))
    out_tok = _nonneg_int(usage.get("completion_tokens", usage.get("output_tokens")))
    cache_read = _nonneg_int(
        p_details.get("cached_tokens", usage.get("cache_read_tokens"))
    )
    cache_write = _nonneg_int(
        p_details.get("cache_write_tokens", usage.get("cache_write_tokens"))
    )
    reasoning = _nonneg_int(
        c_details.get("reasoning_tokens", usage.get("reasoning_tokens"))
    )
    if (
        in_tok is None
        and out_tok is None
        and cache_read is None
        and cache_write is None
        and reasoning is None
    ):
        return None
    return UsagePayload(
        input_tokens=in_tok,
        output_tokens=out_tok,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        reasoning_tokens=reasoning,
        estimated=False,
    )


def _usage_attr_dict(usage: UsagePayload | None) -> dict[str, int] | None:
    if usage is None:
        return None
    out: dict[str, int] = {}
    for key in (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
    ):
        val = getattr(usage, key, None)
        if val is not None:
            out[key] = val
    return out or None


@dataclass
class _WireToolCall:
    lineno: int
    ts: str | None
    call_id: str | None
    name: str | None
    arguments: Any


@dataclass
class _WireToolResult:
    lineno: int
    ts: str | None
    call_id: str | None
    output: str
    is_error: bool


@dataclass
class _WireStepSlot:
    step_n: int
    start_line: int
    last_line: int
    started_at: str | None
    finished_at: str | None
    thinking_parts: list[str] = field(default_factory=list)
    thinking_line: int | None = None
    thinking_ts: str | None = None
    text_parts: list[str] = field(default_factory=list)
    text_line: int | None = None
    text_ts: str | None = None
    tool_calls: list[_WireToolCall] = field(default_factory=list)
    tool_results: list[_WireToolResult] = field(default_factory=list)
    usage: dict[str, Any] | None = None


@dataclass
class _State:
    steps: list[TrajectoryStep] = field(default_factory=list)
    step_seq: int = 0
    call_seq: int = 0
    evidence: list[Any] = field(default_factory=list)
    last_ts: str | None = None
    session_id: str | None = None


class KimiCodeNormalizer:
    """kimi-code 的 native normalizer（wire.jsonl 优先，events.jsonl 降级）。"""

    producer = PRODUCER_NAME
    parser_version = PARSER_VERSION
    raw_file = RAW_FILE

    def has_input(self, attempt_dir: Path) -> bool:
        attempt_dir = Path(attempt_dir)
        return _find_wire_file(attempt_dir) is not None or (
            attempt_dir / RAW_FILE
        ).is_file()

    def normalize(self, *, attempt_id: str, attempt_dir: Path) -> NormalizeResult:
        attempt_dir = Path(attempt_dir)
        wire_path = _find_wire_file(attempt_dir)
        if wire_path is not None:
            wire_result = self._normalize_from_wire(
                attempt_id=attempt_id, attempt_dir=attempt_dir, wire_path=wire_path
            )
            step_count = len(wire_result.trajectory.get("steps", []))
            if (
                wire_result.evidence
                or step_count > 0
                or wire_result.parse_errors > 0
                or not (attempt_dir / RAW_FILE).is_file()
            ):
                return wire_result

        events_path = attempt_dir / RAW_FILE
        if events_path.is_file():
            return self._normalize_from_events(
                attempt_id=attempt_id, events_path=events_path
            )

        result = NormalizeResult(raw_file=RAW_FILE)
        result.trajectory = empty_trajectory(attempt_id)
        return result

    # ---------- wire.jsonl 路径 ----------

    def _normalize_from_wire(
        self, *, attempt_id: str, attempt_dir: Path, wire_path: Path
    ) -> NormalizeResult:
        try:
            rel_file = wire_path.relative_to(attempt_dir).as_posix()
        except ValueError:
            rel_file = wire_path.name

        result = NormalizeResult(raw_file=rel_file)
        st = _State(session_id=wire_path.parent.name or None)
        current: _WireStepSlot | None = None
        auto_step_n = 0

        def flush_slot() -> None:
            nonlocal current
            if current is None:
                return
            slot = current
            current = None
            self._emit_wire_slot(
                attempt_id=attempt_id, raw_file=rel_file, slot=slot, st=st
            )

        for lineno, record in _iter_jsonl(wire_path):
            if record is None:
                result.record_error(lineno)
                continue
            if record.get("type") == "metadata" or "message" not in record:
                continue
            message = record.get("message")
            if not isinstance(message, dict):
                result.record_error(lineno)
                continue

            ts = _iso_from_seconds(record.get("timestamp"))
            if ts:
                st.last_ts = ts
            mtype = message.get("type")
            payload = (
                message.get("payload")
                if isinstance(message.get("payload"), dict)
                else {}
            )

            if mtype == "TurnBegin":
                flush_slot()
                user_input = payload.get("user_input")
                if isinstance(user_input, str) and user_input:
                    self._append_text_step(
                        attempt_id=attempt_id,
                        raw_file=rel_file,
                        lineno=lineno,
                        kind="user",
                        text=user_input,
                        ts=ts,
                        st=st,
                        anchor_suffix="turn_begin",
                    )
                continue

            if mtype == "TurnEnd":
                flush_slot()
                continue

            if mtype == "StepBegin":
                flush_slot()
                raw_n = payload.get("n")
                auto_step_n += 1
                step_n = (
                    raw_n
                    if isinstance(raw_n, int) and not isinstance(raw_n, bool)
                    else auto_step_n
                )
                current = _WireStepSlot(
                    step_n=step_n,
                    start_line=lineno,
                    last_line=lineno,
                    started_at=ts,
                    finished_at=ts,
                )
                continue

            if current is None:
                continue

            current.last_line = lineno
            if ts:
                current.finished_at = ts

            if mtype == "ContentPart":
                ptype = payload.get("type")
                if ptype == "think":
                    think = payload.get("think")
                    if isinstance(think, str) and think:
                        current.thinking_parts.append(think)
                        if current.thinking_line is None:
                            current.thinking_line = lineno
                            current.thinking_ts = ts
                elif ptype == "text":
                    text = payload.get("text")
                    if isinstance(text, str) and text:
                        current.text_parts.append(text)
                        if current.text_line is None:
                            current.text_line = lineno
                            current.text_ts = ts
            elif mtype == "ToolCall":
                fn = (
                    payload.get("function")
                    if isinstance(payload.get("function"), dict)
                    else {}
                )
                cid = payload.get("id")
                name = fn.get("name") or payload.get("name")
                current.tool_calls.append(
                    _WireToolCall(
                        lineno=lineno,
                        ts=ts,
                        call_id=str(cid) if cid else None,
                        name=str(name) if name else None,
                        arguments=_parse_arguments(fn.get("arguments")),
                    )
                )
            elif mtype == "ToolResult":
                rv = payload.get("return_value")
                cid = payload.get("tool_call_id")
                current.tool_results.append(
                    _WireToolResult(
                        lineno=lineno,
                        ts=ts,
                        call_id=str(cid) if cid else None,
                        output=_tool_output_text(rv),
                        is_error=bool(rv.get("is_error"))
                        if isinstance(rv, dict)
                        else False,
                    )
                )
            elif mtype == "StatusUpdate":
                usage = payload.get("token_usage")
                if isinstance(usage, dict):
                    current.usage = usage

        flush_slot()
        result.evidence.extend(st.evidence)
        result.last_ts = st.last_ts
        return self._finish(result, attempt_id, st.steps, rel_file)

    def _emit_wire_slot(
        self,
        *,
        attempt_id: str,
        raw_file: str,
        slot: _WireStepSlot,
        st: _State,
    ) -> None:
        thinking = "".join(slot.thinking_parts)
        text = "".join(slot.text_parts)
        usage_payload = _usage_from_wire(slot.usage)
        if not (thinking or text or slot.tool_calls or usage_payload is not None):
            return

        seq_no = st.call_seq
        st.call_seq += 1
        anchor = correlate.sequence_anchor(SOURCE_KIND, SOURCE_INSTANCE, seq_no)
        lc = ids.logical_call_id(attempt_id=attempt_id, call_anchor=anchor)

        ir_parts: list[dict[str, Any]] = []
        if thinking:
            ir_parts.append({"type": "text", "text": thinking})
        if text:
            ir_parts.append({"type": "text", "text": text})
        for tc in slot.tool_calls:
            ir_parts.append(
                {
                    "type": "tool_call",
                    "name": tc.name or "",
                    "arguments": tc.arguments,
                }
            )
        content_hash, _ = hashing.part_semantic_hash(ir_parts)
        hash_domain = hashing.DOMAIN_SEMANTIC if content_hash else None

        st.evidence.append(
            NativeLlmCallEvidence(
                evidence_id=ids.evidence_id(
                    attempt_id=attempt_id,
                    source_kind=SOURCE_KIND,
                    source_instance=SOURCE_INSTANCE,
                    raw_ref=f"{raw_file}:{slot.start_line}",
                    producer_id=f"kimi-step-{seq_no}",
                ),
                attempt_id=attempt_id,
                phase="agent_run",
                source=EvidenceSource(kind=SOURCE_KIND, instance=SOURCE_INSTANCE),
                producer=EvidenceProducer(name=PRODUCER_NAME, version=PARSER_VERSION),
                time=EvidenceTime(
                    observed_at=slot.finished_at or slot.started_at or _EPOCH_TS,
                    started_at=slot.started_at,
                    finished_at=slot.finished_at,
                ),
                raw_ref=EvidenceRawRef(
                    kind="events-jsonl", file=raw_file, line=slot.start_line
                ),
                correlation_hints=CorrelationHints(
                    producer_session_id=st.session_id,
                    producer_call_id=None,
                    model=None,
                    sequence=seq_no,
                ),
                capabilities={"call_boundary": True},
                redaction=EvidenceRedaction(
                    policy="metadata",
                    status="applied",
                    hash_algorithm="sha256" if content_hash else None,
                    hash_domain=hash_domain,
                ),
                errors=[],
                extensions={},
                payload=NativeLlmCallPayload(
                    producer_call_id=None,
                    model=None,
                    call_role="main",
                    request_summary=RequestSummary(
                        model=None,
                        message_count=None,
                        message_bytes=None,
                        system_hash=None,
                        messages_hash=None,
                        tools_hash=None,
                        hash_domain=None,
                    ),
                    response_summary=ResponseSummary(
                        content_hash=content_hash,
                        hash_domain=hash_domain,
                        message_bytes=None,
                        output_blocks=len(ir_parts),
                    ),
                    usage=usage_payload,
                    finish_reason="tool_calls" if slot.tool_calls else "stop",
                ),
            )
        )

        usage_attr = _usage_attr_dict(usage_payload)
        base_attrs: dict[str, Any] = {"kimi_step": slot.step_n}
        if usage_attr:
            base_attrs["token_usage"] = usage_attr

        attached_usage = False
        if thinking:
            attrs = dict(base_attrs) if not text and not slot.tool_calls else {"kimi_step": slot.step_n}
            if "token_usage" in attrs:
                attached_usage = True
            self._append_text_step(
                attempt_id=attempt_id,
                raw_file=raw_file,
                lineno=slot.thinking_line or slot.start_line,
                kind="thinking",
                text=thinking,
                ts=slot.thinking_ts or slot.started_at,
                st=st,
                logical_call_id=lc,
                anchor_suffix=f"step-{slot.step_n}-thinking",
                attributes=attrs,
            )

        if text or (not thinking and not slot.tool_calls):
            attrs = dict(base_attrs)
            attached_usage = True
            self._append_text_step(
                attempt_id=attempt_id,
                raw_file=raw_file,
                lineno=slot.text_line or slot.start_line,
                kind="assistant",
                text=text,
                ts=slot.text_ts or slot.started_at,
                st=st,
                logical_call_id=lc,
                anchor_suffix=f"step-{slot.step_n}-assistant",
                attributes=attrs,
            )

        for idx, tc in enumerate(slot.tool_calls):
            tc_hash, tc_bytes = hashing.part_semantic_hash(
                [
                    {
                        "type": "tool_call",
                        "name": tc.name or "tool",
                        "arguments": tc.arguments,
                    }
                ]
            )
            tc_attrs: dict[str, Any] = {"kimi_step": slot.step_n}
            if not attached_usage and usage_attr:
                tc_attrs["token_usage"] = usage_attr
                attached_usage = True
            st.step_seq += 1
            st.steps.append(
                TrajectoryStep(
                    step_id=ids.trajectory_step_id(
                        attempt_id=attempt_id,
                        step_anchor=(
                            f"{raw_file}:{tc.lineno}:tool_call:"
                            f"{tc.call_id or idx}"
                        ),
                    ),
                    sequence=st.step_seq,
                    timestamp=tc.ts,
                    agent_id=_MAIN,
                    parent_agent_id=None,
                    kind="tool_call",
                    producer_event_refs=({"file": raw_file, "line": tc.lineno},),
                    tool_call_id=tc.call_id,
                    tool_name=tc.name,
                    logical_call_id=lc,
                    content_hash=tc_hash,
                    content_bytes=tc_bytes,
                    attributes=tc_attrs,
                )
            )

        for idx, tr in enumerate(slot.tool_results):
            tr_hash, tr_bytes = (
                hashing.part_semantic_hash(
                    [{"type": "tool_result", "content": tr.output}]
                )
                if tr.output
                else (None, None)
            )
            tr_attrs: dict[str, Any] = {"kimi_step": slot.step_n}
            if tr.is_error:
                tr_attrs["is_error"] = True
            st.step_seq += 1
            st.steps.append(
                TrajectoryStep(
                    step_id=ids.trajectory_step_id(
                        attempt_id=attempt_id,
                        step_anchor=(
                            f"{raw_file}:{tr.lineno}:tool_result:"
                            f"{tr.call_id or idx}"
                        ),
                    ),
                    sequence=st.step_seq,
                    timestamp=tr.ts,
                    agent_id=_MAIN,
                    parent_agent_id=None,
                    kind="tool_result",
                    producer_event_refs=({"file": raw_file, "line": tr.lineno},),
                    tool_call_id=tr.call_id,
                    content_hash=tr_hash,
                    content_bytes=tr_bytes,
                    attributes=tr_attrs,
                )
            )

    # ---------- events.jsonl 降级路径 ----------

    def _normalize_from_events(
        self, *, attempt_id: str, events_path: Path
    ) -> NormalizeResult:
        result = NormalizeResult(raw_file=RAW_FILE)
        st = _State()

        for lineno, event in _iter_jsonl(events_path):
            if event is None:
                result.record_error(lineno)
                continue
            if "raw_line" in event and event.get("role") is None and event.get("type") is None:
                result.record_error(lineno)
                continue

            ts = _iso_from_seconds(event.get("timestamp"))
            if ts:
                st.last_ts = ts

            if event.get("type") == "session.resume_hint":
                sid = event.get("session_id")
                if isinstance(sid, str) and sid and st.session_id is None:
                    st.session_id = sid
                continue

            nested = event.get("message") if isinstance(event.get("message"), dict) else None
            msg = nested if nested is not None else event
            role = event.get("role") or msg.get("role")
            etype = event.get("type")

            if role == "user":
                text = _extract_plain_text(msg.get("content"))
                if text:
                    self._append_text_step(
                        attempt_id=attempt_id,
                        raw_file=RAW_FILE,
                        lineno=lineno,
                        kind="user",
                        text=text,
                        ts=ts,
                        st=st,
                        anchor_suffix="user",
                    )
                continue

            if role == "thinking" or etype == "thinking":
                text = _extract_plain_text(msg.get("content") or event.get("thinking"))
                if text:
                    self._append_text_step(
                        attempt_id=attempt_id,
                        raw_file=RAW_FILE,
                        lineno=lineno,
                        kind="thinking",
                        text=text,
                        ts=ts,
                        st=st,
                        anchor_suffix="thinking",
                    )
                continue

            if role == "assistant":
                self._apply_events_assistant(
                    attempt_id=attempt_id,
                    event=event,
                    msg=msg,
                    lineno=lineno,
                    ts=ts,
                    st=st,
                )
                continue

            if role == "tool":
                tc_id = msg.get("tool_call_id") or event.get("tool_call_id")
                tc_id_str = str(tc_id) if isinstance(tc_id, str) and tc_id else None
                out_text = _extract_plain_text(msg.get("content"))
                tr_hash, tr_bytes = (
                    hashing.part_semantic_hash(
                        [{"type": "tool_result", "content": out_text}]
                    )
                    if out_text
                    else (None, None)
                )
                st.step_seq += 1
                st.steps.append(
                    TrajectoryStep(
                        step_id=ids.trajectory_step_id(
                            attempt_id=attempt_id,
                            step_anchor=(
                                f"{RAW_FILE}:{lineno}:tool_result:{tc_id_str or lineno}"
                            ),
                        ),
                        sequence=st.step_seq,
                        timestamp=ts,
                        agent_id=_MAIN,
                        parent_agent_id=None,
                        kind="tool_result",
                        producer_event_refs=({"file": RAW_FILE, "line": lineno},),
                        tool_call_id=tc_id_str,
                        content_hash=tr_hash,
                        content_bytes=tr_bytes,
                    )
                )

        result.evidence.extend(st.evidence)
        result.last_ts = st.last_ts
        return self._finish(result, attempt_id, st.steps, RAW_FILE)

    def _apply_events_assistant(
        self,
        *,
        attempt_id: str,
        event: dict[str, Any],
        msg: dict[str, Any],
        lineno: int,
        ts: str | None,
        st: _State,
    ) -> None:
        text, thinking = _extract_assistant_content(msg.get("content"))
        if not thinking:
            raw_r = msg.get("reasoning_content") or msg.get("reasoning")
            if isinstance(raw_r, str) and raw_r:
                thinking = raw_r

        raw_calls = event.get("tool_calls")
        if not isinstance(raw_calls, list):
            raw_calls = msg.get("tool_calls")
        tool_calls = [tc for tc in (raw_calls or []) if isinstance(tc, dict)]

        usage_payload = _usage_from_events_row(event.get("usage") or msg.get("usage"))
        if not (text or thinking or tool_calls or usage_payload is not None):
            return

        model = event.get("model") or msg.get("model")
        model_str = model if isinstance(model, str) and model else None
        turn_id = event.get(_TURN_ID_KEY)
        turn_index = event.get(_TURN_INDEX_KEY)
        ext: dict[str, Any] = {}
        if isinstance(turn_id, str) and turn_id:
            ext[_TURN_ID_KEY] = turn_id
            if isinstance(turn_index, int) and not isinstance(turn_index, bool):
                ext[_TURN_INDEX_KEY] = turn_index

        seq_no = st.call_seq
        st.call_seq += 1
        anchor = correlate.sequence_anchor(SOURCE_KIND, SOURCE_INSTANCE, seq_no)
        lc = ids.logical_call_id(attempt_id=attempt_id, call_anchor=anchor)

        ir_parts: list[dict[str, Any]] = []
        if thinking:
            ir_parts.append({"type": "text", "text": thinking})
        if text:
            ir_parts.append({"type": "text", "text": text})
        parsed_calls: list[tuple[str | None, str | None, Any]] = []
        for tc in tool_calls:
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            cid = tc.get("id")
            name = fn.get("name") or tc.get("name")
            args = _parse_arguments(fn.get("arguments", tc.get("arguments")))
            cid_s = str(cid) if cid else None
            name_s = str(name) if name else None
            parsed_calls.append((cid_s, name_s, args))
            ir_parts.append(
                {"type": "tool_call", "name": name_s or "", "arguments": args}
            )

        content_hash, _ = hashing.part_semantic_hash(ir_parts)
        hash_domain = hashing.DOMAIN_SEMANTIC if content_hash else None

        st.evidence.append(
            NativeLlmCallEvidence(
                evidence_id=ids.evidence_id(
                    attempt_id=attempt_id,
                    source_kind=SOURCE_KIND,
                    source_instance=SOURCE_INSTANCE,
                    raw_ref=f"{RAW_FILE}:{lineno}",
                    producer_id=f"kimi-call-{seq_no}",
                ),
                attempt_id=attempt_id,
                phase="agent_run",
                source=EvidenceSource(kind=SOURCE_KIND, instance=SOURCE_INSTANCE),
                producer=EvidenceProducer(name=PRODUCER_NAME, version=PARSER_VERSION),
                time=EvidenceTime(
                    observed_at=ts or _EPOCH_TS, started_at=None, finished_at=ts
                ),
                raw_ref=EvidenceRawRef(kind="events-jsonl", file=RAW_FILE, line=lineno),
                correlation_hints=CorrelationHints(
                    producer_session_id=st.session_id,
                    producer_call_id=None,
                    model=model_str,
                    sequence=seq_no,
                ),
                capabilities={"call_boundary": True},
                redaction=EvidenceRedaction(
                    policy="metadata",
                    status="applied",
                    hash_algorithm="sha256" if content_hash else None,
                    hash_domain=hash_domain,
                ),
                errors=[],
                extensions=ext,
                payload=NativeLlmCallPayload(
                    producer_call_id=None,
                    model=model_str,
                    call_role="main",
                    request_summary=RequestSummary(
                        model=model_str,
                        message_count=None,
                        message_bytes=None,
                        system_hash=None,
                        messages_hash=None,
                        tools_hash=None,
                        hash_domain=None,
                    ),
                    response_summary=ResponseSummary(
                        content_hash=content_hash,
                        hash_domain=hash_domain,
                        message_bytes=None,
                        output_blocks=len(ir_parts),
                    ),
                    usage=usage_payload,
                    finish_reason="tool_calls" if parsed_calls else "stop",
                ),
            )
        )

        usage_attr = _usage_attr_dict(usage_payload)
        if thinking:
            self._append_text_step(
                attempt_id=attempt_id,
                raw_file=RAW_FILE,
                lineno=lineno,
                kind="thinking",
                text=thinking,
                ts=ts,
                st=st,
                logical_call_id=lc,
                anchor_suffix="thinking",
            )
        if text or not parsed_calls:
            self._append_text_step(
                attempt_id=attempt_id,
                raw_file=RAW_FILE,
                lineno=lineno,
                kind="assistant",
                text=text,
                ts=ts,
                st=st,
                logical_call_id=lc,
                anchor_suffix="assistant",
                attributes={"token_usage": usage_attr} if usage_attr else None,
            )
        for idx, (cid_s, name_s, args) in enumerate(parsed_calls):
            tc_hash, tc_bytes = hashing.part_semantic_hash(
                [{"type": "tool_call", "name": name_s or "tool", "arguments": args}]
            )
            st.step_seq += 1
            st.steps.append(
                TrajectoryStep(
                    step_id=ids.trajectory_step_id(
                        attempt_id=attempt_id,
                        step_anchor=f"{RAW_FILE}:{lineno}:tool_call:{cid_s or idx}",
                    ),
                    sequence=st.step_seq,
                    timestamp=ts,
                    agent_id=_MAIN,
                    parent_agent_id=None,
                    kind="tool_call",
                    producer_event_refs=({"file": RAW_FILE, "line": lineno},),
                    tool_call_id=cid_s,
                    tool_name=name_s,
                    logical_call_id=lc,
                    content_hash=tc_hash,
                    content_bytes=tc_bytes,
                )
            )

    # ---------- 辅助 ----------

    def _append_text_step(
        self,
        *,
        attempt_id: str,
        raw_file: str,
        lineno: int,
        kind: str,
        text: str,
        ts: str | None,
        st: _State,
        anchor_suffix: str,
        logical_call_id: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        c_hash, c_bytes = (
            hashing.part_semantic_hash([{"type": "text", "text": text}])
            if text
            else (None, None)
        )
        st.step_seq += 1
        st.steps.append(
            TrajectoryStep(
                step_id=ids.trajectory_step_id(
                    attempt_id=attempt_id,
                    step_anchor=f"{raw_file}:{lineno}:{anchor_suffix}",
                ),
                sequence=st.step_seq,
                timestamp=ts,
                agent_id=_MAIN,
                parent_agent_id=None,
                kind=kind,
                producer_event_refs=({"file": raw_file, "line": lineno},),
                logical_call_id=logical_call_id,
                content_hash=c_hash,
                content_bytes=c_bytes,
                attributes=attributes or None,
            )
        )

    def _finish(
        self,
        result: NormalizeResult,
        attempt_id: str,
        steps: list[TrajectoryStep],
        raw_file: str,
    ) -> NormalizeResult:
        step_tuple = tuple(steps)
        if PRODUCER_NAME in _VALID_PRODUCERS:
            trajectory = Trajectory(
                schema_version=TRAJECTORY_SCHEMA_VERSION,
                attempt_id=attempt_id,
                steps=step_tuple,
                producer=PRODUCER_NAME,
            )
            v_errors = trajectory.validate()
            traj_dict = trajectory_to_dict(trajectory)
        else:
            seen_ids = [s.step_id for s in step_tuple]
            if len(seen_ids) != len(set(seen_ids)):
                raise ValueError("step_id 在同一 trajectory 内必须唯一")
            v_errors = []
            v_errors.extend(_check_step_sequence(step_tuple))
            v_errors.extend(_check_cc_tool_call_pairing(step_tuple))
            traj_dict = {
                "schema_version": TRAJECTORY_SCHEMA_VERSION,
                "attempt_id": attempt_id,
                "steps": [trajectory_step_to_dict(s) for s in step_tuple],
            }

        if v_errors:
            result.evidence.append(
                trajectory_validation_evidence(
                    attempt_id=attempt_id,
                    producer=PRODUCER_NAME,
                    parser_version=PARSER_VERSION,
                    errors=v_errors,
                    last_ts=result.last_ts,
                    raw_file=raw_file,
                )
            )
        result.trajectory = traj_dict
        return result


def _extract_plain_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str) and item:
                parts.append(item)
            elif isinstance(item, dict):
                t = item.get("text") or item.get("content") or item.get("think")
                if isinstance(t, str) and t:
                    parts.append(t)
        return "\n".join(parts)
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False)


def _extract_assistant_content(content: Any) -> tuple[str, str | None]:
    if isinstance(content, str):
        return content, None
    if isinstance(content, list):
        texts: list[str] = []
        thinks: list[str] = []
        for item in content:
            if isinstance(item, str) and item:
                texts.append(item)
            elif isinstance(item, dict):
                itype = item.get("type")
                if itype in {"think", "thinking", "reasoning"}:
                    t = item.get("think") or item.get("thinking") or item.get("text") or item.get("content")
                    if isinstance(t, str) and t:
                        thinks.append(t)
                elif itype in {"text", "output_text"} or itype is None:
                    t = item.get("text") or item.get("content")
                    if isinstance(t, str) and t:
                        texts.append(t)
        return "\n".join(texts), ("\n".join(thinks) if thinks else None)
    return "", None
