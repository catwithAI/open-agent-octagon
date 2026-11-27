"""Runner / Evaluator 共享的进程级配置。

`create_attempt` / `run_attempt` 测试签名不接受 `data_path` / `envs`,所以
这两件事走模块级状态。FastAPI lifespan 在 startup 时调一次 `bind(...)`,
测试在 fixture 里也调一次,bind 完后整个模块按这份状态工作。

设计动机:单进程内可信——这个状态本来就只在
octagon 后端进程内有效。多进程部署或 evaluator 独立运行时再切换到显式注入。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
import logging
from pathlib import Path
import threading
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class RuntimeState:
    data_path: Path
    db_path: Path
    envs: dict[str, Any] = field(default_factory=dict)  # name -> LoadedEnv
    active_tasks: dict[str, list[asyncio.Task]] = field(default_factory=dict)  # run_id -> tasks
    # attempt_id -> dispatch 协程。`active_tasks` 按 run_id 索引，够不到单个
    # attempt——而 deadline sweeper 要收的正是「同一个 run 里只有部分 attempt
    # 超期」这种情况。没有这张表，sweeper 只能改 DB 状态，协程照跑、
    # 并发闸门照占，超期 attempt 会一直挂到人工 Stop。
    attempt_tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    # attempt_id -> scoring 协程。与 attempt_tasks 同理：sweeper 回收超期
    # 评分时必须按 attempt 粒度取消协程，否则卡死的 scorer 会一直占着
    # scoring_semaphore 槽位。
    scoring_tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    # SSE 推送事件总线：run_id -> {(loop, asyncio.Event)}。状态变更方调用
    # notify_run_update(run_id) 唤醒所有订阅该 run 的流式连接。
    run_update_subscribers: dict[
        str, set[tuple[asyncio.AbstractEventLoop, asyncio.Event]]
    ] = field(default_factory=dict)
    run_update_lock: threading.Lock = field(default_factory=threading.Lock)
    # sweeper 跑在 asyncio.to_thread 里，cancel 必须 call_soon_threadsafe 回到
    # 派发所在的 loop；工作线程里 get_running_loop() 拿到的不是它。
    # 由 `_dispatch_with_lease` 登记 attempt 时惰性写入——bind() 可能发生在
    # 没有运行中 loop 的地方（测试直接调 bind），那时取不到。
    loop: asyncio.AbstractEventLoop | None = None
    # Shared Run service concurrency/admission state.  Kept on RuntimeState so
    # each app/test bind gets fresh event-loop primitives and counters.
    attempt_semaphore: asyncio.Semaphore | None = None
    attempt_semaphore_capacity: int | None = None
    active_attempt_count: int = 0
    max_observed_active_attempts: int = 0
    scoring_semaphore: asyncio.Semaphore | None = None
    scoring_semaphore_capacity: int | None = None
    legacy_admission_event: asyncio.Event | None = None
    active_run_groups: int = 0
    run_group_lock: asyncio.Lock | None = None
    run_group_launch_lock: asyncio.Lock | None = None
    next_group_launch_at: float = 0.0
    # 进程级 Settings。成本核算需要在没有 request 上下文的地方（如
    # _refresh_run_status 这类同步回调）读 cost 配置；其它模块仍走各自的
    # settings 注入，不要把这里当成全局配置入口。
    settings: Any = None
    # run_id -> RunCredential（run 专属上游 key，成本核算用）。
    # **只在内存**：明文 key 不进 DB、不进日志、不进 external_refs。
    # run 结算完成后由 cost.credential.clear() 移除。
    run_credentials: dict[str, Any] = field(default_factory=dict)
    # docker 沙盒启动检查结果（backend.process.sandbox_preflight.SandboxStatus）。
    # None = 尚未检查；dispatch 在沙盒开启且未检查时惰性补做一次。
    sandbox_status: Any = None


_state: RuntimeState | None = None


def bind(
    *,
    data_path: Path,
    db_path: Path,
    envs: dict[str, Any],
    settings: Any = None,
) -> None:
    global _state
    _state = RuntimeState(
        data_path=Path(data_path),
        db_path=Path(db_path),
        envs=dict(envs),
        settings=settings,
    )


def get() -> RuntimeState:
    if _state is None:
        raise RuntimeError(
            "runtime_state 未 bind:Octagon 后端 lifespan 必须先调用 runtime_state.bind(...)"
        )
    return _state


def reset() -> None:
    """测试 teardown 用,避免跨用例状态泄漏。"""
    global _state
    _state = None


def notify_run_update(run_id: str) -> None:
    """线程安全 / 跨 loop 安全地唤醒所有订阅 `run_id` 的 SSE 消费者。

    runtime_state 未 bind 时静默 no-op，确保独立单测不依赖进程级初始化。
    """
    if not run_id:
        return
    state = _state
    if state is None:
        return
    with state.run_update_lock:
        subscribers = list(state.run_update_subscribers.get(run_id, ()))
    if not subscribers:
        return
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None
    for loop, ev in subscribers:
        if loop.is_closed():
            continue
        if current_loop is loop:
            ev.set()
        else:
            try:
                loop.call_soon_threadsafe(ev.set)
            except RuntimeError:
                pass


@contextmanager
def subscribe_run_updates(run_id: str) -> Iterator[asyncio.Event]:
    """订阅指定 `run_id` 的状态更新事件，退出上下文时自动注销并清理空桶。"""
    state = get()
    loop = asyncio.get_running_loop()
    ev = asyncio.Event()
    entry = (loop, ev)
    with state.run_update_lock:
        state.run_update_subscribers.setdefault(run_id, set()).add(entry)
    try:
        yield ev
    finally:
        with state.run_update_lock:
            subs = state.run_update_subscribers.get(run_id)
            if subs is not None:
                subs.discard(entry)
                if not subs:
                    state.run_update_subscribers.pop(run_id, None)


def track_background_task(group: str, task: asyncio.Task) -> asyncio.Task:
    """把后台 Task 纳入受管注册表：持有强引用、完成自动清理、记录未捕获异常。"""
    state = get()
    state.active_tasks.setdefault(group, []).append(task)

    def _on_done(done_task: asyncio.Task) -> None:
        tasks = state.active_tasks.get(group)
        if tasks is not None:
            try:
                tasks.remove(done_task)
            except ValueError:
                pass
            if not tasks:
                state.active_tasks.pop(group, None)
        if done_task.cancelled():
            return
        exc = done_task.exception()
        if exc is not None:
            logger.exception(
                "background task failed group=%s name=%s",
                group,
                done_task.get_name(),
                exc_info=exc,
            )

    task.add_done_callback(_on_done)
    return task
