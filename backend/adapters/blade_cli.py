"""BladeCliAdapter — 通过 blade-cli 子进程调用 BA。

与 `blade_service.BladeServiceAdapter` 的关系：两条路都打到同一个 blade server，
但通道不同。

- 老路（SDK）：`blade_agent_kit` + Socket.IO 长连接，实时事件流、token usage、
  断线重连、优先恢复 checkpoint 全都有。
- 新路（本文件）：`blade` 命令行。`blade chat run` 阻塞跑完一轮，终态才吐
  `{session_id, status, messages[]}`；逐步轨迹靠事后 `blade session history
  --json` 重建，产物靠 `blade session copy-dir` 拉 zip。

**刻意接受的数据缺口**（见 docs/agents.md「blade-cli 接入」）：CLI 不暴露
token usage，也不暴露实时事件时序。因此本 adapter 的 `token_usage` 恒为空，
`events.jsonl` 是事后重建的历史节点而非实时流。横评矩阵里 BA 的 token 成本列
会是空的——这是已知取舍，不是采集失败。需要 token 口径时用 BladeServiceAdapter。

blade-cli 自身把错误分类映射成固定退出码（cli/internal/cmd/root.go ExitCodeFor）：
2=输入错 4=鉴权/配置错 5=服务错 6=网络错 124=超时 130=中断。本 adapter 直接按
退出码归类，不去解析错误文案。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import tempfile
import zipfile
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
from ..conversation.turns import with_turn_ext, write_checkpoint
from ..conversation.writer import CONVERSATION_FILENAME, ConversationTraceWriter
from .base import (
    AdapterCapabilities,
    AdapterResult,
    AdapterRunInput,
    ConversationTurn,
    build_security_meta,
)
from .blade_service import (
    BLADE_SANDBOX_SECURITY_EXTRA,
    UNEXPECTED_INTERACTION_AUTO_ANSWER,
    AdapterEnv,
    BladeAdapterConfig,
    _render_turn_prompt,
)
from .token_usage import compact_usage, empty_usage

logger = logging.getLogger(__name__)

#: blade-cli 退出码 → (status, error_code)。见模块 docstring。
#: 124/130 单独留给超时与中断：它们不是 agent 能力问题，横评必须能区分出来。
_EXIT_CODE_MAP: dict[int, tuple[str, str]] = {
    2: ("cli_error", "blade_cli_input_error"),
    4: ("auth_failed", "blade_cli_auth_error"),
    5: ("chat_failed", "blade_cli_server_error"),
    6: ("chat_failed", "blade_cli_network_error"),
    124: ("timeout", "blade_cli_timeout"),
    130: ("interrupted", "blade_cli_interrupted"),
}

#: blade 会话终态 → Octagon status。CLI 的 normalizeTerminalStatus 只会吐这几个。
_SESSION_STATUS_MAP: dict[str, str] = {
    "completed": "completed",
    "waiting_for_input": "completed",
    "failed": "chat_failed",
    "interrupted": "interrupted",
}

#: 产物回收时跳过的 blade 运行时内部目录，与 blade_service 的口径保持一致。
_ARTIFACT_SKIP_DIRS = {".octagon", ".git", "node_modules", "__pycache__"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BladeCliAdapter:
    """用 blade-cli 子进程跑一次 BA attempt。

    刻意不继承 BladeServiceAdapter：两者只共享 prompt 渲染与配置结构，
    执行通道完全不同，继承只会让「哪条路在跑」变得不可读。
    """

    agent_name = "blade-agent"
    # blade server 自己在容器里跑 agent；CLI 只是本机的一个瘦客户端。
    # execution_locus 跟着 BladeServiceAdapter 写 docker-sandbox，保持
    # 安全扫描口径一致（agent 代码并不在本机执行）。
    capabilities = AdapterCapabilities(
        execution_locus="docker-sandbox",
        network_required="local_service",
        system_requires=("blade",),
        # CLI 的 AskUserQuestion 应答就是普通 `chat send`，但本版不接场景
        # 预声明的结构化应答编排（无 askuser_answer 通道），先如实声明 False
        # （dispatch 会对含 answer_interaction 的 conversation fail fast）。
        # 未声明的提问则在本 adapter 内用 `chat send` 自动回一句拒绝话术续跑
        # （见 _run_turn 的 waiting_for_input 循环）。
        interaction_answer=False,
        iterative_session=True,
    )

    def __init__(
        self,
        config: BladeAdapterConfig,
        *,
        cli_path: str | None = None,
    ) -> None:
        self.config = config
        # 允许注入以便测试；默认在 PATH 里找。
        self._cli_path_override = cli_path

    @property
    def wire_capture_capabilities(self) -> dict[str, bool]:
        # CLI 通道拿不到 blade session metadata（SDK 才有），如实声明 False。
        return {"blade_session_metadata": False}

    # ---------- CLI 调用 ----------

    def _resolve_cli(self) -> str | None:
        return self._cli_path_override or shutil.which("blade")

    def _cli_env(self) -> dict[str, str]:
        """blade-cli 只认 BLADE_API_URL / BLADE_API_TOKEN 两个环境变量。

        Octagon 侧配置在 settings.blade.base_url / api_key，这里做一次映射。
        """
        env = dict(os.environ)
        env["BLADE_API_URL"] = self.config.base_url
        if self.config.api_key:
            env["BLADE_API_TOKEN"] = self.config.api_key
        return env

    async def _run_cli(
        self,
        cli_path: str,
        args: list[str],
        *,
        timeout: float | None,
        cwd: Path | None = None,
    ) -> tuple[int, str, str]:
        """跑一条 blade 子命令，返回 (returncode, stdout, stderr)。

        超时按 CLI 自己的超时码 124 归一，让上层只有一套判定。
        """
        proc = await asyncio.create_subprocess_exec(
            cli_path,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._cli_env(),
            cwd=str(cwd) if cwd else None,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            return 124, "", f"blade {' '.join(args[:2])} timed out"
        return (
            proc.returncode or 0,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )

    # ---------- 主流程 ----------

    async def run(
        self,
        task: AdapterRunInput,
        env: AdapterEnv,
        data_path: Path,
    ) -> AdapterResult:
        started = datetime.now(timezone.utc)
        data_path = Path(data_path)
        attempt_dir = data_path / "attempts" / task.attempt_id
        attempt_dir.mkdir(parents=True, exist_ok=True)
        workspace = attempt_dir / "skill_workspace"
        workspace.mkdir(parents=True, exist_ok=True)

        cli_path = self._resolve_cli()
        if not cli_path:
            return AdapterResult(
                attempt_id=task.attempt_id,
                status="cli_not_found",
                error_code="blade_cli_not_in_path",
                error_message="blade CLI not found in PATH",
            )

        try:
            return await self._run_inner(
                task, env, data_path, attempt_dir, workspace, cli_path, started
            )
        except Exception as exc:  # noqa: BLE001 - 协议要求不得抛出
            logger.exception("blade-cli adapter crashed")
            return AdapterResult(
                attempt_id=task.attempt_id,
                status=getattr(exc, "status", "cli_error"),
                error_code=getattr(exc, "error_code", "adapter_crashed"),
                error_message=str(exc),
                duration_ms=int(
                    (datetime.now(timezone.utc) - started).total_seconds() * 1000
                ),
            )

    async def _run_inner(
        self,
        task: AdapterRunInput,
        env: AdapterEnv,
        data_path: Path,
        attempt_dir: Path,
        workspace: Path,
        cli_path: str,
        started: datetime,
    ) -> AdapterResult:
        plan = effective_conversation(task)
        deadline = AttemptDeadline(task.timeout_seconds)
        writer = ConversationTraceWriter(
            attempt_dir / CONVERSATION_FILENAME, attempt_id=task.attempt_id
        )
        external_refs: dict[str, Any] = {
            "blade_base_url": self.config.base_url,
            "blade_transport": "cli",
            "blade_cli_path": cli_path,
        }
        checkpoint_state: dict[str, Any] = {
            "blade_cli_path": cli_path,
            "blade_session_id": None,
            "last_completed_turn_index": None,
            "recoverability": "checkpoint-only",
        }

        session_id: str | None = None
        status = "completed"
        error_code: str | None = None
        error_message: str | None = None
        # 未声明的提问（waiting_for_input 终态）被自动拒绝话术应答的次数。
        interaction_auto_answered_count = 0

        writer.conversation_started(
            turn_count=len(plan.turns),
            is_legacy=plan.is_legacy,
            score_turn_id=plan.score_turn.turn_id,
        )

        async def _run_turn(turn: Any) -> tuple[str, str | None, str | None]:
            """跑一轮，返回 (status, error_code, error_message)。

            首轮 `chat run` 建会话，后续轮 `chat send` 复用同一 session——
            这是 CLI 通道能做到的「同一逻辑会话」的全部含义。

            BA 可能在本轮中途提出未预声明的提问：chat run/send 阻塞到终态
            waiting_for_input，说明 agent 在等用户应答。answer_unexpected_interaction
            打开时自动 `chat send` 一句无信息的拒绝话术把会话解出来继续跑
            （再次被提问则循环应答，由 deadline 兜底）；关闭时维持原语义：
            waiting_for_input 视作本轮完成。
            """
            nonlocal session_id
            nonlocal interaction_auto_answered_count
            prompt = _render_turn_prompt(task, turn, env, data_path)
            remaining = deadline.remaining()

            if session_id is None:
                code, out, err = await self._chat_run(
                    cli_path, task, env, prompt, workspace, remaining
                )
            else:
                code, out, err = await self._chat_send(
                    cli_path, session_id, prompt, remaining
                )

            result = _parse_chat_result(out)
            if result.get("session_id") and session_id is None:
                session_id = str(result["session_id"])
                external_refs["blade_session_id"] = session_id
                checkpoint_state["blade_session_id"] = session_id
                write_checkpoint(attempt_dir, checkpoint_state)
                # 会话一建立就通知 dispatch：动态续聊靠这个钩子确认送达。
                if task.prompt_delivery_handler is not None:
                    task.prompt_delivery_handler()

            if code != 0:
                turn_status, turn_error = _EXIT_CODE_MAP.get(
                    code, ("cli_error", "blade_cli_error")
                )
                return turn_status, turn_error, (err or out).strip()[:2000]

            # 自动应答未声明的提问：CLI 的终态 waiting_for_input 说明 agent 在
            # 等应答，不续就会让 attempt 静默停在「没真正完成」。
            while self.config.answer_unexpected_interaction and session_id is not None:
                session_status = str(result.get("status") or "completed")
                if session_status != "waiting_for_input":
                    break
                interaction_auto_answered_count += 1
                logger.warning(
                    "blade-cli attempt=%s 收到未声明的交互请求（waiting_for_input），"
                    "自动应答「%s」续跑",
                    task.attempt_id, UNEXPECTED_INTERACTION_AUTO_ANSWER,
                )
                remaining = deadline.remaining()
                code, out, err = await self._chat_send(
                    cli_path, session_id, UNEXPECTED_INTERACTION_AUTO_ANSWER, remaining
                )
                if code != 0:
                    turn_status, turn_error = _EXIT_CODE_MAP.get(
                        code, ("cli_error", "blade_cli_error")
                    )
                    return turn_status, turn_error, (err or out).strip()[:2000]
                result = _parse_chat_result(out)

            session_status = str(result.get("status") or "completed")
            if session_status not in ("completed", "waiting_for_input"):
                return (
                    _SESSION_STATUS_MAP.get(session_status, "chat_failed"),
                    f"blade_session_{session_status}",
                    f"blade session ended with status {session_status}",
                )
            return "completed", None, None

        completed_all_turns = False
        try:
            for turn in plan.turns:
                deadline.check_before_turn()
                writer.turn_started(turn, producer_session_id=session_id)
                status, error_code, error_message = await _run_turn(turn)
                if status != "completed":
                    writer.turn_failed(
                        turn,
                        producer_session_id=session_id,
                        error_code=error_code,
                        error_summary=error_message,
                    )
                    break

                checkpoint_state["last_completed_turn_index"] = turn.turn_index
                write_checkpoint(attempt_dir, checkpoint_state)
                writer.turn_completed(turn, producer_session_id=session_id)
            else:
                completed_all_turns = True

            # 动态返工：静态轮跑完后由平台决定还要不要继续发消息。
            # capabilities.iterative_session=True 就必须真的消费这个 handler，
            # 否则迭代类 env 会静默停在静态轮，被读成"agent 不返工"。
            handler = task.iteration_turn_handler
            if completed_all_turns and handler is not None:
                with deadline.suspend():
                    decision = await handler.on_turn_completed(
                        producer_session_id=session_id
                    )
                while decision is not None and decision.next_prompt is not None:
                    turn_index = int(decision.round_index) + 1
                    dynamic_turn = ConversationTurn(
                        turn_id=f"iteration-{turn_index}",
                        turn_index=turn_index,
                        purpose="task",
                        prompt=decision.next_prompt,
                    )
                    try:
                        deadline.check_before_turn()
                    except AttemptBudgetExhausted:
                        status = "timeout"
                        error_code = ERROR_BUDGET_EXHAUSTED_BETWEEN_TURNS
                        error_message = "time budget exhausted between turns"
                        writer.turn_failed(
                            dynamic_turn,
                            producer_session_id=session_id,
                            error_code=error_code,
                            error_summary=error_message,
                        )
                        break
                    handler.mark_feedback_sending(decision)
                    writer.turn_started(
                        dynamic_turn, producer_session_id=session_id
                    )
                    handler.mark_feedback_delivered(decision)
                    status, error_code, error_message = await _run_turn(dynamic_turn)
                    if status != "completed":
                        writer.turn_failed(
                            dynamic_turn,
                            producer_session_id=session_id,
                            error_code=error_code,
                            error_summary=error_message,
                        )
                        handler.finalize_last_successful_submission()
                        break
                    writer.turn_completed(
                        dynamic_turn, producer_session_id=session_id
                    )
                    with deadline.suspend():
                        decision = await handler.on_turn_completed(
                            producer_session_id=session_id
                        )
        except AttemptBudgetExhausted:
            status = "timeout"
            error_code = ERROR_BUDGET_EXHAUSTED_BETWEEN_TURNS
            error_message = "time budget exhausted between turns"

        if status == "completed":
            writer.conversation_completed()
        else:
            writer.conversation_failed(
                error_code=error_code, error_summary=error_message
            )
        writer.close()

        events_count = 0
        last_event_at: str | None = None
        tool_call_count = 0
        # 轨迹与产物即便失败也要尽力回收：横评要能分辨"跑挂了"和"什么都没做"。
        if session_id:
            events_count, last_event_at, tool_call_count = await self._rebuild_events(
                cli_path, session_id, attempt_dir, task
            )
            external_refs["artifact_sync"] = await self._recover_artifacts(
                cli_path, session_id, workspace
            )

        if interaction_auto_answered_count:
            external_refs["unexpected_interaction_auto_answered"] = (
                interaction_auto_answered_count
            )

        return AdapterResult(
            attempt_id=task.attempt_id,
            status=status,
            external_refs=external_refs,
            error_code=error_code,
            error_message=error_message,
            # CLI 无实时流：事件是事后从 session history 重建的。
            transport_status="reconstructed" if session_id else "not_applicable",
            events_count=events_count,
            last_event_at=last_event_at,
            # CLI 不区分 thinking 节点，恒 0（见模块 docstring 的数据缺口说明）。
            thinking_count=0,
            tool_call_count=tool_call_count,
            # 刻意留空：blade-cli 不暴露 token usage。
            token_usage=compact_usage(empty_usage()),
            duration_ms=int(
                (datetime.now(timezone.utc) - started).total_seconds() * 1000
            ),
            conversation_summary=summarize_conversation(attempt_dir),
            security_meta=build_security_meta(
                execution_locus=self.capabilities.execution_locus,
                permission_mode="sandbox",
                workspace_root=str(workspace.resolve()),
                extra={**BLADE_SANDBOX_SECURITY_EXTRA, "blade_transport": "cli"},
            ),
        )

    # ---------- 子命令 ----------

    async def _chat_run(
        self,
        cli_path: str,
        task: AdapterRunInput,
        env: AdapterEnv,
        prompt: str,
        workspace: Path,
        timeout: float | None,
    ) -> tuple[int, str, str]:
        """建会话并跑首轮。

        `--solution-id` / `--biz-role` 对齐 blade_service 的原生入口语义：
        skill 由 blade server 侧注册，这里只指名，不上传薄壳。
        """
        args = ["chat", "run", prompt, "--json"]
        solution_id = getattr(env, "solution_id", None)
        biz_role_id = getattr(env, "biz_role_id", None)
        primary_skill = getattr(env, "effective_primary_skill_id", None)
        if solution_id:
            args += ["--solution-id", str(solution_id)]
        if biz_role_id:
            args += ["--biz-role", str(biz_role_id)]
        if primary_skill and not solution_id:
            # 原生 skill 入口：CLI 没有 --primary-skill，用 --solution-id 承载
            # 的是 solution；skill 场景回落到 server 默认，由 env 侧保证注册。
            logger.info(
                "blade-cli: primary_skill_id=%s 由 server 侧注册，CLI 不重复传",
                primary_skill,
            )
        if self.config.model:
            args += ["--model", self.config.model]
        args += self._upload_args(task, workspace)
        if timeout is not None:
            args += ["--timeout", f"{int(timeout)}s"]
        return await self._run_cli(cli_path, args, timeout=_hard_timeout(timeout))

    async def _chat_send(
        self,
        cli_path: str,
        session_id: str,
        prompt: str,
        timeout: float | None,
    ) -> tuple[int, str, str]:
        args = ["chat", "send", session_id, prompt, "--json"]
        if self.config.model:
            args += ["--model", self.config.model]
        if timeout is not None:
            args += ["--timeout", f"{int(timeout)}s"]
        return await self._run_cli(cli_path, args, timeout=_hard_timeout(timeout))

    def _upload_args(
        self, task: AdapterRunInput, workspace: Path
    ) -> list[str]:
        """把 attempt.json 与任务物料变成 `--file LOCAL=REMOTE`。

        attempt.json 是薄壳 skill 回调 Octagon 的凭据，落 `.octagon/attempt.json`，
        与 blade_service._upload_materials 的远端路径逐字对齐。
        """
        args: list[str] = []
        attempt_json = json.dumps(
            {
                "attempt_id": task.attempt_id,
                "env_base_url": (
                    self.config.sandbox_env_base_url or task.env_base_url
                ),
                "env_token": task.env_token,
            },
            ensure_ascii=False,
        )
        # 临时文件交给 CLI 读；attempt_dir 下留一份会被产物扫描误判为 agent 产出，
        # 所以放系统临时目录，run 结束由 OS 回收。
        fd = tempfile.NamedTemporaryFile(
            mode="w", suffix="_attempt.json", delete=False, encoding="utf-8"
        )
        with fd:
            fd.write(attempt_json)
        args += ["--file", f"{fd.name}=.octagon/attempt.json"]

        uploaded = task.task_context.get("uploaded_files")
        if isinstance(uploaded, list):
            for uf in uploaded:
                name = uf.get("name", "") if isinstance(uf, dict) else ""
                if not name:
                    continue
                src = workspace / name
                if not src.is_file():
                    # 与 blade_service / dispatch 对齐：物料缺失硬失败，
                    # 否则 agent 在空 workspace 里瞎搜，浪费整个 attempt。
                    raise FileNotFoundError(
                        f"任务物料不在本地 workspace: {name} ({src})"
                    )
                args += ["--file", f"{src}={name}"]

        material_files = task.task_context.get("_agent_material_files")
        if isinstance(material_files, list):
            for rel in sorted({str(x) for x in material_files}):
                src = workspace / rel
                if src.is_file():
                    args += ["--file", f"{src}={rel}"]
        return args

    # ---------- 事后回收 ----------

    async def _rebuild_events(
        self,
        cli_path: str,
        session_id: str,
        attempt_dir: Path,
        task: AdapterRunInput,
    ) -> tuple[int, str | None, int]:
        """用 `blade session history --json` 事后重建 events.jsonl。

        这不是实时流的等价物：时序是 blade 端记录的 node timestamp，
        粒度到消息节点为止。返回 (events_count, last_event_at, tool_call_count)。
        """
        code, out, err = await self._run_cli(
            cli_path,
            ["session", "history", session_id, "--json", "--max", "0"],
            timeout=self.config.request_timeout_seconds or 30.0,
        )
        if code != 0:
            logger.warning("blade-cli session history failed (%s): %s", code, err)
            return 0, None, 0

        try:
            payload = json.loads(out)
        except json.JSONDecodeError:
            logger.warning("blade-cli session history returned non-JSON")
            return 0, None, 0

        nodes = payload.get("nodes") or []
        events_path = attempt_dir / "events.jsonl"
        count = 0
        tool_calls = 0
        last_at: str | None = None
        with events_path.open("w", encoding="utf-8") as f:
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                event = {
                    "timestamp": node.get("timestamp") or _now(),
                    "type": node.get("kind") or node.get("role") or "message",
                    "role": node.get("role"),
                    "content": node.get("content"),
                    "tool_calls": node.get("tool_calls") or [],
                    # 标记来源，避免下游把它当实时事件流解读。
                    "source": "blade_cli_history",
                }
                f.write(
                    json.dumps(
                        with_turn_ext(event, None, None), ensure_ascii=False
                    )
                    + "\n"
                )
                count += 1
                tool_calls += len(node.get("tool_calls") or [])
                if node.get("timestamp"):
                    last_at = str(node["timestamp"])
        return count, last_at, tool_calls

    async def _recover_artifacts(
        self, cli_path: str, session_id: str, workspace: Path
    ) -> dict[str, Any]:
        """用 `blade session copy-dir` 把远端 workspace 拉成 zip 再解到本地。

        scorer 只看本地 skill_workspace，所以这一步失败必须显式记账，
        否则会被读成"agent 什么都没产出"。
        """
        with tempfile.TemporaryDirectory(prefix="blade-cli-artifacts-") as tmp:
            zip_path = Path(tmp) / "workspace.zip"
            code, _out, err = await self._run_cli(
                cli_path,
                ["session", "copy-dir", session_id, ".", "--out", str(zip_path)],
                timeout=self.config.request_timeout_seconds or 30.0,
            )
            if code != 0 or not zip_path.is_file():
                return {"error": (err or f"copy-dir exit {code}").strip()[:500]}

            extracted = 0
            try:
                with zipfile.ZipFile(zip_path) as zf:
                    for member in zf.infolist():
                        if member.is_dir():
                            continue
                        rel = Path(member.filename)
                        if any(part in _ARTIFACT_SKIP_DIRS for part in rel.parts):
                            continue
                        # zip slip 防护：解出来的路径必须留在 workspace 里。
                        target = (workspace / rel).resolve()
                        if not str(target).startswith(str(workspace.resolve())):
                            logger.warning("skip unsafe zip member: %s", rel)
                            continue
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(member) as src, target.open("wb") as dst:
                            shutil.copyfileobj(src, dst)
                        extracted += 1
            except zipfile.BadZipFile as exc:
                return {"error": f"bad zip: {exc}"}
            return {"files": extracted}


def _parse_chat_result(stdout: str) -> dict[str, Any]:
    """解析 `blade chat run/send --json` 的终态输出。

    CLI 失败时 stdout 可能为空或非 JSON（错误走 stderr），此时返回空 dict，
    由调用方按退出码归类。
    """
    text = stdout.strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _hard_timeout(turn_timeout: float | None) -> float | None:
    """子进程硬超时：比传给 CLI 的 --timeout 宽限 30s。

    让 CLI 自己先超时（退出码 124，错误信息更准确），硬杀只是兜底，
    防止 CLI 卡在网络 IO 上不退出。
    """
    if turn_timeout is None:
        return None
    return turn_timeout + 30.0
