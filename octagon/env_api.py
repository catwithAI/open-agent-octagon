"""env 作者面向的窄接口。

两条主路径:

- `@env_tool(name=, description=, parameters=)` 装饰业务函数,装完即被注册到
  "当前 import 中模块"的 module-level registry。env loader 在 import env 的
  `core.py` 之前先调 `clear_current_registry()`,import 完再 `get_current_registry()`
  把 registry 绑定到 env_name。
- 业务函数被调用时实际执行的是 `RegisteredTool.call(ctx, **kwargs)`,wrapper
  负责计时 + 写 trace + 异常重抛。**业务函数自己不需要 try/except 写 trace。**

trace 文件路径约定:
    `<data_path>/attempts/{attempt_id}/trace.jsonl`

测试契约见 `tests/test_t02_env_api.py`。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


# ---------- TraceWriter ---------------------------------------------------


class TraceWriter:
    """把工具调用序列以 JSONL 形式写到 attempt 专属文件。

    线程安全:多个并发 tool 调用可能共用同一个 attempt(M1 不允许,但保险起见
    加锁),写入用 line-buffered 模式 + lock 串行化。
    """

    def __init__(self, *, data_path: Path | str, attempt_id: str, env_session_id: str) -> None:
        self._data_path = Path(data_path)
        self._attempt_id = attempt_id
        self._env_session_id = env_session_id
        self._lock = threading.Lock()
        self._path = self._data_path / "attempts" / attempt_id / "trace.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def record(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        result: Any,
        is_error: bool,
        duration_ms: int,
    ) -> None:
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "attempt_id": self._attempt_id,
            "env_session_id": self._env_session_id,
            "tool_name": tool_name,
            "arguments": arguments,
            "result": result,
            "is_error": is_error,
            "duration_ms": duration_ms,
        }
        line = json.dumps(row, ensure_ascii=False, default=str) + "\n"
        with self._lock, self._path.open("a", encoding="utf-8") as fp:
            fp.write(line)


# ---------- EnvContext ----------------------------------------------------


@dataclass
class EnvContext:
    """env tool 收到的统一上下文。

    Env Attempt Server 在派发前构造,业务函数只读使用。
    """

    attempt_id: str
    env_session_id: str
    db: sqlite3.Connection
    trace: TraceWriter


# ---------- RegisteredTool ------------------------------------------------


@dataclass
class RegisteredTool:
    name: str
    description: str
    parameters: dict[str, Any]
    func: Callable[..., Any]
    is_async: bool = field(default=False)

    def call(self, ctx: EnvContext, **kwargs: Any) -> Any:
        """同步入口。即使 func 是 async,这里也会 `asyncio.run` 它。

        Env Attempt Server 在 FastAPI handler 里如果遇到 async tool,应该用
        `acall` 而不是 `call`,避免重复 event loop。M1 同步路径优先,够用。
        """
        if self.is_async:
            return asyncio.run(self._invoke_async(ctx, kwargs))
        return self._invoke_sync(ctx, kwargs)

    async def acall(self, ctx: EnvContext, **kwargs: Any) -> Any:
        """异步入口。同步 func 也走这条,直接执行(不开 thread)。"""
        if self.is_async:
            return await self._invoke_async(ctx, kwargs)
        return self._invoke_sync(ctx, kwargs)

    def _invoke_sync(self, ctx: EnvContext, kwargs: dict[str, Any]) -> Any:
        started = time.monotonic()
        try:
            result = self.func(ctx, **kwargs)
            duration_ms = int((time.monotonic() - started) * 1000)
            ctx.trace.record(
                tool_name=self.name,
                arguments=kwargs,
                result=result,
                is_error=False,
                duration_ms=duration_ms,
            )
            return result
        except Exception as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            ctx.trace.record(
                tool_name=self.name,
                arguments=kwargs,
                result={"error": str(exc), "type": exc.__class__.__name__},
                is_error=True,
                duration_ms=duration_ms,
            )
            raise

    async def _invoke_async(self, ctx: EnvContext, kwargs: dict[str, Any]) -> Any:
        started = time.monotonic()
        try:
            result = await self.func(ctx, **kwargs)
            duration_ms = int((time.monotonic() - started) * 1000)
            ctx.trace.record(
                tool_name=self.name,
                arguments=kwargs,
                result=result,
                is_error=False,
                duration_ms=duration_ms,
            )
            return result
        except Exception as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            ctx.trace.record(
                tool_name=self.name,
                arguments=kwargs,
                result={"error": str(exc), "type": exc.__class__.__name__},
                is_error=True,
                duration_ms=duration_ms,
            )
            raise


# ---------- registry ------------------------------------------------------


_current_registry: dict[str, RegisteredTool] = {}


def clear_current_registry() -> None:
    """env loader 在 import 一个 env `core.py` 之前调用,清空模块级 registry。"""
    _current_registry.clear()


def get_current_registry() -> dict[str, RegisteredTool]:
    """返回当前已注册工具的浅拷贝,供 env loader 取走绑定到 env_name。"""
    return dict(_current_registry)


def env_tool(
    *,
    name: str,
    description: str,
    parameters: dict[str, Any],
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """把业务函数注册到当前模块级 registry。

    业务函数签名约定:`func(ctx: EnvContext, **business_kwargs) -> dict | Any`,
    返回值会被 trace 原样落盘——保持 JSON 可序列化(必要时业务自己 dict 化)。
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        is_async = asyncio.iscoroutinefunction(func)
        if name in _current_registry:
            raise ValueError(f"重复注册 env_tool: {name!r}")
        tool = RegisteredTool(
            name=name,
            description=description,
            parameters=parameters,
            func=func,
            is_async=is_async,
        )
        _current_registry[name] = tool
        # 返回原函数本身(不是 wrapper),让 env 内部仍可以直接互调,不走 trace。
        return func

    return decorator
