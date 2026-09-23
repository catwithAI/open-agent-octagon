"""CodexAdapter — 通过 codex CLI 子进程调用 Codex。"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

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
from ..wire.injection import WireInjection
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
    ConversationTurn,
    build_security_meta,
    prompt_context,
    time_budget_notice,
)
from .error_taxonomy import classify as classify_cli_error
from .token_usage import compact_usage, empty_usage, usage_detail

logger = logging.getLogger(__name__)


#: cli 版本探测缓存：(cli_path, mtime) → version | None。见 claude_code._detect_cli_version。
_CODEX_CLI_VERSION_CACHE: dict[tuple[str, float], str | None] = {}


def _detect_cli_version(cli_path: str) -> str | None:
    """best-effort 探测 codex CLI 版本,失败/超时返回 None(不阻塞)。

    缓存按 (cli_path, mtime),每个二进制只探测一次。沙盒/docker 镜像的
    agent 版本由 launcher 记(security_meta.agent_version),不走这里。
    """
    try:
        stat = Path(cli_path).stat()
    except OSError:
        return None
    key = (cli_path, stat.st_mtime)
    if key in _CODEX_CLI_VERSION_CACHE:
        return _CODEX_CLI_VERSION_CACHE[key]
    version: str | None = None
    try:
        import subprocess

        proc = subprocess.run(
            [cli_path, "--version"],
            capture_output=True,
            timeout=3.0,
        )
        text = proc.stdout.decode("utf-8", errors="replace").strip().splitlines()
        if text:
            version = text[0].strip()
    except Exception:  # noqa: BLE001 —— best-effort 探测
        version = None
    _CODEX_CLI_VERSION_CACHE[key] = version
    return version


class CodexAdapter:
    # 能力静态声明：见 AdapterCapabilities docstring。
    agent_name = "codex"
    capabilities = AdapterCapabilities(
        execution_locus="host",
        network_required="public_internet",
        # `codex exec` 无运行中交互应答通道：含 answer_interaction
        # turn 的 conversation 由 dispatch 在启动前拒绝，不静默忽略该轮。
        interaction_answer=False,
        iterative_session=True,
    )

    def __init__(
        self,
        *,
        model: str = "gpt-5.5",
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

        llm_headers：codex 的静态 provider header 通道未定义，待再评估。
        """
        return {
            "process_env": True,
            "llm_base_url": True,
            "llm_headers": False,
            "mcp_rewrites": True,
        }

    def _provider_cli_args(
        self,
        model_ref: ModelRef,
        injection: WireInjection,
        run_id: str | None = None,
        attempt_id: str | None = None,
        translate_url: Callable[[str], str] | None = None,
    ) -> list[str]:
        """provider 前缀模型 → -c 单次覆盖注入命名 provider，不碰全局 config.toml。

        wire 消费点：injection.llm_base_url 覆盖本次
        model_providers.<id>.base_url。

        成本核算：run 专属 key 的 base_url 必须与 key 同源（见下方 base_url
        解析），否则会拿 run key 打向旧 endpoint。key 本身走 env_key 指向的
        环境变量注入，不出现在命令行。
        """
        if model_ref.provider is None:
            return ["-m", model_ref.model]
        p = self.providers[model_ref.provider]
        name = model_ref.provider
        # Codex 走非 Responses provider（chat/messages）在 adapter
        # 启动前 fail fast——Codex 只支持 responses wire_api，chat 会静默错配。
        if p.effective_wire_api() != "responses":
            raise ValueError(
                f"Codex 不支持 provider kind={p.kind!r}（wire_api="
                f"{p.effective_wire_api()!r}）；Codex 需要 openai-responses provider。"
            )
        base_url = (
            injection.llm_base_url
            if injection.enabled and injection.llm_base_url
            # wire 未接管时，run 专属 key 的 base_url 优先（与 key 同源）。
            else resolve_agent_base_url(run_id, p.base_url, attempt_id)
        )
        if translate_url is not None:
            # 沙盒：wire 反代挂在宿主机 127.0.0.1，容器内要走 host.docker.internal。
            base_url = translate_url(base_url)
        args = [
            "-c", f'model_providers.{name}.name="{name}"',
            "-c", f'model_providers.{name}.base_url="{base_url}"',
            "-c", f'model_providers.{name}.wire_api="{p.effective_wire_api()}"',
        ]
        if p.api_key_env:
            args += ["-c", f'model_providers.{name}.env_key="{p.api_key_env}"']
        # capture token：走 Codex 的 env_http_headers 映射——
        # 把 X-Octagon-Capture-Token 头的值从 OCTAGON_WIRE_CAPTURE_TOKEN 环境变量读，
        # Codex 请求（反代的）base_url 时带上该头。用 env 映射而非 http_headers 静态值，
        # token 不出现在命令行（-c 参数）。
        if injection.enabled and injection.capture_token:
            args += [
                "-c",
                'model_providers.'
                f'{name}.env_http_headers='
                '{ "X-Octagon-Capture-Token" = "OCTAGON_WIRE_CAPTURE_TOKEN" }',
            ]
        args += [
            "-c", f'model_provider="{name}"',
            "-m", model_ref.model,
        ]
        return args

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
        # agent 工作区（design：skill_workspace 是 agent 的唯一世界边界）。
        # cwd 设在这里，agent 产物落这里，与 blade 的产物拉回点一致——三家同构。
        # attempt 根目录保留给框架（wire/trajectory/隔离 home），不与 agent 产物混。
        # dispatch 的 _copy_env_scripts 已把 SKILL.md/物料拷进来；单跑时防御性 mkdir。
        workspace = attempt_dir / "skill_workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        events_path = attempt_dir / "events.jsonl"
        thinking_path = attempt_dir / "thinking.jsonl"
        # -o 的落点必须是 agent 进程可写的路径：宿主机就是 attempt 根，沙盒是隔离 HOME。
        final_message_path = sandbox.host_home(attempt_dir) / "codex_final.txt"

        cli_path = sandbox.resolve_cli("codex")
        if not cli_path:
            return AdapterResult(
                attempt_id=task.attempt_id,
                status="cli_not_found",
                error_code="codex_not_in_path",
                error_message="codex CLI not found in PATH",
            )
        # 版本锚(host CLI):一次缓存探测,best-effort,None 表示探测失败。
        cli_version = _detect_cli_version(cli_path)

        prompt = self._render_prompt(task)
        model_ref = parse_model_ref(self.model, self.providers)
        # 非 Responses provider 在 agent 启动前 fail fast，
        # 返回明确 terminal 而非让 ValueError 冒成 adapter_crashed。
        try:
            provider_args = self._provider_cli_args(
                model_ref, task.wire_injection, task.run_id, task.attempt_id,
                translate_url=sandbox.translate_url,
            )
        except ValueError as exc:
            return AdapterResult(
                attempt_id=task.attempt_id,
                status="cli_error",
                error_code="provider_protocol_unsupported",
                error_message=str(exc),
            )
        # conversation plan：单轮 → 一个 legacy turn，argv 与改造前一致
        # （含 --ephemeral）；多轮 → 首轮建 thread、后续 resume 同一 thread。
        plan = effective_conversation(task)
        session_required = (
            (not plan.is_legacy) or task.iteration_turn_handler is not None
        )
        # 首轮从 thread.started 事件读到的权威 thread ID。
        # 不自己派生——Codex 的 thread ID 由 CLI 生成，只能读不能指定。
        codex_thread_id: str | None = None
        # 多轮 checkpoint：thread ID 一拿到就落盘，进程随后崩溃也不会
        # 丢——它是唯一能找回这个 attempt 服务端会话的凭据。
        #
        # **当前只做持久化，不做恢复执行**：Codex 的 startup recovery 路径
        # （从 checkpoint 续跑未完成的轮）尚未实现。checkpoint 先写
        # 起来，恢复能力落地时不必再补采数据。
        checkpoint_state: dict[str, Any] = {
            "codex_cli_path": cli_path,
            "conversation_plan_hash": plan.plan_hash if not plan.is_legacy else None,
            "conversation_turn_count": len(plan.turns) if not plan.is_legacy else None,
            "codex_thread_id": None,
            "last_completed_turn_index": None,
            "active_turn_index": None,
            "recoverability": "checkpoint-only",
        }

        def _checkpoint(**updates: Any) -> None:
            """更新并落盘 checkpoint。thread ID / 轮次进度变化时调用。

            只允许更新已声明的键——`**kwargs` 拼错键名会静默新增一个无效字段
            而不是更新目标（实测踩过：`thread_id=` 写成了这个而非
            `codex_thread_id=`，checkpoint 里的 ID 一直是 None）。
            """
            unknown = set(updates) - set(checkpoint_state)
            if unknown:
                raise KeyError(f"未知的 checkpoint 字段: {sorted(unknown)}")
            checkpoint_state.update(updates)
            write_checkpoint(attempt_dir, checkpoint_state)

        def _common_args(*, with_workspace: bool = True) -> list[str]:
            """provider / workspace / MCP 等每轮都相同的参数。

            `with_workspace=False` 用于 `exec resume`——该子命令**不接受
            `-C`**（实测 codex-cli 0.144.5 报 "unexpected argument '-C'"）。
            工作目录由 subprocess 的 `cwd=workspace` 保证，去掉这个 flag
            不改变 agent 实际的工作区。
            """
            args = [
                "--json",
                "--skip-git-repo-check",
                "--ignore-rules",
                "--dangerously-bypass-approvals-and-sandbox",
                *provider_args,
            ]
            if with_workspace:
                args += ["-C", sandbox.path(workspace)]
            args += ["-o", sandbox.path(final_message_path)]
            if sandbox.server_side_tools == "deny":
                # 服务端 web search 不经容器；deny 时经 config 关掉（待实测 key）。
                args += ["-c", 'web_search="disabled"']
            for spec in task.mcp_servers:
                mcp_command, mcp_args = self._mcp_command_and_args(task, spec)
                args += [
                    "-c", f"mcp_servers.{spec.name}.command="
                    f"{json.dumps(mcp_command, ensure_ascii=True)}",
                    "-c", f"mcp_servers.{spec.name}.args="
                    f"{json.dumps(mcp_args, ensure_ascii=True)}",
                ]
                if spec.cwd:
                    args += [
                        "-c", f"mcp_servers.{spec.name}.cwd="
                        f"{json.dumps(spec.cwd, ensure_ascii=True)}",
                    ]
            return args

        def _build_cmd(turn: Any, *, is_first: bool) -> list[str]:
            """构造某一轮的 argv。

            单轮**不再**插 `--ephemeral`：ephemeral 会话不落盘，沙盒里就没有
            codex 的本地对话历史（threads 表空），ATIF 还原拿不到源。去掉后
            codex 把 session rollout 写到 `.codex-iso-home/sessions/`，供
            ``scripts/emit_atif.py`` / ``backend/atif`` 还原。**行为变更**：单轮
            也会持久化会话（且会生成 codex_thread_id，不再是 None）。代价是
            历史 attempt（改前单轮）没有会话文件，还原报 not_available。
            多轮必须去掉 `--ephemeral`——ephemeral 会话不落盘就无法 resume。
            后续轮用 `codex exec resume <thread_id> <prompt>`，**禁止
            `--last`**：它挑"最近一次记录的会话"，并发多 attempt 时会 resume
            到别人的 thread 上。
            """
            turn_prompt = render_turn_prompt(task, turn, base_prompt=prompt)
            if not session_required:
                # 单轮：与多轮首轮同构，不插 --ephemeral。
                return [cli_path, "exec", *_common_args(), turn_prompt]
            if is_first:
                return [cli_path, "exec", *_common_args(), turn_prompt]
            if not codex_thread_id:
                raise RuntimeError(
                    "多轮 Codex 续跑缺 thread ID：首轮未收到 thread.started 事件"
                )
            return [
                cli_path, "exec", "resume", codex_thread_id,
                *_common_args(with_workspace=False), turn_prompt,
            ]

        if task.mcp_servers:
            self._write_mcp_config_snapshot(task, attempt_dir, sandbox=sandbox)

        events_count = 0
        thinking_count = 0
        # Codex item.completed 是一次已完成的动作；只统计工具/命令 item，
        # 不把 agent_message 当成工具调用。
        tool_call_count = 0
        seen_tool_item_ids: set[str] = set()
        # 五维累计（token_cost_accounting）：None = 该 producer 没报过该字段。
        total_usage: dict[str, int | None] = empty_usage()
        last_event_at: str | None = None
        error_message: str | None = None
        turn_failed_message: str | None = None
        started_at = datetime.now(timezone.utc)
        proc = None

        # 本地状态隔离：把 CODEX_HOME 指向 attempt 内的干净空目录，codex 就不读
        # 宿主机 ~/.codex 的全局 config.toml（32KB）/skills/plugins/memories/history，
        # 而在干净目录里从零建配置，排除同事私人配置污染；不禁用 Codex 内建能力。实测：
        # 空 CODEX_HOME 下 codex 新建自己的 memories/state sqlite，不读宿主机全局态。
        iso_codex_home = sandbox.host_home(attempt_dir / ".codex-iso-home")
        iso_codex_home.mkdir(parents=True, exist_ok=True)
        subprocess_env = {
            **os.environ,
            "CODEX_HOME": sandbox.path(iso_codex_home),
        }
        # adapter 显式注入、必须进容器的变量名（见 ExecSpec.env_keep）。
        # codex 的 provider key 靠 `-c model_providers.*.env_key` 在子进程环境里
        # 按名查找，值又常常就来自宿主机同名变量——不声明所有权会被沙盒的
        # 「值等于宿主机 → 不透传」规则丢掉，表现为容器内 Missing environment
        # variable。
        owned_env: set[str] = set()
        # Codex 的 MCP 配置经 argv 传入，secret 不可写进 -c；仅场景确实提供 MCP
        # 时才让 stdio child 从父进程环境继承 attempt 凭据。
        if task.mcp_servers:
            subprocess_env.update({
                "OCTAGON_ATTEMPT_ID": task.attempt_id,
                "OCTAGON_ENV_TOKEN": task.env_token,
                "OCTAGON_BASE_URL": sandbox.translate_url(task.env_base_url),
            })
        if model_ref.provider is not None:
            provider = self.providers[model_ref.provider]
            # 成本核算：run 专属 key 优先，回落 provider 配置。
            # 回落时 used_run_key=False，审计据此降级为共享 key 上界。
            api_key, _used_run_key = resolve_agent_key(
                task.run_id, resolve_api_key(provider), task.attempt_id,
                provider.base_url,
            )
            # key 缺失时 codex 会在 turn.failed 里报 Missing environment
            # variable——这里 fail fast 给出可操作的定位信息，不启子进程。
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
            if provider.api_key_env and api_key:
                subprocess_env[provider.api_key_env] = api_key
                owned_env.add(provider.api_key_env)
        # wire injection 消费点：provider/MCP -c 参数已在 cmd 构造时应用，
        # 这里最后合并 process_env（表 Codex 行）。
        if task.wire_injection.enabled:
            subprocess_env.update(task.wire_injection.process_env)
            owned_env.update(task.wire_injection.process_env)
            # capture token：走 injection 专用字段，adapter 注入子进程。
            if task.wire_injection.capture_token:
                subprocess_env["OCTAGON_WIRE_CAPTURE_TOKEN"] = (
                    task.wire_injection.capture_token
                )
                owned_env.add("OCTAGON_WIRE_CAPTURE_TOKEN")

        # attempt 总 deadline：多轮共享一个预算。单轮时与改造前的
        # wait_for(timeout=task.timeout_seconds) 等价。
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

        try:
            async def _run_turn(turn: Any, *, is_first: bool) -> None:
                """跑一轮：spawn 一个 codex exec 子进程并消费其 stdout 到结束。"""
                nonlocal proc, events_count, thinking_count, last_event_at
                nonlocal tool_call_count
                nonlocal turn_failed_message, error_message, codex_thread_id

                # turn.failed 是轮级状态：上一轮的失败消息不能泄漏进本轮判定。
                turn_failed_message = None
                # 本轮开始时的 attempt 累计量。Codex 的 usage 语义：**每个
                # `codex exec` 进程内部累计、进程之间彼此独立**（实测
                # 2026-07-20：两轮分别 7336 / 14696，非 7336 / 22032）。
                # 所以跨轮必须"轮基线 + 本轮值"，直接 max 会让后续轮覆盖掉
                # 前面轮次的量。
                turn_base = dict(total_usage)
                async def _consume() -> None:
                    nonlocal events_count, thinking_count, last_event_at
                    nonlocal tool_call_count
                    nonlocal turn_failed_message, codex_thread_id, error_message
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

                        event_sequence = events_count
                        _append_jsonl(events_path, with_turn_ext(
                            {"timestamp": ts, **data},
                            current_turn_id, current_turn_index,
                        ))
                        events_count += 1

                        item = data.get("item")
                        if data.get("type") == "item.completed" and isinstance(item, dict):
                            if item.get("type") in {"mcp_tool_call", "command_execution"}:
                                item_id = item.get("id")
                                dedupe_key = (
                                    f"id:{item_id}" if item_id else
                                    f"event:{event_sequence}"
                                )
                                if dedupe_key not in seen_tool_item_ids:
                                    seen_tool_item_ids.add(dedupe_key)
                                    tool_call_count += 1

                        # 权威 thread ID：只能从 producer 事件读，
                        # 不自己派生——后续轮 resume 全靠它。
                        if data.get("type") == "thread.started":
                            tid = data.get("thread_id")
                            if isinstance(tid, str) and tid:
                                if codex_thread_id and tid != codex_thread_id:
                                    # resume 却拿到**不同** thread：上下文已断
                                    # （session_continuity_broken）。不能
                                    # 只记 warning 就覆盖——那会让 attempt 静默
                                    # 切到新 thread、后续轮继续 resume 它，最终
                                    # runner 在断裂的上下文上评分。立即置错误，
                                    # 由轮末的 turn_status 判定终止 conversation。
                                    session_broken = (
                                        f"session_continuity_broken: 期望 thread "
                                        f"{codex_thread_id}，resume 返回 {tid}"
                                    )
                                    logger.error(
                                        "codex attempt=%s %s",
                                        task.attempt_id, session_broken,
                                    )
                                    if not error_message:
                                        error_message = session_broken
                                    # 保留**原**thread ID：它才是这个 attempt 的
                                    # 身份，不被意外的新 ID 覆盖。
                                else:
                                    codex_thread_id = tid
                                    _checkpoint(codex_thread_id=tid)

                        # turn.failed 是 codex 的权威失败事件（如上游 404）。stderr
                        # 末行往往只是常规提示（"Reading additional input from
                        # stdin..."），拿它当 error_message 会完全误导排查。
                        if data.get("type") == "turn.failed":
                            err = data.get("error")
                            msg = err.get("message") if isinstance(err, dict) else None
                            if isinstance(msg, str) and msg:
                                turn_failed_message = msg

                        text = _message_text(data)
                        if text and _looks_like_reasoning(data):
                            thinking_count += 1
                            _append_jsonl(thinking_path, {
                                "timestamp": ts,
                                "sequence": thinking_count,
                                "content": text,
                                "type": "thinking",
                            })
                        # 轮内取 max（同一进程的 usage 事件是递增快照），
                        # 跨轮加基线（进程间独立，见上方 turn_base 注释）。
                        # 五维逐字段独立做，缺字段保持 None（不可得）。
                        for field, value in _usage(data).items():
                            if value is None:
                                continue
                            base = turn_base.get(field) or 0
                            current = total_usage.get(field)
                            candidate = base + value
                            total_usage[field] = (
                                candidate if current is None
                                else max(current, candidate)
                            )

                async with sandbox.exec(ExecSpec(
                    argv=_build_cmd(turn, is_first=is_first),
                    cwd=str(workspace),
                    env=subprocess_env,
                    turn_id=getattr(turn, "turn_id", None),
                    env_keep=frozenset(owned_env),
                )) as proc:
                    turn_proc = proc
                    try:
                        await asyncio.wait_for(
                            _consume(), timeout=deadline.remaining(),
                        )
                        await asyncio.wait_for(turn_proc.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        error_message = f"timeout after {task.timeout_seconds}s"

                # 错误消息优先级：timeout（已置）> turn.failed 权威事件 > stderr 尾行
                if not error_message and turn_failed_message:
                    error_message = turn_failed_message[:500]

                if turn_proc.stderr:
                    stderr = (
                        await turn_proc.stderr.read()
                    ).decode("utf-8", errors="replace").strip()
                    if stderr:
                        (attempt_dir / "stderr.txt").write_text(
                            stderr, encoding="utf-8",
                        )
                        if (
                            not error_message
                            and turn_proc.returncode
                            and turn_proc.returncode != 0
                        ):
                            error_message = stderr[:500]

            completed_all_turns = False
            for index, turn in enumerate(plan.send_message_turns):
                current_turn_id = None if plan.is_legacy else turn.turn_id
                current_turn_index = None if plan.is_legacy else turn.turn_index
                try:
                    deadline.check_before_turn()
                except AttemptBudgetExhausted:
                    # 轮间预算耗尽：不再 spawn 下一个进程
                    error_message = (
                        error_message or f"timeout after {task.timeout_seconds}s"
                    )
                    conversation_trace.turn_failed(
                        turn, producer_session_id=codex_thread_id,
                        error_code=ERROR_BUDGET_EXHAUSTED_BETWEEN_TURNS,
                        error_summary=error_message,
                    )
                    break
                conversation_trace.turn_started(
                    turn, producer_session_id=codex_thread_id,
                )
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
                        turn, producer_session_id=codex_thread_id,
                        error_code=(
                            "session_continuity_broken"
                            if "session_continuity_broken" in error_message
                            else None
                        ),
                        error_summary=error_message,
                    )
                    break
                conversation_trace.turn_completed(
                    turn, producer_session_id=codex_thread_id,
                )
                if not plan.is_legacy:
                    # turn.completed 之后才落 last_completed
                    # （它是唯一能证明该轮已发送且完成的权威 checkpoint）
                    _checkpoint(
                        active_turn_index=None,
                        last_completed_turn_index=turn.turn_index,
                    )
            else:
                completed_all_turns = True
            handler = task.iteration_turn_handler
            finalized_from_last_successful = False
            dynamic_decision: Any | None = None
            if completed_all_turns and handler is not None:
                with deadline.suspend():
                    dynamic_decision = await handler.on_turn_completed(
                        producer_session_id=codex_thread_id,
                    )
                while dynamic_decision.next_prompt is not None:
                    turn_index = int(dynamic_decision.round_index) + 1
                    dynamic_turn = ConversationTurn(
                        turn_id=f"iteration-{turn_index}",
                        turn_index=turn_index,
                        purpose="task",
                        prompt=dynamic_decision.next_prompt,
                    )
                    current_turn_id = dynamic_turn.turn_id
                    current_turn_index = dynamic_turn.turn_index
                    try:
                        deadline.check_before_turn()
                    except AttemptBudgetExhausted:
                        error_message = (
                            error_message or f"timeout after {task.timeout_seconds}s"
                        )
                        conversation_trace.turn_failed(
                            dynamic_turn,
                            producer_session_id=codex_thread_id,
                            error_code=ERROR_BUDGET_EXHAUSTED_BETWEEN_TURNS,
                            error_summary=error_message,
                        )
                        break
                    handler.mark_feedback_sending(dynamic_decision)
                    conversation_trace.turn_started(
                        dynamic_turn, producer_session_id=codex_thread_id,
                    )
                    handler.mark_feedback_delivered(dynamic_decision)
                    await _run_turn(dynamic_turn, is_first=False)
                    turn_status = _classify_outcome(
                        proc.returncode if proc is not None else None,
                        error_message,
                    )
                    if turn_status != "completed":
                        if not error_message:
                            error_message = (
                                f"turn {dynamic_turn.turn_id!r} 未正常完成"
                                f"（status={turn_status}）"
                            )
                        conversation_trace.turn_failed(
                            dynamic_turn,
                            producer_session_id=codex_thread_id,
                            error_code=(
                                "session_continuity_broken"
                                if "session_continuity_broken" in error_message
                                else None
                            ),
                            error_summary=error_message,
                        )
                        finalized_from_last_successful = (
                            handler.finalize_last_successful_submission()
                        )
                        break
                    conversation_trace.turn_completed(
                        dynamic_turn, producer_session_id=codex_thread_id,
                    )
                    with deadline.suspend():
                        dynamic_decision = await handler.on_turn_completed(
                            producer_session_id=codex_thread_id,
                        )
            if not error_message:
                conversation_trace.conversation_completed()
            if error_message:
                conversation_trace.conversation_failed(
                    error_code=None, error_summary=error_message,
                )
            iteration_refs: dict[str, Any] = {}
            if handler is not None and dynamic_decision is not None:
                iteration_refs["iteration_completed"] = bool(
                    getattr(dynamic_decision, "completed", False)
                    or finalized_from_last_successful
                )
                if finalized_from_last_successful:
                    iteration_refs["iteration_finalized_from_last_successful"] = True
                iteration_refs["iteration_final_round"] = int(
                    getattr(dynamic_decision, "round_index", 0)
                )
            conversation_trace.close()

        except FileNotFoundError:
            return AdapterResult(
                attempt_id=task.attempt_id,
                status="cli_not_found",
                error_code="codex_exec_failed",
                error_message="failed to execute codex CLI",
            )
        except Exception as exc:
            failure_status = str(getattr(exc, "status", "cli_error"))
            failure_code = str(getattr(exc, "error_code", "unexpected_error"))
            # Submission Judge 在候选 turn.completed 之后运行；Judge 失败应落到
            # scoring 轴，不能把已完成的 Codex 候选伪装成执行失败。
            if failure_status == "scoring_failed":
                conversation_trace.conversation_completed()
            else:
                conversation_trace.conversation_failed(
                    error_code=failure_code, error_summary=str(exc),
                )
            conversation_trace.close()
            return AdapterResult(
                attempt_id=task.attempt_id,
                status=failure_status,
                external_refs={
                    "cli_path": cli_path,
                    "cli_version": cli_version,
                    "model_used": self.model,
                    "codex_thread_id": codex_thread_id,
                    "iteration_error_status": failure_status,
                    "iteration_error_code": failure_code,
                },
                error_code=failure_code,
                error_message=str(exc),
                events_count=events_count,
                last_event_at=last_event_at,
                thinking_count=thinking_count,
                tool_call_count=tool_call_count,
                token_usage=compact_usage(total_usage),
                duration_ms=int(
                    (datetime.now(timezone.utc) - started_at).total_seconds() * 1000
                ),
                conversation_summary=summarize_conversation(attempt_dir),
            )

        duration_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
        status = _classify_outcome(proc.returncode if proc else None, error_message)
        return AdapterResult(
            attempt_id=task.attempt_id,
            status=status,
            external_refs={
                "cli_path": cli_path,
                "cli_version": cli_version,
                "model_used": self.model,
                # 权威 thread ID（来自 thread.started）。多轮 resume 用它；
                # 单轮去掉 --ephemeral 后也会落盘会话，thread_id 正常非 None。
                "codex_thread_id": codex_thread_id,
                **iteration_refs,
                # 稳定错误归因：泛化的 cli_error 无法区分 CLI 启动、
                # 配置、模型上游和运行时错误——Apple · DeepSeek 的正式 Run
                # 就是这样只能记成 partial 而查不出所以然。
                **(
                    {}
                    if status == "completed"
                    else classify_cli_error(error_message).as_refs()
                ),
            },
            error_code=(
                None if status == "completed"
                # session 断裂有专用码：它不是普通 cli_error，评测/UI 要能
                # 区分"agent 跑挂了"与"上下文断了、结论不可用"。
                else "session_continuity_broken"
                if error_message and "session_continuity_broken" in error_message
                # 稳定错误码而不是把原始 error_message 当 code 用：后者每次
                # 措辞都不同，无法聚合、无法据此判断是否该重试。
                else classify_cli_error(error_message).code
            ),
            error_message=error_message,
            events_count=events_count,
            last_event_at=last_event_at,
            thinking_count=thinking_count,
            tool_call_count=tool_call_count,
            token_usage=compact_usage(total_usage),
            duration_ms=duration_ms,
            conversation_summary=summarize_conversation(attempt_dir),
            # codex 在宿主机以 bypass-approvals-and-sandbox 跑，cwd=skill_workspace（-C）
            security_meta=build_security_meta(
                execution_locus=self.capabilities.execution_locus,
                permission_mode="--dangerously-bypass-approvals-and-sandbox",
                workspace_root=str(workspace.resolve()),
                extra={
                    **sandbox.security_fields(),
                    "server_side_network": (
                        "disabled" if sandbox.server_side_tools == "deny" else "available"
                    ),
                },
            ),
        )

    def _mcp_command_and_args(self, task: AdapterRunInput, spec: Any) -> tuple[str, list[str]]:
        """MCP server 的最终 command/args；wire mcp rewrite 在此应用。"""
        command = spec.command
        args = list(spec.args)
        rewrite = task.wire_injection.mcp_rewrites.get(spec.name)
        if task.wire_injection.enabled and rewrite is not None:
            args = [*rewrite.args_prefix, command, *args]
            command = rewrite.command
        return command, args

    def _write_mcp_config_snapshot(
        self, task: AdapterRunInput, attempt_dir: Path, *, sandbox: AttemptSandbox | None = None,
    ) -> None:
        servers: dict[str, Any] = {}
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
        config = {"mcp_servers": servers}
        (attempt_dir / "codex_mcp_config.json").write_text(
            json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def _render_prompt(self, task: AdapterRunInput) -> str:
        parts: list[str] = []
        # 时间预算放在最前（codex exec 无独立 system 通道，回落 message 顶部；
        # 语义仍是"框架级约束"，置顶更醒目）。None（不限时）不注入。
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
    # error_message 优先于 returncode：codex 的权威失败事件是 stdout 里的
    # `turn.failed`（如上游 404），此时进程**仍可能以 0 退出**——错误已在
    # JSON 事件里表达完毕。只看 returncode 会把这种 attempt 判成 completed，
    # 失败被静默吞掉（多轮下还会继续发送后续轮）。
    if error_message:
        return "cli_error"
    if returncode == 0:
        return "completed"
    return "cli_error"


def _usage(data: dict[str, Any]) -> dict[str, int | None]:
    """codex 事件里的 usage → 五维（token_cost_accounting）。

    codex 用 `cached_input_tokens` / `reasoning_output_tokens`，且**没有 cache
    write 概念**（该维保持 None = 不可得，不能填 0）。usage 只在
    `turn.completed` 里出现——attempt 被 timeout 截断时该事件永远不会到达，
    五维全 None 是预期结果，不是采集故障（实测 2026-07-27 六平台实验）。
    """
    usage = data.get("usage") or data.get("token_usage") or {}
    return usage_detail(usage if isinstance(usage, dict) else None)


def _message_text(data: Any) -> str:
    if isinstance(data, str):
        return data
    if isinstance(data, list):
        return "\n".join(filter(None, (_message_text(x) for x in data)))
    if not isinstance(data, dict):
        return ""
    if isinstance(data.get("text"), str):
        return data["text"]
    if isinstance(data.get("content"), str):
        return data["content"]
    return "\n".join(filter(None, (_message_text(v) for v in data.values())))


def _looks_like_reasoning(data: dict[str, Any]) -> bool:
    text = json.dumps(data, ensure_ascii=False).lower()
    return "reasoning" in text or "thinking" in text


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _append_jsonl(path: Path, data: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, default=str) + "\n")
