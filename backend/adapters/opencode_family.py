"""OpencodeFamilyAdapter — 通过 opencode / mimo CLI 子进程调用。

**为什么一个 adapter 承载两个 agent**：mimo（MiMo Code，小米）是 opencode 的
下游 fork，CLI 契约逐位同构——`run --format json` 的事件流、`sessionID`、
`part.text`、`tokens` 字段、`--session` resume、config/auth 的 `*_CONFIG` /
`*_CONFIG_DIR` 环境变量全部一致，只有可执行文件名与环境变量前缀不同
（`OPENCODE_*` vs `MIMOCODE_*`）。为两者各写一份会让同一段解析逻辑双份维护、
双份漂移；这里用 `_FamilyProfile` 把差异收敛成三个字段（executable /
env_prefix / agent_name），其余共用。

实测契约（2026-07-27，49 机器 opencode 1.18.5 / mimo 0.1.9）：

    {"type":"step_start","sessionID":"ses_...","part":{...}}
    {"type":"text","sessionID":"ses_...","part":{"type":"text","text":"PONG",...}}
    {"type":"step_finish","sessionID":"ses_...","part":{"tokens":{"input":N,"output":M,...}}}

注意与 agent-arena AgentSpec 的差异：arena 的 spec 写了
`--dangerously-skip-permissions`，实测 `run` 子命令**没有**这个 flag
（opencode 的 `run` 有 `--auto`，mimo 的 `run` 两个都没有）——mimo 的自动批准
只能走 `MIMOCODE_DANGEROUSLY_SKIP_PERMISSIONS` 环境变量。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass
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
from ..process.runtime import agent_process
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

# opencode/mimo 的 provider 走 AI SDK 的 openai-compatible 包；provider id 固定
# 用这个名字，模型在 argv 里以 `<PROVIDER_ID>/<model>` 引用。
_PROVIDER_ID = "octagon"


@dataclass(frozen=True)
class _FamilyProfile:
    """opencode 与其 fork 之间**仅有**的差异。新增同族 CLI 只加一条。

    `auto_approve_flag` 是 fork 分叉得最厉害的一处：同一个语义（自动批准工具
    调用）两边取了不同的 flag 名。**必须传**——不传的话每个工具调用都会被拒
    （events 里是 "The user rejected permission to use this specific tool
    call."），agent 跑完整个 loop 却一个文件都没落，最终静默得 0 分。
    """

    agent_name: str
    executable: str
    env_prefix: str
    auto_approve_flag: str

    @property
    def config_env(self) -> str:
        return f"{self.env_prefix}_CONFIG"

    @property
    def config_dir_env(self) -> str:
        return f"{self.env_prefix}_CONFIG_DIR"


FAMILY_PROFILES: dict[str, _FamilyProfile] = {
    "opencode": _FamilyProfile(
        agent_name="opencode", executable="opencode", env_prefix="OPENCODE",
        auto_approve_flag="--auto",
    ),
    "mimo-code": _FamilyProfile(
        agent_name="mimo-code", executable="mimo", env_prefix="MIMOCODE",
        auto_approve_flag="--dangerously-skip-permissions",
    ),
}


class OpencodeFamilyAdapter:
    # 能力静态声明：见 AdapterCapabilities docstring。
    capabilities = AdapterCapabilities(
        execution_locus="host",
        network_required="public_internet",
        # `run` 是一次性非交互执行，无运行中交互应答通道（与 codex exec 同）。
        interaction_answer=False,
    )

    def __init__(
        self,
        *,
        agent_name: str,
        model: str,
        octagon_project_path: str | Path = ".",
        providers: dict[str, ModelProviderSection] | None = None,
    ) -> None:
        if agent_name not in FAMILY_PROFILES:
            raise ValueError(f"未知的 opencode 系 agent: {agent_name!r}")
        self.profile = FAMILY_PROFILES[agent_name]
        self.model = model
        self.octagon_project_path = str(Path(octagon_project_path).resolve())
        self.providers = providers or {}

    @property
    def wire_capture_capabilities(self) -> dict[str, Any]:
        """wire injection 消费能力声明（lifecycle 在 agent 启动前据此过滤）。

        base URL 与 headers 都由本 adapter 生成的 config JSON 写入
        （`provider.options.baseURL` / `provider.options.headers`），
        故两者都可消费。
        """
        return {
            "process_env": True,
            "llm_base_url": True,
            "llm_headers": True,
            "mcp_rewrites": True,
        }

    # ---------- 配置生成 ----------

    def _provider_block(
        self,
        model_ref: ModelRef,
        base_url: str,
        api_key: str | None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """openai-compatible provider 声明。

        模型必须显式列在 `models` 里——CLI 只认注册过的模型 id，未声明的会在
        启动时报 unknown model 而不是回落默认。
        """
        options: dict[str, Any] = {"baseURL": base_url}
        if api_key:
            options["apiKey"] = api_key
        if headers:
            options["headers"] = dict(headers)
        return {
            "npm": "@ai-sdk/openai-compatible",
            "name": f"Octagon {model_ref.provider or 'default'}",
            "options": options,
            "models": {model_ref.model: {"name": model_ref.model}},
        }

    def _write_config(
        self,
        task: AdapterRunInput,
        attempt_dir: Path,
        model_ref: ModelRef,
    ) -> tuple[Path, str | None]:
        """生成本次 attempt 专用的配置文件，返回 (路径, api_key)。

        provider / MCP 都写进这里：CLI 没有等价的 `-c key=value` 单次覆盖通道
        （codex 有），配置文件是唯一的单次注入点。文件落在 attempt 目录内，
        天然按 attempt 隔离，且不碰宿主机 ~/.config/<cli>。
        """
        config: dict[str, Any] = {"$schema": "https://opencode.ai/config.json"}
        api_key: str | None = None

        if model_ref.provider is not None:
            provider = self.providers[model_ref.provider]
            # 成本核算：run 专属 key 优先，回落 provider 配置。
            # base_url 必须与 key 同源，否则拿 run key 打向旧 endpoint。
            api_key, _used_run_key = resolve_agent_key(
                task.run_id, resolve_api_key(provider), task.attempt_id,
                provider.base_url,
            )
            base_url = resolve_agent_base_url(task.run_id, provider.base_url, task.attempt_id)
            # wire 消费点：injection 覆盖本次 provider base URL。
            headers: dict[str, str] = {}
            if task.wire_injection.enabled:
                if task.wire_injection.llm_base_url:
                    base_url = task.wire_injection.llm_base_url
                headers.update(task.wire_injection.llm_headers)
                # capture token 走独立头（不占 Authorization——那被 provider
                # auth 占用且反代会剥）。缺了它反代直接 401 capture token
                # required，attempt 一个字都跑不出来。
                if task.wire_injection.capture_token:
                    headers["X-Octagon-Capture-Token"] = (
                        task.wire_injection.capture_token
                    )
            config["provider"] = {
                _PROVIDER_ID: self._provider_block(
                    model_ref, base_url, api_key, headers or None
                )
            }

        if task.mcp_servers:
            # opencode 的 MCP dialect：mcp.<name> = {type:"local", command:[...]}
            # ——command 是**单个数组**（含可执行文件本体），与 CC/codex 的
            # command+args 分开表达不同，转换在这里做。
            mcp: dict[str, Any] = {}
            for spec in task.mcp_servers:
                command, args = self._mcp_command_and_args(task, spec)
                entry: dict[str, Any] = {
                    "type": "local",
                    "command": [command, *args],
                    "enabled": True,
                    "environment": {
                        "OCTAGON_ATTEMPT_ID": task.attempt_id,
                        "OCTAGON_ENV_TOKEN": task.env_token,
                        "OCTAGON_BASE_URL": task.env_base_url,
                    },
                }
                mcp[spec.name] = entry
            config["mcp"] = mcp

        path = attempt_dir / f"{self.profile.executable}_config.json"
        path.write_text(
            json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return path, api_key

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
        attempt_dir = data_path / "attempts" / task.attempt_id
        attempt_dir.mkdir(parents=True, exist_ok=True)
        # agent 工作区：与 CC/codex/blade 同构，产物落这里，attempt 根目录留给框架。
        workspace = attempt_dir / "skill_workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        events_path = attempt_dir / "events.jsonl"
        thinking_path = attempt_dir / "thinking.jsonl"

        cli_path = shutil.which(self.profile.executable)
        if not cli_path:
            return AdapterResult(
                attempt_id=task.attempt_id,
                status="cli_not_found",
                error_code=f"{self.profile.executable}_not_in_path",
                error_message=f"{self.profile.executable} CLI not found in PATH",
            )

        model_ref = parse_model_ref(self.model, self.providers)
        # key 缺失时 CLI 只会在上游报 401，错误面目全非——这里 fail fast。
        if model_ref.provider is not None:
            provider = self.providers[model_ref.provider]
            # 判定必须走 resolve_agent_key（合并 cost 的 run/attempt 专属 key），
            # 不能只看静态 resolve_api_key：开了 cost.enabled 且无静态
            # OPENROUTER_API_KEY 时，key 只存在于动态 run key 里，裸
            # resolve_api_key 会误判缺 key。与 kimi_code 一致。
            _probe_key, _ = resolve_agent_key(
                task.run_id, resolve_api_key(provider), task.attempt_id,
                provider.base_url,
            )
            if provider.api_key_env and _probe_key is None:
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

        config_path, _api_key = self._write_config(task, attempt_dir, model_ref)
        prompt = self._render_prompt(task)

        # argv 里引用模型时必须带 provider id 前缀；无命名 provider 时原样透传
        # 用户给的模型串（走 CLI 自己已配置的 provider）。
        cli_model = (
            f"{_PROVIDER_ID}/{model_ref.model}"
            if model_ref.provider is not None
            else self.model
        )

        plan = effective_conversation(task)
        session_id: str | None = None
        checkpoint_state: dict[str, Any] = {
            "cli_path": cli_path,
            "agent": self.profile.agent_name,
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

            后续轮用 `--session <id>` 显式续跑；**禁止 `--continue`**——它挑
            "最近一次会话"，并发多 attempt 时会串到别人的 session 上
            （与 codex 禁用 `--last` 同理）。
            """
            turn_prompt = render_turn_prompt(task, turn, base_prompt=prompt)
            args = [
                cli_path, "run", "--format", "json",
                self.profile.auto_approve_flag,
                # `--dir` 必须显式给（对齐 codex 的 `-C`）：**只设 subprocess 的
                # cwd 不够**——opencode 系会自行向上探测 project root，于是把
                # Octagon 仓库根当成工作目录。实测 2026-07-28：agent 去读
                # `<repo>/REQUIREMENT.md` 失败后 glob 到上一个实验残留在仓库根
                # 的 `SPEC.md`（火山喷发规格），把**别的实验的题目**当成本次
                # 需求——跨实验串题，评测结果直接失去意义。
                "--dir", str(workspace.resolve()),
                "-m", cli_model,
            ]
            if not is_first:
                if not session_id:
                    raise RuntimeError(
                        f"{self.profile.agent_name} 多轮续跑缺 session ID："
                        "首轮未从事件流读到 sessionID"
                    )
                args += ["--session", session_id]
            # `--` 之后是 message 位置参数；prompt 以 `-` 开头时不会被当成 flag。
            args += ["--", turn_prompt]
            return args

        events_count = 0
        thinking_count = 0
        # 工具调用数原先从未统计（与 cc/codex 不同），头部统计恒显示 0。按 callID
        # 去重：同一次调用会随状态推进重复出现多条 part 事件。
        tool_call_count = 0
        seen_tool_call_ids: set[str] = set()
        # 五维累计（token_cost_accounting）：None = 该 producer 没报过这个字段。
        total_usage: dict[str, int | None] = empty_usage()
        last_event_at: str | None = None
        error_message: str | None = None
        started_at = datetime.now(timezone.utc)
        proc = None

        # 本地状态隔离：config/data 目录指向 attempt 内的干净目录，CLI 不读宿主机
        # 全局配置（provider 凭据、skills、plugins、session 历史），排除私人配置
        # 污染结果；CLI 产品内建能力不在这里禁用。
        iso_home = attempt_dir / f".{self.profile.executable}-iso-home"
        iso_config_dir = iso_home / "config"
        iso_config_dir.mkdir(parents=True, exist_ok=True)
        subprocess_env = {
            **os.environ,
            "HOME": str(iso_home.resolve()),
            "XDG_CONFIG_HOME": str((iso_home / ".config").resolve()),
            "XDG_DATA_HOME": str((iso_home / ".local" / "share").resolve()),
            "XDG_CACHE_HOME": str((iso_home / ".cache").resolve()),
            "XDG_STATE_HOME": str((iso_home / ".local" / "state").resolve()),
            self.profile.config_env: str(config_path.resolve()),
            self.profile.config_dir_env: str(iso_config_dir.resolve()),
            # 评测期间禁用自动升级：跑到一半换 CLI 版本会让同批次不可比。
            f"{self.profile.env_prefix}_DISABLE_AUTOUPDATE": "1",
        }
        if task.mcp_servers:
            subprocess_env.update({
                "OCTAGON_ATTEMPT_ID": task.attempt_id,
                "OCTAGON_ENV_TOKEN": task.env_token,
                "OCTAGON_BASE_URL": task.env_base_url,
            })
        # wire injection 消费点：provider/MCP 已写进 config，这里合并 process_env。
        if task.wire_injection.enabled:
            subprocess_env.update(task.wire_injection.process_env)
            if task.wire_injection.capture_token:
                subprocess_env["OCTAGON_WIRE_CAPTURE_TOKEN"] = (
                    task.wire_injection.capture_token
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

        try:
            async def _run_turn(turn: Any, *, is_first: bool) -> None:
                nonlocal proc, events_count, thinking_count, last_event_at
                nonlocal tool_call_count
                nonlocal total_usage
                nonlocal error_message, session_id

                # token 语义：每个 run 进程内部累计、进程之间独立（与 codex 同），
                # 故跨轮用「轮基线 + 本轮值」，直接 max 会让后续轮覆盖前面轮。
                turn_base = dict(total_usage)
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

                        # 工具调用计数：与归一器 `_opencode_calls` 认同一形状
                        # （part.type == "tool"），保持两处口径一致。
                        part = data.get("part")
                        if (
                            data.get("type") == "tool_use"
                            and isinstance(part, dict)
                            and part.get("type") == "tool"
                        ):
                            call_id = str(part.get("callID") or "")
                            key = call_id or f"event:{events_count}"
                            if key not in seen_tool_call_ids:
                                seen_tool_call_ids.add(key)
                                tool_call_count += 1

                        # 权威 session ID：只从 producer 事件读，不自己派生。
                        sid = data.get("sessionID")
                        if isinstance(sid, str) and sid:
                            if session_id is None:
                                session_id = sid
                                _checkpoint(session_id=sid)
                            elif sid != session_id:
                                # resume 却拿到**不同** session：上下文已断
                                # 。不能只记 warning 就覆盖——那会让
                                # attempt 静默切到新会话、最终在断裂上下文上评分。
                                broken = (
                                    f"session_continuity_broken: 期望 session "
                                    f"{session_id}，resume 返回 {sid}"
                                )
                                logger.error(
                                    "%s attempt=%s %s",
                                    self.profile.agent_name, task.attempt_id, broken,
                                )
                                if not error_message:
                                    error_message = broken
                                # 保留**原** session ID：它才是这个 attempt 的身份。

                        part = data.get("part")
                        part = part if isinstance(part, dict) else {}
                        etype = data.get("type")

                        # reasoning 事件 → thinking.jsonl（与 CC/codex 同构）。
                        if etype == "reasoning" or part.get("type") == "reasoning":
                            text = part.get("text")
                            if isinstance(text, str) and text:
                                thinking_count += 1
                                _append_jsonl(thinking_path, {
                                    "timestamp": ts,
                                    "sequence": thinking_count,
                                    "content": text,
                                    "type": "thinking",
                                })

                        # 失败事件：CLI 用 type=error / part.type=error 表达上游
                        # 错误（如 401/404），此时进程**仍可能 0 退出**。
                        if etype == "error" or part.get("type") == "error":
                            msg = _error_text(data)
                            if msg and not error_message:
                                error_message = msg[:500]

                        tokens = part.get("tokens")
                        if isinstance(tokens, dict):
                            # 轮内取 max（同进程内是递增快照），跨轮加基线。
                            # 五维逐字段独立做，缺字段保持 None（不可得）。
                            turn_detail = _tokens_detail(tokens)
                            for field, value in turn_detail.items():
                                if value is None:
                                    continue
                                base = turn_base.get(field) or 0
                                current = total_usage.get(field)
                                candidate = base + value
                                total_usage[field] = (
                                    candidate if current is None
                                    else max(current, candidate)
                                )

                async with agent_process(
                    argv=_build_cmd(turn, is_first=is_first),
                    data_path=data_path,
                    attempt_id=task.attempt_id,
                    cwd=str(workspace),
                    env=subprocess_env,
                ) as proc:
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
                error_code=f"{self.profile.executable}_exec_failed",
                error_message=f"failed to execute {self.profile.executable} CLI",
            )
        except Exception as exc:
            return AdapterResult(
                attempt_id=task.attempt_id,
                status="cli_error",
                error_code="unexpected_error",
                error_message=str(exc),
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
                # 稳定错误归因：Smoke · Luna 的 Mimo 撞上 Azure upstream
                # error 时，终态没有说明该错误是否可重试、重试了几次或该改
                # 哪类配置。分类让 upstream_error / configuration_error /
                # adapter_error 在 error_code 层就能分开。
                **(
                    {}
                    if status == "completed"
                    else classify_cli_error(error_message).as_refs()
                ),
            },
            error_code=(
                None if status == "completed"
                else "session_continuity_broken"
                if error_message and "session_continuity_broken" in error_message
                else classify_cli_error(error_message).code
            ),
            error_message=error_message,
            events_count=events_count,
            tool_call_count=tool_call_count,
            last_event_at=last_event_at,
            thinking_count=thinking_count,
            token_usage=compact_usage(total_usage),
            duration_ms=duration_ms,
            conversation_summary=summarize_conversation(attempt_dir),
            security_meta=build_security_meta(
                execution_locus=self.capabilities.execution_locus,
                permission_mode=self.profile.auto_approve_flag,
                workspace_root=str(workspace.resolve()),
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
    # error_message 优先于 returncode：上游错误经 JSON 事件表达时进程仍可能 0
    # 退出，只看 returncode 会把失败判成 completed（与 codex 同）。
    if error_message:
        return "cli_error"
    if returncode == 0:
        return "completed"
    return "cli_error"


def _tokens_detail(tokens: dict[str, Any]) -> dict[str, int | None]:
    """opencode 系的 `part.tokens` → 五维（token_cost_accounting）。

    实测形状（49，2026-07-27）：
        {"total":39116,"input":30688,"output":71,"reasoning":8357,
         "cache":{"write":0,"read":0}}

    与 OpenAI/Anthropic 都不同（cache 是嵌套对象、字段名是裸 input/output），
    故不走共用的 `usage_detail()`，在这里单独翻译。缺字段填 None 而非 0——
    None 是"不可得"，0 是"确实为零"，两者在计价时含义不同。
    """
    def _int(v: Any) -> int | None:
        if v is None:
            return None
        try:
            return max(0, int(v))
        except (TypeError, ValueError):
            return None

    cache = tokens.get("cache")
    cache = cache if isinstance(cache, dict) else {}
    output = _int(tokens.get("output"))
    reasoning = _int(tokens.get("reasoning"))
    return {
        "input_tokens": _int(tokens.get("input")),
        # opencode's total = input + output + reasoning: output and reasoning
        # are disjoint. Canonical output_tokens includes the billable total.
        "output_tokens": (
            output + reasoning
            if output is not None and reasoning is not None
            else output if output is not None
            else reasoning
        ),
        "cache_read_tokens": _int(cache.get("read")),
        "cache_write_tokens": _int(cache.get("write")),
        "reasoning_tokens": reasoning,
    }


def _error_text(data: dict[str, Any]) -> str:
    """从 error 事件里取人类可读消息，取不到就退回整行 JSON。

    实测（49，2026-07-27）顶层 error 事件的正文藏在 `error.data.message`：
        {"type":"error","error":{"name":"UnknownError",
         "data":{"message":"Model not found: ..."}}}
    只看 `part.message` / 顶层 `message` 会漏掉它，错误退化成整行 JSON。
    """
    part = data.get("part")
    if isinstance(part, dict):
        for key in ("message", "text", "error"):
            v = part.get(key)
            if isinstance(v, str) and v:
                return v
    err = data.get("error")
    if isinstance(err, dict):
        inner = err.get("data")
        if isinstance(inner, dict):
            v = inner.get("message")
            if isinstance(v, str) and v:
                return v
        v = err.get("message")
        if isinstance(v, str) and v:
            return v
    for key in ("message", "error"):
        v = data.get(key)
        if isinstance(v, str) and v:
            return v
    return json.dumps(data, ensure_ascii=False)


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _append_jsonl(path: Path, data: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, default=str) + "\n")
