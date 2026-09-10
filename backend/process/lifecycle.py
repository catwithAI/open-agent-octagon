"""本地 Agent 进程的终止与后端关停语义。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from pathlib import Path

from .identity import agent_process_is_alive, read_agent_process

logger = logging.getLogger(__name__)


def kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    """杀掉 CLI 子进程及其整个进程组；进程已退出时静默返回。"""
    if proc.returncode is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


# Stop、超时和后端关停都表现为 task.cancel()，adapter 的 finally 无法区分。
# 标志必须在同进程重复启停的 teardown 中清除，否则会泄漏给后续 app。
_shutting_down = False


def begin_shutdown() -> None:
    """标记进入关停；此后仍在运行的 Agent 不随服务一起终止。"""
    global _shutting_down
    _shutting_down = True


def end_shutdown() -> None:
    """清除关停标志。仅供 teardown 与同进程内重复启停使用。"""
    global _shutting_down
    _shutting_down = False


def is_shutting_down() -> bool:
    return _shutting_down


def terminate_unless_shutting_down(proc: asyncio.subprocess.Process) -> bool:
    """runtime 退出判据：返回是否已执行 kill、因而还需要 await 回收。"""
    if proc.returncode is not None:
        return False
    if _shutting_down:
        logger.warning(
            "后端关停：保留仍在运行的 CLI 进程组 pid=%s，不随服务一起终止",
            getattr(proc, "pid", None),
        )
        return False
    kill_process_tree(proc)
    return True


def kill_recorded_agent_process(data_path: Path | None, attempt_id: str) -> bool:
    """按落盘身份杀掉跨重启存活的 Agent，返回是否确实杀到。"""
    if not agent_process_is_alive(data_path, attempt_id):
        return False
    identity = read_agent_process(data_path, attempt_id)
    if identity is None:
        return False
    if identity.kind == "docker":
        from .docker_launcher import kill_container

        killed = kill_container(identity.container_id or "")
        if killed:
            logger.warning(
                "Stop：杀掉跨重启存活的沙盒容器 attempt=%s container=%s",
                attempt_id, identity.container_id,
            )
        return killed
    for killer, target in (
        (os.killpg, identity.pgid),
        (os.kill, identity.pid),
    ):
        if target is None:
            continue
        try:
            killer(target, signal.SIGKILL)
            logger.warning(
                "Stop：杀掉跨重启存活的 agent 进程 attempt=%s pid=%s pgid=%s",
                attempt_id,
                identity.pid,
                identity.pgid,
            )
            return True
        except (ProcessLookupError, PermissionError, OSError):
            continue
    return False
