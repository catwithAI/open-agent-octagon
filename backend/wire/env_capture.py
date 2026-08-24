"""Env Attempt Server inbound 工具请求采集。

Env Server 已鉴权、attempt_id 在 URL、token 在请求里——采集 inbound 工具调用的
size/timing/attempt 归属**零新增鉴权**。每次工具调用产一条 ``http_exchange``
WireEvidence（direction=inbound），写进该 attempt 的
``wire-sources/env-inbound.jsonl``。

并发：每个 (data_path, attempt_id) 一个 SpoolWriter，进程内用锁串行 append
（SpoolWriter 非线程安全；多请求并发到同一 attempt 时串行落盘，不同 attempt
各自独立 spool 不互串）。

phase 归属：从 lifecycle 原子写的 ``wire-sources/phase-state.json`` 在
**请求到达时**快照。文件缺失 / attempt 不匹配 / 无 phase 时写 ``unknown``，
**禁止默认 ``agent_run``**——unknown phase 的 evidence 不进 agent_run 聚合。

fail-open：采集异常绝不影响工具调用本身（trace 写入、HTTP 响应照常）。
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic as _monotonic

from backend.wire import ids, paths, spool
from backend.wire.evidence import (
    CaptureEventEvidence,
    CaptureEventPayload,
    CorrelationHints,
    EvidenceProducer,
    EvidenceRawRef,
    EvidenceRedaction,
    EvidenceSource,
    EvidenceTime,
    HttpExchangeEvidence,
    HttpExchangePayload,
    TimingPayload,
    null_payload,
)

logger = logging.getLogger(__name__)

SOURCE_KIND = "env-inbound"
PRODUCER_NAME = "octagon-env-server"
PARSER_VERSION = "env-inbound-v1"

# 进程 generation anchor：每次进程启动生成一个随机 UUID，混入
# env-inbound evidence ID。env-inbound 是 live source（不从 raw 离线重建），
# 因此加随机 anchor 不破坏幂等——反而杜绝重启后与旧数据（可能稀疏，无法可靠
# 反推 max seq）撞 ID。seq 仍存 extensions 只用于排序，不再是 ID 唯一性来源。
import uuid as _uuid  # noqa: E402

_PROCESS_GENERATION = _uuid.uuid4().hex

# lifecycle 未管理该 attempt phase 时的兜底——绝不用 agent_run。
UNKNOWN_PHASE = "unknown"

# 合法 phase 枚举（与 evidence.Phase 一致）；phase-state 给出集合外的值也降 unknown。
_VALID_PHASES = frozenset(
    {"attempt_setup", "agent_run", "verification", "artifact_collection", "attempt_cleanup"}
)


class _Entry:
    __slots__ = ("writer", "lock", "sealed", "inflight", "cond", "dropped")

    def __init__(self, writer: spool.SpoolWriter) -> None:
        self.writer = writer
        self.lock = threading.Lock()      # 串行 append
        self.sealed = False               # close 后 sealed，禁止重开/再写
        self.inflight = 0                 # 已 begin 未 end 的请求数（drain 用）
        self.cond = threading.Condition() # inflight 归零通知
        self.dropped = 0                  # sealed 后被丢弃的请求数（进 manifest）


class _SpoolRegistry:
    """(data_path, attempt_id) → 一个 attempt-scoped writer entry。进程级单例。

    生命周期：active → sealed。close 时：seal（禁止新建 writer）→ **等待
    in-flight 请求结束**（drain active）→ close writer。已 begin 但
    close 抢先 seal 的请求被计入 dropped，close 时写一条 drop capture_event，
    manifest 据此把 env-inbound 标 partial（不静默丢）。
    seal 状态长期保留（进程内），杜绝 finalize 后重开竞态。
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._entries: dict[tuple[str, str], _Entry] = {}
        self._sealed: set[tuple[str, str]] = set()

    def begin(self, data_path: Path, attempt_id: str) -> _Entry | None:
        """请求进入采集：取/建 entry 并登记 in-flight；sealed 返回 None。
        必须与 ``end`` 成对——``end`` 递减 in-flight 并唤醒 drain。"""
        key = (str(data_path), attempt_id)
        with self._guard:
            if key in self._sealed:
                return None  # finalize 后不再采集
            entry = self._entries.get(key)
            if entry is None:
                entry = _Entry(spool.SpoolWriter(
                    paths.source_spool_file(data_path, attempt_id, SOURCE_KIND),
                    expected_attempt_id=attempt_id,
                ))
                self._entries[key] = entry
            with entry.cond:
                entry.inflight += 1
            return entry

    @staticmethod
    def end(entry: _Entry) -> None:
        with entry.cond:
            entry.inflight -= 1
            if entry.inflight <= 0:
                entry.cond.notify_all()

    def close(self, data_path: Path, attempt_id: str, *, drain_timeout: float = 5.0) -> None:
        """seal → drain active in-flight → close。幂等。"""
        key = (str(data_path), attempt_id)
        with self._guard:
            already = key in self._sealed
            self._sealed.add(key)          # 先 seal：此后 begin() 一律 None
            entry = self._entries.pop(key, None)
        if entry is None or already:
            return
        # drain：等待已 begin 的请求 end（有上限，防某请求卡死）。
        deadline_reached = False
        with entry.cond:
            end_at = _monotonic() + drain_timeout
            while entry.inflight > 0:
                remaining = end_at - _monotonic()
                if remaining <= 0:
                    deadline_reached = True
                    break
                entry.cond.wait(remaining)
            stuck = entry.inflight if deadline_reached else 0
        with entry.lock:
            entry.sealed = True
            # drain 超时仍有 in-flight：这些请求之后会 append，被 sealed 挡住 →
            # 计 dropped（不静默丢）。
            if stuck:
                entry.dropped += stuck
            if entry.dropped:
                try:
                    _append_drop_event(entry, attempt_id, entry.dropped)
                except Exception:
                    logger.exception("env-inbound drop 事件写入失败 attempt=%s", attempt_id)
            try:
                entry.writer.close()
            except Exception:
                logger.exception("env-inbound spool close 失败 attempt=%s", attempt_id)


_REGISTRY = _SpoolRegistry()


def snapshot_capture_state(data_path: Path, attempt_id: str) -> tuple[bool, str]:
    """请求到达时快照 (capture_enabled, phase)。区分「未启用」与「故障」
    ——故障不被静默当 policy off，而是采集+unknown 让 manifest 降级。

    - 文件**不存在** → capture 未启用（policy off / 无 wire prepare）：不采集；
    - 文件存在但 ``capture_enabled=False`` → policy off 的显式态：不采集；
    - 文件损坏 / 读失败 → capture 基础设施**故障**：仍采集，phase=unknown
      （finalizer 见 unknown 会把 phase_attribution 降 degraded，不误报 off）；
    - attempt 不匹配 / phase 非法 → 采集但 phase=unknown。
    """
    state_path = paths.phase_state_file(data_path, attempt_id)
    if not state_path.exists():
        return False, UNKNOWN_PHASE  # 未启用（policy off / 无 prepare）
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # 文件在但读/解析失败：control channel 故障，不当 off——采集并降级
        return True, UNKNOWN_PHASE
    if state.get("capture_enabled") is False:
        return False, UNKNOWN_PHASE  # 显式 off
    if state.get("attempt_id") != attempt_id:
        return True, UNKNOWN_PHASE
    phase = state.get("phase")
    return True, (phase if phase in _VALID_PHASES else UNKNOWN_PHASE)


def begin_request(
    data_path: Path, attempt_id: str, capture_enabled: bool
) -> "_Entry | None":
    """请求到达时登记 in-flight（close 的 drain 会等它 end）。

    capture 未启用返回 None。返回的 entry 必须传给 record + end_request（成对）。
    """
    if not capture_enabled:
        return None
    try:
        return _REGISTRY.begin(data_path, attempt_id)
    except Exception:
        logger.exception("env-inbound begin 失败 attempt=%s", attempt_id)
        return None


def end_request(entry: "_Entry | None") -> None:
    """请求结束递减 in-flight。与 begin_request 成对。"""
    if entry is not None:
        try:
            _REGISTRY.end(entry)
        except Exception:
            logger.exception("env-inbound end 失败")


def record_inbound_tool_call(
    *,
    entry: "_Entry | None",
    data_path: Path,
    attempt_id: str,
    tool_name: str,
    request_bytes: int | None,
    response_bytes: int | None,
    status_code: int,
    started_at: str,
    finished_at: str,
    duration_ms: float,
    seq: int,
    phase: str,
    capture_enabled: bool,
) -> None:
    """写一条 inbound http_exchange evidence。fail-open：任何异常只记日志。

    ``entry`` 是 begin_request 返回的 in-flight 句柄（None=未采集/已 sealed）。
    ``phase``/``capture_enabled`` 由**请求到达时**（`_wire_inbound_start`）
    快照传入（不在请求结束时读，避免长耗时工具跨 phase 误归属）。
    ``capture_enabled=False`` 时直接不采集（policy off 零采集）。
    ``seq`` 用于 evidence ID 去重（同 attempt 多次调用）。
    """
    if not capture_enabled or entry is None:
        return  # policy off / sealed：不采集，不落任何盘
    try:
        payload_dict = {
            **null_payload("http_exchange"),
            "direction": "inbound",  # Env Server 收到工具请求
            "method": "POST",
            "scheme": "http",
            "path": f"/attempts/{attempt_id}/tools/{tool_name}",
            "status_code": status_code,
            "request_bytes": request_bytes,
            "response_bytes": response_bytes,
            "streamed": False,
            "partial": False,
        }
        evidence = HttpExchangeEvidence(
            evidence_id=ids.evidence_id(
                attempt_id=attempt_id, source_kind=SOURCE_KIND,
                source_instance=SOURCE_KIND,
                # 混入进程 generation：重启后即便旧数据稀疏、seq 复用，evidence
                # ID 仍唯一。
                raw_ref=f"env-inbound:{_PROCESS_GENERATION}:{seq}",
                producer_id=tool_name,
            ),
            attempt_id=attempt_id,
            phase=phase,  # type: ignore[arg-type]
            source=EvidenceSource(kind=SOURCE_KIND, instance=SOURCE_KIND),
            producer=EvidenceProducer(name=PRODUCER_NAME, version=PARSER_VERSION),
            time=EvidenceTime(
                observed_at=finished_at, started_at=started_at, finished_at=finished_at
            ),
            raw_ref=EvidenceRawRef(kind="trace-jsonl", file="trace.jsonl", line=None),
            correlation_hints=CorrelationHints(),
            capabilities={"inbound_tool_metadata": True},
            redaction=EvidenceRedaction(policy="metadata", status="applied"),
            errors=[],
            # seq 存进 namespaced extensions：raw_ref.line 造假
            # provenance（seq≠trace 行号），改用扩展字段；重启后从已落盘
            # evidence 的 max(seq)+1 恢复，稀疏序号（[0,2]）不复用。
            extensions={"x-octagon.env-inbound-seq": seq},
            payload=HttpExchangePayload(
                **{
                    k: payload_dict.get(k)
                    for k in HttpExchangePayload.model_fields
                    if k != "timing"
                },
                timing=TimingPayload(
                    started_at=started_at, finished_at=finished_at,
                    duration_ms=duration_ms, ttft_ms=None,
                ),
            ),
        )
        with entry.lock:
            if entry.sealed:
                # drain 超时后 seal 抢先：计 dropped（close 会写 drop 事件）。
                entry.dropped += 1
                return
            entry.writer.append(evidence)
    except Exception:
        # fail-open：采集失败绝不影响工具调用/trace/HTTP 响应
        logger.exception("env-inbound 采集失败 attempt=%s tool=%s", attempt_id, tool_name)


def _append_drop_event(entry: "_Entry", attempt_id: str, dropped: int) -> None:
    """写一条 drop capture_event（cumulative counter），manifest 据此标 partial。"""
    ev = CaptureEventEvidence(
        evidence_id=ids.evidence_id(
            attempt_id=attempt_id, source_kind=SOURCE_KIND,
            source_instance=SOURCE_KIND, raw_ref="env-inbound:drop",
            producer_id="registry",
        ),
        attempt_id=attempt_id,
        phase="unknown",
        source=EvidenceSource(kind=SOURCE_KIND, instance=SOURCE_KIND),
        producer=EvidenceProducer(name=PRODUCER_NAME, version=PARSER_VERSION),
        time=EvidenceTime(observed_at=_now_iso(), started_at=None, finished_at=None),
        raw_ref=None,
        correlation_hints=CorrelationHints(),
        capabilities={},
        redaction=EvidenceRedaction(policy="metadata", status="applied"),
        errors=[],
        extensions={},
        payload=CaptureEventPayload(
            event="drop", source_instance=SOURCE_KIND, status=None,
            reason_code="sealed_during_request", message=None,
            counters={"records_dropped": dropped}, effective_capabilities=None,
        ),
    )
    entry.writer.append(ev)


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def close_attempt_spool(
    data_path: Path, attempt_id: str, *, drain_timeout: float = 5.0
) -> None:
    """attempt 收尾时关闭其 inbound spool（seal→drain→rename）。fail-open。"""
    _REGISTRY.close(data_path, attempt_id, drain_timeout=drain_timeout)
