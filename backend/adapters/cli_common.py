"""CLI adapter 共享工具：并发 stderr 排空、多轮 stderr 追加与安全关闭 writer。

背景：
- OS 管道缓冲区默认仅 64KB；若子进程在运行期向 stderr 持续输出而父进程只在
  stdout 消费完后才 `await proc.stderr.read()`，子进程会阻塞在 `write(2)`，
  父进程阻塞在 stdout 读取，形成死锁。`drain_stream_to_bytes()` 与 stdout
  消费并发排空 stderr，超出上限后仍继续读到 EOF，确保子进程永不阻塞。
- 多轮 conversation 每轮起一个新 CLI 子进程；旧实现用 `write_text()` 写
  `stderr.txt` 会静默覆盖前面轮次的错误现场。`append_stderr_file()` 保留每轮
  非空 stderr 并在轮次间插入清晰分隔符。
- `close_quietly()` 用于 `finally:` 块安全关闭 `ConversationTraceWriter` 等句柄，
  不掩盖主异常。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_STDERR_MAX_BYTES = 2 * 1024 * 1024
_DRAIN_CHUNK_SIZE = 8192


async def drain_stream_to_bytes(
    stream: asyncio.StreamReader | None,
    *,
    max_bytes: int = DEFAULT_STDERR_MAX_BYTES,
) -> bytes:
    """并发分块排空子进程流（如 stderr）直到 EOF，防止 64KB 管道写满死锁。

    最多保留 ``max_bytes`` 字节正文；超出部分仍继续读取丢弃直到 EOF（保证子进程
    不会因管道满而阻塞），并在末尾附加截断提示。
    """
    if stream is None:
        return b""

    limit = max(0, max_bytes)
    buf = bytearray()
    truncated = False
    discarded = 0

    while True:
        chunk = await stream.read(_DRAIN_CHUNK_SIZE)
        if not chunk:
            break
        if len(buf) < limit:
            remaining = limit - len(buf)
            if len(chunk) <= remaining:
                buf.extend(chunk)
            else:
                buf.extend(chunk[:remaining])
                truncated = True
                discarded += len(chunk) - remaining
        else:
            truncated = True
            discarded += len(chunk)

    if truncated:
        notice = (
            f"\n... [stderr truncated: exceeded {limit} bytes, "
            f"discarded {discarded} additional bytes]\n"
        ).encode("utf-8")
        buf.extend(notice)

    return bytes(buf)


async def finish_stream_drain(
    task: asyncio.Task[bytes] | None,
    *,
    timeout: float = 5.0,
) -> bytes:
    """等待并发排空任务完成；超时则取消并回收，避免遗留子进程句柄导致挂起。"""
    if task is None:
        return b""
    try:
        return await asyncio.wait_for(task, timeout=timeout)
    except asyncio.TimeoutError:
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        return b""
    except asyncio.CancelledError:
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        raise
    except Exception:  # noqa: BLE001 — stderr 读取异常不应覆盖主流程结果
        logger.debug("stderr drain task raised unexpectedly", exc_info=True)
        return b""


async def cancel_stream_drain(task: asyncio.Task[bytes] | None) -> None:
    """在 finally 中取消并回收未完成的排空任务，且不掩盖主异常。"""
    if task is None:
        return
    if not task.done():
        task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


def append_stderr_file(
    stderr_path: Path,
    text: str,
    *,
    turn_index: int | None = None,
) -> None:
    """将非空 stderr 追加写入 ``stderr_path``，多轮运行时保留前序轮次内容。

    文件已有内容时插入清晰的轮次分隔符，避免后续轮次静默覆盖前序轮次的诊断信息。
    """
    cleaned = text.strip()
    if not cleaned:
        return
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    has_existing = stderr_path.exists() and stderr_path.stat().st_size > 0
    with stderr_path.open("a", encoding="utf-8") as f:
        if has_existing:
            if turn_index is not None:
                f.write(f"\n--- turn {turn_index} ---\n")
            else:
                f.write("\n---\n")
        f.write(cleaned)


def close_quietly(writer: Any) -> None:
    """安全关闭 writer（如 ``ConversationTraceWriter``），不掩盖主异常。"""
    if writer is None:
        return
    close_fn = getattr(writer, "close", None)
    if not callable(close_fn):
        return
    try:
        close_fn()
    except Exception:  # noqa: BLE001 — cleanup in finally must never mask primary errors
        logger.debug("close_quietly(%r) ignored error", writer, exc_info=True)
