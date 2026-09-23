"""多 agent 对比调度。

POST /runs 创建多个 attempt（每个 agent 一个），通过 BackgroundTasks 并行执行。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

from . import runtime_state
from .adapters.base import AdapterRunInput, McpServerSpec
from .adapters.blade_service import (
    AdapterEnv,
    BladeAdapterConfig,
    BladeServiceAdapter,
)
from .config import Settings
from .db import _iso_after, _now_iso, _open_sync, append_attempt_observation
from .runner import run_attempt
from .model_providers import parse_model_ref
from .process.launcher import AgentLauncher, HostLauncher
from .process.sandbox_preflight import (
    ATTEMPT_STATUS_SANDBOX_UNAVAILABLE,
    SandboxUnavailable,
    check_sandbox,
    require_sandbox_agent,
)
from .wire.lifecycle import (
    CapturePreparationError,
    WireCaptureSession,
    capture_capabilities_for,
)
from .wire.policy import resolve_effective_policy

logger = logging.getLogger(__name__)

# 反代只覆盖走纯 HTTP+SSE、base URL 可改的 agent：CC/Codex。
# blade 走 SDK（REST+Socket.IO），模型调用在 blade 进程内，反代够不着。
_HTTP_PROXY_AGENTS = frozenset({
    "claude-code", "codex", "kimi-code", "opencode", "mimo-code",
    # dsh 走 SDK stdio JSON-RPC，但模型调用是子进程内的普通 HTTP+SSE，
    # base_url 由 adapter 生成的 pi-ai route 决定 → 反代插得进去。
    "dsh",
})
# MCP stdio tap 覆盖走本机 stdio MCP server 的 agent：CC/Codex 及 kimi/opencode
# 系。blade 的工具经 Env Attempt Server HTTP 回调（env-inbound source
# 覆盖），不走本机 MCP stdio。
_MCP_TAP_AGENTS = frozenset({
    "claude-code", "codex", "kimi-code", "opencode", "mimo-code",
    # dsh 的 MCP 同样是本机 stdio 子进程（dsh-mcp-client 的 transport: stdio）。
    "dsh",
})

# BA 的编程能力由内建软件工厂 solution 提供。编程环境不应回落到 Octagon
# 为业务场景同步的薄壳 skill；create_session 必须显式选择这个 solution。
_BA_CODING_SOLUTION_ID = "app-dev"
# 角色必须显式传。blade 侧 `Solution.role()` 在 biz_role_id 为 None 时回落 "default"，
# 但那是隐式行为：blade_service 只在 env.biz_role_id 非空时才把该字段带进
# create_session，落到 session 上就是 biz_role_id=None。产品里真实会话是
# app-dev/default，这里显式对齐，避免"看着配了 solution、角色其实没绑"。
_BA_CODING_BIZ_ROLE_ID = "default"
_BA_GENERAL_CHAT_SOLUTION_ID = "general_chat"


def _build_wire_sources(
    *, agent_name: str, model: str | None, settings: Settings, attempt_id: str,
    env_name: str, data_path: Any, mcp_server_names: tuple[str, ...] = (),
) -> list[Any]:
    """组装该 attempt 的 wire source：
    - HttpProxySource：CC/Codex 且模型是命名第三方 provider；
    - McpStdioSource：CC/Codex（仅包装场景显式提供的 MCP server）。
    blade attempt 一律不挂（走 SDK / Env Server HTTP，另有 source）。"""
    sources: list[Any] = []
    if agent_name in _HTTP_PROXY_AGENTS and model:
        ref = parse_model_ref(model, settings.model_providers or {})
        if ref.provider is not None:
            from .wire.sources.http_proxy_source import HttpProxySource
            sources.append(HttpProxySource(
                attempt_id=attempt_id, provider=ref.provider,
                public_base_url=settings.octagon.public_base_url,
            ))
    if agent_name in _MCP_TAP_AGENTS and mcp_server_names:
        from .wire.sources.mcp_stdio import McpStdioSource
        for server_name in mcp_server_names:
            sources.append(McpStdioSource(
                attempt_id=attempt_id, env_name=env_name, data_path=data_path,
                server_name=server_name,
            ))
    return sources

KNOWN_AGENTS = (
    "blade-agent", "claude-code", "codex", "kimi-code", "opencode", "mimo-code",
    "dsh",
    # 无凭据的确定性适配器，仅供 CI / 冒烟；不参与真实对比评测。
    "fake",
)


def _blade_model_is_definitely_not_anthropic(model: str | None) -> bool:
    """blade 的 model 是否**明确**为非 Anthropic 系（据此放行多轮 thinking）。

    背景：signature 400 是 Anthropic extended thinking 独有机制——历史 thinking
    block 的服务端 `signature` 跨请求失效即 400；非 Anthropic 上游根本没有这个字段。
    故多轮关 thinking 的兜底只需覆盖 Anthropic 系模型，非 Anthropic 多轮 blade 可保
    留 thinking。

    **保守语义**：仅在能明确判定非 Anthropic 时返回 True。判不准（model 为 None／空／
    blade server 默认模型未知路由）一律返回 False → 兜底关 thinking。放炸的代价（整个
    attempt 400 失败）远大于误关一次 thinking，故不确定时偏向关。

    blade 的 model 取值形如 provider/name（`qwen/qwen3.7-max`、`z-ai/glm-5.2`、
    `anthropic/claude-sonnet-5`），也有无前缀的裸名（`kimi-for-coding`、`k3`）——裸名
    provider 未知，按保守语义视作"判不准"。
    """
    if not model:
        return False
    normalized = model.strip().lower()
    if "/" not in normalized:
        # 无 provider 前缀的裸名：provider 未知，保守视作判不准。
        return False
    prefix = normalized.split("/", 1)[0]
    # 明确带 anthropic 前缀 → 是 Anthropic 系（不放行）；其余带已知前缀的都不是。
    return prefix != "anthropic"


_DEFAULT_MODELS = {
    "claude-code": "opus",
    "codex": "gpt-5.5",
    # kimi/opencode/mimo 没有"内建默认模型"可用——三者都要求模型显式注册到
    # 各自的 provider 配置里（kimi 甚至会因未注册直接 config.invalid 退出）。
    # 故默认值指向 octagon.yaml 里为它们各自声明的 OpenRouter provider 前缀。
    "kimi-code": "or-kimi/anthropic/claude-sonnet-5",
    "opencode": "or-opencode/anthropic/claude-sonnet-5",
    "mimo-code": "or-mimo/anthropic/claude-sonnet-5",
    # dsh 同理：模型必须落在为它声明的 provider 前缀下，adapter 才能拿到
    # base_url/key 并写进 pi-ai route。
    "dsh": "or-dsh/deepseek/deepseek-chat",
}


def _build_blade_config(
    settings: Settings,
    *,
    model: str | None = None,
    blade_enable_thinking: bool | None = None,
) -> BladeAdapterConfig:
    """settings.blade → BladeAdapterConfig。CLI 与 SDK 两条通道共用。"""
    blade = settings.blade
    # 模型名的 provider 前缀剥离只发生在 API 边界（api.py
    # _normalize_model_for_agent），这里拿到的已是 BA 原生 ID。不能再剥一次：
    # 若剥离后首段恰好与某个 provider 重名（如配了名为 upstream 的 provider），
    # 二次剥离会把已通过 catalog 校验的模型名改成未经校验的错误 ID。
    return BladeAdapterConfig(
        base_url=blade.base_url,
        skills_path=Path(blade.skills_path),
        keep_blade_session=blade.keep_blade_session,
        api_key=blade.api_key.get_secret_value() if blade.api_key else None,
        model=model,
        enable_thinking=blade_enable_thinking,
        sandbox_env_base_url=blade.sandbox_env_base_url,
        request_timeout_seconds=blade.request_timeout_seconds,
        inactivity_timeout_seconds=blade.inactivity_timeout_seconds,
        reconnect_timeout_seconds=blade.reconnect_timeout_seconds,
        progress_poll_interval_seconds=blade.progress_poll_interval_seconds,
        answer_unexpected_interaction=blade.answer_unexpected_interaction,
    )


def build_blade_sdk_adapter(
    settings: Settings, model: str | None = None
) -> BladeServiceAdapter:
    """恢复路径专用：无视 transport 配置，始终返回 SDK adapter。

    断点恢复依赖 `recover_existing` / `recover_iterative_existing` 与
    `run(resume_session_id=...)`——这些是 Socket.IO 通道独有的能力，
    blade-cli 没有对应语义（CLI 每次 `chat run` 都新建会话）。
    因此即便新 attempt 默认走 CLI，恢复既有 blade session 仍走 SDK。
    """
    return BladeServiceAdapter(_build_blade_config(settings, model=model))


def build_adapter(
    agent_name: str,
    settings: Settings,
    model: str | None = None,
    compare_mode: str = "multi-agent",
    blade_enable_thinking: bool | None = None,
) -> Any:
    # 统一策略：blade-agent / claude-code 都在本机跑（blade server / claude CLI
    # 在同一台机器），不区分 compare_mode，一律走本机 settings.blade /
    # 本机 ClaudeCodeAdapter。SshClaudeCodeAdapter 保留但不再由 dispatch
    # 自动选择，仅供接入远端机器时手动启用。
    if agent_name == "fake":
        # 无凭据的确定性适配器，仅供 CI / 冒烟；不参与真实对比评测。
        from .adapters.fake_agent import FakeAgentAdapter

        return FakeAgentAdapter()

    # 本机 CLI agent 的进程启动层。沙盒（docker）接入后按 settings.sandbox
    # 在这里换成 DockerLauncher；adapter 自身不感知执行场合。
    launcher = _build_launcher(settings, agent_name)

    if agent_name == "blade-agent":
        config = _build_blade_config(
            settings, model=model, blade_enable_thinking=blade_enable_thinking
        )
        # 默认走 blade-cli 子进程；settings.blade.transport="sdk" 切回老的
        # Socket.IO adapter。两条路打同一个 blade server，但采集能力不同
        # （CLI 无 token usage / 无实时事件流），见 adapters/blade_cli.py。
        if settings.blade.transport == "cli":
            from .adapters.blade_cli import BladeCliAdapter

            return BladeCliAdapter(config, cli_path=settings.blade.cli_path)
        return BladeServiceAdapter(config)

    if agent_name == "claude-code":
        from .adapters.claude_code import ClaudeCodeAdapter

        return ClaudeCodeAdapter(
            launcher=launcher,
            octagon_project_path=Path(".").resolve(),
            model=model or _DEFAULT_MODELS["claude-code"],
            providers=settings.model_providers,
        )

    if agent_name == "codex":
        from .adapters.codex import CodexAdapter

        return CodexAdapter(
            launcher=launcher,
            octagon_project_path=Path(".").resolve(),
            model=model or _DEFAULT_MODELS["codex"],
            providers=settings.model_providers,
        )

    if agent_name == "kimi-code":
        from .adapters.kimi_code import KimiCodeAdapter

        return KimiCodeAdapter(
            launcher=launcher,
            octagon_project_path=Path(".").resolve(),
            model=model or _DEFAULT_MODELS["kimi-code"],
            providers=settings.model_providers,
        )

    if agent_name == "dsh":
        from .adapters.dsh import DshAdapter

        return DshAdapter(
            launcher=launcher,
            octagon_project_path=Path(".").resolve(),
            model=model or _DEFAULT_MODELS["dsh"],
            providers=settings.model_providers,
        )

    # opencode 与其 fork（mimo）CLI 契约同构，共用一个 adapter，仅 profile 不同。
    if agent_name in ("opencode", "mimo-code"):
        from .adapters.opencode_family import OpencodeFamilyAdapter

        return OpencodeFamilyAdapter(
            agent_name=agent_name,
            launcher=launcher,
            octagon_project_path=Path(".").resolve(),
            model=model or _DEFAULT_MODELS[agent_name],
            providers=settings.model_providers,
        )

    return None


def _build_launcher(settings: Settings, agent_name: str) -> AgentLauncher:
    """本机 CLI agent 的启动层：沙盒关闭 → 宿主机；开启 → docker，且必须可用。

    沙盒开启但不可用时抛 SandboxUnavailable，由 dispatch 落 sandbox_unavailable
    终态——绝不静默回落宿主机执行（spec D-07）。
    """
    if not settings.sandbox.enabled:
        return HostLauncher()
    state = runtime_state.get()
    status = state.sandbox_status
    if status is None:
        status = check_sandbox(settings)
        state.sandbox_status = status
    image = require_sandbox_agent(status, agent_name)
    from .process.docker_launcher import DockerLauncher

    return DockerLauncher(settings=settings, image=image)


class _BoundAdapter:
    """把 adapter 绑定到一次具体执行，并在此实施有界重试。

    重试放在这里而不是各 adapter 内部：四个 CLI adapter 会各写一遍循环，
    而且只有这一层同时看得到 execution deadline——「deadline 内还有余量」
    是 ``should_retry`` 的三个闸之一，缺了它重试就会突破用户的耐心预算。

    位置也刻意选在 ``capture.prepare()`` 与 ``_open_attempt_cost_key()``
    **之后**：一个 attempt 自始至终共用一把临时 key 和一个 wire spool，
    重试因此不会另开一把 key（那会让 attempt 的实扣被拆成两笔而漏计），
    也不会把同一次 attempt 的观测拆到两份 manifest 里。
    """

    def __init__(
        self,
        *,
        adapter,
        task: AdapterRunInput,
        env,
        data_path: Path,
        max_attempts: int = 1,
    ) -> None:
        self.attempt_id = task.attempt_id
        self._adapter = adapter
        self._task = task
        self._env = env
        self._data_path = data_path
        self._max_attempts = max(1, int(max_attempts))

    def _remaining_seconds(self, started: float) -> float | None:
        """本次执行的 deadline 余量；未设超时返回 None（不设闸）。"""
        timeout = self._task.timeout_seconds
        if not timeout or timeout <= 0:
            return None
        return max(0.0, float(timeout) - (time.monotonic() - started))

    async def run(self):
        from .adapters.error_taxonomy import (
            classify,
            retry_delay_seconds,
            should_retry,
        )

        started = time.monotonic()
        attempt = 1
        while True:
            result = await self._adapter.run(self._task, self._env, self._data_path)
            # 只有失败结果才谈重试；成功/超时/被停止都直接返回。
            # 超时**不重试**：deadline 是用户的耐心预算，不是可以再来一次的配额。
            if result.status != "cli_error" or self._max_attempts <= 1:
                return result

            classification = classify(result.error_message, phase="agent_run")
            if not should_retry(
                classification,
                attempt=attempt,
                max_attempts=self._max_attempts,
                remaining_seconds=self._remaining_seconds(started),
            ):
                return result

            delay = retry_delay_seconds(classification, attempt)
            logger.warning(
                "attempt=%s 第 %d 次执行失败（%s，可重试），%.1fs 后重试",
                self.attempt_id, attempt, classification.code, delay,
            )
            await asyncio.sleep(delay)
            attempt += 1


def _build_blade_adapter_env(env: Any) -> AdapterEnv:
    """按测试场景选择与 BA 前端一致的入口。

    - coding：软件工厂 project bootstrap（app-dev/default）；
    - 显式 blade_native：benchmark 声明的原生 skill / solution；
    - 其它（办公、文件处理、通用问答等）：普通聊天（general_chat）。
    """
    blade_skill_dir = Path(env.env_dir) / "blade_skill"
    has_blade_skill_dir = blade_skill_dir.is_dir()

    if env.meta.get("type") == "coding":
        return AdapterEnv(
            name=env.name,
            skill_id="",
            blade_skill_dir=blade_skill_dir if has_blade_skill_dir else None,
            solution_id=_BA_CODING_SOLUTION_ID,
            biz_role_id=_BA_CODING_BIZ_ROLE_ID,
            surface="software_factory",
        )

    blade_native = (env.meta.get("entrypoints") or {}).get("blade_native") or {}
    if blade_native:
        return AdapterEnv(
            name=env.name,
            skill_id=env.skill_id if has_blade_skill_dir else "",
            blade_skill_dir=blade_skill_dir if has_blade_skill_dir else None,
            primary_skill_id=blade_native.get("primary_skill_id"),
            solution_id=blade_native.get("solution_id"),
            biz_role_id=blade_native.get("biz_role_id"),
            surface="native",
        )

    return AdapterEnv(
        name=env.name,
        skill_id="",
        blade_skill_dir=blade_skill_dir if has_blade_skill_dir else None,
        solution_id=_BA_GENERAL_CHAT_SOLUTION_ID,
        surface="chat",
    )


async def dispatch(
    *,
    settings: Settings,
    attempt_id: str,
    agent_name: str,
    task_id: str,
    task_prompt: str,
    task_context: dict[str, Any],
    timeout_seconds: int | None,
    env_name: str,
    env_token: str,
    model: str | None = None,
    compare_mode: str = "multi-agent",
    blade_enable_thinking: bool | None = None,
    capture_policy: str | None = None,
) -> None:
    state = runtime_state.get()
    # Execution input is immutable for every newly-created attempt. Historical
    # attempts without a snapshot resolve through the explicit legacy fallback;
    # relational RunGroup attempts fail closed when their snapshot is missing.
    from .input_snapshots import InputSnapshotError, resolve_attempt_input

    try:
        frozen_input = resolve_attempt_input(
            data_path=state.data_path,
            db_path=state.db_path,
            attempt_id=attempt_id,
        )
    except InputSnapshotError as exc:
        from .runner import _finalize_no_score

        _finalize_no_score(
            db_path=state.db_path,
            attempt_id=attempt_id,
            status="input_snapshot_missing",
            error_code="input_snapshot_missing",
            error_message=str(exc),
            pass_threshold=60,
        )
        _refresh_run_status(state.db_path, attempt_id)
        return
    task_prompt = frozen_input.prompt
    task_context = frozen_input.context
    if frozen_input.timeout_seconds is not None:
        timeout_seconds = frozen_input.timeout_seconds
    _mark_running(state.db_path, attempt_id, timeout_seconds)
    _start_attempt_heartbeat(state.db_path, attempt_id, timeout_seconds)
    env = state.envs.get(env_name)
    if env is None:
        from .runner import _finalize_no_score
        logger.error("dispatch: env %s not found, skipping", env_name)
        _finalize_no_score(
            db_path=state.db_path,
            attempt_id=attempt_id,
            status="blade_service_unavailable",
            error_code="env_not_loaded",
            error_message=f"env not loaded: {env_name}",
            pass_threshold=60,
        )
        _refresh_run_status(state.db_path, attempt_id)
        return

    from .iteration.policy import parse_iterative_review_policy

    iteration_policy = parse_iterative_review_policy(env.meta)

    # 兜底：多轮 conversation + blade + Anthropic 系模型 + thinking 会撞 Anthropic
    # thinking-block signature 400（历史回传的 thinking block signature 失效）。此坑
    # 是 Anthropic extended thinking 独有——signature 是 Anthropic 特有字段，非
    # Anthropic 上游没有可带错的字段，故非 Anthropic 多轮 blade 保留 thinking。
    # 强制关闭无论前端/调用方传什么——前端置灰只防 UI 误操作，这里保证绕过 UI 直接
    # 打 API 也不会 400。判不准模型（None/裸名）保守视作可能 Anthropic → 仍关。
    from .conversation.plan import CONVERSATION_CONTEXT_KEY

    _conv = task_context.get(CONVERSATION_CONTEXT_KEY)
    _is_multi_turn = isinstance(_conv, list) and len(_conv) > 1
    _maybe_anthropic = not _blade_model_is_definitely_not_anthropic(model)
    if (
        agent_name == "blade-agent"
        and _is_multi_turn
        and blade_enable_thinking
        and _maybe_anthropic
    ):
        logger.warning(
            "dispatch: 多轮 + 可能 Anthropic 系模型（model=%s）强制关闭 blade thinking"
            "（避免 thinking-block signature 400） attempt=%s task=%s",
            model, attempt_id, task_id,
        )
        blade_enable_thinking = False

    try:
        adapter = build_adapter(
            agent_name,
            settings,
            model=model,
            compare_mode=compare_mode,
            blade_enable_thinking=blade_enable_thinking,
        )
    except SandboxUnavailable as exc:
        from .runner import _finalize_no_score
        logger.error(
            "dispatch: 沙盒不可用，attempt=%s agent=%s code=%s: %s",
            attempt_id, agent_name, exc.error_code, exc,
        )
        _finalize_no_score(
            db_path=state.db_path,
            attempt_id=attempt_id,
            status=ATTEMPT_STATUS_SANDBOX_UNAVAILABLE,
            error_code=exc.error_code,
            error_message=str(exc),
            pass_threshold=int((getattr(env, "meta", {}) or {}).get("pass_threshold", 60)),
        )
        _refresh_run_status(state.db_path, attempt_id)
        return
    if adapter is None:
        from .runner import _finalize_no_score
        logger.warning("dispatch: no adapter for agent %s, marking cli_not_found", agent_name)
        _finalize_no_score(
            db_path=state.db_path,
            attempt_id=attempt_id,
            status="cli_not_found",
            error_code="adapter_not_implemented",
            error_message=f"adapter for {agent_name} not yet implemented",
            pass_threshold=60,
        )
        _refresh_run_status(state.db_path, attempt_id)
        return
    # 本机 agent 跑在 docker 沙盒里时：MCP 入口翻译成容器内命令、回连地址换成
    # 容器可达地址、wire 的 MCP stdio tap 不适用（它用宿主机 python 包装 server）。
    launcher = getattr(adapter, "launcher", None)
    in_sandbox = getattr(launcher, "locus", "host") == "docker-sandbox"
    if iteration_policy is not None and not adapter.capabilities.iterative_session:
        from .runner import _finalize_no_score

        _finalize_no_score(
            db_path=state.db_path,
            attempt_id=attempt_id,
            status="cli_not_found",
            error_code="iterative_session_unsupported",
            error_message=f"adapter {agent_name} 不支持动态多轮 session",
            pass_threshold=int(env.meta.get("pass_threshold", 60)),
        )
        _refresh_run_status(state.db_path, attempt_id)
        return

    adapter_env = _build_blade_adapter_env(env)

    # 多轮 conversation：task_context 的
    # `_conversation` 数组在 agent 启动前解析并做 capability gate——
    # 含 answer_interaction 轮而 adapter 无运行中应答通道时 fail fast，
    # 不得静默忽略该轮或带着无法应答的计划开跑。API 入口已做同样校验，
    # 这里是纵深防御（file task / 恢复路径不经过 API 校验）。
    from .conversation.plan import (
        ConversationPlanError,
        conversation_turns_from_context,
    )
    try:
        conversation_turns = conversation_turns_from_context(
            task_id=task_id, task_context=task_context,
        )
    except ConversationPlanError as exc:
        from .runner import _finalize_no_score
        logger.error("dispatch: invalid conversation task=%s: %s", task_id, exc)
        _finalize_no_score(
            db_path=state.db_path,
            attempt_id=attempt_id,
            status="cli_error",
            error_code="invalid_conversation",
            error_message=str(exc),
            pass_threshold=int(env.meta.get("pass_threshold", 60)),
        )
        _refresh_run_status(state.db_path, attempt_id)
        return
    if any(t.action == "answer_interaction" for t in conversation_turns) and not getattr(
        adapter.capabilities, "interaction_answer", False
    ):
        from .runner import _finalize_no_score
        logger.error(
            "dispatch: agent %s 不支持运行中交互应答，拒绝含 answer_interaction "
            "的 conversation attempt=%s", agent_name, attempt_id,
        )
        _finalize_no_score(
            db_path=state.db_path,
            attempt_id=attempt_id,
            status="cli_error",
            error_code="interaction_answer_unsupported",
            error_message=(
                f"agent {agent_name} 无运行中交互应答通道，无法执行含 "
                "answer_interaction 轮的 conversation"
            ),
            pass_threshold=int(env.meta.get("pass_threshold", 60)),
        )
        _refresh_run_status(state.db_path, attempt_id)
        return

    # wire capture：prepare 固定先于 adapter.run。早期阶段无真实
    # source，prepare 是 noop 并返回零注入；fail-open 失败时 injection 保持零值，
    # adapter 拿到的 env/base_url 不被污染（无需恢复——注入从未发生）。
    # capability 取自已构建的 adapter 实例（能反映运行时配置，如 blade 的
    # session_metadata_capability），保证 gap 在 agent 启动前产生；
    # protected_env_keys 收全部 provider 的 credential env 名，防止 source 用
    # process_env 覆盖真实 credential（如任意命名的 api_key_env）。
    protected_env_keys = frozenset(
        p.api_key_env
        for p in (settings.model_providers or {}).values()
        if getattr(p, "api_key_env", None)
    )
    try:
        mcp_servers = _mcp_server_specs(env)
        if in_sandbox and mcp_servers:
            from .process.docker_launcher import translate_mcp_specs

            attempt_dir = state.data_path / "attempts" / attempt_id
            mcp_servers = translate_mcp_specs(
                mcp_servers, attempt_dir=attempt_dir,
                workspace=attempt_dir / "skill_workspace",
                env_dir=Path(env.env_dir),
            )
    except SandboxUnavailable as exc:
        from .runner import _finalize_no_score
        logger.error("dispatch: 沙盒 MCP 入口不可用 env=%s: %s", env_name, exc)
        _finalize_no_score(
            db_path=state.db_path,
            attempt_id=attempt_id,
            status="cli_error",
            error_code=exc.error_code,
            error_message=str(exc),
            pass_threshold=int(env.meta.get("pass_threshold", 60)),
        )
        _refresh_run_status(state.db_path, attempt_id)
        return
    except ValueError as exc:
        from .runner import _finalize_no_score
        logger.error("dispatch: invalid MCP entrypoint env=%s: %s", env_name, exc)
        _finalize_no_score(
            db_path=state.db_path,
            attempt_id=attempt_id,
            status="cli_error",
            error_code="invalid_scene_mcp_entrypoint",
            error_message=str(exc),
            pass_threshold=int(env.meta.get("pass_threshold", 60)),
        )
        _refresh_run_status(state.db_path, attempt_id)
        return
    # CC/Codex 命名第三方 provider → 挂 HttpProxySource（反代 base URL 注入）。
    wire_sources = _build_wire_sources(
        agent_name=agent_name, model=model, settings=settings, attempt_id=attempt_id,
        env_name=env_name, data_path=state.data_path,
        mcp_server_names=() if in_sandbox else tuple(server.name for server in mcp_servers),
    )
    # capture_policy：run/task 请求的 policy 与 server maximum 求最严格交集。
    # 未指定时默认 metadata（只记 size/timing，不落 body）。
    effective_policy = resolve_effective_policy(
        server_max=getattr(settings.octagon, "wire_capture_max_policy", None),
        run_requested=capture_policy,
    )
    capture = WireCaptureSession(
        attempt_id=attempt_id,
        data_path=state.data_path,
        agent_name=agent_name,
        sources=wire_sources,
        adapter_capabilities=capture_capabilities_for(agent_name, adapter),
        policy=effective_policy,
        protected_env_keys=protected_env_keys,
    )
    try:
        injection = await capture.prepare(phase="agent_run")
    except CapturePreparationError as exc:
        # strict 且改写型 source 无法 ready：agent 未启动。落独立的 capture/
        # infrastructure 终态（capture 状态不得复用/伪装 agent status，
        # 尤其不能借用 blade_service_unavailable——CC/Codex 的代理启动失败与
        # blade 无关）。
        from .runner import _finalize_no_score
        logger.error("dispatch: capture prepare 失败 attempt=%s: %s", attempt_id, exc)
        _finalize_no_score(
            db_path=state.db_path,
            attempt_id=attempt_id,
            status="capture_infrastructure_failed",
            error_code="capture_preparation_failed",
            error_message=str(exc),
            pass_threshold=60,
        )
        _refresh_run_status(state.db_path, attempt_id)
        return

    # 成本核算：每个 attempt 一把独立 key，**必须在 agent 进程启动前**
    # 建好并落起点——它是重启后重新结算的唯一锚点。失败只降级，不阻断执行。
    await _open_attempt_cost_key(state, attempt_id, agent_name)

    # prepare 之后到 run_attempt 返回为止是统一的 abort 边界：这段里任何
    # 异常（物料拷贝、approval seed、bound/scorer 构造、run_attempt 自身）
    # 都不能留下已启动的 source/sidecar（abort 路径）。
    try:
        scorer = _resolve_scorer(env)
        if scorer is None:
            logger.error("dispatch: env %s missing scorer", env_name)

            def _missing_scorer(**_kwargs):
                raise RuntimeError(f"env {env_name} missing scorer")

            scorer = _missing_scorer

        iteration_handler = None
        if iteration_policy is not None:
            submission_scorer = getattr(
                env.scorer_module, "evaluate_submission", None
            )
            if not callable(submission_scorer):
                raise RuntimeError(
                    f"迭代环境 {env_name} 缺少 evaluate_submission()"
                )
            from .iteration.controller import IterativeAttemptController

            iteration_handler = IterativeAttemptController(
                attempt_id=attempt_id,
                attempt_dir=state.data_path / "attempts" / attempt_id,
                task={
                    "id": task_id,
                    "env_name": env_name,
                    "prompt": task_prompt,
                    "context": task_context,
                    "timeout_seconds": timeout_seconds,
                },
                env=env,
                policy=iteration_policy,
                scorer=submission_scorer,
                judge_deadline_seconds=settings.octagon.scoring_deadline_seconds,
                execution_db_path=state.db_path,
            )

        task = AdapterRunInput(
            attempt_id=attempt_id,
            task_id=task_id,
            task_prompt=task_prompt,
            task_context=task_context,
            timeout_seconds=timeout_seconds,
            env_name=env_name,
            env_skill_id=env.skill_id,
            env_token=env_token,
            env_base_url=(
                settings.sandbox.resolve_env_base_url(settings.octagon.public_base_url)
                if in_sandbox else settings.octagon.public_base_url
            ),
            notify_model_of_timeout=bool(
                task_context.get("_octagon_notify_model_of_timeout", True)
            ),
            run_id=_run_id_of(state.db_path, attempt_id),
            mcp_servers=mcp_servers,
            wire_injection=injection,
            conversation_turns=conversation_turns,
            iteration_turn_handler=iteration_handler,
        )

        # 把 env scripts 拷贝到 workspace（防止 agent 修改源文件）
        _copy_env_scripts(state.data_path, attempt_id, env)
        _copy_agent_materials(state.data_path, attempt_id, env, task_context)
        # 把 uploaded_files 落到 workspace；物料缺失直接判失败，
        # 不能让 agent 拿着"你有一段视频"的 prompt 在空 workspace 里瞎找
        _copy_uploads(state.data_path, attempt_id, task_context)
        # HITL 场景：把 task 预置的批复策略落到 attempt 目录，供
        # request_human_approval 工具读取。决策由 task 固定，不由 agent 代拟。
        _seed_approval_policy(state.data_path, attempt_id, task_context)
        # 把 task_context 里的路径简化为文件名，agent 通过 skill 工具在 workspace 下访问
        _normalize_upload_paths(task)

        bound = _BoundAdapter(
            adapter=adapter,
            task=task,
            env=adapter_env,
            data_path=state.data_path,
            max_attempts=int(settings.octagon.cli_max_attempts),
        )

        result = await run_attempt(
            adapter=bound,
            scorer=scorer,
            observer=capture,
            defer_scoring=True,
            scoring_capacity=int(settings.octagon.max_active_scoring_jobs),
        )
    except BaseException as exc:
        # abort 幂等，且对 run_attempt 内已正常 attempt_end 的 session 是 no-op。
        import contextlib as _ctx
        with _ctx.suppress(Exception):
            await capture.abort_before_or_during_run()
        if not isinstance(exc, Exception):
            raise
        from .runner import _finalize_no_score
        if isinstance(exc, FileNotFoundError):
            logger.error("dispatch: material missing attempt=%s: %s", attempt_id, exc)
            result = _finalize_no_score(
                db_path=state.db_path,
                attempt_id=attempt_id,
                status="session_create_failed",
                error_code="missing_uploaded_file",
                error_message=str(exc),
                pass_threshold=60,
            )
        else:
            logger.exception("dispatch crashed attempt=%s", attempt_id)
            result = _finalize_no_score(
                db_path=state.db_path,
                attempt_id=attempt_id,
                status="blade_service_unavailable",
                error_code="dispatch_crashed",
                error_message=str(exc),
                pass_threshold=60,
            )
    _refresh_run_status(state.db_path, attempt_id)
    logger.info(
        "dispatch attempt=%s agent=%s status=%s score=%s",
        result.attempt_id, agent_name, result.status, result.score_total,
    )


def _start_attempt_heartbeat(
    db_path: Path, attempt_id: str, timeout_seconds: int | None = None
) -> None:
    """启动轻量控制面 heartbeat，不侵入 adapter 主执行循环。"""
    async def _loop() -> None:
        await append_attempt_observation(
            db_path, attempt_id, stage="execution", event_type="attempt.started",
            payload={"message": "agent execution started", "timeout_seconds": timeout_seconds},
            heartbeat=True,
        )
        while True:
            await asyncio.sleep(5.0)
            row = await asyncio.to_thread(_attempt_heartbeat_state, db_path, attempt_id)
            if row is None or row["execution_status"] not in {"queued", "running"}:
                return
            await append_attempt_observation(
                db_path, attempt_id, stage="execution", event_type="attempt.heartbeat",
                payload={
                    "message": "agent execution active",
                    "execution_status": row["execution_status"],
                    "deadline_at": row["execution_deadline_at"],
                },
                heartbeat=True,
            )

    try:
        asyncio.create_task(_loop(), name=f"attempt-heartbeat:{attempt_id}")
    except RuntimeError:
        logger.debug("attempt heartbeat skipped without running event loop: %s", attempt_id)


def _attempt_heartbeat_state(db_path: Path, attempt_id: str) -> dict[str, Any] | None:
    with _open_sync(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT execution_status,execution_deadline_at FROM attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def _mark_running(db_path: Path, attempt_id: str, timeout_seconds: int | None = None) -> None:
    """标记 attempt 开始执行，并**持久化绝对 deadline**。

    deadline 必须是绝对时钟而非 ``time.monotonic()``：monotonic 只在进程内有效，
    重启后无从复原，常驻 sweeper 也无法判断。此前这三个 ``*_deadline_at`` 列
    是死 schema——从不写入、从不比较，卡死的 attempt 因此只能等下次重启才被
    发现（周末现场有 attempt 保持 running 约两天）。
    """
    now = _now_iso()
    deadline = None
    if timeout_seconds and timeout_seconds > 0:
        deadline = _iso_after(now, timeout_seconds)
    with _open_sync(db_path) as conn:
        conn.execute(
            "UPDATE attempts SET status='running', started_at=?,"
            "execution_status='running',execution_started_at=?,"
            "execution_deadline_at=COALESCE(?,execution_deadline_at),"
            "execution_agent_deadline_at=COALESCE(?,execution_agent_deadline_at,"
            "execution_deadline_at),execution_evaluator_started_at=NULL"
            " WHERE id=? AND status='queued'",
            (now, now, deadline, deadline, attempt_id),
        )
        conn.execute(
            "UPDATE runs SET status='running', started_at=COALESCE(started_at, ?)"
            " WHERE id=(SELECT run_id FROM attempts WHERE id=?) AND status='queued'",
            (now, attempt_id),
        )
        conn.commit()


def _schedule_cost_checkpoint(
    db_path: Path, run_id: str, execution_converged: bool, run_terminal: bool
) -> None:
    """把成本 checkpoint 丢到事件循环后台执行。

    `_refresh_run_status` 是同步函数且在热路径上，而 checkpoint 要轮询上游
    等 usage 稳定（默认最长 300s）——**绝不能在这里同步等**。没有运行中的
    事件循环时（同步测试）直接跳过：后台 settler 会在下一轮扫描时补上，
    结算不依赖这次触发。
    """
    if not execution_converged:
        return
    try:
        settings = runtime_state.get().settings
        if not getattr(settings.cost, "legacy_run_key_enabled", False):
            return  # 旧 run 级审计默认关闭
    except (RuntimeError, AttributeError):
        return
    if settings is None or not getattr(settings, "cost", None) or not settings.cost.enabled:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def _checkpoint() -> None:
        try:
            from .cost.audit import mark_execution_done, mark_scoring_done

            if run_terminal:
                await mark_scoring_done(db_path, run_id, settings=settings)
            else:
                await mark_execution_done(db_path, run_id, settings=settings)
        except Exception:
            logger.exception("cost checkpoint failed run=%s", run_id)

    loop.create_task(_checkpoint())


def _run_id_of(db_path: Path, attempt_id: str) -> str | None:
    """attempt → run_id。成本核算据此查 run 专属上游 key。

    从库里反查而不是加 dispatch 参数：dispatch 的调用点分散在 run_service、
    recovery、experiments 多处，加参数会波及一片；这里本来就在做 DB 读。
    """
    with _open_sync(db_path) as conn:
        row = conn.execute(
            "SELECT run_id FROM attempts WHERE id=?", (attempt_id,)
        ).fetchone()
    return row[0] if row else None


def _refresh_run_status(db_path: Path, attempt_id: str) -> None:
    ok = {"completed", "gave_up"}
    with _open_sync(db_path) as conn:
        row = conn.execute(
            "SELECT run_id FROM attempts WHERE id=?", (attempt_id,)
        ).fetchone()
        if not row:
            return
        run_id = row[0]
        rows = conn.execute(
            "SELECT status,execution_status,scoring_status FROM attempts WHERE run_id=?",
            (run_id,),
        ).fetchall()
        statuses = [r[0] for r in rows]
        if any(r[1] in {"queued", "running"} for r in rows):
            conn.execute(
                "UPDATE runs SET status='running',ended_at=NULL WHERE id=?", (run_id,)
            )
            # 仍有 attempt 在执行 → execution 未收敛，还不能采成本 checkpoint。
            execution_converged = False
            run_terminal = False
        elif any(r[2] in {"queued", "running"} for r in rows):
            conn.execute(
                "UPDATE runs SET status='scoring',ended_at=NULL WHERE id=?", (run_id,)
            )
            execution_converged = True
            run_terminal = False
        elif statuses:
            # 用户主动停止是独立终态，不能塌缩成 failed：把一次
            # 操作决定报成设施故障，会让 run 在 attempt=cancelled 时显示
            # failed，同一次 Stop 在不同层级自相矛盾。只有在**没有任何真实
            # 执行失败**时才投影 cancelled——真实失败必须保留。
            if all(s in ok for s in statuses):
                run_status = "completed"
            elif any(s == "cancelled" for s in statuses) and all(
                s in ok or s == "cancelled" for s in statuses
            ):
                run_status = "cancelled"
            else:
                run_status = "failed"
            conn.execute(
                "UPDATE runs SET status=?, ended_at=? WHERE id=?",
                (run_status, _now_iso(), run_id),
            )
            execution_converged = True
            run_terminal = True
        else:
            execution_converged = False
            run_terminal = False
        conn.commit()
    # 成本 checkpoint：execution 收敛 → 采 usage_after_execution；
    # run 进入终态 → 采 usage_final 并结算。幂等由审计记录的条件迁移保证
    # （多个 attempt 先后终止会重复触发，只有第一个推进得动）。
    _schedule_cost_checkpoint(db_path, run_id, execution_converged, run_terminal)
    # 逐 key 账：该 attempt 已终止 → 结算它自己那把 key。
    # 与上面的 run 级 checkpoint 并存——两条生命周期切换期间都要推进。
    _schedule_attempt_settle(db_path, attempt_id)
    try:
        from .experiments.recovery import project_cell_for_run

        project_cell_for_run(db_path, run_id)
    except Exception:
        logger.exception("failed to project experiment cell for run=%s", run_id)


def _copy_env_scripts(data_path: Path, attempt_id: str, env: Any) -> None:
    """把 env 目录下的 scripts/ 等可执行文件拷贝到 workspace，agent 只能改副本。"""
    workspace = data_path / "attempts" / attempt_id / "skill_workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    env_dir = Path(env.env_dir)
    for src_name in ("scripts", "SKILL.md", "tools.py", "plan.example.json"):
        src = env_dir / src_name
        dest = workspace / src_name
        if dest.exists():
            continue
        if src.is_dir():
            shutil.copytree(src, dest)
        elif src.is_file():
            shutil.copy2(src, dest)
    logger.info("_copy_env_scripts: %s -> %s", env_dir.name, workspace)


def _mcp_server_specs(env: Any) -> tuple[McpServerSpec, ...]:
    """把场景声明转换为 adapter 输入；不根据目录内容推断 MCP 能力。"""
    entrypoints = (getattr(env, "meta", {}) or {}).get("entrypoints") or {}
    raw = entrypoints.get("mcp")
    if not isinstance(raw, dict) or not raw.get("enabled", False):
        return ()
    if raw.get("transport", "stdio") != "stdio":
        raise ValueError(f"env {env.name} 当前只支持 MCP stdio transport")
    command = raw.get("command")
    if not isinstance(command, list) or not command or not all(
        isinstance(part, str) and part for part in command
    ):
        raise ValueError(f"env {env.name} entrypoints.mcp.command 必须是非空字符串数组")
    name = raw.get("name") or f"octagon-{env.name}"
    if not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9_-]+", name) is None:
        raise ValueError(
            f"env {env.name} entrypoints.mcp.name 只允许字母、数字、下划线和连字符"
        )
    project_root = Path(env.env_dir).resolve().parent.parent
    return (McpServerSpec(
        name=name,
        command=command[0],
        args=tuple(command[1:]),
        cwd=str(project_root),
    ),)


def _seed_approval_policy(
    data_path: Path, attempt_id: str, task_context: dict[str, Any]
) -> None:
    """把 task_context.approval_policy 写到 attempt 目录的 approval_policy.json。

    request_human_approval 工具从 ctx.trace.path.parent 读它，据此固定返回批复。
    无 approval_policy 的普通任务跳过（工具缺文件时默认 approve）。
    """
    policy = task_context.get("approval_policy")
    if not isinstance(policy, dict) or not policy:
        return
    attempt_dir = data_path / "attempts" / attempt_id
    attempt_dir.mkdir(parents=True, exist_ok=True)
    (attempt_dir / "approval_policy.json").write_text(
        json.dumps(policy, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("_seed_approval_policy: %s -> attempt %s", policy, attempt_id)


def _copy_agent_materials(
    data_path: Path,
    attempt_id: str,
    env: Any,
    task_context: dict[str, Any],
) -> None:
    """把 meta.yaml 声明的 agent-visible materials 复制到 attempt workspace。

    `materials.agent` 是通用目录/文件物料机制：

        materials:
          agent:
            - path: materials/public
              target: .

    `path` 相对 env 目录解析；`target` 相对 agent workspace。目录 target 为 `.`
    时复制目录内容到 workspace 根。复制出的相对文件路径写入 task_context 的内部
    `_agent_material_files`，供 Blade adapter 上传，prompt_context 会过滤 `_` 字段。
    """
    entries = _material_entries(env, "agent")
    if not entries:
        return

    workspace = (data_path / "attempts" / attempt_id / "skill_workspace").resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    env_dir = Path(env.env_dir)
    copied: list[str] = []
    for entry in entries:
        source_rel = Path(str(entry["path"]))
        if source_rel.is_absolute() or ".." in source_rel.parts:
            raise ValueError(f"material path 必须是 env 相对路径: {entry['path']!r}")
        src = (env_dir / source_rel).resolve()
        target = str(entry.get("target") or Path(str(entry["path"])).name)
        dest = _safe_workspace_target(workspace, target)
        if not src.exists():
            raise FileNotFoundError(f"agent material 不存在: {src}")
        if src.is_dir():
            copied.extend(_copy_dir_contents(src, dest, workspace))
        elif src.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            copied.append(dest.relative_to(workspace).as_posix())
        else:
            raise FileNotFoundError(f"agent material 不是文件或目录: {src}")

    existing = task_context.get("_agent_material_files")
    if not isinstance(existing, list):
        existing = []
    merged = sorted({str(x) for x in existing} | set(copied))
    task_context["_agent_material_files"] = merged
    logger.info("_copy_agent_materials: %s files -> %s", len(copied), workspace)


def _material_entries(env: Any, audience: str) -> list[dict[str, str]]:
    materials = (getattr(env, "meta", {}) or {}).get("materials") or {}
    raw = materials.get(audience) or []
    if not isinstance(raw, list):
        raise ValueError(f"materials.{audience} 必须是 list")
    entries: list[dict[str, str]] = []
    for item in raw:
        if isinstance(item, str):
            entries.append({"path": item, "target": Path(item).name})
            continue
        if isinstance(item, dict) and item.get("path"):
            entries.append({
                "path": str(item["path"]),
                "target": str(item.get("target") or Path(str(item["path"])).name),
            })
            continue
        raise ValueError(f"materials.{audience} 项必须是 string 或含 path 的 object: {item!r}")
    return entries


def _safe_workspace_target(workspace: Path, target: str) -> Path:
    rel = Path(target)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError(f"material target 必须是 workspace 相对路径: {target!r}")
    dest = (workspace / rel).resolve()
    dest.relative_to(workspace.resolve())
    return dest


def _copy_dir_contents(src: Path, dest: Path, workspace: Path) -> list[str]:
    # dest 由 _safe_workspace_target() resolve 过（绝对路径），workspace 可能是相对路径；
    # 统一 resolve 后再算相对路径，否则 out.relative_to(workspace) 必抛 ValueError。
    workspace = workspace.resolve()
    copied: list[str] = []
    dest.mkdir(parents=True, exist_ok=True)
    for child in sorted(src.rglob("*")):
        if child.is_dir():
            continue
        rel = child.relative_to(src)
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(child, out)
        copied.append(out.relative_to(workspace).as_posix())
    return copied


def _copy_uploads(data_path: Path, attempt_id: str, task_context: dict[str, Any]) -> None:
    """把任务物料落到 attempt workspace。缺失即抛 FileNotFoundError——
    宁可 attempt 判失败，也不让 agent 在空 workspace 里搜文件。"""
    uploaded = task_context.get("uploaded_files")
    if not uploaded or not isinstance(uploaded, list):
        return
    workspace = data_path / "attempts" / attempt_id / "skill_workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    for f in uploaded:
        src = Path(f.get("path", ""))
        if not src.is_absolute():
            src = Path(".").resolve() / src
        if not src.is_file():
            raise FileNotFoundError(
                f"任务物料不存在: {f.get('name') or src.name} ({src})"
            )
        name = str(f.get("name") or src.name)
        if Path(name).name != name or name in {"", ".", ".."}:
            raise FileNotFoundError(f"任务物料名称必须是普通文件名: {name!r}")
        dest = workspace / name
        if dest.exists():
            continue
        shutil.copy2(src, dest)
        logger.info("_copy_uploads: %s -> %s", src, dest)


def _normalize_upload_paths(task: AdapterRunInput) -> None:
    """把 task_context.uploaded_files 里的绝对路径改成文件名。

    文件已通过 _link_uploads symlink 到 skill_workspace/，agent 只需用文件名
    传给 skill 工具，工具在 workspace（cwd）下找到文件即可。
    避免远端 agent 看到本地绝对路径导致困惑。
    """
    uploaded = task.task_context.get("uploaded_files")
    if not uploaded or not isinstance(uploaded, list):
        return
    for f in uploaded:
        if "path" in f:
            f["path"] = str(f.get("name") or Path(f["path"]).name)


def _resolve_scorer(env: Any):
    mod = env.scorer_module
    if mod is None:
        return None
    score = getattr(mod, "score", None)
    if not callable(score):
        return None
    return score


async def _open_attempt_cost_key(state: Any, attempt_id: str, agent_name: str) -> None:
    """为该 attempt 开独占 key。失败只记日志——成本观测不该阻断实验。"""
    settings = getattr(state, "settings", None)
    if settings is None or not getattr(settings, "cost", None):
        return
    if not settings.cost.per_attempt_enabled:
        return
    run_id = _run_id_of(state.db_path, attempt_id)
    if not run_id:
        return
    try:
        from .cost.keys import open_key

        await open_key(
            state.db_path, run_id=run_id, scope="attempt", scope_id=attempt_id,
            settings=settings, agent_name=agent_name,
        )
    except Exception:
        logger.exception("open attempt cost key failed attempt=%s", attempt_id)


async def _settle_attempt_cost_key(state: Any, attempt_id: str) -> None:
    """attempt 终止后结算它的 key。判稳可能耗时，调用方应放后台。"""
    settings = getattr(state, "settings", None)
    if settings is None or not getattr(settings, "cost", None):
        return
    if not settings.cost.enabled:
        return
    try:
        from .cost.keys import settle_key

        await settle_key(
            state.db_path, scope="attempt", scope_id=attempt_id, settings=settings
        )
    except Exception:
        logger.exception("settle attempt cost key failed attempt=%s", attempt_id)


def _schedule_attempt_settle(db_path: Path, attempt_id: str) -> None:
    """把 attempt key 的结算丢到事件循环后台。

    判稳要轮询（默认最长 300s），**绝不能在 _refresh_run_status 这个同步
    热路径上等**。没有运行中事件循环时跳过——后台 settler 会补上。
    """
    try:
        state = runtime_state.get()
        settings = state.settings
    except (RuntimeError, AttributeError):
        return
    if settings is None or not getattr(settings, "cost", None):
        return
    if not settings.cost.enabled:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_settle_attempt_cost_key(state, attempt_id))
