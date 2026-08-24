"""MCP stdio tap。

作为可执行模块包在真实 MCP server 外面：

    python -m backend.wire.mcp_tap \
      --attempt-id <id> --phase agent_run \
      --spool-dir <attempt>/wire-sources --policy metadata \
      -- <原 MCP server command...>

在 CC/Codex（父）与真实 MCP server（child）之间做**透明双向 bytes pump**：

    parent stdin  ── pump/capture ──> child stdin
    child  stdout ── pump/capture ──> parent stdout
    child  stderr ─────────────────> parent stderr（直通，不 capture）

铁律：
- **bytes pump**：不 decode 后再转发——原样透传字节，capture 侧另维护解析 buffer；
- **stdout/stderr 绝不写日志**：所有 capture/诊断只进 spool，混入会破坏 JSON-RPC 通信；
- **capture 故障不中断主通信**（fail-open）：spool 写失败只吞掉，pump 继续；
- **信号传播**：SIGTERM/SIGINT 转发给 child 的 process group；tap 给 child 建**独立
  PGID/session**，只向 child PGID 转发，绝不 killpg 自己；
- **SIGKILL 路径不做优雅关闭**：完整性由 spool 逐行 flush + `.partial` 恢复保证。

本模块只做透明 pump 骨架 + spool 生命周期；JSON-RPC 帧解析/配对在 frames.py，
通过 ``FrameCapture`` 接口挂进 pump。
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
from pathlib import Path

SOURCE_KIND = "mcp-stdio"
PRODUCER_NAME = "octagon-mcp-tap"
PARSER_VERSION = "mcp-tap-v1"

# 单次 read 的块大小。大 payload 跨多块，capture 侧靠 buffer 重组。
_CHUNK = 64 * 1024


def _parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    """解析 tap 自己的参数；``--`` 之后是原 MCP server command（整体后置）。"""
    parser = argparse.ArgumentParser(prog="mcp_tap", add_help=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--phase", default="agent_run")
    parser.add_argument("--spool-dir", required=True)
    parser.add_argument("--policy", default="metadata")
    parser.add_argument("--instance", default=None,
                        help="source instance（多 MCP server 区分）；默认取原 command basename")
    parser.add_argument("--max-frame-bytes", type=int, default=8 * 1024 * 1024)
    if "--" not in argv:
        parser.error("缺少 `--`：其后必须是原 MCP server command")
    sep = argv.index("--")
    ns = parser.parse_args(argv[:sep])
    child_cmd = argv[sep + 1:]
    if not child_cmd:
        parser.error("`--` 之后为空：需要原 MCP server command")
    return ns, child_cmd


def _pump(
    src: "object", dst: "object", capture, *, on_error,
) -> None:
    """把 src 的字节透明泵到 dst，并旁路喂给 capture。

    ``src``/``dst`` 是二进制 fileno 流（stdin/stdout/child pipes）。**先写 dst 再
    capture**：主通信优先，capture 异常绝不影响转发。EOF/断裂时关闭 dst 写端。
    """
    src_fd = src if isinstance(src, int) else src.fileno()
    dst_fd = dst if isinstance(dst, int) else dst.fileno()
    try:
        while True:
            try:
                chunk = os.read(src_fd, _CHUNK)
            except OSError:
                break
            if not chunk:
                break  # EOF
            # 1) 主通信：原样透传（可能部分写，循环写完）。
            _write_all(dst_fd, chunk)
            # 2) capture 旁路：fail-open。
            if capture is not None:
                try:
                    capture.feed(chunk)
                except Exception:
                    pass  # 采集故障不中断 pump
    except Exception as exc:  # 防御：任何意外都不能让 pump 线程静默死掉
        on_error(exc)
    finally:
        # 关闭 dst 写端，让下游看到 EOF（child stdin / parent stdout）。
        try:
            os.close(dst_fd)
        except OSError:
            pass
        if capture is not None:
            try:
                capture.close_direction()
            except Exception:
                pass


def _write_all(fd: int, data: bytes) -> None:
    """完整写出 data（处理部分写）；下游关闭时静默停止（EPIPE）。"""
    view = memoryview(data)
    while view:
        try:
            n = os.write(fd, view)
        except BrokenPipeError:
            return
        except OSError:
            return
        view = view[n:]


def _write_unavailable_marker(
    attempt_id: str, phase: str, spool_dir: Path, instance: str,
) -> None:
    """capture 初始化失败时写一条 unavailable capture_event。

    与失败的 FrameCapture spool 分开——用独立文件名，尽力而为；任何异常都吞掉
    （纯透明 pump 不受影响）。finalizer 见此 event 把 mcp source 标 degraded。"""
    try:
        from datetime import datetime, timezone

        from backend.wire import ids, spool
        from backend.wire.evidence import (
            CaptureEventEvidence, CaptureEventPayload, CorrelationHints,
            EvidenceProducer, EvidenceRedaction, EvidenceSource, EvidenceTime,
        )

        spool_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        valid = phase if phase in (
            "attempt_setup", "agent_run", "verification",
            "artifact_collection", "attempt_cleanup") else "unknown"
        ev = CaptureEventEvidence(
            evidence_id=ids.evidence_id(
                attempt_id=attempt_id, source_kind=SOURCE_KIND,
                source_instance=instance, raw_ref="mcp-stdio:capture-unavailable",
                producer_id="tap"),
            attempt_id=attempt_id, phase=valid,  # type: ignore[arg-type]
            source=EvidenceSource(kind=SOURCE_KIND, instance=instance),
            producer=EvidenceProducer(name=PRODUCER_NAME, version=PARSER_VERSION),
            time=EvidenceTime(observed_at=now, started_at=None, finished_at=None),
            raw_ref=None, correlation_hints=CorrelationHints(), capabilities={},
            redaction=EvidenceRedaction(policy="metadata", status="applied"),
            errors=[], extensions={},
            payload=CaptureEventPayload(
                event="error", source_instance=instance, status="unavailable",
                reason_code="mcp_tap_capture_unavailable",
                message="MCP tap 采集初始化失败，已退化为纯透明 pump（通信不受影响）",
                counters=None, effective_capabilities=None),
        )
        w = spool.SpoolWriter(
            spool_dir / f"{SOURCE_KIND}@{instance}.jsonl",
            expected_attempt_id=attempt_id)
        w.append(ev)
        w.close()
    except Exception:
        pass  # marker 也写不了：纯透明 pump 继续，不中断通信


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ns, child_cmd = _parse_args(argv)

    instance = ns.instance or Path(child_cmd[-1]).name or SOURCE_KIND

    # capture：FrameCapture。构造失败（如 spool 建不了）→ 退化为无 capture
    # 的纯透明 pump（不中断通信，capability probe 失败退化 byte metadata）。
    capture = None
    try:
        from backend.wire.mcp_frames import FrameCapture
        capture = FrameCapture(
            attempt_id=ns.attempt_id, phase=ns.phase,
            spool_dir=Path(ns.spool_dir), instance=instance,
            policy=ns.policy, max_frame_bytes=ns.max_frame_bytes,
        )
    except Exception:
        # 不静默降级。尽力写一条 unavailable capture_event，让 finalizer/
        # manifest 能区分「没有 MCP 调用」与「采集器没工作」（后者 coverage 降级，
        # 不误报 complete）。marker 写失败也不影响通信（纯透明 pump 继续）。
        capture = None
        _write_unavailable_marker(ns.attempt_id, ns.phase, Path(ns.spool_dir), instance)

    import subprocess

    # child **不放独立 session**：tap 与 child 留在 CLI 的进程组，
    # 这样 adapter 超时对 CLI 进程组发 SIGKILL 能一并杀掉 tap + MCP child，不留孤儿。
    # 信号隔离改用**按 child.pid 精确转发**（而非 killpg 自己）实现——既不误杀
    # tap，也不把 child 隔离到 adapter 够不着的独立组。Linux 再加 PR_SET_PDEATHSIG
    # 兜底：tap 一死内核立即 SIGKILL child（跨平台无此机制时靠同组 killpg 覆盖）。
    def _preexec():
        # 仅 Linux：父（tap）死时给本进程发 SIGKILL，杜绝 tap 被 SIGKILL 时的孤儿。
        try:
            import ctypes
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            PR_SET_PDEATHSIG = 1
            libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
        except Exception:
            pass  # 非 Linux / 受限：靠同进程组的 killpg 覆盖

    try:
        proc = subprocess.Popen(
            child_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,  # stderr 直通父进程，不 capture、不改
            preexec_fn=_preexec,
            bufsize=0,
        )
    except FileNotFoundError:
        sys.stderr.write(f"mcp_tap: child command not found: {child_cmd[0]}\n")
        if capture is not None:
            capture.close_all()
        return 127

    # 信号传播：SIGTERM/SIGINT → **child 单进程**（os.kill，不 killpg 自己）。
    def _forward_signal(signum, _frame):
        try:
            os.kill(proc.pid, signum)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _forward_signal)
        except (ValueError, OSError):
            pass  # 非主线程/受限环境

    errors: list[Exception] = []

    def _on_err(exc: Exception) -> None:
        errors.append(exc)

    # 两条 pump 线程：parent.stdin→child.stdin，child.stdout→parent.stdout。
    # stderr 由 child 直接继承父 stderr（Popen stderr=None），无需 pump。
    t_in = threading.Thread(
        target=_pump,
        kwargs=dict(
            src=sys.stdin.buffer, dst=proc.stdin,
            capture=(capture.client_to_server() if capture else None),
            on_error=_on_err,
        ),
        daemon=True,
    )
    t_out = threading.Thread(
        target=_pump,
        kwargs=dict(
            src=proc.stdout, dst=sys.stdout.buffer,
            capture=(capture.server_to_client() if capture else None),
            on_error=_on_err,
        ),
        daemon=True,
    )
    t_in.start()
    t_out.start()

    # 等 child 退出：它退出后 stdout EOF，t_out 自然结束；stdin pump 可能仍阻塞在
    # parent stdin read，daemon 线程随进程退出被回收。
    rc = proc.wait()
    t_out.join(timeout=5.0)

    if capture is not None:
        try:
            capture.close_all(exit_code=rc)
        except Exception:
            pass
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
