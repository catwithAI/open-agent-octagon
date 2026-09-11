"""常驻 deadline sweeper。

此前收敛只发生在**启动时**（``recovery.schedule_startup_recovery`` 扫一次），
服务持续运行期间卡死的 attempt 无人处理——周末现场有 attempt 的进程早已消失，
Attempt / Run / Cell / RunGroup 却保持 running 约两天，直到人工介入。

sweeper 把那段逻辑从「启动一次性」提升为「周期常驻」，判据全部来自数据库里的
**绝对时间**，因此对协程异常、worker 崩溃和服务重启一致有效：

    execution_status='running' AND now > execution_deadline_at
    scoring_status  ='running' AND now > scoring_deadline_at

收敛走 :mod:`backend.convergence` 的同一实现，所以 sweeper、Stop 和启动恢复
三条路径产出完全一致的终态与投影。

超时不是取消：执行超时记 ``timeout``、评分超时记 ``timed_out``，
两者都与用户主动停止的 ``cancelled`` 区分开。这说的是**终态语义**——
执行层面 sweeper 确实会取消协程（见下），但记录的结论不是 ``cancelled``。

收敛执行超时时必须**同时**取消 dispatch 协程：并发闸门的信号量要等
``await dispatch_attempt(...)`` 返回才释放，只改 DB 的话协程照跑、闸门照占，
现场表现是"attempt 超期 40% 仍 running，必须人工 Stop 才推进"。
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path

from . import runtime_state
from .convergence import cancel_attempt_tasks, converge_attempts
from .db import _now_iso, _open_sync

logger = logging.getLogger(__name__)

# 宽限期：deadline 到点后再等一会儿才收敛，避免和正常收尾路径抢同一个 attempt
# （adapter 自己的超时分支通常在 deadline 附近就把状态写好了）。
DEFAULT_GRACE_SECONDS = 60
DEFAULT_INTERVAL_SECONDS = 60


def _expired_execution(db_path: Path, now: str) -> list[str]:
    with _open_sync(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id FROM attempts WHERE execution_status='running' "
            "AND execution_deadline_at IS NOT NULL AND execution_deadline_at < ?",
            (now,),
        ).fetchall()
    return [row["id"] for row in rows]


def _expired_scoring(db_path: Path, now: str) -> list[str]:
    with _open_sync(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id FROM attempts WHERE scoring_status='running' "
            "AND scoring_deadline_at IS NOT NULL AND scoring_deadline_at < ?",
            (now,),
        ).fetchall()
    return [row["id"] for row in rows]


def sweep_once(db_path: Path, *, grace_seconds: int = DEFAULT_GRACE_SECONDS) -> dict[str, int]:
    """扫描一轮并收敛过期项。同步、幂等，可直接在测试里调用。"""
    from .db import _iso_after

    # 判据点 = now - grace：只有过期超过宽限期的才收。
    cutoff = _iso_after(_now_iso(), -grace_seconds)

    execution_ids = _expired_execution(db_path, cutoff)
    # 取消协程 + 写 DB。只写 DB 不取消的话，信号量要等
    # `await dispatch_attempt(...)` 返回才释放——状态是 timeout 了、协程还在跑、
    # 并发闸门照占，后续 attempt 一直排队，现场只能靠人工 Stop 推进。
    #
    # **取消先发起，但不保证先生效**。生产路径上 sweep_once 跑在
    # `asyncio.to_thread` 的工作线程里，取消走 `call_soon_threadsafe` 只是把
    # cancel 排进派发 loop 就返回；下面的 converge_attempts 在工作线程里继续
    # 执行时，目标协程通常还活着（实测如此）。所以这里**没有**「先取消因而
    # 关闭了覆盖窗口」这回事。
    #
    # 之所以仍然安全：converge_attempts 只推进非终态的行（幂等的基础），
    # 而协程被取消后走的是 adapter 的取消路径，不会把已终态的 timeout 改回去。
    # 两边写同一行的竞态由「只动非终态」这条规则消解，不靠时序。
    #
    # 取消失败绝不能掐掉收敛：DB 收敛是主线（要解决的正是「永远挂在
    # running」），取消只是尽力而为的附加动作。异常吞掉并记日志。
    if execution_ids:
        try:
            scheduled = cancel_attempt_tasks(execution_ids)
        except Exception:
            logger.exception("sweeper 取消超期协程失败（继续收敛 DB）")
        else:
            if scheduled:
                # 措辞是「已发起」而非「已取消」：跨线程时这里只是排了 cancel，
                # 回调是否真的执行取决于 loop 是否还在转。运维看日志判断闸门
                # 是否释放时，这个区别很要紧。
                logger.warning("sweeper 已发起取消超期执行协程 %d 个", scheduled)
    swept_execution = converge_attempts(
        db_path,
        execution_ids,
        status="timeout",
        error_code="execution_deadline_exceeded",
        error_message="执行超过持久化 deadline，由 sweeper 收敛",
        execution_status="timeout",
        # 执行超时后产物可能仍可评分，评分轴交给评分链路自己决定，
        # 这里只把**已经悬挂**的评分轴收口（converge 只动非终态轴）。
        scoring_status="skipped",
    )

    scoring_ids = _expired_scoring(db_path, cutoff)
    # 注意：评分轴**有和执行轴一样的闸门占用问题，且尚未修**。
    # `_execute_job`（scoring_queue.py:317）同样 `async with _scoring_semaphore`
    # 跨整个 job 持有，这里只改 DB 不取消协程，卡住的 scorer 会一直占着评分
    # 并发位。当前只修了执行轴，评分轴留待单独处理——需要一套评分 job 的
    # 登记表，与 attempt_tasks 不是同一个东西。
    swept_scoring = converge_attempts(
        db_path,
        scoring_ids,
        # 评分超时不改写执行结论：Agent 做完了就是做完了。
        status="scoring_failed",
        error_code="scoring_deadline_exceeded",
        error_message="评分超过持久化 deadline，由 sweeper 收敛",
        execution_status=None,
        scoring_status="timed_out",
    )

    if swept_execution or swept_scoring:
        logger.warning(
            "sweeper 收敛：execution=%d scoring=%d", swept_execution, swept_scoring
        )
    result = {"execution": swept_execution, "scoring": swept_scoring}
    result.update(_sweep_sandbox_containers(db_path))
    return result


def _attempt_is_active(db_path: Path, attempt_id: str) -> bool:
    from .models import NON_TERMINAL_ATTEMPT_STATUSES

    with _open_sync(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM attempts WHERE id=?", (attempt_id,)
        ).fetchone()
    return row is not None and row[0] in NON_TERMINAL_ATTEMPT_STATUSES


def _sweep_sandbox_containers(db_path: Path) -> dict[str, int]:
    """docker 沙盒开启时回收孤儿容器：已退出的 rm；attempt 已终态却还在跑的 kill。

    后端崩溃时 attempt() 上下文没机会退出，容器会留下来——这里是兜底。
    """
    try:
        settings = runtime_state.get().settings
    except Exception:  # noqa: BLE001 —— 测试直接调 sweep_once 时可能未 bind
        return {}
    if settings is None or not getattr(settings, "sandbox", None) or not settings.sandbox.enabled:
        return {}
    try:
        from .process.docker_launcher import sweep_sandbox_containers

        swept = sweep_sandbox_containers(
            is_attempt_active=lambda attempt_id: _attempt_is_active(db_path, attempt_id),
        )
    except Exception:
        logger.exception("sweeper 回收沙盒容器失败（继续）")
        return {}
    if swept.get("containers_killed") or swept.get("containers_removed"):
        logger.warning("sweeper 沙盒容器：killed=%d removed=%d",
                       swept["containers_killed"], swept["containers_removed"])
    return swept


async def run_deadline_sweeper(
    db_path: Path,
    *,
    stop_event: asyncio.Event,
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    grace_seconds: int = DEFAULT_GRACE_SECONDS,
) -> None:
    """常驻循环。异常绝不逃逸——sweeper 挂掉不能连累整个后端。"""
    logger.info("deadline sweeper 启动（间隔 %ds，宽限 %ds）", interval_seconds, grace_seconds)
    while not stop_event.is_set():
        try:
            # 阻塞的 sqlite 扫描放到线程里，别拖慢事件循环。
            await asyncio.to_thread(sweep_once, db_path, grace_seconds=grace_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("deadline sweeper 单轮失败（继续下一轮）")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            pass
    logger.info("deadline sweeper 停止")
