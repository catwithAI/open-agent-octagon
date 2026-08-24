"""Attempt 级总 deadline。

timeout_seconds 是整个 attempt 的墙钟预算：deadline 只在 attempt 开始创建
一次，每轮用 remaining() 取剩余时长做轮级 wait，不得每轮重新获得完整预算。
monotonic clock 注入以便 fake clock 单测。
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Callable, Iterator

# 统一错误码：轮间预算耗尽（下一轮根本不该启动）与轮内超时
# （当前轮被截断）是不同的失败语义，UI/分析按码区分。
ERROR_BUDGET_EXHAUSTED_BETWEEN_TURNS = "conversation_budget_exhausted"
ERROR_TURN_TIMEOUT = "turn_timeout"


class AttemptBudgetExhausted(Exception):
    """轮间检查发现预算已尽：不再发起新请求/新进程。"""

    error_code = ERROR_BUDGET_EXHAUSTED_BETWEEN_TURNS


class AttemptDeadline:
    """一次 attempt 的墙钟预算。timeout_seconds=None → 不限时。"""

    def __init__(
        self,
        timeout_seconds: float | None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._deadline: float | None = (
            None if timeout_seconds is None else clock() + float(timeout_seconds)
        )
        self._suspend_depth = 0
        self._suspend_started: float | None = None

    @property
    def unlimited(self) -> bool:
        return self._deadline is None

    def remaining(self) -> float | None:
        """剩余秒数；不限时返回 None；已超时返回 0.0（不返回负数）。"""
        if self._deadline is None:
            return None
        return max(0.0, self._deadline - self._clock())

    def expired(self) -> bool:
        if self._deadline is None:
            return False
        return self._clock() >= self._deadline

    def check_before_turn(self) -> float | None:
        """轮启动前的门禁：耗尽即抛，否则返回可用于 wait 的剩余时长。"""
        remaining = self.remaining()
        if remaining is not None and remaining <= 0:
            raise AttemptBudgetExhausted(
                "attempt 总预算已耗尽，不再启动下一轮"
            )
        return remaining

    @contextmanager
    def suspend(self) -> Iterator[None]:
        """暂停 Agent 活跃预算计时。

        Submission Judge / reviewer 属于 evaluator 时间，不应消耗候选 Agent 的
        execution budget。退出上下文时把暂停期间的墙钟耗时整体补回 deadline。
        不限时模式保持 no-op。
        """
        if self._deadline is None:
            yield
            return
        if self._suspend_depth == 0:
            self._suspend_started = self._clock()
        self._suspend_depth += 1
        try:
            yield
        finally:
            self._suspend_depth -= 1
            if self._suspend_depth == 0:
                started = self._suspend_started
                self._suspend_started = None
                if started is not None:
                    self._deadline += max(0.0, self._clock() - started)
