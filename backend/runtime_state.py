"""Runner / Evaluator 共享的进程级配置。

`create_attempt` / `run_attempt` 测试签名不接受 `data_path` / `envs`,所以
这两件事走模块级状态。FastAPI lifespan 在 startup 时调一次 `bind(...)`,
测试在 fixture 里也调一次,bind 完后整个模块按这份状态工作。

设计动机:单进程内可信——这个状态本来就只在
octagon 后端进程内有效。多进程部署或 evaluator 独立运行时再切换到显式注入。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


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
