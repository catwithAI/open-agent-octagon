"""WireInjection：source 进入 adapter 的唯一接口。

本模块只定义无副作用的数据对象，供 ``backend/adapters/base.py`` 依赖。
**禁止**在这里 import adapter、source 或 lifecycle——否则
``adapters.base → wire.injection`` 会形成循环依赖。

约束（由 lifecycle 的 merge/校验强制，adapter 只消费不校验）：

- ``process_env``/``llm_headers`` 禁止携带 provider auth key，认证永远来自
  ``ModelProviderConfig``；
- ``capture_token`` 不进 repr，不允许序列化到 manifest/spool；
- ``PhaseStateRef`` 必须且只能设置一种 transport。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class CommandRewrite:
    """MCP server command 包装：新 command 前缀 + 原命令整体后置。

    最终形态：``[command, *args_prefix, <原 command>, <原 args...>]``。
    """

    command: str
    args_prefix: tuple[str, ...] = ()


@dataclass(frozen=True)
class PhaseStateRef:
    """独立进程 phase 归属通道：文件或 control URL 二选一。"""

    path: Path | None = None
    control_url: str | None = None

    def __post_init__(self) -> None:
        if (self.path is None) == (self.control_url is None):
            raise ValueError("PhaseStateRef 必须且只能设置 path 或 control_url 之一")


@dataclass(frozen=True)
class WireInjection:
    """lifecycle 合并所有 source 后交给 adapter 的最终注入。

    默认值即「零注入」：enabled=False 时 adapter 的行为必须与 wire 层
    不存在时完全一致（回归基线）。
    """

    enabled: bool = False
    phase: str = "agent_run"
    process_env: Mapping[str, str] = field(default_factory=dict)
    llm_base_url: str | None = None
    llm_headers: Mapping[str, str] = field(default_factory=dict)
    mcp_rewrites: Mapping[str, CommandRewrite] = field(default_factory=dict)
    blade_session_metadata: Mapping[str, str] = field(default_factory=dict)
    phase_state: PhaseStateRef | None = None
    # 不进 repr；lifecycle 保证它不落任何持久化通道。
    capture_token: str | None = field(default=None, repr=False)
