"""本地 Agent CLI 的单一启动—收尾生命周期入口。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from .identity import record_agent_process
from .lifecycle import terminate_unless_shutting_down

logger = logging.getLogger(__name__)


@asynccontextmanager
async def agent_process(
    *,
    argv: Sequence[str],
    data_path: Path | None,
    attempt_id: str,
    stdout: int | None = asyncio.subprocess.PIPE,
    stderr: int | None = asyncio.subprocess.PIPE,
    limit: int = 10 * 1024 * 1024,
    **popen_kwargs: Any,
) -> AsyncIterator[asyncio.subprocess.Process]:
    """启动、登记并按关停策略收敛一个本地 Agent CLI 进程。"""
    for forbidden in ("start_new_session", "preexec_fn"):
        if forbidden in popen_kwargs:
            raise TypeError(f"agent_process() does not accept {forbidden}")

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=stdout,
        stderr=stderr,
        limit=limit,
        start_new_session=True,
        **popen_kwargs,
    )
    try:
        record_agent_process(data_path, attempt_id, proc)
    except Exception:
        logger.warning("无法记录 agent 进程信息 attempt=%s", attempt_id, exc_info=True)

    try:
        yield proc
    finally:
        if terminate_unless_shutting_down(proc):
            await proc.wait()
