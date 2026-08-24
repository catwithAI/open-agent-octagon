"""Attempt 终态收敛与用户停止的**单一实现**。

设计前提：

- **Attempt 事实是权威数据**，Run / Cell / RunGroup / Experiment 都是投影。
  一次收敛必须同时写三条状态轴（legacy ``status`` / ``execution_status`` /
  ``scoring_status``），再从 attempt 事实重建全部上层投影——只改顶层 status
  正是某些边界下需要人工二次修复的原因。
- **用户停止是独立终态 ``cancelled``**，不是 ``failed`` 的子类，也不复用
  ``timeout``。借用任何失败态都会把一次操作决定统计成设施故障。
- **未知分数保持 NULL**，绝不写业务 0 分。
- 一切操作**幂等**：重复 Stop、重复启动恢复都不改变已收敛的结果。

在此之前 Stop 有两份互相偏离的实现（``api.py`` 的 legacy run stop 与
``coordinator.stop_group``），前者甚至绕过整条投影链直接改写 ``runs.status``。
本模块把两者统一到 :func:`converge_attempts`。
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from . import runtime_state
from .db import _now_iso, _open_sync

logger = logging.getLogger(__name__)

# 非终态的三条轴。收敛只推进这些行，已终态的行一律不动（幂等的基础）。
_OPEN_LEGACY = ("queued", "starting_blade_session", "running", "scoring")
_OPEN_EXECUTION = ("queued", "running")
_OPEN_SCORING = ("not_ready", "queued", "running")


def _placeholders(values: Iterable[str]) -> str:
    return ",".join("?" for _ in values)


def converge_attempts(
    db_path: Path,
    attempt_ids: list[str],
    *,
    status: str,
    error_code: str,
    error_message: str,
    execution_status: str | None = None,
    scoring_status: str | None = None,
) -> int:
    """把一批 attempt 一次性收敛到相容终态，并重建全部上层投影。

    只推进仍处于非终态的 attempt；已终态的行保持原样，因此重复调用安全。

    ``execution_status`` / ``scoring_status`` 省略时按 ``status`` 推导：
    用户停止 → 两轴都 ``cancelled``；其余 → execution 取 ``status`` 的执行语义、
    scoring 记 ``cancelled``（评分还没出结果就被终止，不是评分失败）。

    返回实际被推进的 attempt 数量。
    """
    if not attempt_ids:
        return 0
    cancelled = status == "cancelled"
    exec_status = execution_status or ("cancelled" if cancelled else status)
    score_status = scoring_status or "cancelled"
    now = _now_iso()

    with _open_sync(db_path) as conn:
        conn.row_factory = sqlite3.Row
        marks = _placeholders(attempt_ids)
        rows = conn.execute(
            f"SELECT id,run_id,status,execution_status,scoring_status FROM attempts "
            f"WHERE id IN ({marks})",
            tuple(attempt_ids),
        ).fetchall()
        touched: list[str] = []
        run_ids: set[str] = set()
        for row in rows:
            legacy_open = row["status"] in _OPEN_LEGACY
            exec_open = row["execution_status"] in _OPEN_EXECUTION
            score_open = row["scoring_status"] in _OPEN_SCORING
            if not (legacy_open or exec_open or score_open):
                continue  # 已完全收敛：幂等，不动
            # 三条轴分别推进：真实执行失败必须保留，不能被一次 Stop 抹成
            # cancelled；但残留的非终态轴一定要收口，否则就是那种
            # 「顶层 timeout / execution_status 仍 running」的悬挂态。
            if legacy_open:
                conn.execute(
                    "UPDATE attempts SET status=?,error_code=?,error_message=?,"
                    "ended_at=COALESCE(ended_at,?) WHERE id=?",
                    (status, error_code, error_message, now, row["id"]),
                )
            if exec_open:
                conn.execute(
                    "UPDATE attempts SET execution_status=?,execution_ended_at="
                    "COALESCE(execution_ended_at,?),execution_error_code=?,"
                    "execution_error_message=? WHERE id=?",
                    (exec_status, now, error_code, error_message, row["id"]),
                )
            if score_open:
                # score_total 刻意不碰：未知分数保持 NULL，绝不写业务 0 分。
                conn.execute(
                    "UPDATE attempts SET scoring_status=?,scoring_ended_at="
                    "COALESCE(scoring_ended_at,?),scoring_error_code=? WHERE id=?",
                    (score_status, now, error_code, row["id"]),
                )
            touched.append(row["id"])
            if row["run_id"]:
                run_ids.add(row["run_id"])
        conn.commit()

    # 投影重建放在事务外：_refresh_run_status 自己开连接，且会向下串
    # project_cell_for_run → rebuild_group_projection → _project_experiment。
    for attempt_id in touched:
        _reproject(db_path, attempt_id)
    if touched:
        logger.warning(
            "converged %d attempt(s) → %s (%s)", len(touched), status, error_code
        )
    return len(touched)


def _reproject(db_path: Path, attempt_id: str) -> None:
    """从 attempt 事实重建 Run→Cell→RunGroup→Experiment 的全部投影。"""
    try:
        from .run_dispatch import _refresh_run_status

        _refresh_run_status(db_path, attempt_id)
    except Exception:
        logger.exception("re-projection failed for attempt=%s", attempt_id)


def reproject_attempt(db_path: Path, attempt_id: str) -> None:
    """对外的幂等重投影入口（启动恢复 / Stop 之后统一收口用）。"""
    _reproject(db_path, attempt_id)


def open_attempt_ids_for_runs(db_path: Path, run_ids: list[str]) -> list[str]:
    """列出这些 run 下仍有任一状态轴未收敛的 attempt。"""
    if not run_ids:
        return []
    with _open_sync(db_path) as conn:
        marks = _placeholders(run_ids)
        rows = conn.execute(
            f"SELECT id FROM attempts WHERE run_id IN ({marks}) AND ("
            f"status IN ({_placeholders(_OPEN_LEGACY)}) OR "
            f"execution_status IN ({_placeholders(_OPEN_EXECUTION)}) OR "
            f"scoring_status IN ({_placeholders(_OPEN_SCORING)}))",
            (*run_ids, *_OPEN_LEGACY, *_OPEN_EXECUTION, *_OPEN_SCORING),
        ).fetchall()
    return [row[0] for row in rows]


def cancel_attempts_for_runs(db_path: Path, run_ids: list[str]) -> int:
    """用户停止：取消这些 run 下所有未收敛的 attempt。

    调用方负责先取消 dispatch 协程与 scoring job；本函数只负责状态收敛，
    因此可以在杀进程之后安全地重复调用。
    """
    attempt_ids = open_attempt_ids_for_runs(db_path, run_ids)
    return converge_attempts(
        db_path,
        attempt_ids,
        status="cancelled",
        error_code="user_stopped",
        error_message="用户手动停止",
    )


def cancel_dispatch_tasks(run_ids: list[str]) -> int:
    """取消这些 run 在内存里的 dispatch 协程；adapter 的 finally 负责杀进程组。"""
    state = runtime_state.get()
    cancelled = 0
    for run_id in run_ids:
        for task in state.active_tasks.pop(run_id, []):
            if not task.done():
                task.cancel()
                cancelled += 1
    return cancelled


def cancel_attempt_tasks(attempt_ids: list[str]) -> int:
    """按 **attempt 粒度**发起 dispatch 协程取消；adapter 的 finally 负责杀进程组。

    返回**已发起**取消的数量，不是已生效的数量——见下面的跨线程说明。

    与 :func:`cancel_dispatch_tasks` 的区别是粒度：那个按 run_id 取消整个 run，
    Stop 用它没问题；deadline sweeper 不行——同一个 run 里通常只有部分 attempt
    超期，连坐会把正常跑着的一起杀掉。

    **为什么必须取消而不能只改 DB**：并发闸门是 ``_dispatch_with_lease``
    里的 ``async with semaphore``，信号量要等 ``await dispatch_attempt(...)``
    返回才释放。sweeper 只写 DB 的话，状态是 ``timeout`` 了、协程还在跑、闸门
    位置照占，后续 attempt 一直排队——现场表现就是"必须人工 Stop 才推进"。

    **跨线程且异步生效**：sweeper 跑在 ``asyncio.to_thread`` 的工作线程里，
    那里 ``task.cancel()`` 不能直接调（不是 task 所属 loop 的线程）。所以取
    ``state.loop`` 用 ``call_soon_threadsafe`` 把 cancel 排进派发 loop——
    **调度即返回，协程此刻通常还活着**。若 loop 已停转，回调永不执行，
    而计数仍然 +1；日志措辞因此用"已发起"，别据此断言闸门已释放。
    在 loop 线程内调用时直接 cancel，那条路径才是同步生效的。
    """
    # runtime_state 未 bind 时不得抛异常：本函数在 sweep_once 里先于 DB 收敛
    # 调用，抛出会连带掐掉整轮 sweep（含评分轴收敛）。取消是「尽力而为」的
    # 附加动作，**DB 收敛才是不可失败的主线**。
    try:
        state = runtime_state.get()
    except RuntimeError:
        return 0

    loop = state.loop
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    cancelled = 0
    for attempt_id in attempt_ids:
        task = state.attempt_tasks.get(attempt_id)
        if task is None or task.done():
            continue
        if current_loop is not None and current_loop is loop:
            task.cancel()
        elif loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(task.cancel)
        else:
            # 没有可用 loop（进程正在关停，或从未派发过）——协程要么已经没了，
            # 要么马上会随 loop 一起结束。DB 收敛仍然继续。
            logger.warning(
                "cancel_attempt_tasks: 无可用 loop，跳过 attempt=%s", attempt_id
            )
            continue
        cancelled += 1
    return cancelled


def kill_orphaned_agent_processes(db_path: Path, run_ids: list[str]) -> int:
    """杀掉这些 run 下**没有内存协程**、但进程仍存活的本地 Agent。

    崩溃或重启后，CLI 进程被刻意保留下来，可它已经不在 active_tasks
    里——``cancel_dispatch_tasks`` 够不着它，adapter 的 finally 也不会再跑。
    此前 Stop 只把 DB 改成 cancelled，进程却继续烧 token，直到人工介入
    （周末现场的孤儿 Kimi 就是这样跑了两天）。

    落盘的 pid/pgid 正是为此存在：Stop 直接照它把进程组杀掉。杀不掉只记
    日志——状态收敛仍要继续，否则 attempt 会永远挂在 running 上。
    """
    state = runtime_state.get()
    killed = 0
    for attempt_id in open_attempt_ids_for_runs(db_path, run_ids):
        try:
            from .process.lifecycle import kill_recorded_agent_process

            if kill_recorded_agent_process(state.data_path, attempt_id):
                killed += 1
        except Exception:
            logger.exception("kill orphaned agent failed attempt=%s", attempt_id)
    return killed
