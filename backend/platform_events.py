"""后端退出来源归因与 supervisor 决策记录。

周末事故的日志只能证明 Uvicorn 走了优雅关闭流程，**无法回答是谁或什么发送了
终止信号**，也没有持久化退出码、信号、运行时长和重新拉起决策。守卫随后连续
651 次收到 connection refused，服务中断约 10 小时 50 分钟。

本模块给每次启动/退出留下可查询的记录：

- 进程启动即写 ``startup``（含 boot_id / pid / 启动时刻）；
- 收到 SIGTERM/SIGINT/SIGHUP 时记下**信号名**，退出时写 ``shutdown``；
- 没有信号却走到退出 → ``graceful=0``，作为异常退出的证据；
- 上一次启动没有配对的退出记录 → 下次启动时补写 ``crash``，
  这正是 SIGKILL / OOM / 断电这类「来不及留遗言」的情况。

可观测性与自动拉起是两个独立验收面：本模块只负责**归因**，
自动拉起由 systemd 单元（``deploy/octagon.service``）负责，
其重启决策通过 ``supervisor`` 字段记录在这里。

安全：本表可经 API 读取，因此只记录信号/退出码/时长这类元数据，
绝不写入凭据、环境变量原文或命令行。
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from .db import _now_iso, _open_sync

logger = logging.getLogger(__name__)

# 本进程的启动标识与起始时刻。用于把 startup / shutdown 两条记录配对，
# 并在下次启动时判断上一个进程是否「没留下遗言」。
BOOT_ID = uuid.uuid4().hex[:16]
_started_monotonic: float | None = None

# 收到的终止信号（由 install_signal_handlers 填充）。None 表示没收到过信号——
# 那么一次退出就不是被谁「叫停」的，属于异常路径。
_received_signal: str | None = None

_TERMINATION_SIGNALS = ("SIGTERM", "SIGINT", "SIGHUP", "SIGQUIT")


def record_event(
    db_path: Path,
    *,
    event_type: str,
    signal_name: str | None = None,
    exit_code: int | None = None,
    graceful: bool | None = None,
    uptime_seconds: float | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """写一条平台事件。**绝不抛出**——归因失败不能连累启动或关闭。"""
    try:
        with _open_sync(db_path) as conn:
            conn.execute(
                "INSERT INTO platform_events(event_type,occurred_at,pid,boot_id,"
                "signal_name,exit_code,graceful,uptime_seconds,detail_json)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    event_type,
                    _now_iso(),
                    os.getpid(),
                    BOOT_ID,
                    signal_name,
                    exit_code,
                    None if graceful is None else int(graceful),
                    uptime_seconds,
                    json.dumps(detail or {}, ensure_ascii=False),
                ),
            )
            conn.commit()
    except Exception:
        logger.exception("平台事件写入失败 event_type=%s（忽略）", event_type)


def _detect_unclean_previous_exit(db_path: Path) -> dict[str, Any] | None:
    """上一次启动是否没有配对的退出记录（SIGKILL / OOM / 断电的证据）。"""
    try:
        with _open_sync(db_path) as conn:
            conn.row_factory = sqlite3.Row
            prev = conn.execute(
                "SELECT boot_id,occurred_at,pid FROM platform_events "
                "WHERE event_type='startup' AND boot_id<>? "
                "ORDER BY id DESC LIMIT 1",
                (BOOT_ID,),
            ).fetchone()
            if prev is None:
                return None
            closed = conn.execute(
                "SELECT 1 FROM platform_events WHERE boot_id=? "
                "AND event_type IN ('shutdown','crash') LIMIT 1",
                (prev["boot_id"],),
            ).fetchone()
            if closed is not None:
                return None
            return {
                "previous_boot_id": prev["boot_id"],
                "previous_pid": prev["pid"],
                "previous_started_at": prev["occurred_at"],
            }
    except Exception:
        logger.exception("检测上次退出状态失败（忽略）")
        return None


def record_startup(db_path: Path, *, supervisor: str | None = None) -> None:
    """进程启动：写 startup，并为上一次「无遗言」的退出补写 crash 证据。"""
    global _started_monotonic
    _started_monotonic = time.monotonic()

    unclean = _detect_unclean_previous_exit(db_path)
    if unclean is not None:
        # 上个进程没走到 shutdown——SIGKILL、OOM killer 或断电。补写归因，
        # 否则这段中断在事后完全不可查（周末事故正是这种情况）。
        record_event(
            db_path,
            event_type="crash",
            graceful=False,
            detail={
                **unclean,
                "reason": "previous_process_left_no_shutdown_record",
                "inferred_by": f"startup_of_{BOOT_ID}",
            },
        )
        logger.warning(
            "检测到上一个后端进程异常退出（无 shutdown 记录）：%s", unclean
        )

    record_event(
        db_path,
        event_type="startup",
        detail={
            "supervisor": supervisor or _detect_supervisor(),
            "python_pid": os.getpid(),
        },
    )


def record_shutdown(db_path: Path) -> None:
    """进程退出：写 shutdown，含信号来源、运行时长与是否优雅。"""
    uptime = None
    if _started_monotonic is not None:
        uptime = round(time.monotonic() - _started_monotonic, 3)
    # 收到过终止信号 = 有人明确叫停，属于优雅关闭；否则是异常路径。
    graceful = _received_signal is not None
    record_event(
        db_path,
        event_type="shutdown",
        signal_name=_received_signal,
        graceful=graceful,
        uptime_seconds=uptime,
        detail={
            "supervisor": _detect_supervisor(),
            "signal_observed": _received_signal is not None,
        },
    )


def _detect_supervisor() -> str:
    """识别当前进程由谁托管。只读公开环境标记，不记录任何凭据。"""
    if os.environ.get("INVOCATION_ID"):
        return "systemd"  # systemd 为每个服务调用注入
    if os.getppid() == 1:
        return "orphaned_or_init"
    return "manual"


def install_signal_handlers() -> None:
    """记录终止信号来源，然后交回默认行为让 uvicorn 正常收尾。

    只做「记下是哪个信号」这一件事——不拦截、不改变关闭流程，
    因此不会影响 uvicorn 自己的优雅关闭。
    """
    previous: dict[int, Any] = {}

    def _handler(signum: int, frame: Any) -> None:
        global _received_signal
        try:
            _received_signal = signal.Signals(signum).name
        except ValueError:
            _received_signal = str(signum)
        logger.warning("后端收到终止信号：%s", _received_signal)
        # 交回原处理器，保持 uvicorn 的关闭语义不变。
        handler = previous.get(signum)
        if callable(handler):
            handler(signum, frame)

    for name in _TERMINATION_SIGNALS:
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            previous[int(signum)] = signal.getsignal(signum)
            signal.signal(signum, _handler)
        except (ValueError, OSError):
            # 非主线程或平台不支持：跳过，不影响启动。
            continue


def recent_events(db_path: Path, limit: int = 50) -> list[dict[str, Any]]:
    """最近的平台事件，供 API 与部署验收查询。"""
    try:
        with _open_sync(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM platform_events ORDER BY id DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
    except Exception:
        logger.exception("读取平台事件失败")
        return []
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["detail"] = json.loads(item.pop("detail_json", "{}") or "{}")
        except json.JSONDecodeError:
            item["detail"] = {}
        if item.get("graceful") is not None:
            item["graceful"] = bool(item["graceful"])
        result.append(item)
    return result
