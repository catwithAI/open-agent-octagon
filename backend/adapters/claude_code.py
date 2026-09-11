"""ClaudeCodeAdapter — 通过 claude CLI 子进程调用 Claude Code。

核心流程：
1. 生成临时 MCP 配置文件（含 attempt_id / env_token / base_url 环境变量）
2. spawn `claude -p "{prompt}" --output-format stream-json --verbose --mcp-config {config}`
3. 逐行解析 stdout JSONL，采集 thinking / usage / events
4. env trace 由 env attempt server 自动记录（与 blade adapter 共用路径）

stdout stream-json 格式（调研确认）：
- type=system subtype=init: 初始化信息
- type=assistant: LLM turn，message.content[] 含 text/thinking/tool_use/tool_result blocks
- type=result: 最终汇总，含 usage / total_cost_usd / num_turns
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..conversation.deadline import (
    ERROR_BUDGET_EXHAUSTED_BETWEEN_TURNS,
    AttemptBudgetExhausted,
    AttemptDeadline,
)
from ..conversation.plan import effective_conversation
from ..cost.credential import resolve_agent_base_url, resolve_agent_key
from ..conversation.summary import summarize_conversation
from ..conversation.turns import render_turn_prompt, with_turn_ext
from ..conversation.writer import CONVERSATION_FILENAME, ConversationTraceWriter
from ..model_providers import (
    ModelProviderSection,
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
    ConversationTurn,
    build_security_meta,
    prompt_context,
    time_budget_notice,
)
from .token_usage import (
    estimate_tokens_from_event,
    compact_usage,
    empty_usage,
    result_usage_tokens,
    usage_detail,
)

logger = logging.getLogger(__name__)


# attempt_id → CC session UUID 的确定性命名空间：恢复/重建时可复算，
# 不用随机值。固定常量，改动会让历史 attempt 的 session ID 变化。
_CC_SESSION_NAMESPACE = uuid.UUID("6f9d3a4e-6d0f-4a1e-9a5a-2f6b1c8d7e30")



def _clear_stale_cc_session(iso_home: Path, session_id: str) -> None:
    """删掉隔离 home 里该 session ID 的残留记录（同 attempt 重跑用）。

    只在 attempt 专属的 iso_home 内按**精确文件名**匹配删除，不递归删目录、
    不触碰其它 session——宿主机全局 ~/.claude 更不在范围内
    （"cleanup 仅操作精确 attempt 目录"）。找不到就静默返回。
    """
    claude_dir = iso_home / ".claude"
    if not claude_dir.is_dir():
        return
    for path in claude_dir.rglob(f"{session_id}.jsonl"):
        try:
            path.unlink()
            logger.info("清理同 attempt 重跑残留的 CC session 文件: %s", path)
        except OSError as exc:  # pragma: no cover - 权限/竞态兜底
            logger.warning("清理 CC session 文件失败 %s: %s", path, exc)



class ClaudeCodeAdapter:
    # 能力静态声明：execution_locus 是 build_security_meta 的
    # 权威取值来源；network_required/system_requires 只声明不消费。
    agent_name = "claude-code"
    capabilities = AdapterCapabilities(
        execution_locus="host",
        network_required="public_internet",
        # `claude -p` 无运行中交互应答通道：含 answer_interaction
        # turn 的 conversation 由 dispatch 在启动前拒绝，不静默忽略该轮。
        interaction_answer=False,
        iterative_session=True,
    )

    def __init__(
        self,
        *,
        model: str = "sonnet",
        max_budget_usd: float = 5.0,
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
        self.max_budget_usd = max_budget_usd
        self.octagon_project_path = str(Path(octagon_project_path).resolve())
        self.providers = providers or {}

    @property
    def wire_capture_capabilities(self) -> dict[str, Any]:
        """wire injection 消费能力声明（lifecycle 在 agent 启动前据此过滤）。"""
        return {
            "process_env": True,
            "llm_base_url": True,
            "llm_headers": True,
            "mcp_rewrites": True,
        }

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

        cli_path = sandbox.resolve_cli("claude")
        if not cli_path:
            return AdapterResult(
                attempt_id=task.attempt_id,
                status="cli_not_found",
                error_code="claude_not_in_path",
                error_message="claude CLI not found in PATH",
            )

        # mcp_config 落到只读交付目录：宿主机执行就是 attempt 根（与历史一致），
        # 沙盒执行是 sandbox_ro/（容器内 /attempt），路径经 sandbox.path 翻译。
        mcp_config_path = self._write_mcp_config(
            task, sandbox.host_ro(attempt_dir), sandbox=sandbox,
        )
        prompt = self._render_prompt(task, attempt_dir)
        # 时间预算（测单位时间能力上限）走 CC 原生 --append-system-prompt，
        # 语义是"框架级约束"而非任务本身；None（不限时）返回 None，不注入。
        # 注：codex/blade 无对等 system 通道，回落到 message 顶部（见各自 adapter）。
        budget_notice = (
            time_budget_notice(task.timeout_seconds)
            if task.notify_model_of_timeout
            else None
        )
        # provider 前缀模型 → 子进程独立 env 指向第三方端点，不改全局 settings.json，
        # 并发多 session 互不干扰；--model 传拆分后的模型名（CLI 不认 provider/ 前缀）。
        # 认证 env 由 provider.auth_mode 决定：bearer 用
        # ANTHROPIC_AUTH_TOKEN（CLI 发 Authorization: Bearer）、api-key 用
        # ANTHROPIC_API_KEY（CLI 发 x-api-key）——两者对应不同 HTTP 认证协议，
        # 互斥注入、不做 token 值转写。默认 bearer（blade llm-gateway 等网关只认
        # Bearer，实测 x-api-key 会被 401）。
        model_ref = parse_model_ref(self.model, self.providers)
        subprocess_env = {**os.environ}
        # 本地状态隔离：把 CLAUDE_CONFIG_DIR / HOME 指向 attempt 内的干净空目录，
        # CC 就不会读宿主机 ~/.claude 的全局 skill/plugin/MCP/记忆/CLAUDE.md/settings
        # 避免同事私人配置污染结果。每个 attempt 一个独立目录，互不干扰；
        # Claude Code 产品内建的原生能力不在这里禁用。
        iso_home = sandbox.host_home(attempt_dir / ".cc-iso-home")
        (iso_home / ".claude").mkdir(parents=True, exist_ok=True)
        subprocess_env["CLAUDE_CONFIG_DIR"] = sandbox.path(iso_home / ".claude")
        subprocess_env["HOME"] = sandbox.path(iso_home)
        # 服务端工具（WebSearch / WebFetch 在 Anthropic 侧执行，沙盒管不到）：
        # sandbox.server_side_tools=deny 时经 settings 禁用；两态都记进 security_meta。
        server_side_network = "available"
        if sandbox.server_side_tools == "deny":
            _write_server_side_tools_deny(iso_home / ".claude")
            server_side_network = "disabled"
        if model_ref.provider is not None:
            provider = self.providers[model_ref.provider]
            subprocess_env["ANTHROPIC_BASE_URL"] = resolve_agent_base_url(
                task.run_id, provider.base_url, task.attempt_id
            )
            # 成本核算：run 专属 key 优先，回落 provider 配置。
            # run 内 N 个 session 与 judge 共用这把 key，其累计实扣差值就是
            # 本次 run 的资金口径。回落时 used_run_key=False，审计据此降级为
            # 共享 key 上界——**不能只取 key 而丢掉这个标志**，否则会把含背景
            # 流量的上界静默标成可审计实扣。
            api_key, _used_run_key = resolve_agent_key(
                task.run_id, resolve_api_key(provider), task.attempt_id,
                provider.base_url,
            )
            # key 缺失时 CLI 只会报「Not logged in · Please run /login」，
            # 错误面目全非——这里 fail fast 给出可操作的定位信息。
            if api_key is None and provider.api_key_env:
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
            if api_key:
                if provider.effective_auth_mode() == "api-key":
                    subprocess_env["ANTHROPIC_API_KEY"] = api_key
                    subprocess_env.pop("ANTHROPIC_AUTH_TOKEN", None)
                else:
                    subprocess_env["ANTHROPIC_AUTH_TOKEN"] = api_key
                    subprocess_env.pop("ANTHROPIC_API_KEY", None)
            if provider.custom_headers:
                subprocess_env["ANTHROPIC_CUSTOM_HEADERS"] = provider.custom_headers
        # wire injection 消费点（表）：构造 subprocess_env 后、启动
        # subprocess 前。injection 已由 lifecycle 合并/校验（保留名、secret），
        # 这里只消费不再校验。
        wi = task.wire_injection
        if wi.enabled:
            subprocess_env.update(wi.process_env)
            if wi.llm_base_url:
                subprocess_env["ANTHROPIC_BASE_URL"] = sandbox.translate_url(
                    wi.llm_base_url
                )
            extra_headers = dict(wi.llm_headers) if wi.llm_headers else {}
            # capture token：必须进**真实 HTTP 请求头**——CLI 不会
            # 自动把 env var 转成 header，只有 ANTHROPIC_CUSTOM_HEADERS 里的头才会
            # 随请求发给（反代的）base URL。走独立 X-Octagon-Capture-Token（不占
            # Authorization——那被 provider auth 占用且反代会剥）。也留一份 env 供
            # 排查/其它用途。
            if wi.capture_token:
                extra_headers["X-Octagon-Capture-Token"] = wi.capture_token
                subprocess_env["OCTAGON_WIRE_CAPTURE_TOKEN"] = wi.capture_token
            if extra_headers:
                subprocess_env["ANTHROPIC_CUSTOM_HEADERS"] = _merge_custom_headers(
                    subprocess_env.get("ANTHROPIC_CUSTOM_HEADERS"), extra_headers
                )
        # conversation plan：单轮任务 → 一个 legacy turn，命令行与改造前
        # 完全一致；多轮 → 首轮 --session-id 建会话、后续 --resume 同一 ID。
        plan = effective_conversation(task)
        session_required = (
            (not plan.is_legacy) or task.iteration_turn_handler is not None
        )
        # attempt 派生的确定性 UUID：不用随机值，恢复/重建时可复算；CLI 要求
        # 标准 UUID 格式，用 uuid5 保证形状合法。
        cc_session_id = str(uuid.uuid5(_CC_SESSION_NAMESPACE, task.attempt_id))
        if session_required:
            # session ID 由 attempt_id 确定性派生，同一 attempt 重跑（失败重试、
            # 恢复、手工重放）会撞上上次留在隔离 home 里的记录，CLI 直接报
            # "Session ID ... is already in use" 且一个字都不执行。隔离 home 是
            # attempt 专属目录，里面这个 ID 的残留只可能来自本 attempt 的上一次
            # 运行，清掉是安全的——不会碰到别的 attempt 或宿主机会话。
            _clear_stale_cc_session(iso_home, cc_session_id)

        def _build_cmd(turn: Any, *, is_first: bool) -> list[str]:
            """构造某一轮的 argv。

            首轮显式指定 session ID，后续轮 resume 同一个——**禁止
            `--continue`**（它选"最近一次会话"，并发多 attempt 时会串到别人的
            会话上）。
            """
            turn_prompt = render_turn_prompt(
                task, turn, base_prompt=prompt,
            )
            cmd = [
                cli_path,
                "-p", turn_prompt,
                "--output-format", "stream-json",
                "--verbose",
                "--model", model_ref.model,
                "--max-budget-usd", str(self.max_budget_usd),
                "--dangerously-skip-permissions",
                # 子 agent 可观测性的必要条件：不加这个 flag，Task
                # 工具派生的子 agent 的文本/thinking/usage 事件不会进 stream-json，
                # 子 agent 压缩实验就无从判定。需要 CLI 支持（2.1.215 起可用）。
                "--forward-subagent-text",
            ]
            if not session_required:
                # 单轮：保持改造前的 argv，不引入 session 参数（向后兼容）。
                pass
            elif is_first:
                cmd += ["--session-id", cc_session_id]
            else:
                cmd += ["--resume", cc_session_id]
            # 隔离 HOME 只去掉同事本机的私人配置；不禁用 Claude Code 的 WebSearch、
            # Task、skills 等原生能力。场景提供 MCP 时才追加对应配置。
            if mcp_config_path is not None:
                cmd += ["--mcp-config", sandbox.path(mcp_config_path)]
            # 时间预算是 attempt 级框架约束，只在首轮注入——后续轮重复宣称
            # "本任务限时 X" 会误导 agent（与 blade 侧同一原则）。
            if budget_notice and is_first:
                cmd += ["--append-system-prompt", budget_notice]
            return cmd

        events_count = 0
        thinking_count = 0
        # CLI 事件是 DB/UI 工具调用数的事实来源。按 tool_use id 去重：Claude
        # stream-json 可能重复发送同一个 assistant message 的增量/最终事件。
        tool_call_count = 0
        seen_tool_use_ids: set[str] = set()
        # 五维累计（token_cost_accounting）：None = 该 producer 没报过该字段。
        total_usage: dict[str, int | None] = empty_usage()
        estimated_input_tokens = 0
        estimated_output_tokens = 0
        last_event_at: str | None = None
        final_result: dict | None = None
        error_message: str | None = None
        model_used: str | None = None
        started_at = datetime.now(timezone.utc)

        # attempt 总 deadline：多轮共享一个预算，不是每轮重新获得完整时限。
        # 单轮时行为与改造前的 wait_for(timeout=task.timeout_seconds)
        # 等价。
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
        proc: asyncio.subprocess.Process | None = None

        try:
            async def _run_turn(turn: Any, *, is_first: bool) -> None:
                """跑一轮：spawn 一个 claude -p 子进程并消费其 stdout 到结束。"""
                nonlocal proc, events_count, thinking_count, last_event_at
                nonlocal tool_call_count
                nonlocal final_result
                nonlocal estimated_input_tokens, estimated_output_tokens
                nonlocal model_used, error_message

                # 本轮开始时的累计量：result 事件用它作基线重算，避免
                # assistant 累加与 result 权威值两种口径叠加（重复计数）。
                turn_base = dict(total_usage)
                # final_result 是轮级状态，必须每轮清空：否则某轮非零退出、
                # 没发 result 事件、stderr 又为空时，_classify_outcome 会读到
                # **上一轮**的 success result 并在检查 returncode 前返回
                # completed——失败轮被误判成功，后续轮继续跑。
                final_result = None

                async def _consume() -> None:
                    nonlocal events_count, thinking_count, last_event_at
                    nonlocal tool_call_count
                    nonlocal final_result
                    nonlocal estimated_input_tokens, estimated_output_tokens
                    nonlocal model_used
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
                        est_input, est_output = estimate_tokens_from_event(data)
                        estimated_input_tokens += est_input
                        estimated_output_tokens += est_output

                        msg_type = data.get("type")

                        if msg_type == "system" and data.get("subtype") == "init":
                            model_used = data.get("model") or model_used

                        if msg_type == "assistant":
                            message = data.get("message", {})
                            model_used = message.get("model") or model_used
                            for block_index, block in enumerate(message.get("content", [])):
                                if block.get("type") == "tool_use":
                                    # id 是 Claude tool_use 的稳定调用标识；无 id
                                    # 的异常事件按原始事件序号 + block 位置计一次，
                                    # 避免把独立调用错误合并，也避免静默漏计。
                                    tool_id = block.get("id")
                                    dedupe_key = (
                                        f"id:{tool_id}" if tool_id else
                                        f"event:{event_sequence}:block:{block_index}"
                                    )
                                    if dedupe_key not in seen_tool_use_ids:
                                        seen_tool_use_ids.add(dedupe_key)
                                        tool_call_count += 1
                                if block.get("type") == "thinking":
                                    thinking_count += 1
                                    _append_jsonl(thinking_path, {
                                        "timestamp": ts,
                                        "sequence": thinking_count,
                                        "content": block.get("thinking", ""),
                                        "type": "thinking",
                                    })
                            # assistant 事件逐条累加（五维；缺字段保持不可得）。
                            for field, value in usage_detail(
                                message.get("usage", {})
                            ).items():
                                if value is None:
                                    continue
                                total_usage[field] = (total_usage.get(field) or 0) + value

                        elif msg_type == "result":
                            final_result = data
                            # result 的 usage 是**该轮**的权威累计值，用它覆盖本轮
                            # 由 assistant 事件累加出来的估算量（单轮时即改造前的
                            # 赋值语义）。多轮下必须以"轮基线 + 本轮权威值"重算，
                            # 否则两种口径叠加会重复计数：assistant 累加的量不会
                            # 被后续轮的 result 覆盖掉。
                            result_input, result_output = result_usage_tokens(data)
                            if result_input:
                                total_usage["input_tokens"] = (
                                    (turn_base.get("input_tokens") or 0) + result_input
                                )
                            if result_output:
                                total_usage["output_tokens"] = (
                                    (turn_base.get("output_tokens") or 0) + result_output
                                )
                            # cache/reasoning 三维 result 事件同样是轮级权威值。
                            for field, value in usage_detail(
                                data.get("usage") or {}
                            ).items():
                                if field in ("input_tokens", "output_tokens"):
                                    continue
                                if value is None:
                                    continue
                                total_usage[field] = (
                                    (turn_base.get(field) or 0) + value
                                )

                async with sandbox.exec(ExecSpec(
                    argv=_build_cmd(turn, is_first=is_first),
                    cwd=str(workspace),
                    env=subprocess_env,
                    turn_id=getattr(turn, "turn_id", None),
                )) as proc:
                    turn_proc = proc
                    try:
                        await asyncio.wait_for(
                            _consume(), timeout=deadline.remaining(),
                        )
                        await asyncio.wait_for(turn_proc.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        error_message = f"timeout after {task.timeout_seconds}s"

                if turn_proc.stderr:
                    stderr_bytes = await turn_proc.stderr.read()
                    stderr_text = stderr_bytes.decode(
                        "utf-8", errors="replace"
                    ).strip()
                    if stderr_text:
                        (attempt_dir / "stderr.txt").write_text(
                            stderr_text, encoding="utf-8",
                        )
                        if (
                            not error_message
                            and turn_proc.returncode
                            and turn_proc.returncode != 0
                        ):
                            error_message = stderr_text[:500]

            completed_all_turns = False
            for index, turn in enumerate(plan.send_message_turns):
                current_turn_id = None if plan.is_legacy else turn.turn_id
                current_turn_index = None if plan.is_legacy else turn.turn_index
                try:
                    deadline.check_before_turn()
                except AttemptBudgetExhausted:
                    # 轮间预算耗尽：不再 spawn 下一个进程
                    error_message = (
                        error_message
                        or f"timeout after {task.timeout_seconds}s"
                    )
                    conversation_trace.turn_failed(
                        turn, producer_session_id=cc_session_id,
                        error_code=ERROR_BUDGET_EXHAUSTED_BETWEEN_TURNS,
                        error_summary=error_message,
                    )
                    break
                conversation_trace.turn_started(
                    turn, producer_session_id=cc_session_id,
                )
                await _run_turn(turn, is_first=(index == 0))
                # 任一轮失败即终止 conversation，后续轮不再发送。
                # 判定必须复用 _classify_outcome——非零退出码、result.is_error、
                # budget 超限都是失败，只看 error_message 会漏掉它们
                # （stderr 为空的失败进程就是这种情况）。
                turn_status = _classify_outcome(
                    proc.returncode if proc is not None else None,
                    final_result,
                    error_message,
                )
                if turn_status != "completed":
                    if not error_message:
                        error_message = (
                            f"turn {turn.turn_id!r} 未正常完成"
                            f"（status={turn_status}）"
                        )
                    conversation_trace.turn_failed(
                        turn, producer_session_id=cc_session_id,
                        error_code=None, error_summary=error_message,
                    )
                    break
                conversation_trace.turn_completed(
                    turn, producer_session_id=cc_session_id,
                )
            else:
                completed_all_turns = True
            handler = task.iteration_turn_handler
            finalized_from_last_successful = False
            dynamic_decision: Any | None = None
            if completed_all_turns and handler is not None:
                with deadline.suspend():
                    dynamic_decision = await handler.on_turn_completed(
                        producer_session_id=cc_session_id,
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
                            error_message
                            or f"timeout after {task.timeout_seconds}s"
                        )
                        conversation_trace.turn_failed(
                            dynamic_turn,
                            producer_session_id=cc_session_id,
                            error_code=ERROR_BUDGET_EXHAUSTED_BETWEEN_TURNS,
                            error_summary=error_message,
                        )
                        break
                    handler.mark_feedback_sending(dynamic_decision)
                    conversation_trace.turn_started(
                        dynamic_turn, producer_session_id=cc_session_id,
                    )
                    handler.mark_feedback_delivered(dynamic_decision)
                    await _run_turn(dynamic_turn, is_first=False)
                    turn_status = _classify_outcome(
                        proc.returncode if proc is not None else None,
                        final_result,
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
                            producer_session_id=cc_session_id,
                            error_code=None,
                            error_summary=error_message,
                        )
                        finalized_from_last_successful = (
                            handler.finalize_last_successful_submission()
                        )
                        break
                    conversation_trace.turn_completed(
                        dynamic_turn, producer_session_id=cc_session_id,
                    )
                    with deadline.suspend():
                        dynamic_decision = await handler.on_turn_completed(
                            producer_session_id=cc_session_id,
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
                error_code="claude_exec_failed",
                error_message="failed to execute claude CLI",
            )
        except Exception as exc:
            failure_status = str(getattr(exc, "status", "cli_error"))
            failure_code = str(getattr(exc, "error_code", "unexpected_error"))
            # 迭代 Judge 已发生在候选 turn.completed 之后。它失败属于评分轴，
            # conversation/Agent 执行仍然完成，不能降级成 cli_error。
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
                    "session_id": None if not session_required else cc_session_id,
                    "cli_path": cli_path,
                    "model_used": model_used or self.model,
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
        # proc 是最后一轮的进程；轮间预算耗尽时可能一轮都没跑（proc is None），
        # 此时按"无退出码"判定，error_message 已说明原因。
        status = _classify_outcome(
            proc.returncode if proc is not None else None,
            final_result,
            error_message,
        )
        token_usage_estimated = False
        # 估算兜底只针对 input/output 两维：cache/reasoning 无法从文本估，
        # 保持不可得（None）而非伪造成 0。
        if not (total_usage.get("input_tokens") or total_usage.get("output_tokens")) and (
            estimated_input_tokens or estimated_output_tokens
        ):
            total_usage["input_tokens"] = estimated_input_tokens
            total_usage["output_tokens"] = estimated_output_tokens
            token_usage_estimated = True

        return AdapterResult(
            attempt_id=task.attempt_id,
            status=status,
            external_refs={
                # CC 自报的 session_id 是权威值；多轮时它应等于我们指定的
                # cc_session_id（--session-id/--resume 都用它）。
                "session_id": (
                    final_result.get("session_id") if final_result
                    else (None if not session_required else cc_session_id)
                ),
                "cli_path": cli_path,
                "token_usage_estimated": token_usage_estimated,
                "model_used": model_used or self.model,
                **iteration_refs,
            },
            conversation_summary=summarize_conversation(attempt_dir),
            error_code=None if status == "completed" else (error_message or "cli_error"),
            error_message=error_message,
            events_count=events_count,
            last_event_at=last_event_at,
            thinking_count=thinking_count,
            tool_call_count=tool_call_count,
            token_usage=compact_usage(total_usage),
            duration_ms=duration_ms,
            # claude CLI 在宿主机以 skip-permissions 跑，cwd=skill_workspace
            security_meta=build_security_meta(
                execution_locus=self.capabilities.execution_locus,
                permission_mode="--dangerously-skip-permissions",
                workspace_root=str(workspace.resolve()),
                extra={
                    **sandbox.security_fields(),
                    "server_side_network": server_side_network,
                },
            ),
        )

    def _write_mcp_config(
        self, task: AdapterRunInput, attempt_dir: Path, *, sandbox: AttemptSandbox | None = None,
    ) -> Path | None:
        if not task.mcp_servers:
            return None
        servers: dict[str, dict[str, Any]] = {}
        for spec in task.mcp_servers:
            command = spec.command
            args = list(spec.args)
            rewrite = task.wire_injection.mcp_rewrites.get(spec.name)
            if task.wire_injection.enabled and rewrite is not None:
                args = [*rewrite.args_prefix, command, *args]
                command = rewrite.command
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
        config = {"mcpServers": servers}
        path = attempt_dir / "mcp_config.json"
        path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    def _render_prompt(self, task: AdapterRunInput, attempt_dir: Path) -> str:
        # adapter 不指定求解方式；MCP、WebSearch、Bash、Python 等由场景声明和
        # agent 自身能力共同决定。
        parts = [task.task_prompt]
        context = prompt_context(task.task_context) if task.task_context else {}
        if context:
            parts.append("")
            parts.append("上下文：")
            parts.append(json.dumps(context, ensure_ascii=False, indent=2))
        return "\n".join(parts)


# attempt 级动态胜出的 header 前缀：correlation 语义的 header
# 必须是本次 attempt 的值，provider 配置里残留的旧值（如过期的
# X-Eval-Session-Id）会破坏关联；其余静态 header（如 x-user-id）保留静态值。
_DYNAMIC_HEADER_PREFIXES = ("x-eval-", "x-octagon-")


def _merge_custom_headers(existing: str | None, extra: dict[str, str]) -> str:
    """把 injection 的 attempt 级 header 合入 ANTHROPIC_CUSTOM_HEADERS。

    解析既有 "Name: value" 行做大小写不敏感合并：
    - ``x-eval-*`` / ``x-octagon-*``：attempt（injection）值胜出，覆盖静态旧值；
    - 其他同名 header：provider 静态值保留，注入被丢弃而非追加重复行。
    header name/value 的合法性（token、无 CR/LF）已由 lifecycle merge 校验。
    """
    ordered: list[str] = []  # 小写 name，保持首见顺序
    values: dict[str, tuple[str, str]] = {}  # 小写 name → (原样 name, value)
    if existing:
        for line in existing.splitlines():
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            name = name.strip()
            if not name or name.lower() in values:
                continue
            values[name.lower()] = (name, value.strip())
            ordered.append(name.lower())
    for name, value in extra.items():
        key = name.lower()
        if key not in values:
            values[key] = (name, value)
            ordered.append(key)
        elif key.startswith(_DYNAMIC_HEADER_PREFIXES):
            # attempt 级 correlation header 胜出，覆盖静态旧值（保留静态大小写位置）
            values[key] = (values[key][0], value)
        # 其余同名：静态值保留
    return "\n".join(f"{n}: {v}" for n, v in (values[k] for k in ordered))


def _write_server_side_tools_deny(config_dir: Path) -> None:
    """sandbox.server_side_tools=deny：在隔离 CLAUDE_CONFIG_DIR 的 settings.json
    里禁掉服务端联网工具。隔离目录是 attempt 专属，不碰宿主机 ~/.claude。"""
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "settings.json"
    try:
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        current = {}
    permissions = current.setdefault("permissions", {})
    deny = list(permissions.get("deny") or [])
    for tool in ("WebSearch", "WebFetch"):
        if tool not in deny:
            deny.append(tool)
    permissions["deny"] = deny
    path.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")


def _classify_outcome(
    returncode: int | None,
    final_result: dict | None,
    error_message: str | None,
) -> str:
    if error_message and "timeout" in error_message.lower():
        return "timeout"
    if returncode is None:
        return "cli_error"
    if final_result:
        subtype = final_result.get("subtype", "")
        if "budget" in subtype:
            return "timeout"
        if subtype == "success" and not final_result.get("is_error"):
            return "completed"
        if final_result.get("is_error"):
            return "cli_error"
    if returncode != 0:
        return "cli_error"
    return "completed"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _append_jsonl(path: Path, data: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, default=str) + "\n")
