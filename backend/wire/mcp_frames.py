"""MCP JSON-RPC 帧解析与配对。

``FrameCapture`` 被 ``mcp_tap`` 的双向 pump 旁路喂字节：client→server 与
server→client 各一个方向 buffer，按**换行 framing** 解析 JSON-RPC 帧，落 spool 为
``mcp_frame`` evidence，并按 ``jsonrpc_id`` 配对 request↔response。

约束：
- 独立 buffer、跨 chunk 重组单帧；
- 超过 ``max_frame_bytes`` 后**继续透明转发**（转发在 tap 层，不受此影响），capture
  侧标 dropped 并计数；
- payload 写 spool 前 redaction（metadata 档只记 size/kind，不落 body）；
- request map 用 ``jsonrpc_id`` 配对，完成即释放，有上限防泄漏；
- capture 全程 fail-open：解析/写盘异常绝不影响主通信（tap 已保证 feed 异常被吞）。

spool 逐行 flush + ``.partial`` 恢复保证 SIGKILL 下完整性（tap 不做优雅关闭）。
"""

from __future__ import annotations

import json
import threading
import time as _time
import uuid as _uuid
from typing import Any
from datetime import datetime, timezone
from pathlib import Path

from backend.wire import ids, spool
from backend.wire.evidence import (
    CaptureEventEvidence,
    CaptureEventPayload,
    CorrelationHints,
    EvidenceProducer,
    EvidenceRawRef,
    EvidenceRedaction,
    EvidenceSource,
    EvidenceTime,
    McpFrameEvidence,
    McpFramePayload,
)

SOURCE_KIND = "mcp-stdio"
PRODUCER_NAME = "octagon-mcp-tap"
PARSER_VERSION = "mcp-tap-v1"

# 进程 generation anchor（live source，混入 evidence ID 防重启撞 ID）。
_PROCESS_GENERATION = _uuid.uuid4().hex

# request map 上限 + TTL：防未配对的 request 无限堆积（对端不回
# response 时）。TTL 淘汰超时未配对项，容量上限兜底。
_MAX_PENDING_REQUESTS = 4096
_PENDING_TTL_S = 300.0  # 5 分钟未收到 response 视为不会再配对，淘汰

_VALID_PHASES = frozenset(
    {"attempt_setup", "agent_run", "verification", "artifact_collection", "attempt_cleanup"}
)

_OPPOSITE = {
    "client-to-server": "server-to-client",
    "server-to-client": "client-to-server",
}


def _typed_id(jid: Any) -> tuple[str, Any] | None:
    """类型保真的 JSON-RPC id 键：区分数字 1 与字符串 "1"。

    JSON-RPC id 只能是 string / number / null。返回 (type_tag, value) 元组作键；
    null / 非法类型返回 None（不参与配对）。"""
    if isinstance(jid, bool):  # bool 是 int 子类，但 JSON-RPC id 不该是 bool
        return None
    if isinstance(jid, str):
        return ("s", jid)
    if isinstance(jid, (int, float)):
        return ("n", jid)
    return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class _DirectionFeeder:
    """单方向 buffer：喂字节 → 按换行切帧 → 交给 FrameCapture 处理。"""

    def __init__(self, capture: "FrameCapture", direction: str) -> None:
        self._cap = capture
        self._direction = direction  # "client-to-server" | "server-to-client"
        self._buf = bytearray()
        self._overflow = False  # 当前帧已超 max_frame_bytes → 丢弃到下个换行

    def feed(self, chunk: bytes) -> None:
        """喂入一段（可能跨帧/半帧）字节。tap 已保证本方法异常被吞（fail-open）。"""
        self._buf.extend(chunk)
        # 超大帧保护：buffer 超上限且无换行 → 标 overflow，丢弃到下个换行边界。
        while True:
            nl = self._buf.find(b"\n")
            if nl < 0:
                if len(self._buf) > self._cap.max_frame_bytes:
                    # 半帧已超限：丢弃已积累字节，标 overflow（等换行再复位）。
                    self._cap.note_dropped(self._direction)
                    self._buf.clear()
                    self._overflow = True
                return
            line = bytes(self._buf[:nl])
            del self._buf[: nl + 1]
            if self._overflow:
                # 上一帧溢出的尾巴，丢弃到此换行为止，复位。
                self._overflow = False
                continue
            if len(line) > self._cap.max_frame_bytes:
                self._cap.note_dropped(self._direction)
                continue
            stripped = line.strip()
            if stripped:
                self._cap.on_frame(self._direction, stripped)

    def close_direction(self) -> None:
        # EOF：buffer 里的残帧（无换行结尾）尽力解析一次。
        rest = bytes(self._buf).strip()
        self._buf.clear()
        if rest and not self._overflow and len(rest) <= self._cap.max_frame_bytes:
            self._cap.on_frame(self._direction, rest)


class FrameCapture:
    """双向帧 capture + 配对 + spool。线程安全（两条 pump 线程并发喂）。"""

    def __init__(
        self, *, attempt_id: str, phase: str, spool_dir: Path, instance: str,
        policy: str, max_frame_bytes: int,
    ) -> None:
        self.attempt_id = attempt_id
        self.phase = phase if phase in _VALID_PHASES else "unknown"
        self.instance = instance
        self.policy = policy
        self.max_frame_bytes = max_frame_bytes

        self._lock = threading.Lock()
        self._seq = 0
        # 键 (request_direction, typed_id) → (paired anchor, 登记时 monotonic 秒)。
        # 方向感知 + TTL 淘汰长期未配对的 request（对端不回 response）。
        self._pending: dict[tuple[str, tuple[str, Any]], tuple[str, float]] = {}
        self._dropped = {"client-to-server": 0, "server-to-client": 0}
        # TTL 淘汰计数（未配对 request 过期被清），进 stop 事件 counters。
        self._pending_evicted = 0

        # spool 直接建在 spool-dir 下（tap 是独立进程，不走 env_capture 的 registry）。
        # source_spool_file 需要 data_path+attempt_id 反推 wire-sources 目录——这里
        # spool_dir 已是 wire-sources，直接拼文件名。
        spool_dir = Path(spool_dir)
        spool_dir.mkdir(parents=True, exist_ok=True)
        # 真实文件名：raw_ref.file 要指向实际写入的文件，审计引用才准确。
        self._fname = (
            f"{SOURCE_KIND}@{instance}.jsonl" if instance != SOURCE_KIND
            else f"{SOURCE_KIND}.jsonl"
        )
        self._writer = spool.SpoolWriter(
            spool_dir / self._fname,
            expected_attempt_id=attempt_id,
            max_policy=policy,
        )

    # ---- pump 侧接口 ----

    def client_to_server(self) -> _DirectionFeeder:
        return _DirectionFeeder(self, "client-to-server")

    def server_to_client(self) -> _DirectionFeeder:
        return _DirectionFeeder(self, "server-to-client")

    def note_dropped(self, direction: str) -> None:
        with self._lock:
            self._dropped[direction] = self._dropped.get(direction, 0) + 1

    def _evict_expired(self, now: float) -> None:
        """淘汰超 TTL 未配对的 pending request。调用方持锁。"""
        if not self._pending:
            return
        expired = [k for k, (_a, t) in self._pending.items() if now - t > _PENDING_TTL_S]
        for k in expired:
            del self._pending[k]
        self._pending_evicted += len(expired)

    def on_frame(self, direction: str, raw: bytes) -> None:
        """解析一帧 JSON-RPC 并落 mcp_frame evidence。fail-open。"""
        try:
            self._handle_frame(direction, raw)
        except Exception:
            pass  # 解析/写盘失败不影响通信（tap 也已吞异常，双保险）

    # ---- 生命周期 ----

    def close_all(self, *, exit_code: int | None = None) -> None:
        with self._lock:
            # close 时仍在 pending 的 request 即「未收到 response」——计入淘汰。
            self._pending_evicted += len(self._pending)
            self._pending.clear()
            try:
                need_stop = (
                    any(self._dropped.values())
                    or self._pending_evicted
                    or exit_code not in (None, 0)
                )
                if need_stop:
                    self._append_stop_event(exit_code)
            except Exception:
                pass
            try:
                self._writer.close()
            except Exception:
                pass

    # ---- 内部 ----

    def _handle_frame(self, direction: str, raw: bytes) -> None:
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            # 非 JSON 帧（不该出现在 JSON-RPC stdio）：只记 byte metadata，不解析。
            self._emit(direction, jsonrpc_id=None, message_kind=None,
                       method=None, tool_name=None, nbytes=len(raw),
                       is_error=None, paired_anchor=None)
            return
        if not isinstance(msg, dict):
            self._emit(direction, jsonrpc_id=None, message_kind=None, method=None,
                       tool_name=None, nbytes=len(raw), is_error=None, paired_anchor=None)
            return

        jid = msg.get("id")
        # 类型保真 id：数字 1 与字符串 "1" 是不同 JSON-RPC id，用类型前缀
        # 区分，避免 str(1)==str("1") 撞键。展示值仍用可读字符串。
        typed_id = _typed_id(jid)
        jid_str = str(jid) if jid is not None else None
        method = msg.get("method")
        has_method = isinstance(method, str)
        # JSON-RPC 语义：有 method + 有 id = request；有 method + 无 id = notification；
        # 无 method（有 result/error）= response。
        if has_method and jid is not None:
            kind = "request"
        elif has_method:
            kind = "notification"
        else:
            kind = "response"

        tool_name = _extract_tool_name(method, msg)
        is_error = True if ("error" in msg and kind == "response") else (
            False if kind == "response" else None
        )

        # 配对：MCP 是**双向** RPC——client 与 server 都可发起请求，同一 id
        # 在两个方向是不同逻辑调用。配对键含 request 的**发起方向**；response 用其
        # **相反方向**查找（response 与它应答的 request 方向相反）。
        now = _time.monotonic()
        paired_anchor = None
        with self._lock:
            if kind == "request" and typed_id is not None:
                self._evict_expired(now)  # 每个 request 前顺手淘汰过期项
                key = (direction, typed_id)  # 该请求由 direction 发起
                # anchor 含 id 类型标（typed_id[0]='s'|'n'）：数字 1 与字符串 "1"
                # 的 anchor 不同。
                anchor = f"{self.instance}:{direction}:{typed_id[0]}:{jid_str}"
                if len(self._pending) < _MAX_PENDING_REQUESTS:
                    self._pending[key] = (anchor, now)
                paired_anchor = anchor
            elif kind == "response" and typed_id is not None:
                # response 来自 direction，应答的是**相反方向**发起的 request。
                req_dir = _OPPOSITE.get(direction)
                entry = self._pending.pop((req_dir, typed_id), None)
                paired_anchor = entry[0] if entry is not None else None

        self._emit(direction, jsonrpc_id=jid_str, message_kind=kind, method=method
                   if has_method else None, tool_name=tool_name, nbytes=len(raw),
                   is_error=is_error, paired_anchor=paired_anchor)

    def _emit(
        self, direction: str, *, jsonrpc_id, message_kind, method, tool_name,
        nbytes, is_error, paired_anchor,
    ) -> None:
        with self._lock:
            seq = self._seq
            self._seq += 1
        ev = McpFrameEvidence(
            evidence_id=ids.evidence_id(
                attempt_id=self.attempt_id, source_kind=SOURCE_KIND,
                source_instance=self.instance,
                raw_ref=f"mcp-stdio:{_PROCESS_GENERATION}:{seq}",
                producer_id=direction,
            ),
            attempt_id=self.attempt_id,
            phase=self.phase,  # type: ignore[arg-type]
            source=EvidenceSource(kind=SOURCE_KIND, instance=self.instance),
            producer=EvidenceProducer(name=PRODUCER_NAME, version=PARSER_VERSION),
            time=EvidenceTime(observed_at=_now_iso(), started_at=None, finished_at=None),
            raw_ref=EvidenceRawRef(kind="mcp-stdio", file=self._fname, line=None),
            # jsonrpc_id 进 correlation_hints（finalizer 按它配对 request↔response）。
            correlation_hints=CorrelationHints(jsonrpc_id=jsonrpc_id),
            capabilities={"mcp_frame_metadata": True},
            redaction=EvidenceRedaction(policy=self.policy, status="applied"),  # type: ignore[arg-type]
            errors=[],
            # paired-anchor 存扩展：finalizer 据此把 request/response 挂成一对
            # （schema 里 mcp_frame 无 paired 字段，走 namespaced 扩展）。
            extensions=(
                {"x-octagon.mcp-paired-anchor": paired_anchor, "x-octagon.mcp-seq": seq}
                if paired_anchor is not None else {"x-octagon.mcp-seq": seq}
            ),
            payload=McpFramePayload(
                direction=direction,  # type: ignore[arg-type]
                jsonrpc_id=jsonrpc_id,
                message_kind=message_kind,  # type: ignore[arg-type]
                method=method,
                tool_name=tool_name,
                bytes=nbytes,
                is_error=is_error,
                truncated=False,
            ),
        )
        self._writer.append(ev)

    def _append_stop_event(self, exit_code: int | None) -> None:
        counters = {}
        total_dropped = sum(self._dropped.values())
        if total_dropped:
            counters["frames_dropped"] = total_dropped
        if self._pending_evicted:
            counters["pending_requests_evicted"] = self._pending_evicted
        degraded = total_dropped or self._pending_evicted
        ev = CaptureEventEvidence(
            evidence_id=ids.evidence_id(
                attempt_id=self.attempt_id, source_kind=SOURCE_KIND,
                source_instance=self.instance,
                raw_ref=f"mcp-stdio:{_PROCESS_GENERATION}:stop",
                producer_id="tap",
            ),
            attempt_id=self.attempt_id,
            phase=self.phase,  # type: ignore[arg-type]
            source=EvidenceSource(kind=SOURCE_KIND, instance=self.instance),
            producer=EvidenceProducer(name=PRODUCER_NAME, version=PARSER_VERSION),
            time=EvidenceTime(observed_at=_now_iso(), started_at=None, finished_at=None),
            raw_ref=None,
            correlation_hints=CorrelationHints(),
            capabilities={},
            redaction=EvidenceRedaction(policy="metadata", status="applied"),
            errors=[],
            extensions=({"x-octagon.mcp-child-exit-code": exit_code}
                        if exit_code is not None else {}),
            payload=CaptureEventPayload(
                event="stop", source_instance=self.instance,
                status="partial" if degraded else None,
                reason_code=("frames_dropped" if total_dropped else
                             "pending_requests_evicted" if self._pending_evicted else None),
                message=None, counters=counters or None,
                effective_capabilities=None,
            ),
        )
        self._writer.append(ev)


def _extract_tool_name(method, msg: dict) -> str | None:
    """MCP tools/call 的工具名：method=="tools/call" 时取 params.name。"""
    if method == "tools/call":
        params = msg.get("params")
        if isinstance(params, dict):
            name = params.get("name")
            if isinstance(name, str) and name:
                return name
    return None
