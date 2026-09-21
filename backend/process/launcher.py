"""Agent 进程启动抽象：attempt 级沙盒上下文 + turn 级进程。

两层上下文对应两种生命周期（spec: docs/specs/260909-agent-sandbox/design.md）：

- ``AgentLauncher.attempt()``：一个 attempt 一次。宿主机执行时是空上下文；
  docker 沙盒执行时在这里创建/销毁容器。
- ``AttemptSandbox.exec()``：一轮一次。宿主机执行时就是
  :func:`backend.process.runtime.agent_process`；docker 执行时是 ``docker exec -i``。

adapter 只依赖这两个 Protocol。``HostLauncher`` 是回归基线：注入它之后
adapter 的行为必须与直接调用 ``agent_process()`` 完全一致。
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from .runtime import agent_process

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

ExecutionLocus = Literal["host", "docker-sandbox"]


@dataclass(frozen=True)
class AttemptSpec:
    """一个 attempt 的沙盒参数；宿主机执行只用到 attempt_id / data_path。"""

    attempt_id: str
    data_path: Path
    agent_name: str
    run_id: str | None = None
    # agent 唯一可写目录；沙盒模式下同路径挂载。
    workspace: Path | None = None
    # 场景级保留的家目录子目录（env meta.yaml 的 `sandbox.keep_home_dirs`）。
    # 默认这些目录被挡在 bind mount 之外（可重建、不留证据）；确实要留作
    # 证据的场景在这里指名。None = 用全局默认。
    keep_home_dirs: tuple[str, ...] | None = None


@dataclass(frozen=True)
class ExecSpec:
    """一轮进程的启动参数。argv / cwd 在同路径挂载下宿主机与容器内一致。"""

    argv: Sequence[str]
    cwd: str
    env: Mapping[str, str] = field(default_factory=dict)
    turn_id: str | None = None


class AttemptSandbox(Protocol):
    """attempt 级执行环境。每轮经 ``exec()`` 拿到一个 asyncio 子进程。

    路径 / URL 翻译接口让 adapter 不感知执行场合：宿主机实现全是恒等，
    docker 实现把宿主机路径映射到三处挂载、把 127.0.0.1 映射到 host.docker.internal。
    """

    locus: ExecutionLocus
    container_id: str | None
    # sandbox.server_side_tools：allow | deny。宿主机执行恒为 allow。
    server_side_tools: str

    def host_home(self, default: Path) -> Path:
        """agent 隔离 HOME 在宿主机上的目录；adapter 往这里写状态。"""
        ...

    def host_ro(self, default: Path) -> Path:
        """只读交付目录（mcp_config / prompt 等）在宿主机上的路径。"""
        ...

    def path(self, host_path: Path | str) -> str:
        """宿主机路径 → 交给 agent 进程（argv / env）用的路径。"""
        ...

    def resolve_cli(self, name: str) -> str | None:
        """CLI 可执行文件：宿主机 which()；沙盒由镜像 PATH 提供。"""
        ...

    def translate_url(self, url: str) -> str:
        """后端回连地址（Env Attempt Server / wire 反代）→ agent 进程可达的地址。"""
        ...

    def security_fields(self) -> dict[str, Any]:
        """并入 build_security_meta 的执行场合字段（镜像 digest、容器 id、版本…）。"""
        ...

    def exec(
        self, spec: ExecSpec
    ) -> "AbstractAsyncContextManager[asyncio.subprocess.Process]": ...

    def build_exec_argv(self, spec: ExecSpec) -> tuple[str, ...]:
        """给自己 Popen 的调用方（dsh SDK）用：只借 argv，不经 ``exec()``。"""
        ...


class AgentLauncher(Protocol):
    locus: ExecutionLocus

    def attempt(
        self, spec: AttemptSpec
    ) -> "AbstractAsyncContextManager[AttemptSandbox]": ...


# ---------------------------------------------------------------------------
# HostLauncher：现状行为的零变化包装
# ---------------------------------------------------------------------------


class HostAttemptSandbox:
    locus: ExecutionLocus = "host"
    container_id: str | None = None
    server_side_tools: str = "allow"

    def __init__(self, spec: AttemptSpec) -> None:
        self._spec = spec

    def host_home(self, default: Path) -> Path:
        return default

    def host_ro(self, default: Path) -> Path:
        return default

    def path(self, host_path: Path | str) -> str:
        return str(Path(host_path).resolve())

    def resolve_cli(self, name: str) -> str | None:
        return shutil.which(name)

    def translate_url(self, url: str) -> str:
        return url

    def security_fields(self) -> dict[str, Any]:
        return {}

    @asynccontextmanager
    async def exec(
        self, spec: ExecSpec
    ) -> AsyncIterator[asyncio.subprocess.Process]:
        async with agent_process(
            argv=spec.argv,
            data_path=self._spec.data_path,
            attempt_id=self._spec.attempt_id,
            cwd=spec.cwd,
            env=dict(spec.env),
        ) as proc:
            yield proc

    def build_exec_argv(self, spec: ExecSpec) -> tuple[str, ...]:
        return tuple(spec.argv)


class HostLauncher:
    """宿主机子进程执行。``attempt()`` 无副作用。"""

    locus: ExecutionLocus = "host"

    @asynccontextmanager
    async def attempt(self, spec: AttemptSpec) -> AsyncIterator[HostAttemptSandbox]:
        yield HostAttemptSandbox(spec)

