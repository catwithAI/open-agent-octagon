"""KimiCodeAdapter — 通过 kimi CLI 子进程调用 Kimi Code。

实测契约（2026-07-27，49 机器 kimi-code 0.29.1）——与 agent-arena 的 AgentSpec
声明有出入，以下以实机为准：

- print 模式：`kimi -p <prompt> --print --output-format stream-json -m <model>`。
  0.29/0.38 的 `-p` 隐含 print 模式；**沙盒镜像里的 1.50 拆开了**——`-p` 只给
  prompt，`--output-format` 单独要求 print UI。`--print` 两版都接受，统一显式传。
- 事件流是**每行一个 `{"role":..., "content":...}`**，不是 CC/codex 那种带
  `type` 的结构化事件：
      {"role":"assistant","content":"PONG"}
      {"role":"meta","type":"session.resume_hint","session_id":"session_...",...}
- resume 是 `-r <session_id>`（arena spec 写的 `--session` 在本版**不存在**，
  `-S` 是交互式挑选）。session ID 只能从 `session.resume_hint` 读。
- `--auto` 与 `-p` 互斥（CLI 直接报 "Cannot combine --prompt with --auto"）；
  print 模式本身即非交互自动执行，不需要额外权限 flag。
- 模型必须在 `config.toml` 里显式注册（`[providers.*]` + `[models."*"]` 且带
  `max_context_size`），否则报 `config.invalid: Model ... is not configured`。
  arena spec 里的 `--mcp-config-file` 在本版**不存在**——MCP 走
  `$KIMI_CODE_HOME/mcp.json`，dialect 与 CC 的 `mcpServers` 一致。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..conversation.deadline import (
    ERROR_BUDGET_EXHAUSTED_BETWEEN_TURNS,
    AttemptBudgetExhausted,
    AttemptDeadline,
)
from ..conversation.plan import effective_conversation
from ..conversation.summary import summarize_conversation
from ..conversation.turns import (
    render_turn_prompt,
    with_turn_ext,
    write_checkpoint,
)
from ..conversation.writer import CONVERSATION_FILENAME, ConversationTraceWriter
from ..cost.credential import resolve_agent_base_url, resolve_agent_key
from ..model_providers import (
    ModelProviderSection,
    ModelRef,
    parse_model_ref,
    resolve_api_key,
)
from ..process.launcher import (
    AgentLauncher,
    AttemptSandbox,
    AttemptSpec,
    ExecSpec,
    HostLauncher,
)
from .base import (
    AdapterCapabilities,
    AdapterResult,
    AdapterRunInput,
    build_security_meta,
    prompt_context,
    time_budget_notice,
)
from .error_taxonomy import classify as classify_cli_error
from .token_usage import compact_usage, empty_usage

logger = logging.getLogger(__name__)

# config.toml 里注册的 provider id；模型在 argv 里以 `<id>/<model>` 引用。
_PROVIDER_ID = "octagon"

# 未知模型的 max_context_size 兜底。config.toml 要求这个字段必填，而我们不
# 维护第三方模型的 context 表——给一个足够大的值，让 CLI 不因窗口声明过小而
# 提前截断/压缩；真实上限由上游 API 强制。
_DEFAULT_MAX_CONTEXT = 200_000
_DEFAULT_MAX_OUTPUT = 64_000


def _toml_escape(value: str) -> str:
    """TOML 基本字符串转义。模型名/URL/key 都可能含 `\\` 或 `"`。"""
    return value.replace("\\", "\\\\").replace('"', '\\"')


class KimiCodeAdapter:
    # 能力静态声明：见 AdapterCapabilities docstring。
    agent_name = "kimi-code"
    capabilities = AdapterCapabilities(
        execution_locus="host",
        network_required="public_internet",
        # `kimi -p` 是一次性非交互执行，无运行中交互应答通道（与 codex exec 同）。
        interaction_answer=False,
    )

    def __init__(
        self,
        *,
        model: str,
        octagon_project_path: str | Path = ".",
        providers: dict[str, ModelProviderSection] | None = None,
        launcher: AgentLauncher | None = None,
    ) -> None:
        # 进程启动层：缺省宿主机执行；沙盒模式由 build_adapter 注入 DockerLauncher。
        # execution_locus 随 launcher 取值，类常量只是静态声明。
        self.launcher: AgentLauncher = launcher or HostLauncher()
        self.capabilities = dataclasses.replace(
            type(self).capabilities, execution_locus=self.launcher.locus,
        )
        self.model = model
        self.octagon_project_path = str(Path(octagon_project_path).resolve())
        self.providers = providers or {}

    @property
    def wire_capture_capabilities(self) -> dict[str, Any]:
        """wire injection 消费能力声明（lifecycle 在 agent 启动前据此过滤）。

        llm_headers 走 `KIMI_CODE_CUSTOM_HEADERS`（换行分隔的 `Name: value`，
        格式与 CC 的 ANTHROPIC_CUSTOM_HEADERS 一致）。
        """
        return {
            "process_env": True,
            "llm_base_url": True,
            "llm_headers": True,
            "mcp_rewrites": True,
        }

    # ---------- 配置生成 ----------

    def _render_config_toml(
        self, model_ref: ModelRef, base_url: str, api_key: str | None,
        custom_headers: dict[str, str] | None = None,
    ) -> str:
        """生成 attempt 专用 config.toml。

        只注册本次要用的那一个模型——`kimi provider catalog add` 会拉 343 个
        模型进来，那是交互式使用的便利，对评测只是噪音和额外网络依赖。
        """
        model_key = f"{_PROVIDER_ID}/{model_ref.model}"
        lines = [
            f'default_model = "{_toml_escape(model_key)}"',
            "",
            f"[providers.{_PROVIDER_ID}]",
            # openai-compatible 协议；kimi 的 catalog 导入对 OpenRouter 也是猜
            # 成 "openai"，0.38 可用；**沙盒镜像的 1.50 把它改名成 "openai_legacy"**
            # （chat completions 协议），旧值直接被 pydantic 拒。
            'type = "openai_legacy"',
            f'base_url = "{_toml_escape(base_url)}"',
        ]
        if api_key:
            lines.append(f'api_key = "{_toml_escape(api_key)}"')
        if custom_headers:
            # kimi 1.50 不再读 KIMI_CODE_CUSTOM_HEADERS 环境变量，自定义头改由
            # provider 配置的 custom_headers 表承载（透传给 OpenAILegacy 的
            # default_headers）。wire 反代要求的 X-Octagon-Capture-Token 必须
            # 从这里进，否则每次 LLM 调用都被 401 "capture token required"。
            lines += ["", f"[providers.{_PROVIDER_ID}.custom_headers]"]
            for name, value in custom_headers.items():
                lines.append(f'"{_toml_escape(name)}" = "{_toml_escape(value)}"')
        lines += [
            "",
            f'[models."{_toml_escape(model_key)}"]',
            f'provider = "{_PROVIDER_ID}"',
            f'model = "{_toml_escape(model_ref.model)}"',
            f"max_context_size = {_DEFAULT_MAX_CONTEXT}",
            f"max_output_size = {_DEFAULT_MAX_OUTPUT}",
            # 1.50 的 capabilities 只认 image_in/video_in/thinking/always_thinking，
            # tool_use 不再是合法值（工具调用是默认能力）；留空即可。
            "",
        ]
        return "\n".join(lines)

    def _write_mcp_config(
        self, task: AdapterRunInput, kimi_home: Path, *, sandbox: AttemptSandbox | None = None,
    ) -> Path | None:
        """MCP 声明写进 `$KIMI_CODE_HOME/mcp.json`（dialect 同 CC 的 mcpServers）。

        kimi 本版没有 `--mcp-config-file` flag，home 目录里的 mcp.json 是唯一
        的注入点；因为 home 已按 attempt 隔离，这里天然不污染宿主机配置。
        """
        if not task.mcp_servers:
            return None
        servers: dict[str, dict[str, Any]] = {}
        for spec in task.mcp_servers:
            command, args = self._mcp_command_and_args(task, spec)
            server: dict[str, Any] = {
                "command": command,
                "args": args,
                "env": {
                    "OCTAGON_ATTEMPT_ID": task.attempt_id,
                    "OCTAGON_ENV_TOKEN": task.env_token,
                    "OCTAGON_BASE_URL": (
                        sandbox.translate_url(task.env_base_url)
                        if sandbox is not None else task.env_base_url
                    ),
                },
            }
            if spec.cwd:
                server["cwd"] = spec.cwd
            servers[spec.name] = server
        path = kimi_home / "mcp.json"
        path.write_text(
            json.dumps({"mcpServers": servers}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    def _mcp_command_and_args(
        self, task: AdapterRunInput, spec: Any
    ) -> tuple[str, list[str]]:
        """MCP server 的最终 command/args；wire mcp rewrite 在此应用。"""
        command = spec.command
        args = list(spec.args)
        rewrite = task.wire_injection.mcp_rewrites.get(spec.name)
        if task.wire_injection.enabled and rewrite is not None:
            args = [*rewrite.args_prefix, command, *args]
            command = rewrite.command
        return command, args

    # ---------- 执行 ----------

    async def run(
        self,
        task: AdapterRunInput,
        env: Any,
        data_path: Path,
    ) -> AdapterResult:
        data_path = Path(data_path)
        # attempt 级沙盒上下文包住整个 run：多轮 conversation 的每一轮都在同一
        # 容器里 exec；上下文退出（含异常）先收掉容器，dispatch 才进 scoring。
        attempt_spec = AttemptSpec(
            attempt_id=task.attempt_id,
            data_path=data_path,
            agent_name=self.agent_name,
            run_id=task.run_id,
            workspace=data_path / "attempts" / task.attempt_id / "skill_workspace",
        )
        async with self.launcher.attempt(attempt_spec) as sandbox:
            return await self._run_inner(task, env, data_path, sandbox)

    async def _run_inner(
        self,
        task: AdapterRunInput,
        env: Any,
        data_path: Path,
        sandbox: AttemptSandbox,
    ) -> AdapterResult:
        attempt_dir = data_path / "attempts" / task.attempt_id
        attempt_dir.mkdir(parents=True, exist_ok=True)
        # agent 工作区：与 CC/codex/blade 同构，产物落这里。
        workspace = attempt_dir / "skill_workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        events_path = attempt_dir / "events.jsonl"
        thinking_path = attempt_dir / "thinking.jsonl"

        cli_path = sandbox.resolve_cli("kimi")
        if not cli_path:
            return AdapterResult(
                attempt_id=task.attempt_id,
                status="cli_not_found",
                error_code="kimi_not_in_path",
                error_message="kimi CLI not found in PATH",
            )

        model_ref = parse_model_ref(self.model, self.providers)
        api_key: str | None = None
        base_url: str | None = None
        if model_ref.provider is not None:
            provider = self.providers[model_ref.provider]
            # 成本核算：run 专属 key 优先，回落 provider 配置。
            api_key, _used_run_key = resolve_agent_key(
                task.run_id, resolve_api_key(provider), task.attempt_id,
                provider.base_url,
            )
            # key 缺失时 CLI 只会在上游报 401，这里 fail fast 给可操作信息。
            if provider.api_key_env and api_key is None:
                return AdapterResult(
                    attempt_id=task.attempt_id,
                    status="auth_failed",
                    error_code="provider_api_key_missing",
                    error_message=(
                        f"provider {model_ref.provider!r} 缺 API key："
                        f"环境变量 {provider.api_key_env} 未设置，"
                        f"octagon.yaml model_providers.{model_ref.provider}.api_key 也未填"
                    ),
                )
            # base_url 必须与 key 同源，否则拿 run key 打向旧 endpoint。
            base_url = resolve_agent_base_url(task.run_id, provider.base_url, task.attempt_id)
            # wire 消费点：injection 覆盖本次 provider base URL。
            if task.wire_injection.enabled and task.wire_injection.llm_base_url:
                base_url = sandbox.translate_url(task.wire_injection.llm_base_url)

        # 本地状态隔离：KIMI_CODE_HOME 指向 attempt 内的干净目录，kimi 不读宿主机
        # ~/.kimi-code 的全局 provider 凭据 / skills / sessions / 记忆，
        # 排除私人配置污染；Kimi Code 内建能力不在这里禁用。
        kimi_home = sandbox.host_home(attempt_dir / ".kimi-iso-home")
        kimi_home.mkdir(parents=True, exist_ok=True)
        # 无命名 provider 时不写 config.toml：让 CLI 用它自己已登录的 provider
        # 与 default_model（此时 self.model 原样作为别名传给 -m）。
        #
        # 写出的路径必须再用 `--config-file` 显式指给 CLI：0.29/0.38 从
        # `$KIMI_CODE_HOME/config.toml` 读，**1.50 改成了 `$HOME/.kimi/config.toml`**，
        # 并在启动时在那儿生成一份 `default_model = ""` 的默认配置。只写旧位置的
        # 话新版读不到 provider/模型注册，agent 起手就是 "LLM not set"。显式传
        # 路径两版都认，也不再依赖任何发现规则。
        # wire 自定义头（含 capture token）要先算出来写进 provider 配置；
        # 下面的 KIMI_CODE_CUSTOM_HEADERS 环境变量只对 0.38 生效，保留兼容。
        wire_headers: dict[str, str] = {}
        if task.wire_injection.enabled:
            wire_headers.update(task.wire_injection.llm_headers)
            if task.wire_injection.capture_token:
                wire_headers["X-Octagon-Capture-Token"] = (
                    task.wire_injection.capture_token
                )
        config_path: Path | None = None
        if model_ref.provider is not None:
            config_path = kimi_home / "config.toml"
            config_path.write_text(
                self._render_config_toml(
                    model_ref, base_url or "", api_key,
                    custom_headers=wire_headers or None,
                ),
                encoding="utf-8",
            )
        self._write_mcp_config(task, kimi_home, sandbox=sandbox)

        prompt = self._render_prompt(task)
        cli_model = (
            f"{_PROVIDER_ID}/{model_ref.model}"
            if model_ref.provider is not None
            else self.model
        )

        plan = effective_conversation(task)
        session_id: str | None = None
        checkpoint_state: dict[str, Any] = {
            "cli_path": cli_path,
            "agent": "kimi-code",
            "conversation_plan_hash": plan.plan_hash if not plan.is_legacy else None,
            "conversation_turn_count": len(plan.turns) if not plan.is_legacy else None,
            "session_id": None,
            "last_completed_turn_index": None,
            "active_turn_index": None,
            "recoverability": "checkpoint-only",
        }

        def _checkpoint(**updates: Any) -> None:
            """更新并落盘 checkpoint（键名白名单，防拼错静默新增字段）。"""
            unknown = set(updates) - set(checkpoint_state)
            if unknown:
                raise KeyError(f"未知的 checkpoint 字段: {sorted(unknown)}")
            checkpoint_state.update(updates)
            write_checkpoint(attempt_dir, checkpoint_state)

        def _build_cmd(turn: Any, *, is_first: bool) -> list[str]:
            """构造某一轮的 argv。

            后续轮用 `-r <session_id>` 显式续跑；**禁止 `-c/--continue`**——它挑
            "该工作目录最近一次会话"，并发多 attempt 时会串到别人的会话上
            （与 codex 禁用 `--last` 同理）。
            """
            turn_prompt = render_turn_prompt(task, turn, base_prompt=prompt)
            args = [cli_path]
            if config_path is not None:
                args += ["--config-file", sandbox.path(config_path)]
            if not is_first:
                if not session_id:
                    raise RuntimeError(
                        "kimi 多轮续跑缺 session ID："
                        "首轮未收到 session.resume_hint 事件"
                    )
                args += ["-r", session_id]
            args += [
                "-p", turn_prompt,
                # kimi ≥ 1.50：`-p` 只提供 prompt，不再隐含 print 模式，而
                # `--output-format` 明确要求 print UI（缺了它 CLI 直接以
                # "Output format is only supported for print UI" 退出）。
                # 旧版 0.38 里 `-p` 兼作 print 开关，显式传也接受。
                "--print",
                "--output-format", "stream-json",
                "-m", cli_model,
            ]
            return args

        events_count = 0
        # 工具调用数原先从未统计（与 cc/codex 不同），头部统计恒显示 0。
        # kimi 是 OpenAI 风格顶层 tool_calls[]，按 call id 去重。
        tool_call_count = 0
        seen_tool_call_ids: set[str] = set()
        thinking_count = 0
        last_event_at: str | None = None
        error_message: str | None = None
        started_at = datetime.now(timezone.utc)
        proc = None

        subprocess_env = {
            **os.environ,
            "KIMI_CODE_HOME": sandbox.path(kimi_home),
            "HOME": sandbox.path(kimi_home),
            # 评测期间禁用自动升级：跑到一半换 CLI 版本会让同批次不可比。
            "KIMI_CLI_NO_AUTO_UPDATE": "1",
            "KIMI_CODE_NO_AUTO_UPDATE": "1",
        }
        if task.mcp_servers:
            subprocess_env.update({
                "OCTAGON_ATTEMPT_ID": task.attempt_id,
                "OCTAGON_ENV_TOKEN": task.env_token,
                "OCTAGON_BASE_URL": sandbox.translate_url(task.env_base_url),
            })
        # wire injection 消费点：provider/MCP 已写进配置文件，这里合并 process_env
        # 与自定义头。
        if task.wire_injection.enabled:
            subprocess_env.update(task.wire_injection.process_env)
            extra_headers = dict(task.wire_injection.llm_headers)
            if task.wire_injection.capture_token:
                # capture token 走独立头（不占 Authorization——那被 provider auth
                # 占用且反代会剥）。缺了它反代直接 401 capture token required。
                extra_headers["X-Octagon-Capture-Token"] = (
                    task.wire_injection.capture_token
                )
                # 也留一份 env 供排查/其它用途（与 CC 一致）。
                subprocess_env["OCTAGON_WIRE_CAPTURE_TOKEN"] = (
                    task.wire_injection.capture_token
                )
            if extra_headers:
                subprocess_env["KIMI_CODE_CUSTOM_HEADERS"] = "\n".join(
                    f"{name}: {value}" for name, value in extra_headers.items()
                )

        deadline = AttemptDeadline(task.timeout_seconds)
        conversation_trace = ConversationTraceWriter(
            attempt_dir / CONVERSATION_FILENAME, attempt_id=task.attempt_id,
        )
        conversation_trace.conversation_started(
            turn_count=len(plan.turns), is_legacy=plan.is_legacy,
            score_turn_id=plan.score_turn.turn_id,
        )
        current_turn_id: str | None = None
        current_turn_index: int | None = None
        final_texts: list[str] = []

        try:
            async def _run_turn(turn: Any, *, is_first: bool) -> None:
                nonlocal proc, events_count, thinking_count, last_event_at
                nonlocal error_message, session_id

                async def _consume() -> None:
                    nonlocal events_count, thinking_count, last_event_at
                    nonlocal tool_call_count
                    nonlocal session_id, error_message
                    assert turn_proc.stdout is not None
                    async for raw_line in turn_proc.stdout:
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue
                        ts = _now_iso()
                        last_event_at = ts
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            _append_jsonl(events_path, with_turn_ext(
                                {"timestamp": ts, "raw_line": line},
                                current_turn_id, current_turn_index,
                            ))
                            events_count += 1
                            continue

                        _append_jsonl(events_path, with_turn_ext(
                            {"timestamp": ts, **data},
                            current_turn_id, current_turn_index,
                        ))
                        events_count += 1

                        # 与归一器 `_openai_tool_calls` 认同一形状，保持口径一致。
                        raw_calls = data.get("tool_calls")
                        if not isinstance(raw_calls, list):
                            nested = data.get("message")
                            raw_calls = (
                                nested.get("tool_calls")
                                if isinstance(nested, dict) else None
                            )
                        if isinstance(raw_calls, list):
                            for idx, call in enumerate(raw_calls):
                                if not isinstance(call, dict):
                                    continue
                                cid = str(call.get("id") or "")
                                key = cid or f"event:{events_count}:{idx}"
                                if key not in seen_tool_call_ids:
                                    seen_tool_call_ids.add(key)
                                    tool_call_count += 1

                        role = data.get("role")
                        content = data.get("content")

                        # 权威 session ID 只在 meta 行的 session.resume_hint 里。
                        if data.get("type") == "session.resume_hint":
                            sid = data.get("session_id")
                            if isinstance(sid, str) and sid:
                                if session_id is None:
                                    session_id = sid
                                    _checkpoint(session_id=sid)
                                elif sid != session_id:
                                    # resume 却拿到**不同** session：上下文已断
                                    # 。静默覆盖会让后续轮在断裂上下文
                                    # 上继续跑、最终评错分。
                                    broken = (
                                        f"session_continuity_broken: 期望 session "
                                        f"{session_id}，resume 返回 {sid}"
                                    )
                                    logger.error(
                                        "kimi attempt=%s %s", task.attempt_id, broken,
                                    )
                                    if not error_message:
                                        error_message = broken
                                    # 保留**原** session ID：它是这个 attempt 的身份。

                        if role == "assistant" and isinstance(content, str) and content:
                            final_texts.append(content)
                        elif role == "thinking" or data.get("type") == "thinking":
                            text = content if isinstance(content, str) else ""
                            if text:
                                thinking_count += 1
                                _append_jsonl(thinking_path, {
                                    "timestamp": ts,
                                    "sequence": thinking_count,
                                    "content": text,
                                    "type": "thinking",
                                })
                        elif role == "error" or data.get("type") == "error":
                            msg = content if isinstance(content, str) else json.dumps(
                                data, ensure_ascii=False
                            )
                            if msg and not error_message:
                                error_message = msg[:500]

                async with sandbox.exec(ExecSpec(
                    argv=_build_cmd(turn, is_first=is_first),
                    cwd=str(workspace),
                    env=subprocess_env,
                    turn_id=getattr(turn, "turn_id", None),
                )) as proc:
                    turn_proc = proc
                    try:
                        await asyncio.wait_for(_consume(), timeout=deadline.remaining())
                        await asyncio.wait_for(turn_proc.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        error_message = f"timeout after {task.timeout_seconds}s"

                if turn_proc.stderr:
                    stderr = (
                        await turn_proc.stderr.read()
                    ).decode("utf-8", errors="replace").strip()
                    if stderr:
                        (attempt_dir / "stderr.txt").write_text(
                            stderr, encoding="utf-8",
                        )
                        # kimi 的致命错误（config.invalid / failed to run prompt）
                        # 只出现在 stderr，stdout 一行都没有——非 0 退出时必须
                        # 采信它，否则 attempt 会以「completed 但零事件」收场。
                        if not error_message and turn_proc.returncode != 0:
                            error_message = stderr[:500]

            for index, turn in enumerate(plan.send_message_turns):
                current_turn_id = None if plan.is_legacy else turn.turn_id
                current_turn_index = None if plan.is_legacy else turn.turn_index
                try:
                    deadline.check_before_turn()
                except AttemptBudgetExhausted:
                    error_message = (
                        error_message or f"timeout after {task.timeout_seconds}s"
                    )
                    conversation_trace.turn_failed(
                        turn, producer_session_id=session_id,
                        error_code=ERROR_BUDGET_EXHAUSTED_BETWEEN_TURNS,
                        error_summary=error_message,
                    )
                    break
                conversation_trace.turn_started(turn, producer_session_id=session_id)
                if not plan.is_legacy:
                    _checkpoint(active_turn_index=turn.turn_index)
                await _run_turn(turn, is_first=(index == 0))
                # 任一轮失败即终止 conversation，后续轮不再发送。
                turn_status = _classify_outcome(
                    proc.returncode if proc is not None else None, error_message,
                )
                if turn_status != "completed":
                    if not error_message:
                        error_message = (
                            f"turn {turn.turn_id!r} 未正常完成"
                            f"（status={turn_status}）"
                        )
                    conversation_trace.turn_failed(
                        turn, producer_session_id=session_id,
                        error_code=(
                            "session_continuity_broken"
                            if "session_continuity_broken" in error_message
                            else None
                        ),
                        error_summary=error_message,
                    )
                    break
                conversation_trace.turn_completed(turn, producer_session_id=session_id)
                if not plan.is_legacy:
                    _checkpoint(
                        active_turn_index=None,
                        last_completed_turn_index=turn.turn_index,
                    )
            else:
                conversation_trace.conversation_completed()
            if error_message:
                conversation_trace.conversation_failed(
                    error_code=None, error_summary=error_message,
                )
            conversation_trace.close()

        except FileNotFoundError:
            return AdapterResult(
                attempt_id=task.attempt_id,
                status="cli_not_found",
                error_code="kimi_exec_failed",
                error_message="failed to execute kimi CLI",
            )
        except Exception as exc:
            return AdapterResult(
                attempt_id=task.attempt_id,
                status="cli_error",
                error_code="unexpected_error",
                error_message=str(exc),
            )

        if final_texts:
            (attempt_dir / "kimi_final.txt").write_text(
                "\n".join(final_texts), encoding="utf-8",
            )

        duration_ms = int(
            (datetime.now(timezone.utc) - started_at).total_seconds() * 1000
        )
        status = _classify_outcome(proc.returncode if proc else None, error_message)
        return AdapterResult(
            attempt_id=task.attempt_id,
            status=status,
            external_refs={
                "cli_path": cli_path,
                "model_used": self.model,
                "session_id": session_id,
            },
            error_code=(
                None if status == "completed"
                else "session_continuity_broken"
                if error_message and "session_continuity_broken" in error_message
                else (error_message or "cli_error")
            ),
            error_message=error_message,
            events_count=events_count,
            tool_call_count=tool_call_count,
            last_event_at=last_event_at,
            thinking_count=thinking_count,
            # kimi 0.29 的 stream-json **不吐 usage**（实测只有 assistant / meta
            # 两种 role）。留空而不是填 0——0 会被读成"真的用了 0 token"。
            # 空 dict 即五维全"不可得"（token_cost_accounting 的 compact 形状），
            # 与其它 adapter 的缺键语义一致。补齐路径是从 wire 反代的响应体
            # 解析 usage（需 capture_policy=full），不在 adapter 层。
            token_usage=compact_usage(empty_usage()),
            duration_ms=duration_ms,
            conversation_summary=summarize_conversation(attempt_dir),
            security_meta=build_security_meta(
                execution_locus=self.capabilities.execution_locus,
                # print 模式本身即非交互自动执行（--auto 与 -p 互斥，见模块 docstring）。
                permission_mode="print-mode(-p)",
                workspace_root=str(workspace.resolve()),
                # kimi 是否有服务端联网工具未核实，如实记 unknown。
                extra={**sandbox.security_fields(), "server_side_network": "unknown"},
            ),
        )

    def _render_prompt(self, task: AdapterRunInput) -> str:
        """与 codex 同构：无独立 system 通道，时间预算回落 message 顶部。"""
        parts: list[str] = []
        notice = (
            time_budget_notice(task.timeout_seconds)
            if task.notify_model_of_timeout
            else None
        )
        if notice:
            parts += [notice, ""]
        parts.append(task.task_prompt)
        context = prompt_context(task.task_context) if task.task_context else {}
        if context:
            parts.append("")
            parts.append("上下文:")
            parts.append(json.dumps(context, ensure_ascii=False, indent=2))
        return "\n".join(parts)


def _classify_outcome(returncode: int | None, error_message: str | None) -> str:
    if error_message and "timeout" in error_message.lower():
        return "timeout"
    if error_message:
        return "cli_error"
    if returncode == 0:
        return "completed"
    return "cli_error"


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _append_jsonl(path: Path, data: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, default=str) + "\n")
