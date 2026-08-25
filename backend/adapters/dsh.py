"""DshAdapter — 通过 DeepSeek Harness 的 Python SDK 驱动。

**为什么走 SDK 而不是 CLI**：dsh 的 CLI headless（`dsh --profile headless`）
只把最后一条 assistant 文本写到 stdout，没有 `--output-format`、没有 `--cwd`、
没有超时 flag；能吐 JSONL 的 `headless-driver.ts` 被上游明确标注为
"test infrastructure, not a supported CLI output format"。而 `BENCHMARK.md`
指定的官方 benchmark 路线就是 Python SDK，它直接返回结构化
`RunResult(events, notifications, finish_reason, ...)`。

与其余五个 CLI adapter 的**结构性差异**（三处，都不是风格问题）：

1. **可用性看 import 不看 PATH** —— 装的是 pip 包，没有可执行文件。
2. **子进程由 SDK 内部 spawn**，本模块不直接起进程，故在
   `tests/test_adapter_spawn_guard.py` 的 EXEMPT 表里（与 blade_service 同类）。
3. **没有取消通道** —— SDK 只有 initialize/session_prompt/shutdown 三个 method，
   core 的 `Agent.cancel()` 未暴露。超时只能 `close()` 硬杀，见 `_run_turn`。

实测契约（2026-08-13，本机 macOS arm64，`deepseek-harness-sdk==0.1.0rc6`，
上游走 OpenRouter；完整实测见 docs/specs/dsh_agent_integration/spike-dsh-runtime.md）：

    {"type":"tool/call","seq":N,"time":<epoch ms>,
     "data":{"turn":1,"step":1,"callId":"call_...","name":"write","arguments":"{...}"}}
    {"type":"tool/result","data":{"turn":1,"step":1,
     "message":{"content":[{"type":"tool-result","toolCallId":"call_...","isError":false}]}}}
    {"type":"assistant/message","data":{"message":{"content":[...]},"usage":{...}}}

三个实测才发现、漏了就跑不通的点：

- **`max_tokens` 必须显式传**：`llm-deepseek` 默认 256000 且不按 contextWindow
  夹紧，打到任何上下文 < 256K 的模型直接 400 CONTEXT_WINDOW_EXCEEDED。
- **skill 隔离要三招齐上**：`DSH_HOME` + `DSH_AGENTS_HOME` 只挡 rank 400/500；
  仓库内那批 `.agents/skills` 是 rank 200，由 projectRoot（最近的 `.git`
  祖先）推导，只能靠在工作区放一个空 `.git` 挡住。少一样就漏。
- **SDK 默认插件组合没有文件工具**（`fs-local` 只是 provider，`tool-fs` 没挂），
  所以 cordis.yml 必须自己生成，见 `_build_cordis`。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

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
from .base import (
    AdapterCapabilities,
    AdapterResult,
    AdapterRunInput,
    build_security_meta,
    prompt_context,
    time_budget_notice,
)
from .dsh_events import merge_usage, usage_from_event
from .error_taxonomy import classify as classify_cli_error
from .token_usage import compact_usage, empty_usage

logger = logging.getLogger(__name__)

AGENT_NAME = "dsh"

#: pi-ai 的 provider route 名。`initialize(provider=...)` 传的是**这个**，
#: 不是插件名——server 用 `llm.listProviders()` 的 id 匹配（server.ts:236）。
#: 拼错的表现是 JsonRpcError 秒失败并关子进程，不降级。
PROVIDER_ROUTE = "octagon"

#: pi-ai route 里 `apiKeyEnv` 引用的环境变量名。配置文件里只写引用，
#: 真实 key 经 SDK 的 env 注入——key 因此不落盘到 attempt 目录。
API_KEY_ENV = "OCTAGON_DSH_API_KEY"

#: 上游拿不到显式配置时的输出上限。**不能不给**（见模块 docstring）。
DEFAULT_MAX_TOKENS = 8192
DEFAULT_CONTEXT_WINDOW = 200_000

#: 单次 JSON-RPC 请求超时（initialize / session_prompt / shutdown）。
#: SDK 默认 None = 永不超时，`_request_raw` 在无 timeout 的 queue.get() 上死等。
#: 这**不是** attempt 的 deadline（run 的执行时长另由 AttemptDeadline 管），
#: 只是防握手/关停单步卡死。
_REQUEST_TIMEOUT_SECONDS = 60.0

#: dsh 对 MCP serverName 的硬约束（mcp-client/src/index.ts:36）：
#: 同一 root 下唯一，重名是 plugin load 阶段硬失败，不是静默覆盖。
_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_SERVER_NAME_MAX = 32


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def dsh_server_name(name: str) -> str:
    """`McpServerSpec.name` → dsh `serverName`（≤32，确定性，抗碰撞）。

    Octagon 的默认名是 `octagon-<env_name>`，字符集已由 run_dispatch 校验
    （与 dsh 的 pattern 一致），**但没有长度上限**——实测启用 MCP 的场景里
    `octagon-agent-parallel-scheduling` 是 33 字符，超 1 位。

    不能裸截断：两个长名截到 32 位可能相同，而 dsh 对重名是硬失败。
    保留可读前缀 + 全名 sha1 前 8 位，截断碰撞时后缀仍不同。
    """
    if len(name) <= _SERVER_NAME_MAX:
        out = name
    else:
        digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
        out = f"{name[:23]}-{digest}"  # 23 + 1 + 8 = 32
    if _SERVER_NAME_RE.fullmatch(out) is None:
        # 上游校验将来放宽时要在这里炸，而不是在 dsh plugin load 时炸成
        # 一个难懂的错误。
        raise ValueError(f"MCP serverName 不合 dsh 约束: {out!r}（源自 {name!r}）")
    return out


class _TurnSink:
    """一轮的流式事件消费者。

    **为什么流式而不是等 `RunResult`**：超时由 `close()` 硬杀，
    `Session.run()` 不会返回——攒到最后才写的话，那一轮已经产生的全部事件
    都会丢。而长轮恰恰是最可能超时、也最值得留证据的。

    `Session.run(on_notification=...)` 的回调在 SDK 的 reader 线程上跑
    （`api.py` 的 `collect`），所以这里只做追加写与计数，不碰 asyncio。
    """

    def __init__(
        self, turn: Any, events_path: Path, thinking_path: Path,
        *, session_id: str,
    ) -> None:
        self._turn = turn
        self._events_path = events_path
        self._thinking_path = thinking_path
        self._session_id = session_id
        self.events = 0
        self.thinking = 0
        self.tool_calls = 0
        self.skipped_descendant = 0
        self.last_event_at: str | None = None
        self.usage: dict[str, int | None] = empty_usage()
        #: 已被并进 attempt 级统计（`_absorb` 的幂等标记）。超时分支与
        #: except 块可能先后触及同一个 sink，不标记就会重复计数。
        self.absorbed = False

    def __call__(self, notification: Any) -> None:
        """on_notification 回调：只认 **root session** 的 `session.event`。

        ⚠️ **必须自己过滤 sessionId**。SDK 的 subscription 用的是
        `_notification_belongs_to_session_tree`（**含 descendant**），而
        `on_notification` 在 sessionId 过滤**之前**就被调用
        （`api.py` 的 `collect`：先回调，再判 `sessionId == self.id` 才收进
        `RunResult.events`）。

        不过滤的后果：subagent 的工具调用与 usage 会混进 root 的
        `events.jsonl`——安全轴多算工具调用、成本多算 token，而且这些数字
        看起来完全合理，只有跟 `RunResult.events` 对账时才会发现不一致。
        """
        if getattr(notification, "method", None) != "session.event":
            return
        payload = getattr(notification, "payload", None)
        if not isinstance(payload, dict):
            return
        if payload.get("sessionId") != self._session_id:
            # 子 session 的事件：计数但不落 root trace。
            # （子 agent 身份不可得——见 normalizer 的 CAPABILITIES 声明。）
            self.skipped_descendant += 1
            return
        event = payload.get("event")
        if isinstance(event, dict):
            self.consume(event)

    def consume(self, ev: dict[str, Any]) -> None:
        self.events += 1
        _append_jsonl(
            self._events_path,
            with_turn_ext(dict(ev), self._turn.turn_id, self._turn.turn_index),
        )
        ts = ev.get("time")
        if isinstance(ts, (int, float)) and not isinstance(ts, bool):
            try:
                self.last_event_at = datetime.fromtimestamp(
                    ts / 1000, tz=timezone.utc
                ).isoformat()
            except (OverflowError, OSError, ValueError):
                pass

        etype = ev.get("type")
        data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
        if etype == "tool/call":
            self.tool_calls += 1
        elif etype == "assistant/message":
            message = data.get("message")
            blocks = (
                message.get("content")
                if isinstance(message, dict)
                and isinstance(message.get("content"), list)
                else []
            )
            for block in blocks:
                # dsh 的思考块是 `reasoning`（不是 CC 的 `thinking`），
                # 文本在 `text` 字段。经 OpenRouter 端点通常拿不到内容
                # （上游发 `reasoning`、dsh 读 `reasoning_content`），
                # 但换端点就会出现。
                if isinstance(block, dict) and block.get("type") == "reasoning":
                    text = block.get("text")
                    if isinstance(text, str) and text:
                        self.thinking += 1
                        _append_jsonl(self._thinking_path, {
                            "turn": self._turn.turn_index,
                            "text": text,
                            "timestamp": _now_iso(),
                        })
            if "usage" in data:
                self.usage = merge_usage(self.usage, usage_from_event(data["usage"]))


class DshAdapter:
    capabilities = AdapterCapabilities(
        # dsh-bash-local / dsh-fs-local 是本机执行，且 SDK 路线下**没有**
        # approval/sandbox 插件（DSH_PERMISSION_MODE 只对 CLI profile 生效）。
        execution_locus="host",
        network_required="public_internet",
        # runtime-bin 是平台 wheel，自带 Node，无本机二进制依赖。
        system_requires=(),
        interaction_answer=False,
        iterative_session=True,
    )

    def __init__(
        self,
        *,
        model: str,
        octagon_project_path: Path,
        providers: dict[str, ModelProviderSection] | None = None,
    ) -> None:
        self.model = model
        self.octagon_project_path = Path(octagon_project_path)
        self.providers = providers or {}

    @property
    def wire_capture_capabilities(self) -> dict[str, bool]:
        """四项全 True（与 opencode 同级）：
        process_env → SDK 的 env；llm_base_url / llm_headers → pi-ai route 的
        baseURL / headers；mcp_rewrites → 生成 cordis 时改写 mcp-client 的
        command/args。"""
        return {
            "process_env": True,
            "llm_base_url": True,
            "llm_headers": True,
            "mcp_rewrites": True,
        }

    # ---------- 配置生成 ----------

    def _llm_row(
        self, task: AdapterRunInput, model_ref: ModelRef
    ) -> tuple[dict[str, Any], str | None, int]:
        """pi-ai 的 llm 插件行，返回 (row, api_key, max_tokens)。

        用 `llm-pi-ai` 而非 `llm-deepseek`：后者只服务 DeepSeek 原生方言，
        接不了任意兼容网关，dsh 就进不了「同模型对比」。
        """
        max_tokens = DEFAULT_MAX_TOKENS
        headers: dict[str, str] = {}

        # provider 一定存在：run() 在此之前已 fail fast（dsh_provider_unresolved）。
        provider = self.providers[model_ref.provider]
        # 成本核算：run/attempt 专属 key 优先，回落 provider 配置。
        # 必须取二元组第二个值的语义——只取 key 会让审计把共享 key 的
        # 上界当实扣（cost/credential.py 的核心错误模式）。
        api_key, _used_run_key = resolve_agent_key(
            task.run_id, resolve_api_key(provider), task.attempt_id,
            provider.base_url,
        )
        base_url = resolve_agent_base_url(
            task.run_id, provider.base_url, task.attempt_id
        )

        if task.wire_injection.enabled:
            if task.wire_injection.llm_base_url:
                base_url = task.wire_injection.llm_base_url
            headers.update(task.wire_injection.llm_headers)
            # capture token 走独立头：不能占 Authorization（被 provider auth
            # 占用且反代会剥），缺了它反代直接 401。
            if task.wire_injection.capture_token:
                headers["X-Octagon-Capture-Token"] = task.wire_injection.capture_token

        # hand-declared route：pi-ai catalog 里没有 `octagon`，所以
        # api + baseURL + 非空 models 三者必填。
        profile: dict[str, Any] = {
            "displayName": "Octagon",
            "apiKeyEnv": API_KEY_ENV,
            "api": "openai-completions",
            "defaultContextWindow": DEFAULT_CONTEXT_WINDOW,
            "defaultMaxTokens": max_tokens,
            "models": [
                {
                    "id": model_ref.model,
                    "contextWindow": DEFAULT_CONTEXT_WINDOW,
                    "maxTokens": max_tokens,
                }
            ],
        }
        if base_url:
            profile["baseURL"] = base_url
        if headers:
            profile["headers"] = headers

        row = {
            "id": "llm-pi-ai",
            "name": "@deepseek-ai/dsh-llm-pi-ai",
            "config": {"providers": {PROVIDER_ROUTE: profile}},
        }
        return row, api_key, max_tokens

    def _mcp_rows(self, task: AdapterRunInput, workspace: Path) -> list[dict[str, Any]]:
        """`McpServerSpec` → dsh `StdioConfig` 插件行（每个 server 一行）。

        三处不是逐字段直译：serverName 长度、cwd 的 None、
        failOnStartupError 的默认值。
        """
        rows: list[dict[str, Any]] = []
        seen: dict[str, str] = {}
        for spec in task.mcp_servers:
            server_name = dsh_server_name(spec.name)
            if server_name in seen:
                raise ValueError(
                    f"MCP serverName 映射后重名: {server_name!r} "
                    f"（来自 {seen[server_name]!r} 与 {spec.name!r}）"
                )
            seen[server_name] = spec.name

            command = spec.command
            args = list(spec.args)
            rewrite = task.wire_injection.mcp_rewrites.get(spec.name)
            if task.wire_injection.enabled and rewrite is not None:
                args = [*rewrite.args_prefix, command, *args]
                command = rewrite.command

            rows.append({
                "id": f"mcp-{server_name}",
                "name": "@deepseek-ai/dsh-mcp-client",
                "config": {
                    "serverName": server_name,
                    "transport": "stdio",
                    "command": command,
                    "args": args,
                    # dsh 的 cwd schema 是 z.string().default('')，且原样传给
                    # spawn——空串是非法路径，不是"继承父进程 cwd"。
                    "cwd": spec.cwd or str(workspace.resolve()),
                    "env": {
                        "OCTAGON_ATTEMPT_ID": task.attempt_id,
                        "OCTAGON_ENV_TOKEN": task.env_token,
                        "OCTAGON_BASE_URL": task.env_base_url,
                    },
                    "toolCallTimeoutMs": 60000,
                    # 默认 false 会在连不上时静默无工具——评测里等于白跑却
                    # 拿不到失败信号。
                    "failOnStartupError": True,
                },
            })
        return rows

    def _build_cordis(
        self, task: AdapterRunInput, model_ref: ModelRef, workspace: Path
    ) -> tuple[list[dict[str, Any]], str | None, int]:
        """本次 attempt 的完整插件组合。

        以 SDK 默认 8 行为底，补齐到「与其余六家可比的 coding agent」：
        文件工具、todo、subagent、压缩都要显式挂——SDK 默认组合都没有。
        显式**不挂** web 插件（其余六家无内建搜索）与 approval/telemetry。
        """
        llm_row, api_key, max_tokens = self._llm_row(task, model_ref)
        rows: list[dict[str, Any]] = [
            # 删了就没法通信。
            {"id": "sdk-jsonrpc-server", "name": "@deepseek-ai/dsh-sdk-jsonrpc-server"},
            {
                "id": "agent-core",
                "name": "@deepseek-ai/dsh-agent-spine-demo",
                "config": {
                    "skills": {"enabled": True},
                    "workspaceContext": {"maxBytes": 65536},
                },
            },
            llm_row,
            {
                "id": "sessions",
                "name": "@deepseek-ai/dsh-session-persistence-jsonl",
                "config": {"root": str((workspace.parent / "dsh_sessions").resolve()),
                           "compression": "none"},
            },
            {"id": "session-checkpoints",
             "name": "@deepseek-ai/dsh-session-checkpoint-policy"},
            {"id": "subprocess", "name": "@deepseek-ai/dsh-subprocess-local"},
            {
                "id": "bash",
                "name": "@deepseek-ai/dsh-bash-local",
                "config": {"cwd": str(workspace.resolve()), "timeoutMs": 60000},
            },
            # 文件工具三件套：SDK 默认只有 fs-local(provider)，没有 tool-fs，
            # 不补的话 agent 只能用 bash 改文件。
            {
                "id": "fs-local",
                "name": "@deepseek-ai/dsh-fs-local",
                "config": {"cwd": str(workspace.resolve())},
            },
            {"id": "fs-observation-policy",
             "name": "@deepseek-ai/dsh-fs-observation-policy"},
            {"id": "tool-fs", "name": "@deepseek-ai/dsh-tool-fs"},
            {
                "id": "tool-todo",
                "name": "@deepseek-ai/dsh-tool-todo",
                "config": {"allowParallelInProgress": True},
            },
            # subagent 三行缺一不可：provider 名必须与 tool 的 provider 对上，
            # 只挂 dsh-subagent 拿不到工具。
            {"id": "subagent", "name": "@deepseek-ai/dsh-subagent"},
            {
                "id": "subagent-spawn-in-process",
                "name": "@deepseek-ai/dsh-subagent-spawn-in-process",
                "config": {"providerName": "spawn"},
            },
            {
                "id": "tool-subagent",
                "name": "@deepseek-ai/dsh-tool-subagent",
                "config": {"provider": "spawn", "toolName": "subagent",
                           "enableRunInBackground": False},
            },
            {"id": "token-meter", "name": "@deepseek-ai/dsh-token-meter"},
            {
                "id": "compaction-basic",
                "name": "@deepseek-ai/dsh-compaction-basic",
                "config": {"thresholdRatio": 0.8, "retainRatio": 0.16,
                           "maxTokens": 8192, "compactionRetries": 1},
            },
        ]
        rows.extend(self._mcp_rows(task, workspace))
        return rows, api_key, max_tokens

    def _write_cordis(
        self, task: AdapterRunInput, attempt_dir: Path, model_ref: ModelRef,
        workspace: Path,
    ) -> tuple[Path, str | None, int]:
        """落 `dsh_cordis.yml`，返回 (路径, api_key, max_tokens)。

        顶层是 entry-list（`EntryOptions[]`），**不是** `- insert:` 补丁方言
        ——SDK 走 `boot(..., patches=undefined)`，写成 patch 形式会直接 boot 失败。
        """
        rows, api_key, max_tokens = self._build_cordis(task, model_ref, workspace)
        path = attempt_dir / "dsh_cordis.yml"
        path.write_text(
            yaml.safe_dump(rows, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        return path, api_key, max_tokens

    def _subprocess_env(
        self, task: AdapterRunInput, attempt_dir: Path, workspace: Path,
        api_key: str | None,
    ) -> dict[str, str]:
        """runtime 子进程的环境。

        skill 隔离靠三样东西，这里给两样，第三样（工作区的空 `.git`）在
        `run()` 里建目录时放——少任何一样都会让 dsh 读到不该读的 skill。
        """
        env: dict[str, str] = {
            "DSH_CWD": str(workspace.resolve()),
            "DSH_SESSION_ROOT": str((attempt_dir / "dsh_sessions").resolve()),
            # 挡 rank 400 / 500 的 skill 发现根目录，同时隔离 shell env。
            "DSH_HOME": str((attempt_dir / ".dsh").resolve()),
            "DSH_AGENTS_HOME": str((attempt_dir / ".agents").resolve()),
            # 遥测导出**无任何脱敏**（message text / tool args / workspace
            # paths 全带走），评测数据不能外流。
            "DSH_TELEMETRY_DISABLED": "1",
        }
        if api_key:
            env[API_KEY_ENV] = api_key
        if task.wire_injection.enabled:
            env.update(task.wire_injection.process_env)
            if task.wire_injection.capture_token:
                env["OCTAGON_WIRE_CAPTURE_TOKEN"] = task.wire_injection.capture_token
        return env

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
        workspace = attempt_dir / "skill_workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        # skill 隔离第三招：把 projectRoot 钉在工作区，挡住 rank 100/200。
        # 只设 DSH_HOME/DSH_AGENTS_HOME 挡不住 Octagon 仓库那批 .agents/skills
        # ——它们由 projectRoot（最近的 .git 祖先）推导。实测漏 40+ 个。
        (workspace / ".git").mkdir(exist_ok=True)
        for sub in (".dsh", ".agents", "dsh_sessions"):
            (attempt_dir / sub).mkdir(exist_ok=True)

        events_path = attempt_dir / "events.jsonl"
        thinking_path = attempt_dir / "thinking.jsonl"

        plan = effective_conversation(task)
        session_id = f"octagon-{task.attempt_id}"

        # 四个落盘产物**在任何早退路径上都要存在**（SDK 未安装、
        # provider/key 缺失、cordis 生成失败……）。
        # **空文件与文件缺失语义不同**：前者是"采集了但没有内容"，后者会被
        # 下游当成"这个 agent 没有该通道"——`evaluator.load_trace()` 在文件
        # 缺失时静默返回 []，于是"没采集"和"真干净"在数据上无法区分
        # （docs/architecture.md 记过的同源事故）。
        events_path.touch(exist_ok=True)
        thinking_path.touch(exist_ok=True)
        conversation_trace = ConversationTraceWriter(
            attempt_dir / CONVERSATION_FILENAME, attempt_id=task.attempt_id
        )
        conversation_trace.conversation_started(
            turn_count=len(plan.turns),
            is_legacy=plan.is_legacy,
            score_turn_id=plan.score_turn.turn_id,
        )
        checkpoint_state: dict[str, Any] = {
            "agent": AGENT_NAME,
            "sdk": "deepseek-harness-sdk",
            "conversation_plan_hash": plan.plan_hash if not plan.is_legacy else None,
            "conversation_turn_count": len(plan.turns) if not plan.is_legacy else None,
            "session_id": session_id,
            "last_completed_turn_index": None,
            "active_turn_index": None,
            "recoverability": "checkpoint-only",
        }

        def _checkpoint(**updates: Any) -> None:
            unknown = set(updates) - set(checkpoint_state)
            if unknown:
                raise KeyError(f"未知的 checkpoint 字段: {sorted(unknown)}")
            checkpoint_state.update(updates)
            write_checkpoint(attempt_dir, checkpoint_state)

        _checkpoint()

        def _early_exit(**kwargs: Any) -> AdapterResult:
            """早退：收尾 conversation trace 后返回，保证产物齐全。"""
            conversation_trace.conversation_failed(
                error_code=kwargs.get("error_code"),
                error_summary=(kwargs.get("error_message") or "")[:500],
            )
            conversation_trace.close()
            return AdapterResult(attempt_id=task.attempt_id, **kwargs)

        try:
            from deepseek_harness import DeepSeekHarness
        except ImportError:
            return _early_exit(
                # 复用既有终态而非新增：语义等价（被测 agent 本机不可用），
                # 新增终态会波及 error_taxonomy 与前端状态展示。
                status="cli_not_found",
                error_code="dsh_sdk_not_installed",
                error_message=(
                    "deepseek-harness-sdk 未安装"
                    "（uv pip install 'agent-octagon[dsh]'）"
                ),
            )

        model_ref = parse_model_ref(self.model, self.providers)
        if model_ref.provider is None:
            # 没解析出 provider 就没有 baseURL，而 pi-ai 的 hand-declared route
            # 要求 api + baseURL + models 三者齐全——硬塞过去的表现是 runtime
            # 在 plugin load 阶段炸掉，错误埋在几百行 JS 栈里
            # （'llm-pi-ai: provider "octagon" … needs a baseURL'）。
            # 这里 fail fast，把原因说清楚。
            return _early_exit(
                status="cli_error",
                error_code="dsh_provider_unresolved",
                error_message=(
                    f"模型 {self.model!r} 没有匹配到任何 model_providers 前缀"
                    f"（已知：{sorted(self.providers) or '无'}）。"
                    "dsh 必须经 provider 拿到 base_url/api_key 才能生成 pi-ai route。"
                ),
            )
        provider = self.providers[model_ref.provider]
        # key 缺失时 runtime 只会在上游报 401，错误面目全非——这里 fail fast。
        # 判定必须走 resolve_agent_key（合并 cost 的 run/attempt 专属 key），
        # 不能只看静态 resolve_api_key：开了 cost.enabled 且无静态
        # OPENROUTER_API_KEY 时，key 只存在于动态 run key 里，裸 resolve_api_key
        # 会误判缺 key。与 kimi_code 一致。
        _probe_key, _ = resolve_agent_key(
            task.run_id, resolve_api_key(provider), task.attempt_id,
            provider.base_url,
        )
        if provider.api_key_env and _probe_key is None:
            return _early_exit(
                status="auth_failed",
                error_code="provider_api_key_missing",
                error_message=(
                    f"provider {model_ref.provider!r} 缺 API key："
                    f"环境变量 {provider.api_key_env} 未设置，"
                    f"octagon.yaml model_providers.{model_ref.provider}.api_key 也未填"
                ),
            )

        try:
            # 配置生成也要在保护内：MCP serverName 映射后重名会抛 ValueError
            # （dsh 对重名是 plugin load 硬失败，所以我们提前拦），
            # 而 adapter 契约要求任何失败都包成 error_code，不得向上抛。
            cordis_path, api_key, max_tokens = self._write_cordis(
                task, attempt_dir, model_ref, workspace
            )
            subprocess_env = self._subprocess_env(
                task, attempt_dir, workspace, api_key
            )
        except ValueError as exc:
            message = str(exc)
            return _early_exit(
                status="cli_error",
                error_code=(
                    "dsh_mcp_server_name_collision" if "重名" in message
                    else "dsh_cordis_build_failed"
                ),
                error_message=message,
            )
        except Exception as exc:  # noqa: BLE001 - 契约要求不向上抛
            return _early_exit(
                status="cli_error",
                error_code="dsh_cordis_build_failed",
                error_message=f"{type(exc).__name__}: {exc}",
            )
        prompt = self._render_prompt(task)

        started_at = datetime.now(timezone.utc)
        deadline = AttemptDeadline(task.timeout_seconds)

        events_count = 0
        thinking_count = 0
        tool_call_count = 0
        last_event_at: str | None = None
        total_usage = empty_usage()
        error_message: str | None = None
        status = "completed"
        # 当前轮的 sink。提到循环外是为了让**任何**异常路径都能收口它已经
        # 落盘的事件——sink 是流式写的，磁盘上已有内容，统计却报 0 会让
        # events.jsonl 与 AdapterResult / DB 对不上账（而两边都"看起来正常"）。
        sink: _TurnSink | None = None

        def _absorb(current: _TurnSink | None) -> int:
            """把某一轮已落盘的量并进 attempt 级统计。返回该轮事件数。

            幂等：并过的 sink 会被清零标记，重复调用不会重复计数
            （超时分支与 except 块可能先后触及同一个 sink）。
            """
            nonlocal events_count, thinking_count, tool_call_count
            nonlocal last_event_at, total_usage
            if current is None or current.absorbed:
                return 0
            current.absorbed = True
            events_count += current.events
            thinking_count += current.thinking
            tool_call_count += current.tool_calls
            last_event_at = current.last_event_at or last_event_at
            total_usage = merge_usage(total_usage, current.usage)
            return current.events

        harness: Any = None
        try:
            harness = DeepSeekHarness(
                provider=PROVIDER_ROUTE,
                model=model_ref.model,
                # 不给就 400：llm-pi-ai/llm-deepseek 的默认输出上限远超多数
                # 模型的上下文，且 adapter 不按 contextWindow 夹紧。
                # 传 None 等于没传。
                max_tokens=max_tokens,
                cwd=str(workspace.resolve()),
                runtime_cwd=str(attempt_dir.resolve()),
                session_root=str((attempt_dir / "dsh_sessions").resolve()),
                cordis=str(cordis_path.resolve()),
                env=subprocess_env,
                # **必须给**：默认 None = 单次 JSON-RPC 请求永不超时
                # （`client._request_raw` 在无 timeout 的 queue.get() 上死等）。
                # initialize 卡住时 attempt 会挂到天荒地老——实测踩过：8 秒
                # deadline 的 attempt 跑了 5 分钟仍未返回，栈停在 initialize。
                request_timeout_seconds=_REQUEST_TIMEOUT_SECONDS,
            )
            # start() 是**同步阻塞**的（起子进程 + initialize 握手），
            # 在 async 函数里直接调会让 deadline 管不到它——放线程池并
            # 用 deadline 兜底，与 _run_turn 同一处理。
            await self._call_blocking(harness.start, deadline)
            session = harness.start_session(session_id)

            for turn in plan.send_message_turns:
                try:
                    deadline.check_before_turn()
                except AttemptBudgetExhausted:
                    error_message = ERROR_BUDGET_EXHAUSTED_BETWEEN_TURNS
                    status = "timeout"
                    conversation_trace.turn_failed(
                        turn, producer_session_id=session_id,
                        error_code="timeout", error_summary=error_message,
                    )
                    break

                _checkpoint(active_turn_index=turn.turn_index)
                conversation_trace.turn_started(turn, producer_session_id=session_id)
                turn_prompt = render_turn_prompt(task, turn, base_prompt=prompt)

                # 流式落盘：事件一到就写，**不等 Session.run() 返回**。
                # 超时会由 close() 硬杀，若攒到最后才写，那一轮已经产生的
                # 全部事件都会丢——而长轮恰恰是最可能超时、也最值得留证据的。
                sink = _TurnSink(
                    turn, events_path, thinking_path, session_id=session_id
                )
                try:
                    result = await self._run_turn(
                        session, turn_prompt, deadline, sink
                    )
                except TimeoutError:
                    # SDK 无取消通道，close() 是唯一手段（实测 0.05s、无残留）。
                    self._safe_close(harness)
                    kept = _absorb(sink)
                    error_message = (
                        f"dsh attempt 超时，已强制关闭 runtime"
                        f"（该轮已落盘 {kept} 条事件）"
                    )
                    status = "timeout"
                    conversation_trace.turn_failed(
                        turn, producer_session_id=session_id,
                        error_code="timeout", error_summary=error_message,
                    )
                    break

                _absorb(sink)

                if result.finish_reason not in (None, "completed"):
                    error_message = self._turn_error(result)
                    status = "cli_error"
                    conversation_trace.turn_failed(
                        turn, producer_session_id=session_id,
                        error_code=result.finish_reason,
                        error_summary=error_message,
                    )
                    break

                conversation_trace.turn_completed(
                    turn, producer_session_id=session_id
                )
                if not plan.is_legacy:
                    _checkpoint(
                        active_turn_index=None,
                        last_completed_turn_index=turn.turn_index,
                    )
            else:
                conversation_trace.conversation_completed()

            if error_message:
                conversation_trace.conversation_failed(
                    error_code=None, error_summary=error_message
                )
            conversation_trace.close()

        except (TimeoutError, asyncio.TimeoutError):
            # 握手阶段就超时（deadline 太短，或 runtime 起不来卡在 initialize）。
            # 与轮内超时同一终态——都是"用户的耐心预算用尽"，不是 agent 出错。
            self._safe_close(harness)
            error_message = "dsh attempt 超时（runtime 握手未完成），已强制关闭"
            status = "timeout"
            conversation_trace.conversation_failed(
                error_code="timeout", error_summary=error_message
            )
            conversation_trace.close()
        except Exception as exc:  # noqa: BLE001 - 契约要求不向上抛
            # SDK 的传输/协议异常（JsonRpcError / TransportClosedError / …）
            # 的字符串里已含 exit code 与 stderr tail（client._transport_detail）。
            # SDK 没有公开的 stderr accessor，故不落 stderr.txt——名字要诚实。
            detail = f"{type(exc).__name__}: {exc}"
            (attempt_dir / "sdk_error.txt").write_text(detail, encoding="utf-8")
            # 事件是流式落盘的：异常发生前已经写进 events.jsonl 的那些，
            # 统计里必须算上。不收口的话磁盘有事件而 DB 报 0——两边各自
            # 看起来都正常，只有对账时才发现证据与统计不一致。
            kept = _absorb(sink)
            if kept:
                detail = f"{detail}（该轮已落盘 {kept} 条事件）"
            error_message = detail
            status = "cli_error"
            conversation_trace.conversation_failed(
                error_code="sdk_error", error_summary=detail[:500]
            )
            conversation_trace.close()
        finally:
            self._safe_close(harness)

        duration_ms = int(
            (datetime.now(timezone.utc) - started_at).total_seconds() * 1000
        )
        return AdapterResult(
            attempt_id=task.attempt_id,
            status=status,
            external_refs={
                "model_used": self.model,
                "session_id": session_id,
                "cordis_path": str(cordis_path),
                **(
                    {}
                    if status == "completed"
                    else classify_cli_error(error_message).as_refs()
                ),
            },
            error_code=(
                None if status == "completed"
                else "timeout" if status == "timeout"
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
            security_meta=build_security_meta(
                execution_locus=self.capabilities.execution_locus,
                # 如实记录：SDK 路线下没有 approval 层，也不存在等价于
                # `--dangerously-skip-permissions` 的开关。伪造一个看起来
                # 对齐的值会误导安全轴。
                permission_mode="no-approval-plugin (sdk default)",
                workspace_root=str(workspace.resolve()),
            ),
        )

    @staticmethod
    def _safe_close(harness: Any) -> None:
        """关停 runtime；失败只记日志。

        `close()` 自身抛异常时不能让它改写业务终态——超时就是超时，
        关停不干净是另一回事（且 SDK 的 close 内部已有 terminate→kill 兜底）。
        """
        if harness is None:
            return
        try:
            harness.close()
        except Exception:  # noqa: BLE001
            logger.warning("dsh harness.close() 失败", exc_info=True)

    @staticmethod
    async def _call_blocking(fn: Any, deadline: AttemptDeadline):
        """把 SDK 的同步阻塞调用放线程池，用 deadline 兜底。

        SDK 的 `start()` 与 `Session.run()` 都是**同步阻塞**的，且后者事实上
        无界——它在没有 timeout 的 `queue.Queue.get()` 上等到 session idle。
        SDK 也没有 cancel 方法（只有 initialize/session_prompt/shutdown），
        所以超时后只能由调用方 `close()` 硬杀；那会让阻塞线程抛
        `TransportClosedError` 醒来（实测），future 不需要再 await。

        用 `shield` 是刻意的：`wait_for` 取消不了 executor 线程，不 shield 的话
        取消请求会挂在那个永远不响应取消的 future 上。
        """
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(None, fn)
        # 超时后这个 future 仍活着，稍后被 close() 唤醒并抛 TransportClosedError。
        # 没人 await 它，Python 会在 GC 时打 "Future exception was never
        # retrieved" 噪音——这里主动消费掉：那个异常是**预期**的收尾信号，
        # 不是需要上报的故障（真正的失败已由 status=timeout 表达）。
        fut.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        return await asyncio.wait_for(asyncio.shield(fut), timeout=deadline.remaining())

    async def _run_turn(
        self, session: Any, prompt: str, deadline: AttemptDeadline, sink: "_TurnSink"
    ):
        """跑一轮。`on_notification=sink` 让事件实时落盘（见 _TurnSink）。"""
        return await self._call_blocking(
            lambda: session.run(prompt, on_notification=sink), deadline
        )

    @staticmethod
    def _turn_error(result: Any) -> str:
        """从 turn/end 里挖错误正文（finish_reason 只给 kind）。"""
        for ev in reversed(result.events):
            if not isinstance(ev, dict) or ev.get("type") != "turn/end":
                continue
            data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
            reason = data.get("reason") if isinstance(data.get("reason"), dict) else {}
            err = reason.get("error") if isinstance(reason.get("error"), dict) else {}
            message = err.get("message") or reason.get("kind")
            code = err.get("code")
            if message:
                return f"{message} (code={code})" if code else str(message)
        return f"dsh turn finished with reason={result.finish_reason!r}"

    def _render_prompt(self, task: AdapterRunInput) -> str:
        """与 codex/opencode 同构：无独立 system 通道，时间预算落 message 顶部。"""
        parts: list[str] = []
        notice = (
            time_budget_notice(task.timeout_seconds)
            if task.notify_model_of_timeout
            else None
        )
        if notice:
            parts.append(notice)
        parts.append(task.task_prompt)
        context = prompt_context(task.task_context)
        if context:
            parts.append(
                "任务上下文：\n"
                + json.dumps(context, ensure_ascii=False, indent=2)
            )
        return "\n\n".join(parts)
