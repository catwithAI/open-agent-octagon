"""BladeServiceAdapter — 按 BA 产品场景调用已运行的 blade-agent server。

不 import blade_agent.host.Engine，不自己手写 Socket.IO。
SDK 封装了 REST + Socket.IO + 断线处理 + 并发订阅。

产品入口：
- 软件开发场景：调用 ``/api/agent-board/projects``，由软件工厂创建项目、
  绑定 ``app-dev/default`` 会话并自动启动首轮；Octagon 只订阅既有会话。
- 普通聊天场景：像首页一样创建 ``general_chat`` 会话，再用 SDK Socket
  发送消息。
- 显式 blade_native 场景：保留 benchmark 声明的原生 skill / solution。

原生入口（见 docs/specs/batch_benchmark/design_blade_native.md）：
- session 用 primary_skill_id / solution_id 指向 blade server 上已注册的真实 skill，
  不再向 session 上传 skill 薄壳（旧 env 的薄壳经 sync_blade_skills 同步到
  skills_path，由 server 启动时 BLADE_SKILL_PATHS 注册，同样走 primary_skill_id）。
- prompt 只传任务消息，工具引导交给 blade 真实 AGENTS.md / SKILL.md。
- trace 从 get_history 的 nodes 提取（blade 端权威记录），归一成 Octagon trace.jsonl。
- 产物回收通用化：递归拉取 workspace 文件，scorer 自己判定。
- 会话、文件和 Socket 流程走 SDK；SDK 尚未覆盖的后台任务清理直接调用 FastAPI REST。
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import inspect
import json
import logging
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx  # SDK 未覆盖的 /messages 与后台任务清理直接调用 REST

# blade_agent_kit 是可选依赖：只有真正跑 blade attempt 时才需要。缺失时（如纯展示
# 部署、未安装 blade-agent 仓库）用占位顶替，让模块导入、sync_blade_skills、服务启动
# 都不受影响；BladeServiceAdapter 实例化时才抛出明确错误。
try:
    from blade_agent_kit import (
        BladeAgentClient,
        BladeAuthError,
        BladeChatError,
    )

    _BLADE_KIT_AVAILABLE = True
    _BLADE_KIT_IMPORT_ERROR: Exception | None = None
except ImportError as _exc:  # pragma: no cover - 依赖缺失路径
    _BLADE_KIT_AVAILABLE = False
    _BLADE_KIT_IMPORT_ERROR = _exc

    class BladeAuthError(Exception):
        """占位：blade_agent_kit 未安装时的替身，仅保证 except 子句可引用。"""

    class BladeChatError(Exception):
        """占位：同上。"""

    class BladeAgentClient:  # type: ignore[no-redef]
        """占位：实例化即报错，指明缺失的依赖。"""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError(
                "blade_agent_kit 未安装，无法使用 BladeServiceAdapter。"
                "纯展示/CC/Codex 场景不需要它；要跑 blade attempt 请安装 "
                "blade-agent 仓库的 agent-kit-python。"
            ) from _BLADE_KIT_IMPORT_ERROR

import re

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
    compact_usage,
    empty_usage,
    merge_usage,
    usage_detail,
    usage_input_tokens,
    usage_output_tokens,
)
from .error_taxonomy import classify, retry_delay_seconds, should_retry
from ..artifact_scope import ARTIFACT_SKIP_DIRS
from .edited_paths import agent_edited_paths
from ..conversation.plan import effective_conversation
from ..conversation.summary import summarize_conversation
from ..conversation.turns import with_turn_ext
from ..conversation.writer import CONVERSATION_FILENAME, ConversationTraceWriter

logger = logging.getLogger(__name__)

_CHAT_END_SETTLE_SECONDS = 0.1
_ACTIVE_CHAT_RETRY_DELAYS = (0.2, 0.4, 0.8)
_RECOVERY_READ_MAX_ATTEMPTS = 4
_CLEANUP_MAX_ATTEMPTS = 3
_RUNNING_BACKGROUND_TASK_STATUSES = {"running", "starting"}
_RUNNING_SESSION_STATUSES = {
    "created",
    "initializing",
    "pending",
    "running",
    "starting",
    "queued",
    "in_progress",
}
_TERMINAL_SESSION_STATUSES = {
    "completed",
    "ok",
    "failed",
    "error",
    "cancelled",
    "canceled",
    "interrupted",
    "stopped",
}


def _is_not_found_error(exc: BaseException) -> bool:
    response = getattr(exc, "response", None)
    return (
        getattr(response, "status_code", None) == 404
        or getattr(exc, "status_code", None) == 404
    )


async def _wait_for_chat_end_settle(seconds: float = _CHAT_END_SETTLE_SECONDS) -> None:
    await asyncio.sleep(seconds)


def _is_active_chat_error(result) -> bool:
    """迭代续跑失败是否为瞬态 active-chat 竞态（应重试）。"""
    return "has an active chat" in (result.error_message or "")

# blade 沙盒里 skill 工具经 CLI 执行：`blade skill run "<skill>" <tool> --args '<json>'`。
# trace 的 tool_name 是 Bash，skill 级语义要从 command 解析（对比视图对齐 / DAG 检查用）。
_BLADE_SKILL_RUN_RE = re.compile(
    r"blade\s+skill\s+run\s+\"?([^\"\s]+)\"?\s+(\w+)(?:\s+--args\s+'(.*?)')?",
    re.DOTALL,
)

# 产物回收时跳过的目录。与 API 扫描、评分快照共用同一份净产物边界——
# 这里原本自带一份清单且漏了 dist/build，导致「回收到的产物」与「判分看到的
# 产物」口径不一致。
_ARTIFACT_SKIP_DIRS = ARTIFACT_SKIP_DIRS
_ARTIFACT_MAX_FILES = 500

# 从 events.jsonl 里抠出 agent 用 Edit/Write 类工具写过的 workspace 相对路径。
# 事件里的工具参数可能是原始 JSON，也可能是被再次 JSON 转义的字符串
# （recovered messages），所以两种写法都匹配。只认 file_path / path 两个键。
_EDIT_PATH_PATTERNS = [
    re.compile(r'\\"(?:file_path|path)\\":\s*\\"([^\\"]+)\\"'),
    re.compile(r'"(?:file_path|path)":\s*"([^"]+)"'),
]
_EDIT_TOOL_MARKERS = ("Edit", "Write", "write_file", "edit_file", "MultiEdit", "create_file")


def _agent_edited_paths(attempt_dir: Path) -> list[str]:
    """agent 在远端 workspace 里写过的文件（相对路径），用于产物回收优先拉取。

    2026-09-14 复盘：django 仓库 6000+ 文件，BFS 回收 500 个就截断，
    agent 改的 django/utils/numberformat.py 根本轮不到，本地 workspace
    停在基线 → functional 0 分，而 agent 其实已改完并跑绿测试。
    """
    events_path = attempt_dir / "events.jsonl"
    if not events_path.is_file():
        return []
    found: list[str] = []
    seen: set[str] = set()
    try:
        for line in events_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not any(marker in line for marker in _EDIT_TOOL_MARKERS):
                continue
            for pattern in _EDIT_PATH_PATTERNS:
                for match in pattern.finditer(line):
                    raw = match.group(1).strip()
                    if not raw or raw.startswith("/") or ".." in raw.split("/"):
                        continue
                    rel = raw[2:] if raw.startswith("./") else raw
                    if rel and rel not in seen:
                        seen.add(rel)
                        found.append(rel)
    except OSError:
        return found
    return found
_ARTIFACT_MAX_DEPTH = 5

# 优先回收时试探的远端前缀：日志里的路径未必与远端工作区同根。
_REMOTE_PATH_PREFIXES = ("", "workspace/", "skill_workspace/", "output/")


def _digest_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _digest_of(path: Path) -> str | None:
    """文件当前内容的摘要；不存在返回 None。"""
    try:
        return _digest_bytes(path.read_bytes())
    except OSError:
        return None


def _candidate_remote_paths(rel: str, *, project_workspace: str = "") -> list[str]:
    """一个相对路径在远端可能的几种写法，按可能性排序。"""
    # `lstrip("./")` 按字符集剥，会把 `.hidden/report.md` 削成
    # `hidden/report.md`，每个候选前缀都指向不存在的路径。
    clean = rel
    while clean.startswith("./"):
        clean = clean[2:]
    seen: dict[str, None] = {}
    # software_factory 场景：agent 的 cwd 是项目工作区绝对路径，而文件接口的
    # "." 指向另一份带基线物料的目录——两边都有 repo/…，只有前者带 agent 的
    # 改动。所以它必须排在最前（2026-09-14 现场结论）。
    if project_workspace:
        seen.setdefault(f"{project_workspace.rstrip('/')}/{clean}", None)
    for prefix in _REMOTE_PATH_PREFIXES:
        seen.setdefault(f"{prefix}{clean}", None)
    # 只剩文件名的写法：agent 在工作区根上直接写文件的常见情形。
    name = Path(clean).name
    if name != clean:
        seen.setdefault(name, None)
    return list(seen)

# blade 主循环的 loop_name；fork 出的子 agent 形如 "agent:<8hex>"。
_ROOT_LOOP = "root"


def _event_loop_name(raw: Any) -> Any:
    """从事件里取 loop_name。

    新协议 socket 行：``raw.payload.loop_name``；旧协议 turn:end / poll entry /
    history node：``raw.loop_name``。两处都取不到时归 root——不臆造子 agent 归属。
    """
    if not isinstance(raw, dict):
        return None
    payload = raw.get("payload")
    if isinstance(payload, dict) and payload.get("loop_name"):
        return payload["loop_name"]
    return raw.get("loop_name")


def match_interaction_turn(
    pending: list[Any], pause_tool_data: dict[str, Any], pause_tool: str | None
) -> Any | None:
    """按 wait_for 在待处理 answer_interaction turns 里找匹配项。

    - ``tool_name`` 对 ``pause_tool``（值带 provider 前缀，如
      ``builtin:AskUserQuestion``），大小写敏感全等；
    - ``question_key`` 非空时还需命中 ``arguments.questions[].question``
      （子串匹配，允许场景作者只写关键词）；
    匹配不到返回 None → 调用方按 unexpected interaction 处理，绝不猜答案。
    """
    if not pending:
        return None
    questions = []
    args = pause_tool_data.get("arguments")
    if isinstance(args, dict) and isinstance(args.get("questions"), list):
        questions = [
            str(q.get("question", ""))
            for q in args["questions"]
            if isinstance(q, dict)
        ]
    for turn in pending:
        wait_for = turn.wait_for
        if wait_for is None or wait_for.tool_name != pause_tool:
            continue
        key = wait_for.question_key
        if key and not any(key in q for q in questions):
            continue
        return turn
    return None



def _interaction_question_summary(
    pause_tool_data: dict[str, Any], *, limit: int = 3
) -> list[str]:
    """unexpected interaction 的问题摘要（供错误消息/排查用，非敏感）。"""
    args = pause_tool_data.get("arguments")
    if not isinstance(args, dict) or not isinstance(args.get("questions"), list):
        return []
    out: list[str] = []
    for question in args["questions"][:limit]:
        if isinstance(question, dict):
            text = str(question.get("question", "")).strip()
            if text:
                out.append(text[:200])
    return out


def build_askuser_answer(
    turn: Any, pause_tool_data: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    """把静态 answer 翻译成 blade 的 askuser_answer + 展示用答案文本。

    支持两种写法（spike 结论第 4 点）：
    - blade 原生形状 ``{"selections": {...}}`` / ``{"custom": {...}}`` 直接透传；
    - ``{"option_label": "方案A"}`` 由本函数按 label 在 pause_tool_data 里解析成
      question/option index。
    ``tool_call_id`` 永远取自运行时 pause_tool_data，不从静态 answer 读。
    """
    answer = dict(turn.answer or {})
    tool_call_id = str(pause_tool_data.get("tool_call_id") or "")

    selections = answer.get("selections")
    custom = answer.get("custom")
    text_parts: list[str] = []

    if selections is None and custom is None:
        label = answer.get("option_label")
        args = pause_tool_data.get("arguments")
        questions = (
            args.get("questions")
            if isinstance(args, dict) and isinstance(args.get("questions"), list)
            else []
        )
        resolved: dict[str, list[int]] = {}
        if isinstance(label, str) and label:
            for q_index, question in enumerate(questions):
                if not isinstance(question, dict):
                    continue
                for o_index, option in enumerate(question.get("options") or []):
                    if isinstance(option, dict) and option.get("label") == label:
                        resolved[str(q_index)] = [o_index]
                        text_parts.append(label)
                        break
                if resolved:
                    break
        if not resolved:
            # 没有可解析的选项：退化为自由文本（answer.text 或 option_label 原文）
            free_text = str(answer.get("text") or label or "")
            if not free_text:
                raise ValueError(
                    f"answer_interaction turn {turn.turn_id!r} 的 answer 无法翻译成"
                    " blade 应答：需要 selections/custom/option_label/text 之一"
                )
            custom = {"0": free_text}
            text_parts.append(free_text)
        else:
            selections = resolved

    payload: dict[str, Any] = {"tool_call_id": tool_call_id}
    payload["selections"] = selections if isinstance(selections, dict) else {}
    payload["custom"] = custom if isinstance(custom, dict) else {}

    if not text_parts:
        # 从 selections 反查 label 作为展示文本；失败则用 custom 文本
        args = pause_tool_data.get("arguments")
        questions = (
            args.get("questions")
            if isinstance(args, dict) and isinstance(args.get("questions"), list)
            else []
        )
        for q_key, o_indexes in payload["selections"].items():
            try:
                question = questions[int(q_key)]
                options = question.get("options") or []
            except (ValueError, IndexError, AttributeError, TypeError):
                continue
            for o_index in o_indexes or []:
                try:
                    text_parts.append(str(options[int(o_index)].get("label", "")))
                except (ValueError, IndexError, AttributeError, TypeError):
                    continue
        text_parts.extend(str(v) for v in payload["custom"].values() if v)

    answer_text = "；".join(p for p in text_parts if p) or "（已选择）"
    return payload, answer_text


def _session_create_error_detail(exc: Exception, entry: dict[str, Any]) -> str:
    """Preserve Blade's validation body instead of returning a generic 422."""
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    body = ""
    if response is not None:
        try:
            body = response.text
        except Exception:  # pragma: no cover - defensive for SDK response wrappers
            body = ""
    # 有一类异常 `str()` 是空的（httpx 的超时类尤其常见），只拼 str(exc) 会得到
    # 一条以 "; " 开头、什么都没说的错误——排查时连"是超时还是被拒"都分不出。
    # 异常类名永远非空，兜底用它。
    summary = str(exc) or f"{type(exc).__module__}.{type(exc).__qualname__}"
    parts = [summary, f"blade_entry={json.dumps(entry, ensure_ascii=False)}"]
    if status_code is not None:
        parts.append(f"status_code={status_code}")
    if body:
        parts.append(f"response_body={body[:2000]}")
    return "; ".join(parts)


def _iterative_artifact_sync_error(result: AdapterResult) -> str | None:
    sync = result.external_refs.get("artifact_sync")
    if not isinstance(sync, dict):
        return "产物同步结果缺失"
    if sync.get("error"):
        return str(sync["error"])
    errors = sync.get("errors")
    if isinstance(errors, list) and errors:
        return "; ".join(str(item) for item in errors[:10])
    if sync.get("truncated_at") is not None:
        return f"产物同步在 {sync['truncated_at']} 个文件处截断"
    return None


def artifact_recovery_failed(external_refs: dict[str, Any]) -> bool:
    """优先路径一个都没落地 —— 产物没回收到，分数不可用。

    这是 2026-09-18 横评里 blade-agent 27 个 attempt 全部低分的形状：attempt
    正常跑完、拿到一个真实的低分，而那个分数衡量的是空工作区。把它标成
    infrastructure，统计时才能与「agent 确实没做好」分开（需求 6.3）。

    只有「有依据、且全部落空」才算失败：`priority_source == "none"` 表示压根
    没提取到路径，那是另一种情况（需求 4.4），不在这里判。
    """
    sync = external_refs.get("artifact_sync")
    if not isinstance(sync, dict):
        return False
    missing = sync.get("priority_missing")
    if not isinstance(missing, list) or not missing:
        return False
    got = sync.get("priority_downloaded")
    return not (isinstance(got, list) and got)


def _fail_incomplete_iterative_artifact_sync(result: AdapterResult) -> bool:
    if result.status != "completed":
        return False
    detail = _iterative_artifact_sync_error(result)
    if detail is None:
        return False
    result.status = "chat_failed"
    result.error_code = "iteration_artifact_sync_failed"
    result.error_message = detail
    return True


@dataclass
class BladeAdapterConfig:
    base_url: str
    skills_path: Path
    keep_blade_session: bool = False
    api_key: str | None = None
    model: str | None = None
    enable_thinking: bool | None = None
    # blade docker 沙盒回调 Octagon env server 的地址（沙盒视角）。
    # 不填回落 task.env_base_url（public_base_url）。
    sandbox_env_base_url: str | None = None
    request_timeout_seconds: float = 30.0
    inactivity_timeout_seconds: float = 300.0
    reconnect_timeout_seconds: float = 30.0
    progress_poll_interval_seconds: float = 4.0
    # blade server 是否声明支持 session metadata 透传（协商产物）。
    # False 时收到非空 blade_session_metadata 只记 gap，不改任何请求。
    session_metadata_capability: bool = False


@dataclass
class AdapterEnv:
    name: str
    skill_id: str  # "octagon/<name>"，未声明 blade_native 入口时作为 primary_skill_id 回落
    blade_skill_dir: Path | None = None  # 旧字段：仅 sync_blade_skills 预同步用，运行期不再上传
    primary_skill_id: str | None = None
    solution_id: str | None = None
    biz_role_id: str | None = None
    # software_factory: /api/agent-board/projects（app-dev）
    # chat: /api/sessions + Socket chat:send（general_chat）
    # native: benchmark 显式声明的 Blade skill / solution
    surface: str = "native"

    @property
    def effective_primary_skill_id(self) -> str | None:
        if self.solution_id:
            return None
        return self.primary_skill_id or self.skill_id


# blade 沙盒的执行场合附加字段（两处 security_meta 共用）。
BLADE_SANDBOX_SECURITY_EXTRA = {
    "sandbox_shared": True,
    "sandbox_managed_by": "blade-server",
    "egress_policy": "unrestricted",
}


class BladeServiceAdapter:
    # 能力静态声明：blade-agent server 是本机/局域网内已运行的固定
    # 服务（TCP 可达即可），不是公网 API——network_required 取 local_service
    # 而非 public_internet（三态语义）。
    capabilities = AdapterCapabilities(
        execution_locus="docker-sandbox",
        network_required="local_service",
        # 非 headless + chat:send askuser_answer 通道已实测可用
        # （spike-blade-interaction-answer.md），支持 answer_interaction turn。
        interaction_answer=True,
        iterative_session=True,
    )

    def __init__(self, config: BladeAdapterConfig) -> None:
        self.config = config

    @property
    def wire_capture_capabilities(self) -> dict[str, Any]:
        """wire injection 消费能力声明——由 resolved config 决定，供 lifecycle
        在 agent 启动前过滤（capability 不支持时 metadata 根本不会下发到本
        adapter，create_session 里的 gate 只是纵深防御）。"""
        return {
            "blade_session_metadata": self.config.session_metadata_capability,
        }

    def _new_client(self) -> BladeAgentClient:
        client_kwargs: dict[str, Any] = {
            "token": self.config.api_key,
            "timeout": self.config.request_timeout_seconds,
        }
        if "reconnect_grace_seconds" in inspect.signature(
            BladeAgentClient
        ).parameters:
            client_kwargs["reconnect_grace_seconds"] = (
                self.config.reconnect_timeout_seconds
            )
        client = BladeAgentClient(self.config.base_url, **client_kwargs)
        # 兼容尚未升级构造参数、但已有重连实现的 Python SDK。
        socket_client = getattr(client, "_socket", None)
        if socket_client is not None and hasattr(
            socket_client, "_reconnect_grace_seconds"
        ):
            socket_client._reconnect_grace_seconds = (
                self.config.reconnect_timeout_seconds
            )
        return client

    async def run(
        self,
        task: AdapterRunInput,
        env: AdapterEnv,
        data_path: Path,
        *,
        resume_session_id: str | None = None,
        resume_after_turn_index: int | None = None,
        preserve_session: bool = False,
    ) -> AdapterResult:
        """跑一次 attempt。

        `resume_session_id`/`resume_after_turn_index` 只由多轮恢复路径传入：
        复用已存在的 Blade session，并跳过 checkpoint 证明已完成的
        轮次——已完成轮可能有外部副作用，重放会造成重复执行。二者必须同时给
        （只给 session 不给进度就无从判断该从哪轮续），正常首跑都是 None，
        `AgentAdapter` Protocol 的三参数调用不受影响。
        """
        if (resume_session_id is None) != (resume_after_turn_index is None):
            raise ValueError(
                "resume_session_id 与 resume_after_turn_index 必须同时提供"
            )
        if env.surface == "software_factory" and resume_session_id is None:
            return await self._run_software_factory(task, env, Path(data_path))

        data_path = Path(data_path)
        attempt_dir = data_path / "attempts" / task.attempt_id
        attempt_dir.mkdir(parents=True, exist_ok=True)
        events_path = attempt_dir / "events.jsonl"
        thinking_path = attempt_dir / "thinking.jsonl"

        if not self.config.api_key:
            return AdapterResult(
                attempt_id=task.attempt_id,
                status="auth_failed",
                error_code="api_key_missing",
                error_message="blade.api_key not configured",
            )

        blade_session_id: str | None = None
        events_count = 0
        thinking_count = 0
        tool_call_count = 0
        # attempt 级五维累计（token_cost_accounting）。per-loop 分桶（agent_stats）
        # 仍只记 input/output——它服务于 sub-agent 拓扑展示，不参与计价。
        total_usage: dict[str, int | None] = empty_usage()
        # loop_name -> 该 agent 的 usage/tool_call/thinking。非 headless
        # 起 fork:Agent 可产出真实 sub-agent，顶层四个累加器仍是"全部去重后 unique
        # calls 总和"（子 agent 不写进 root 桶，顶层是各桶加总）。这是 adapter 侧
        # 诊断/对账摘要，不是 normalizer 数据源——per-agent 权威 usage 由 finalizer
        # 从 canonical calls 重新聚合。
        agent_stats: dict[str, dict[str, int]] = {}

        def _bucket(loop_name: Any) -> dict[str, int]:
            key = loop_name if isinstance(loop_name, str) and loop_name else _ROOT_LOOP
            return agent_stats.setdefault(key, {
                "input_tokens": 0, "output_tokens": 0,
                "tool_call_count": 0, "thinking_count": 0,
            })

        last_event_at: str | None = None
        chat_end_status: str | None = None
        chat_end_finish_reason: str | None = None
        # 非 headless 暂停信号（spike 结论 1）：chat:end 的 pause_tool /
        # pause_tool_data，交互应答分支据此匹配 answer_interaction turn。
        chat_end_pause_tool: str | None = None
        chat_end_pause_tool_data: dict[str, Any] | None = None
        interaction_answered_count = 0
        unexpected_interaction: dict[str, Any] | None = None
        error_message: str | None = None
        model_used: str | None = None
        last_activity_monotonic = time.monotonic()
        # 网页端收到服务端 200ms 一批的 turn:events 后才更新一次 store。
        # Python SDK 会把批次重新拆成逐条 RawEvent；若每条都 replace
        # progress.json，实验端会凭空制造网页不存在的高频磁盘 I/O。
        last_progress_write_monotonic = 0.0
        started_at = datetime.now(timezone.utc)
        seen_projection_ids: set[str] = set()
        seen_thinking: set[str] = set()
        # 新流式协议（turn:events → llm:*:delta / llm:response:done）下的统计去重：
        # llm:response:done 只有 stream_sequence 作稳定去重键（断线重连回放时相同），
        # 用它避免 token/tool_call 重复累加。thinking 按回合聚合：一段连续
        # llm:thinking:delta（以 llm:response:done 收尾）算 1 段。
        #
        # 去重键必须带 chat 轮次：服务端每次 chat:send 新建一个 SocketBridge，
        # `_raw_stream_sequence` 随之从 1 重新开始（blade server bridge.py:161）。
        # 多轮 attempt 里第 2 轮的 seq=1 与第 1 轮的 seq=1 是不同事件，只用
        # stream_sequence 去重会把后续所有轮的 usage 全部误判为重放丢弃。
        # 断线重连的回放发生在同一 chat 轮内，加轮次前缀不影响它。
        chat_round = 0
        seen_response_seqs: set[tuple[int, int]] = set()
        seen_tool_call_ids: set[str] = set()
        pending_thinking_parts: list[str] = []
        progress_path = attempt_dir / "progress.json"
        progress: dict[str, Any] = {
            "transport_status": "unknown",
            "last_transport_event_at": None,
            "last_history_growth_at": None,
            "last_agent_activity_at": None,
            "history_node_count": 0,
            "fallback_turn_count": 0,
            "updated_at": _now_iso(),
        }
        _write_json(progress_path, progress)

        try:
            async with self._new_client() as client:
                # 1) 创建 session（路线 A：指定真实注册的 skill / solution）
                try:
                    is_web_chat = env.surface == "chat"
                    create_kwargs: dict[str, Any] = {}
                    # 原生 benchmark 保留历史隔离语义；网页普通聊天不传这个
                    # 字段，交给 BA 服务端采用与首页相同的默认值。
                    if not is_web_chat:
                        create_kwargs["memory_enabled"] = False
                    if self.config.model:
                        create_kwargs["model"] = self.config.model
                    if self.config.enable_thinking is not None:
                        create_kwargs["enable_thinking"] = self.config.enable_thinking
                    if env.solution_id:
                        create_kwargs["solution_id"] = env.solution_id
                        if env.biz_role_id:
                            create_kwargs["biz_role_id"] = env.biz_role_id
                    elif env.effective_primary_skill_id:
                        create_kwargs["primary_skill_id"] = env.effective_primary_skill_id
                    blade_entry = (
                        {"solution_id": env.solution_id, "biz_role_id": env.biz_role_id}
                        if env.solution_id
                        else {"primary_skill_id": env.effective_primary_skill_id}
                    )
                    # wire injection 消费点（表 Blade 行）：仅在服务
                    # capability 声明支持时透传 session metadata；不支持时记 gap，
                    # 且绝不改 base URL（REST/Socket.IO 共用 endpoint）。
                    wi = task.wire_injection
                    if wi.enabled and wi.blade_session_metadata:
                        if self.config.session_metadata_capability:
                            create_kwargs["session_metadata"] = dict(
                                wi.blade_session_metadata
                            )
                        else:
                            logger.info(
                                "wire: blade session metadata capability 未声明，"
                                "记 gap 不透传 attempt=%s", task.attempt_id,
                            )
                    if resume_session_id is not None:
                        # 恢复路径：复用崩溃前的 session（同 attempt 所有
                        # 轮次必须同一根 session），不新建、也不重传物料。
                        blade_session_id = resume_session_id
                        logger.info(
                            "blade attempt=%s 恢复已有 session %s，从 turn_index>%d 续跑",
                            task.attempt_id, resume_session_id, resume_after_turn_index,
                        )
                    else:
                        session = await client.create_session(
                            intent=(
                                _chat_intent(task.task_prompt)
                                if is_web_chat
                                else f"octagon attempt {task.attempt_id}"
                            ),
                            **create_kwargs,
                        )
                        blade_session_id = session.id
                except BladeAuthError as exc:
                    return AdapterResult(
                        attempt_id=task.attempt_id,
                        status="auth_failed",
                        error_code="rest_auth_failed",
                        error_message=str(exc),
                        # not_applicable 专指非 blade 的本地 CLI agent；blade 的
                        # 早期 REST 失败还没建立观察通道，用 unknown
                        transport_status="unknown",
                    )
                except Exception as exc:
                    return AdapterResult(
                        attempt_id=task.attempt_id,
                        status="session_create_failed",
                        error_code="session_create_error",
                        error_message=_session_create_error_detail(
                            exc,
                            locals().get("blade_entry", {
                                "primary_skill_id": env.effective_primary_skill_id,
                                "solution_id": env.solution_id,
                                "biz_role_id": env.biz_role_id,
                            }),
                        ),
                        transport_status="unknown",
                    )

                external_refs: dict[str, Any] = {
                    "blade_session_id": blade_session_id,
                    "blade_base_url": self.config.base_url,
                    "blade_entry": blade_entry,
                }
                if (
                    wi.enabled
                    and wi.blade_session_metadata
                    and not self.config.session_metadata_capability
                ):
                    external_refs["wire_capability_gaps"] = ["blade_session_metadata"]
                # 正常终态才会由 runner 把 external_refs 写回 DB；运行中进程崩溃
                # 时必须另有无敏感信息的检查点，启动恢复才能找回 BA session。
                _write_json(attempt_dir / "recovery.json", external_refs)

                # 2) 上传任务物料（视频等）+ attempt.json（薄壳 skill 回调 Octagon 用；
                #    真实自包含 skill 不读它，无副作用）。
                #    恢复路径跳过：物料在崩溃前已上传到同一 session，重传是多余
                #    的外部副作用（只上传一次）。
                try:
                    if resume_session_id is None:
                        await self._upload_task_materials(
                            client,
                            blade_session_id,
                            task,
                            data_path,
                            web_surface=is_web_chat,
                        )
                except Exception as exc:
                    cleaned = await self._cleanup(client, blade_session_id)
                    if not cleaned:
                        external_refs["cleanup_pending"] = True
                        _persist_cleanup_pending_checkpoint(
                            attempt_dir, blade_session_id, external_refs
                        )
                    return AdapterResult(
                        attempt_id=task.attempt_id,
                        status="session_create_failed",
                        external_refs=external_refs,
                        error_code="material_upload_failed",
                        error_message=str(exc),
                        transport_status="unknown",
                    )

                # 3) 流式 chat（非 headless），用 asyncio.wait_for 包裹 timeout。
                #
                # headless=False 是既定选择：headless=True 时 blade 服务端
                # 硬编码屏蔽 fork:Agent，子 agent 场景结构性不可达。副作用：
                # AskUserQuestion 也不再被屏蔽，暂停由 chat:end status="paused" +
                # pause_tool_data 表达（见 spike-blade-interaction-answer.md），
                # 由下面的交互应答分支处理。
                # conversation plan：单轮任务 → 一个 legacy task turn（行为与
                # 改造前逐字节一致）；多轮 → 按 send_message_turns 顺序发送，
                # answer_interaction turns 不主动发送，在收到匹配的暂停
                # 信号时消费。
                plan = effective_conversation(task)
                pending_interactions = list(plan.interaction_turns)
                conversation_trace = ConversationTraceWriter(
                    attempt_dir / CONVERSATION_FILENAME, attempt_id=task.attempt_id,
                )
                conversation_trace.conversation_started(
                    turn_count=len(plan.turns), is_legacy=plan.is_legacy,
                    score_turn_id=plan.score_turn.turn_id,
                )
                # 当前轮 ID/index：写进原生事件的 namespaced 扩展，
                # 让 wire 层能按轮分组而不改 producer 字段原义。
                current_turn_id: str | None = None
                current_turn_index: int | None = None
                prompt_delivery_notified = False

                # 多轮 checkpoint：进程崩溃后恢复要知道
                # "计划是不是同一个、已经完成到第几轮、当时哪一轮在飞"。
                # 只在多轮 attempt 写这些字段——单轮 recovery.json 形状不变。
                if not plan.is_legacy:
                    external_refs["conversation_plan_hash"] = plan.plan_hash
                    external_refs["conversation_turn_count"] = len(plan.turns)
                    # 恢复路径必须保留既有进度：resume_after_turn_index 证明
                    # 这些轮已完成。无条件写 None 会造成两种损失——已跑完全部
                    # 轮次的恢复会永久留下 None；初始化后、下一次 checkpoint 前
                    # 再次崩溃则丢失全部已完成进度，下次恢复会重放有外部副作用
                    # 的轮次（明确禁止）。-1 表示"一轮都没完成"，归一成 None。
                    external_refs["last_completed_turn_index"] = (
                        resume_after_turn_index
                        if resume_after_turn_index is not None
                        and resume_after_turn_index >= 0
                        else None
                    )
                    external_refs["active_turn_index"] = None
                    _write_json(attempt_dir / "recovery.json", external_refs)

                def _checkpoint_turns(
                    *, active: int | None, last_completed: int | None
                ) -> None:
                    """轮次进度落盘。turn.completed 是权威 checkpoint：
                    只有它证明该轮已发送且完成，恢复时才可以跳过。"""
                    if plan.is_legacy:
                        return
                    external_refs["active_turn_index"] = active
                    external_refs["last_completed_turn_index"] = last_completed
                    _write_json(attempt_dir / "recovery.json", external_refs)

                async def _run_chat(
                    message: Any, **chat_opts: Any
                ) -> None:
                    nonlocal events_count, last_event_at, thinking_count
                    nonlocal tool_call_count, pending_thinking_parts
                    nonlocal total_usage
                    nonlocal chat_end_status, chat_end_finish_reason
                    nonlocal chat_end_pause_tool, chat_end_pause_tool_data
                    nonlocal model_used
                    nonlocal last_activity_monotonic
                    nonlocal last_progress_write_monotonic
                    nonlocal chat_round
                    nonlocal prompt_delivery_notified
                    # 每次 chat:send 服务端新建 bridge、stream_sequence 归 1，
                    # 去重键的轮次维度必须同步递增（含交互应答续跑）。
                    chat_round += 1
                    # 显式持有 chat 流并在退出时关闭：chat:end 时要 break 提前退出，
                    # SDK 的 EventSubscription 不会因此立即从路由表摘除。正常结束
                    # 只 unsubscribe，不发送冗余 chat:stop；异常退出才发送 stop。
                    effective_chat_opts = dict(chat_opts)
                    if is_web_chat:
                        # 普通聊天首页始终把当前 mode 显式带进 chat:send。
                        effective_chat_opts.setdefault("mode", "executing")
                    if self.config.model:
                        effective_chat_opts.setdefault("model", self.config.model)
                    chat_stream = client.chat(
                        blade_session_id,
                        message,
                        headless=False,
                        **effective_chat_opts,
                    )
                    chat_end_received = False
                    try:
                     async for event in chat_stream:
                        if (
                            not prompt_delivery_notified
                            and task.prompt_delivery_handler is not None
                        ):
                            delivered = task.prompt_delivery_handler()
                            if inspect.isawaitable(delivered):
                                await delivered
                            prompt_delivery_notified = True
                        ts = _now_iso()
                        last_activity_monotonic = time.monotonic()
                        last_event_at = ts
                        progress.update({
                            "transport_status": "connected",
                            "last_transport_event_at": ts,
                            "last_agent_activity_at": ts,
                        })
                        now_monotonic = time.monotonic()
                        if (
                            now_monotonic - last_progress_write_monotonic >= 0.2
                            or event.kind
                            in {"chat:end", "turn:end", "llm:response:done"}
                        ):
                            _write_progress(progress_path, progress)
                            last_progress_write_monotonic = now_monotonic

                        projection_id = (
                            _projection_identity(event.raw)
                            if event.kind == "turn:end"
                            else None
                        )
                        is_new = (
                            projection_id is None
                            or projection_id not in seen_projection_ids
                        )
                        if is_new:
                            _append_jsonl(events_path, with_turn_ext({
                                "kind": event.kind,
                                "timestamp": ts,
                                "raw": event.raw,
                                "source": "socket",
                            }, current_turn_id, current_turn_index))
                            events_count += 1
                            if projection_id is not None:
                                seen_projection_ids.add(projection_id)

                        if event.kind == "turn:end":
                            model_used = event.raw.get("model") or model_used
                            # usage/thinking 与事件本体同一去重决策：断线重连回放的
                            # 重复 turn:end 若再次累加 usage，token/费用会翻倍
                            if is_new:
                                bucket = _bucket(_event_loop_name(event.raw))
                                blocks = event.raw.get("blocks", [])
                                for block in blocks:
                                    if block.get("type") == "thinking":
                                        content = str(block.get("content", ""))
                                        # 去重键按 turn 作用域：不同 turn 恰好产生
                                        # 相同 thinking 内容是合法的，不能全局去重
                                        think_key = f"{projection_id}:{content}"
                                        if content and think_key not in seen_thinking:
                                            seen_thinking.add(think_key)
                                            thinking_count += 1
                                            bucket["thinking_count"] += 1
                                            _append_jsonl(thinking_path, {
                                                "timestamp": ts,
                                                "sequence": thinking_count,
                                                "content": content,
                                                "projection_id": projection_id,
                                                "type": "thinking",
                                                "source": "socket",
                                            })
                                usage = event.raw.get("usage") or {}
                                in_tok = usage_input_tokens(usage)
                                out_tok = usage_output_tokens(usage)
                                total_usage = merge_usage(
                                    total_usage, usage_detail(usage)
                                )
                                bucket["input_tokens"] += in_tok
                                bucket["output_tokens"] += out_tok

                        # 新流式协议（server 发 turn:events，SDK 归一成
                        # llm:*:delta / llm:response:done）：旧的 turn:end 变成空壳
                        # （blocks/usage/model 皆空），统计改从 delta 流聚合。
                        elif event.kind == "llm:thinking:delta":
                            # 累积本回合的 thinking 内容；到 llm:response:done 收尾时
                            # 拼成完整一段落盘并计 1 段。
                            payload = event.raw.get("payload") or {}
                            part = str(payload.get("content", ""))
                            if part:
                                pending_thinking_parts.append(part)
                        elif event.kind == "llm:tool_call:created":
                            # 第二条计数路径。只靠 response:done 会漏：
                            # 该事件的 tool_calls 字段是否填充取决于上游流式实现，
                            # 实测存在「事件流里 31 次调用、上报 0」的 attempt。
                            # tool_call:created 则每次调用必发，是权威记录。
                            #
                            # 两条路径共用 seen_tool_call_ids 去重，所以同时消费
                            # 不会重复计数——同一 id 无论从哪条路径先到都只算一次。
                            # 一次调用会被拆成多条 created（首条带 function.name，
                            # 后续是 arguments_delta），id 相同，同样由去重收敛。
                            payload = event.raw.get("payload") or {}
                            tc_id = payload.get("id")
                            if tc_id and tc_id not in seen_tool_call_ids:
                                seen_tool_call_ids.add(tc_id)
                                tool_call_count += 1
                                _bucket(_event_loop_name(event.raw))[
                                    "tool_call_count"
                                ] += 1
                        elif event.kind == "llm:response:done":
                            payload = event.raw.get("payload") or {}
                            seq = event.raw.get("stream_sequence")
                            # stream_sequence 去重：断线重连回放同一 response:done 不重复累加。
                            seq_key = (chat_round, seq) if seq is not None else None
                            if seq_key is None or seq_key not in seen_response_seqs:
                                if seq_key is not None:
                                    seen_response_seqs.add(seq_key)
                                bucket = _bucket(_event_loop_name(event.raw))
                                model_used = payload.get("model") or model_used
                                usage = payload.get("usage") or {}
                                in_tok = usage_input_tokens(usage)
                                out_tok = usage_output_tokens(usage)
                                total_usage = merge_usage(
                                    total_usage, usage_detail(usage)
                                )
                                bucket["input_tokens"] += in_tok
                                bucket["output_tokens"] += out_tok
                                # tool_call：按 id 去重累加（一次调用在 delta 流里被拆成
                                # 多个 llm:tool_call:created；response:done 带该回合完整列表）。
                                for tc in payload.get("tool_calls") or []:
                                    tc_id = tc.get("id") if isinstance(tc, dict) else None
                                    if tc_id and tc_id not in seen_tool_call_ids:
                                        seen_tool_call_ids.add(tc_id)
                                        tool_call_count += 1
                                        bucket["tool_call_count"] += 1
                                # thinking：本回合若累积过 thinking delta，拼成完整一段落盘。
                                thinking_text = "".join(pending_thinking_parts)
                                if thinking_text:
                                    thinking_count += 1
                                    bucket["thinking_count"] += 1
                                    _append_jsonl(thinking_path, {
                                        "timestamp": ts,
                                        "sequence": thinking_count,
                                        "content": thinking_text,
                                        "type": "thinking",
                                        "source": "socket",
                                    })
                            pending_thinking_parts = []

                        if event.kind == "chat:end":
                            chat_end_received = True
                            # chat:end 有两种形态：typed ChatEnd（有 .status/.finish_reason）
                            # 与走 turn:events 批量通道的 RawEvent（只有 payload/raw，
                            # status 在 payload 里）。用 getattr 兜底 payload，两种都吃得下。
                            _end_payload = event.payload if isinstance(getattr(event, "payload", None), dict) else {}
                            chat_end_status = getattr(event, "status", None) or _end_payload.get("status")
                            chat_end_finish_reason = getattr(event, "finish_reason", None) or _end_payload.get("finish_reason")
                            # 非 headless 的暂停信号就在 chat:end 里（spike 结论 1）：
                            # 本轮流正常结束，是否续跑由外层交互分支决定。
                            raw_end = event.raw if isinstance(event.raw, dict) else {}
                            chat_end_pause_tool = raw_end.get("pause_tool")
                            data = raw_end.get("pause_tool_data")
                            chat_end_pause_tool_data = (
                                data if isinstance(data, dict) else None
                            )
                            break
                    finally:
                        close = getattr(chat_stream, "close", None)
                        if close is not None:
                            with contextlib.suppress(Exception):
                                closed = close(send_stop=not chat_end_received)
                                if inspect.isawaitable(closed):
                                    await closed
                        else:
                            aclose = getattr(chat_stream, "aclose", None)
                            if aclose is not None:
                                with contextlib.suppress(Exception):
                                    await aclose()

                monitor_stop = asyncio.Event()

                async def _monitor_progress() -> None:
                    nonlocal events_count, last_event_at, thinking_count
                    nonlocal last_activity_monotonic
                    while not monitor_stop.is_set():
                        try:
                            history = await client.get_history(blade_session_id)
                            node_count = len(history.nodes)
                            if node_count > int(progress.get("history_node_count") or 0):
                                ts = _now_iso()
                                last_activity_monotonic = time.monotonic()
                                progress.update({
                                    "history_node_count": node_count,
                                    "last_history_growth_at": ts,
                                    "last_agent_activity_at": ts,
                                })

                            turns = await self._get_messages_async(blade_session_id)
                            for turn in turns:
                                # 只落终态 turn：进行中的 turn 若被轮询抢先登记
                                # identity（role:turn_id 不含完成状态），socket 随后
                                # 送达的权威终态会被去重丢弃，events.jsonl 里永久
                                # 留下截断内容。status 缺失视为已完成（旧版 blade
                                # /messages 只返回完成的 turn）。
                                if not _turn_is_terminal(turn):
                                    continue
                                projection_id = _projection_identity(turn)
                                if projection_id in seen_projection_ids:
                                    continue
                                seen_projection_ids.add(projection_id)
                                ts = _now_iso()
                                last_activity_monotonic = time.monotonic()
                                _append_jsonl(events_path, with_turn_ext({
                                    "kind": "turn:end",
                                    "timestamp": ts,
                                    "raw": turn,
                                    "source": "messages_poll",
                                }, current_turn_id, current_turn_index))
                                events_count += 1
                                last_event_at = ts
                                progress["fallback_turn_count"] = int(
                                    progress.get("fallback_turn_count") or 0
                                ) + 1
                                progress["last_agent_activity_at"] = ts
                                poll_bucket = _bucket(_event_loop_name(turn))
                                for block in turn.get("blocks") or []:
                                    if not isinstance(block, dict) or block.get("type") != "thinking":
                                        continue
                                    content = str(block.get("content", ""))
                                    think_key = f"{projection_id}:{content}"
                                    if not content or think_key in seen_thinking:
                                        continue
                                    seen_thinking.add(think_key)
                                    thinking_count += 1
                                    poll_bucket["thinking_count"] += 1
                                    _append_jsonl(thinking_path, {
                                        "timestamp": ts,
                                        "sequence": thinking_count,
                                        "content": content,
                                        "projection_id": projection_id,
                                        "type": "thinking",
                                        "source": "messages_poll",
                                    })
                            progress.pop("poll_error", None)
                            _write_progress(progress_path, progress)
                        except Exception as exc:
                            progress["poll_error"] = str(exc)
                            _write_progress(progress_path, progress)

                        try:
                            await asyncio.wait_for(
                                monitor_stop.wait(),
                                timeout=max(self.config.progress_poll_interval_seconds, 0.05),
                            )
                        except asyncio.TimeoutError:
                            pass

                monitor_task = asyncio.create_task(_monitor_progress())
                try:
                    # inactivity 基线从 chat 真正开始时起算：建 session / 上传大物料
                    # （如视频）的 setup 耗时不占首轮无活动窗口，否则 setup 接近
                    # inactivity_timeout 时 prompt 刚发出就会被误判卡死。
                    last_activity_monotonic = time.monotonic()
                    # None → 不限时：total_deadline 设为 +inf，跳过总超时检查
                    # （inactivity 检查仍生效，防止真卡死时无限挂起）。
                    total_deadline = (
                        time.monotonic() + task.timeout_seconds
                        if task.timeout_seconds is not None
                        else float("inf")
                    )

                    async def _await_chat_with_watchdog(
                        message: Any, **chat_opts: Any
                    ) -> None:
                        """跑一次 chat 流并施加总/无活动超时。

                        交互应答会多次调用它；total_deadline 只创建一次，所以
                        应答续跑不会重新获得完整预算。
                        """
                        nonlocal error_message
                        chat_task = asyncio.create_task(
                            _run_chat(message, **chat_opts)
                        )
                        while not chat_task.done():
                            now = time.monotonic()
                            if now >= total_deadline:
                                error_message = (
                                    f"total timeout after {task.timeout_seconds}s"
                                )
                                chat_task.cancel()
                                break
                            inactivity = now - last_activity_monotonic
                            if inactivity >= self.config.inactivity_timeout_seconds:
                                error_message = (
                                    "inactivity timeout after "
                                    f"{self.config.inactivity_timeout_seconds:g}s"
                                )
                                chat_task.cancel()
                                break
                            await asyncio.wait(
                                {chat_task},
                                timeout=min(
                                    0.5,
                                    total_deadline - now,
                                    self.config.inactivity_timeout_seconds - inactivity,
                                ),
                            )
                        if chat_task.cancelled():
                            pass
                        elif chat_task.done():
                            try:
                                chat_task.result()
                            except BladeChatError as exc:
                                error_message = str(exc)
                        if not chat_task.done():
                            chat_task.cancel()
                        if chat_task.cancelled() or not chat_task.done():
                            with contextlib.suppress(asyncio.CancelledError):
                                await chat_task

                    async def _drain_interactions() -> None:
                        """消费本轮遗留的暂停信号。

                        chat:end status="paused" 说明 agent 在等交互应答。有场景
                        预声明的 answer_interaction turn 就应答续跑，没有就判
                        unexpected interaction 失败——不替 agent 猜答案，也不放任
                        session 挂在 WAITING_FOR_INPUT。
                        """
                        nonlocal interaction_answered_count, unexpected_interaction
                        nonlocal chat_end_pause_tool, chat_end_pause_tool_data
                        while (
                            not error_message
                            and chat_end_status == "paused"
                            and chat_end_pause_tool_data is not None
                        ):
                            matched = match_interaction_turn(
                                pending_interactions,
                                chat_end_pause_tool_data,
                                chat_end_pause_tool,
                            )
                            if matched is None:
                                unexpected_interaction = {
                                    "pause_tool": chat_end_pause_tool,
                                    "tool_call_id": chat_end_pause_tool_data.get(
                                        "tool_call_id"
                                    ),
                                    "questions": _interaction_question_summary(
                                        chat_end_pause_tool_data
                                    ),
                                }
                                logger.warning(
                                    "blade attempt=%s 收到未声明的交互请求 %s，判失败",
                                    task.attempt_id, chat_end_pause_tool,
                                )
                                return
                            pending_interactions.remove(matched)
                            try:
                                answer_payload, answer_text = build_askuser_answer(
                                    matched, chat_end_pause_tool_data,
                                )
                            except ValueError as exc:
                                unexpected_interaction = {
                                    "pause_tool": chat_end_pause_tool,
                                    "tool_call_id": chat_end_pause_tool_data.get(
                                        "tool_call_id"
                                    ),
                                    "answer_build_error": str(exc),
                                }
                                return
                            interaction_answered_count += 1
                            _append_jsonl(events_path, with_turn_ext({
                                "kind": "octagon:interaction_answered",
                                "timestamp": _now_iso(),
                                "raw": {
                                    "turn_id": matched.turn_id,
                                    "tool_call_id": answer_payload.get("tool_call_id"),
                                    "pause_tool": chat_end_pause_tool,
                                },
                                "source": "octagon",
                            }, current_turn_id, current_turn_index))
                            conversation_trace.interaction_answered(
                                matched,
                                producer_session_id=blade_session_id,
                                tool_name=str(chat_end_pause_tool or ""),
                            )
                            # 清空暂停状态：新一轮 chat:end 会重新填（可能再次暂停）
                            chat_end_pause_tool = None
                            chat_end_pause_tool_data = None
                            await _await_chat_with_watchdog(
                                answer_text, askuser_answer=answer_payload,
                            )

                    # 按 plan 顺序发送 send_message turns：
                    # 同一个 session、物料只上传一次，每轮一次 client.chat。
                    # 单轮任务恰好是一个 legacy turn，行为与改造前一致。
                    # 初值与 checkpoint 一致：恢复时已完成的轮不能因为循环还没
                    # 走到跳过分支就被写回 None。
                    last_completed_index: int | None = (
                        resume_after_turn_index
                        if resume_after_turn_index is not None
                        and resume_after_turn_index >= 0
                        else None
                    )
                    send_message_turns = plan.send_message_turns
                    for turn_position, turn in enumerate(send_message_turns):
                        # 恢复路径已完成的轮次不重发：有外部副作用的轮
                        # 重放会造成重复执行。resume_after_turn_index 由
                        # recover_existing 注入，正常首跑为 None。
                        if (
                            resume_after_turn_index is not None
                            and turn.turn_index <= resume_after_turn_index
                        ):
                            logger.info(
                                "blade attempt=%s 恢复：跳过已完成轮 %s(index=%d)",
                                task.attempt_id, turn.turn_id, turn.turn_index,
                            )
                            last_completed_index = turn.turn_index
                            continue
                        current_turn_id = None if plan.is_legacy else turn.turn_id
                        current_turn_index = None if plan.is_legacy else turn.turn_index
                        _checkpoint_turns(
                            active=turn.turn_index, last_completed=last_completed_index,
                        )
                        # 轮级状态重置：上一轮的 chat 终态/未收尾的 thinking 分片
                        # 不得泄漏进本轮判定（usage/去重集合是 attempt 级，不重置）。
                        chat_end_status = None
                        chat_end_finish_reason = None
                        chat_end_pause_tool = None
                        chat_end_pause_tool_data = None
                        pending_thinking_parts = []

                        conversation_trace.turn_started(
                            turn, producer_session_id=blade_session_id,
                        )
                        await _await_chat_with_watchdog(
                            _render_turn_prompt(task, turn, env, data_path)
                        )
                        await _drain_interactions()

                        turn_failed = bool(error_message) or (
                            unexpected_interaction is not None
                        )
                        if turn_failed:
                            conversation_trace.turn_failed(
                                turn,
                                producer_session_id=blade_session_id,
                                error_code=(
                                    "unexpected_interaction"
                                    if unexpected_interaction is not None
                                    else None
                                ),
                                error_summary=error_message,
                            )
                            # setup/pressure 失败即终止 conversation：
                            # 不能让 probe 在不完整上下文上继续并产出正常得分。
                            conversation_trace.conversation_failed(
                                error_code=(
                                    "unexpected_interaction"
                                    if unexpected_interaction is not None
                                    else None
                                ),
                                error_summary=error_message,
                            )
                            break
                        conversation_trace.turn_completed(
                            turn, producer_session_id=blade_session_id,
                        )
                        last_completed_index = turn.turn_index
                        # turn.completed 之后才落 checkpoint：active 清空表示
                        # "没有在飞的轮"，恢复时可直接从下一轮继续。
                        _checkpoint_turns(
                            active=None, last_completed=last_completed_index,
                        )
                        # blade server 会先广播 chat:end，再在后台任务 finally 中
                        # 清理 active-chat 状态。立即发送下一轮可能撞上这个极短
                        # 竞态窗口，因此仅在确实还有下一轮消息时等待 100ms。
                        if turn_position + 1 < len(send_message_turns):
                            await _wait_for_chat_end_settle()
                    else:
                        conversation_trace.conversation_completed()
                    conversation_trace.close()

                    if error_message:
                        # 看门狗只 cancel 了本地协程；blade session 在服务端仍在
                        # 继续执行（keep_blade_session=true 时 _cleanup 也不会删），
                        # 必须显式 stop，否则远端持续烧模型直到自然结束。
                        try:
                            await client.stop(blade_session_id)
                        except Exception as exc:
                            logger.warning(
                                "stop blade session %s after timeout failed: %s",
                                blade_session_id, exc,
                            )
                finally:
                    monitor_stop.set()
                    await monitor_task

                # 4) trace：blade 端权威记录（get_history nodes）→ 归一写 trace.jsonl
                try:
                    history = await client.get_history(blade_session_id)
                    (attempt_dir / "blade_history.json").write_text(
                        json.dumps(history.raw, ensure_ascii=False), encoding="utf-8"
                    )
                    trace_rows = _extract_trace_from_history(
                        history.nodes,
                        attempt_id=task.attempt_id,
                        env_session_id=blade_session_id,
                    )
                    trace_path = attempt_dir / "trace.jsonl"
                    added_rows = _append_unique_trace_rows(trace_path, trace_rows)
                    external_refs["blade_trace_rows_added"] = added_rows

                    # token/model 兜底：部分 blade 版本（如 v1.0.16 容器化）headless
                    # 的 turn:end 只有 blocks/role，无 usage/model 字段，实时累计恒为
                    # 0。get_history 的 nodes 带每轮 usage（OpenAI 命名），顶层的
                    # tokenizer_model 可代模型名。仅在实时流没采到时兜底，避免重复计。
                    if not (
                        total_usage.get("input_tokens")
                        or total_usage.get("output_tokens")
                    ):
                        hist_in = hist_out = 0
                        hist_usage: dict[str, int | None] = empty_usage()
                        # 实时流一个 token 都没采到 → 分桶同样为空，history 是唯一
                        # 权威来源；清空避免与 history 口径叠加。
                        for stats in agent_stats.values():
                            stats["input_tokens"] = 0
                            stats["output_tokens"] = 0
                        for node in history.nodes:
                            if not isinstance(node, dict):
                                continue
                            usage = node.get("usage") or {}
                            in_tok = usage_input_tokens(usage)
                            out_tok = usage_output_tokens(usage)
                            hist_in += in_tok
                            hist_out += out_tok
                            hist_usage = merge_usage(hist_usage, usage_detail(usage))
                            node_bucket = _bucket(_event_loop_name(node))
                            node_bucket["input_tokens"] += in_tok
                            node_bucket["output_tokens"] += out_tok
                        if hist_in or hist_out:
                            total_usage = hist_usage
                            external_refs["blade_token_fallback"] = "history_nodes"
                    # model_used 兜底：**不能用 tokenizer_model**——它是 blade 用来
                    # 估 token 的分词器（如 qwen3.5），不是实际推理模型。用它会误报
                    # （例如请求 sonnet 却显示 qwen3.5）。实时流没采到 model 时，回落到
                    # 本次**请求的模型** self.config.model（即 blade_model），最贴近真相。
                    # 最终 external_refs["model_used"] = model_used or self.config.model
                    # 已有此回落，这里不再用 tokenizer_model 覆盖。
                except Exception as exc:
                    logger.warning("extract trace from history failed: %s", exc)
                    external_refs["blade_trace_error"] = str(exc)

                # 4b) thinking / 对话流兜底：blade headless 事件流在部分版本（如
                #     v1.0.16 容器化）只推 chat:end，不推 turn:patch/turn:end，导致
                #     实时采集的 thinking 为空。get_history 的 nodes 也不含 thinking，
                #     但 GET /messages 的 assistant blocks 里有（type=thinking，
                #     结构同实时流）。仅当实时流没采到 thinking 时兜底，避免重复。
                if thinking_count == 0:
                    try:
                        added = await asyncio.to_thread(
                            self._fallback_thinking_from_messages,
                            blade_session_id,
                            thinking_path,
                        )
                        if added:
                            thinking_count += added
                            external_refs["blade_thinking_fallback"] = added
                    except Exception as exc:
                        logger.warning("thinking fallback from messages failed: %s", exc)
                        external_refs["blade_thinking_fallback_error"] = str(exc)

                # 5) 产物回收：递归拉取 blade workspace（scorer 只看本地 skill_workspace）
                try:
                    external_refs["artifact_sync"] = await self._recover_workspace_artifacts(
                        client,
                        blade_session_id,
                        attempt_dir,
                        task,
                        overwrite_existing_files=resume_session_id is not None,
                    )
                except Exception as exc:
                    logger.warning("recover blade artifacts failed: %s", exc)
                    external_refs["artifact_sync"] = {"error": str(exc)}

                # 6) 清理
                if not preserve_session:
                    cleaned = await self._cleanup(client, blade_session_id)
                    if not cleaned:
                        external_refs["cleanup_pending"] = True
                        _persist_cleanup_pending_checkpoint(
                            attempt_dir, blade_session_id, external_refs
                        )

        except Exception as exc:
            return AdapterResult(
                attempt_id=task.attempt_id,
                status="blade_service_unavailable",
                external_refs={"blade_session_id": blade_session_id} if blade_session_id else {},
                error_code="client_connect_failed",
                error_message=str(exc),
                transport_status="disconnected",
            )

        duration_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)

        external_refs["model_used"] = model_used or self.config.model
        external_refs["blade_enable_thinking"] = self.config.enable_thinking
        # per-agent 明细：非 headless 起可能含 root 之外的 fork 桶。
        # 只是 adapter 侧诊断/对账摘要，normalizer/评测仍只消费 canonical wire。
        external_refs["agent_stats"] = agent_stats
        if interaction_answered_count:
            external_refs["interaction_answered_count"] = interaction_answered_count
        if unexpected_interaction is not None:
            external_refs["unexpected_interaction"] = unexpected_interaction
            # 未声明的交互请求：agent 停在等待应答，任务不可能正常完成。
            # 覆盖 chat:end 的 paused 语义，给出明确失败原因而不是笼统 timeout。
            error_message = error_message or (
                "agent 请求了场景未声明的交互（"
                f"{unexpected_interaction.get('pause_tool')}）："
                f"{unexpected_interaction.get('questions')}"
            )
        status = _classify_outcome(
            chat_end_status,
            error_message,
            chat_end_finish_reason,
            unexpected_interaction=unexpected_interaction is not None,
        )
        transport_status = (
            "disconnected"
            if error_message and "socket disconnected" in error_message.lower()
            else "connected"
        )
        progress["transport_status"] = transport_status
        progress["updated_at"] = _now_iso()
        _write_json(progress_path, progress)
        return AdapterResult(
            attempt_id=task.attempt_id,
            status=status,
            external_refs=external_refs,
            error_code=_outcome_error_code(
                status, error_message, chat_end_finish_reason,
                unexpected_interaction=unexpected_interaction is not None,
            ),
            error_message=error_message,
            transport_status=transport_status,
            events_count=events_count,
            last_event_at=last_event_at,
            thinking_count=thinking_count,
            tool_call_count=tool_call_count,
            token_usage=compact_usage(total_usage),
            duration_ms=duration_ms,
            # 多轮摘要：单轮 attempt 也会有 conversation.jsonl
            # （legacy 一轮），summary 的 is_legacy 标明来源。
            conversation_summary=summarize_conversation(attempt_dir),
            # blade skill 工具在 docker 沙盒内执行。sandbox_image 是 blade server 端
            # 启动 env（SANDBOX_IMAGE），adapter 不可知 → 留空。workspace_root 由 blade
            # 在沙盒内按 session 选定（/root/智能助手工作空间/<session>），adapter 事前
            # 不知具体子目录 → 留空，安全扫描时从 trace 命令原文反查前缀。
            security_meta=build_security_meta(
                execution_locus=self.capabilities.execution_locus,
                permission_mode="sandbox",
                workspace_root=None,
                sandbox_id=blade_session_id,
                # blade 沙盒由 blade server 管理：每个 blade 用户一个长驻容器，
                # session 之间共享 /root，公网开放。如实记录，不伪装成本方案的
                # per-attempt 沙盒（spec 260909-agent-sandbox 需求 7）。
                extra=BLADE_SANDBOX_SECURITY_EXTRA,
            ),
        )

    async def _run_software_factory(
        self,
        task: AdapterRunInput,
        env: AdapterEnv,
        data_path: Path,
    ) -> AdapterResult:
        """复刻软件工厂新建项目入口，并附着其自动启动的首轮会话。

        ``POST /api/agent-board/projects`` 不只是 create_session 的另一种写法：
        它同时创建真实项目工作区、绑定 app-dev/default、生成内部 bootstrap
        消息，并在响应返回前调度非 headless 首轮。实验侧不能再额外
        ``chat:send``，否则同一需求会执行两遍。
        """
        started = time.monotonic()
        attempt_dir = data_path / "attempts" / task.attempt_id
        attempt_dir.mkdir(parents=True, exist_ok=True)

        if not self.config.api_key:
            return AdapterResult(
                attempt_id=task.attempt_id,
                status="auth_failed",
                error_code="api_key_missing",
                error_message="blade.api_key not configured",
            )

        try:
            project, factory_contract = await self._create_software_factory_project(
                task, data_path
            )
            project_id = project.get("id")
            session_id = str(project.get("initialization_session_id") or "")
            if not session_id:
                raise RuntimeError(
                    "software factory response missing initialization_session_id"
                )
        except Exception as exc:
            return AdapterResult(
                attempt_id=task.attempt_id,
                status="session_create_failed",
                error_code="software_factory_project_create_error",
                error_message=_session_create_error_detail(
                    exc,
                    {
                        "surface": "software_factory",
                        "solution_id": env.solution_id,
                        "biz_role_id": env.biz_role_id,
                    },
                ),
                transport_status="unknown",
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        base = self.config.base_url.rstrip("/")
        external_refs: dict[str, Any] = {
            "blade_session_id": session_id,
            "blade_base_url": self.config.base_url,
            "blade_entry": {
                "surface": "software_factory",
                "solution_id": env.solution_id,
                "biz_role_id": env.biz_role_id,
            },
            "blade_project_id": project_id,
            "blade_project_name": project.get("name"),
            "blade_project_workspace": project.get("workspace_path"),
            "blade_software_factory_contract": factory_contract,
            "blade_project_url": (
                f"{base}/p/{project_id}/chat/{session_id}"
                if project_id is not None
                else None
            ),
        }
        # 项目 API 已经启动远端会话；先落恢复锚点，服务重启时才能重新附着，
        # 且绝不重复 POST project / 重发 bootstrap。
        _write_json(attempt_dir / "recovery.json", external_refs)

        # create_task 后 HTTP 响应可能比 background chat 先到达客户端。等 session
        # 离开 created/initializing，避免恢复器把尚未起跑误判为 interrupted。
        try:
            async with self._new_client() as client:
                for _ in range(40):
                    session = await client.get_session(session_id)
                    status = str(session.raw.get("status") or "")
                    if status not in {"", "created", "initializing", "pending"}:
                        break
                    await asyncio.sleep(0.05)
        except Exception as exc:
            logger.warning(
                "software factory session start probe failed for %s: %s",
                session_id,
                exc,
            )

        result = await self.recover_existing(
            task=task,
            session_id=session_id,
            data_path=data_path,
            preserve_session=True,
        )
        result.external_refs.update(external_refs)
        result.external_refs["recovered_after_restart"] = False
        result.external_refs["blade_enable_thinking"] = self.config.enable_thinking
        result.external_refs["model_used"] = self.config.model
        result.duration_ms = int((time.monotonic() - started) * 1000)

        result = await self._drive_iterative_review(
            task=task,
            env=env,
            data_path=data_path,
            session_id=session_id,
            result=result,
            resume=False,
        )

        trace_path = attempt_dir / "trace.jsonl"
        if trace_path.is_file():
            result.tool_call_count = sum(
                1 for line in trace_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
            result.external_refs["blade_trace_rows"] = result.tool_call_count
        return result

    async def _drive_iterative_review(
        self,
        *,
        task: AdapterRunInput,
        env: AdapterEnv,
        data_path: Path,
        session_id: str,
        result: AdapterResult,
        resume: bool,
    ) -> AdapterResult:
        handler = task.iteration_turn_handler
        if handler is None or result.status != "completed":
            return result
        try:
            if _fail_incomplete_iterative_artifact_sync(result):
                return result
            if resume:
                decision = await handler.resume_after_restart(
                    producer_session_id=session_id
                )
            else:
                decision = await handler.on_turn_completed(
                    producer_session_id=session_id
                )
            finalized_from_last_successful = False
            while decision.next_prompt is not None:
                turn_index = int(decision.round_index) + 1
                handler.mark_feedback_sending(decision)
                # 上一轮 self.run() 结束会广播 chat:end，blade server 在后台
                # 任务 finally 中才清理 active-chat 状态。迭代续跑是新的递归
                # self.run()，直接对同一 session 发下一个 client.chat 会撞上
                # 这个 active-chat 竞态（实测错误在轮结束后 1.4s+ 仍出现，单纯
                # settle 不够）。先 settle 一次，再对 "Session already has an
                # active chat" 做有界指数退避重试。
                await _wait_for_chat_end_settle()
                continuation = dataclasses.replace(
                    task,
                    conversation_turns=(
                        ConversationTurn(
                            turn_id=f"iteration-{turn_index}",
                            turn_index=0,
                            purpose="task",
                            prompt=decision.next_prompt,
                        ),
                    ),
                    iteration_turn_handler=None,
                    prompt_delivery_handler=lambda decision=decision: (
                        handler.mark_feedback_delivered(decision)
                    ),
                )
                # active-chat 是瞬态：服务端对同一条 chat 短暂保持 active，续跑
                # 撞上即报错，重发很快成功。有界指数退避 200/400/800ms，最多
                # 在首次请求后重试三次；耗尽或遇到其它错误则按原逻辑处理。
                next_result = None
                for _attempt in range(len(_ACTIVE_CHAT_RETRY_DELAYS) + 1):
                    next_result = await self.run(
                        continuation,
                        env,
                        data_path,
                        resume_session_id=session_id,
                        resume_after_turn_index=-1,
                        preserve_session=True,
                    )
                    if next_result.status == "completed" or not _is_active_chat_error(
                        next_result
                    ):
                        break
                    if _attempt >= len(_ACTIVE_CHAT_RETRY_DELAYS):
                        break
                    delay = _ACTIVE_CHAT_RETRY_DELAYS[_attempt]
                    logger.warning(
                        "blade attempt=%s 迭代续跑撞 active-chat（重试 %d/%d），"
                        "%dms 后重试",
                        task.attempt_id,
                        _attempt + 1,
                        len(_ACTIVE_CHAT_RETRY_DELAYS),
                        int(delay * 1000),
                    )
                    await asyncio.sleep(delay)
                result.events_count += next_result.events_count
                result.thinking_count += next_result.thinking_count
                result.tool_call_count += next_result.tool_call_count
                for key, value in next_result.token_usage.items():
                    result.token_usage[key] = int(result.token_usage.get(key, 0)) + int(
                        value or 0
                    )
                result.last_event_at = next_result.last_event_at or result.last_event_at
                result.external_refs.update(next_result.external_refs)
                _fail_incomplete_iterative_artifact_sync(next_result)
                if next_result.status != "completed":
                    finalized_from_last_successful = (
                        handler.finalize_last_successful_submission()
                    )
                    result.status = next_result.status
                    result.error_code = next_result.error_code
                    result.error_message = next_result.error_message
                    break
                decision = await handler.on_turn_completed(
                    producer_session_id=session_id
                )
            result.external_refs["iteration_completed"] = bool(
                getattr(decision, "completed", False)
                or finalized_from_last_successful
            )
            if finalized_from_last_successful:
                result.external_refs["iteration_finalized_from_last_successful"] = True
            result.external_refs["iteration_final_round"] = int(
                getattr(decision, "round_index", 0)
            )
        except Exception as exc:
            logger.exception("iterative turn controller failed")
            result.status = str(getattr(exc, "status", "chat_failed"))
            result.error_code = str(
                getattr(exc, "error_code", "iteration_controller_failed")
            )
            result.error_message = str(exc)
        finally:
            await self._finalize_iterative_session_cleanup(
                task=task,
                data_path=data_path,
                session_id=session_id,
                result=result,
            )
        return result

    async def _finalize_iterative_session_cleanup(
        self,
        *,
        task: AdapterRunInput,
        data_path: Path,
        session_id: str,
        result: AdapterResult,
    ) -> AdapterResult:
        try:
            async with self._new_client() as client:
                cleaned = await self._cleanup(client, session_id)
            if not cleaned:
                result.external_refs["cleanup_pending"] = True
                _persist_cleanup_pending_checkpoint(
                    Path(data_path) / "attempts" / task.attempt_id,
                    session_id,
                    result.external_refs,
                )
        except Exception as exc:
            logger.warning("cleanup iterative blade session failed: %s", exc)
            result.external_refs["cleanup_pending"] = True
            with contextlib.suppress(Exception):
                _persist_cleanup_pending_checkpoint(
                    Path(data_path) / "attempts" / task.attempt_id,
                    session_id,
                    result.external_refs,
                )
        return result

    async def recover_iterative_existing(
        self,
        *,
        task: AdapterRunInput,
        env: AdapterEnv,
        session_id: str,
        data_path: Path,
    ) -> AdapterResult:
        result = await self.recover_existing(
            task=task,
            session_id=session_id,
            data_path=data_path,
            preserve_session=True,
        )
        if result.status != "completed":
            if result.external_refs.get("session_requires_followup"):
                return result
            return await self._finalize_iterative_session_cleanup(
                task=task,
                data_path=data_path,
                session_id=session_id,
                result=result,
            )
        return await self._drive_iterative_review(
            task=task,
            env=env,
            data_path=data_path,
            session_id=session_id,
            result=result,
            resume=True,
        )

    async def _create_software_factory_project(
        self,
        task: AdapterRunInput,
        data_path: Path,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """发送与软件工厂新建项目表单相同的 multipart 请求。"""
        contract = await self._software_factory_contract()
        # Adapter 只转换传输方式，不改写任务语义：软件工厂的 description
        # 始终使用与其他 agent 相同的冻结 task prompt，输入物料则作为附件上传。
        description = task.task_prompt.strip()
        title = _project_prompt_title(description)
        workspace = (
            data_path.resolve()
            / "attempts"
            / task.attempt_id
            / "skill_workspace"
        )

        uploaded = task.task_context.get("uploaded_files")
        material_names: list[str] = []
        for item in uploaded or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            if name:
                material_names.append(name)
        for raw in task.task_context.get("_agent_material_files") or []:
            name = str(raw)
            if name:
                material_names.append(name)

        async def _post(project_name: str, *, auto_name: bool) -> httpx.Response:
            multipart: list[tuple[str, tuple[Any, ...]]] = [
                ("name", (None, project_name)),
                ("description", (None, description)),
                ("create_mode", (None, "blank")),
                ("auto_name", (None, str(auto_name).lower())),
            ]
            if self.config.model:
                multipart.append(("model", (None, self.config.model)))
            if self.config.enable_thinking is not None:
                multipart.append(
                    (
                        "enable_thinking",
                        (None, str(self.config.enable_thinking).lower()),
                    )
                )

            with contextlib.ExitStack() as stack:
                for rel_raw in sorted(set(material_names)):
                    rel = Path(rel_raw)
                    if rel.is_absolute() or ".." in rel.parts:
                        raise FileNotFoundError(
                            f"非法软件工厂项目物料路径: {rel_raw}"
                        )
                    src = workspace / rel
                    if not src.is_file():
                        raise FileNotFoundError(
                            f"软件工厂项目物料不存在: {rel_raw} ({src})"
                        )
                    multipart.append(
                        (
                            "files",
                            (
                                rel.as_posix(),
                                stack.enter_context(src.open("rb")),
                                "application/octet-stream",
                            ),
                        )
                    )
                async with httpx.AsyncClient(
                    timeout=max(self.config.request_timeout_seconds, 30.0)
                ) as http:
                    return await http.post(
                        f"{self.config.base_url.rstrip('/')}"
                        "/api/agent-board/projects",
                        headers={
                            "Authorization": f"Bearer {self.config.api_key}"
                        },
                        files=multipart,
                    )

        response = await _post(title, auto_name=True)
        if response.status_code == 409:
            # 软件工厂用项目名生成目录；重复 benchmark 可能与历史项目同名。
            # 等价于用户在冲突提示后给项目名补一个唯一后缀再提交。
            suffix = task.attempt_id[-8:]
            response = await _post(
                f"{title} · {suffix}",
                auto_name=False,
            )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise TypeError(
                "unexpected software factory response: "
                f"{type(payload).__name__}"
            )
        return payload, contract

    async def _software_factory_contract(self) -> dict[str, Any]:
        """校验当前 BA 服务仍声明软件工厂产品入口，并返回审计摘要。

        这里故意 fail closed：开发场景绝不在契约缺失时回退到普通
        ``/api/sessions``，否则一次 BA 升级就可能把实验悄悄变回近似调用。
        """
        url = f"{self.config.base_url.rstrip('/')}/openapi.json"
        async with httpx.AsyncClient(
            timeout=max(self.config.request_timeout_seconds, 30.0)
        ) as http:
            response = await http.get(
                url,
                headers={"Authorization": f"Bearer {self.config.api_key}"},
            )
        response.raise_for_status()
        document = response.json()
        if not isinstance(document, dict):
            raise RuntimeError("BA OpenAPI response is not an object")
        info = document.get("info")
        paths = document.get("paths")
        operation = (
            paths.get("/api/agent-board/projects", {}).get("post")
            if isinstance(paths, dict)
            else None
        )
        if not isinstance(operation, dict):
            raise RuntimeError(
                "current BA does not expose POST /api/agent-board/projects"
            )
        tags = operation.get("tags")
        if not isinstance(tags, list) or "agent-board" not in tags:
            raise RuntimeError(
                "POST /api/agent-board/projects is not the agent-board "
                "software factory operation"
            )

        identity = {
            "openapi": document.get("openapi"),
            "info": info if isinstance(info, dict) else {},
            "path": "/api/agent-board/projects",
            "method": "POST",
            "operation_id": operation.get("operationId"),
            "tags": tags,
        }
        canonical = json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        identity["contract_hash"] = (
            "sha256:" + hashlib.sha256(canonical).hexdigest()
        )
        return identity

    async def recover_existing(
        self,
        *,
        task: AdapterRunInput,
        session_id: str,
        data_path: Path,
        preserve_session: bool | None = None,
    ) -> AdapterResult:
        """重新附着既有 Blade session，不重复发送 prompt。

        ``preserve_session`` 只用于恢复流程需要把 session 交给后续续跑的
        中间阶段；终态收尾仍尊重用户的 ``keep_blade_session`` 偏好。
        """
        attempt_dir = Path(data_path) / "attempts" / task.attempt_id
        attempt_dir.mkdir(parents=True, exist_ok=True)
        events_path = attempt_dir / "events.jsonl"
        thinking_path = attempt_dir / "thinking.jsonl"
        existing_events: list[dict[str, Any]] = []
        if events_path.is_file():
            for line in events_path.read_text(encoding="utf-8").splitlines():
                with contextlib.suppress(json.JSONDecodeError):
                    row = json.loads(line)
                    if isinstance(row, dict):
                        existing_events.append(row)
        seen_projection_ids = {
            identity
            for row in existing_events
            if isinstance(row.get("raw"), dict)
            if (identity := _projection_identity(row["raw"]))
        }
        seen_thinking: set[str] = set()
        legacy_thinking: set[str] = set()
        if thinking_path.is_file():
            for line in thinking_path.read_text(encoding="utf-8").splitlines():
                with contextlib.suppress(json.JSONDecodeError):
                    row = json.loads(line)
                    if isinstance(row, dict) and row.get("content"):
                        content = str(row["content"])
                        projection_id = row.get("projection_id")
                        if projection_id:
                            seen_thinking.add(f"{projection_id}:{content}")
                        else:
                            legacy_thinking.add(content)

        events_count = len(existing_events)
        thinking_count = len(seen_thinking) + len(legacy_thinking)
        last_event_at: str | None = None
        error_message: str | None = None
        transport_disconnected = False
        cleanup_pending = False
        session_status = ""
        started = time.monotonic()
        # None → 不限时（同 run() 主路径）：+inf 跳过总超时，inactivity 仍生效。
        total_deadline = (
            started + task.timeout_seconds
            if task.timeout_seconds is not None
            else float("inf")
        )
        history_nodes: list[dict[str, Any]] = []

        def append_projection(payload: dict[str, Any], source: str) -> bool:
            nonlocal events_count, thinking_count, last_event_at
            identity = _projection_identity(payload)
            if identity and identity in seen_projection_ids:
                return False
            if identity:
                seen_projection_ids.add(identity)
            ts = _now_iso()
            _append_jsonl(events_path, {
                "kind": "turn:end",
                "timestamp": ts,
                "raw": payload,
                "source": source,
            })
            events_count += 1
            last_event_at = ts
            for block in payload.get("blocks") or []:
                if not isinstance(block, dict) or block.get("type") != "thinking":
                    continue
                content = str(block.get("content") or "")
                thinking_key = f"{identity}:{content}" if identity else content
                if (
                    not content
                    or thinking_key in seen_thinking
                    or content in legacy_thinking
                ):
                    continue
                seen_thinking.add(thinking_key)
                thinking_count += 1
                _append_jsonl(thinking_path, {
                    "timestamp": ts,
                    "sequence": thinking_count,
                    "content": content,
                    "projection_id": identity,
                    "type": "thinking",
                    "source": source,
                })
            return True

        def persist_cleanup_state(pending: bool) -> None:
            checkpoint_path = attempt_dir / "recovery.json"
            checkpoint: dict[str, Any] = {}
            if checkpoint_path.is_file():
                loaded = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    checkpoint = loaded
            checkpoint["cleanup_pending"] = pending
            _write_json(checkpoint_path, checkpoint)

        if not self.config.api_key:
            return AdapterResult(
                attempt_id=task.attempt_id,
                status="interrupted",
                error_code="recovery_api_key_missing",
                error_message="cannot recover Blade session without blade.api_key",
            )

        try:
            async with self._new_client() as client:
                session = await self._retry_recovery_read(
                    session_id,
                    "get_session",
                    lambda: client.get_session(session_id),
                    remaining_seconds=max(total_deadline - time.monotonic(), 0.0),
                )
                session_status = str(session.raw.get("status") or "")
                last_activity = time.monotonic()
                last_history_count = 0

                async def _refresh_recovery_snapshot() -> None:
                    nonlocal history_nodes, last_history_count
                    nonlocal last_activity, session_status
                    history = await self._retry_recovery_read(
                        session_id,
                        "get_history",
                        lambda: client.get_history(session_id),
                        remaining_seconds=max(
                            total_deadline - time.monotonic(), 0.0
                        ),
                    )
                    history_nodes = history.nodes
                    if len(history_nodes) > last_history_count:
                        last_history_count = len(history_nodes)
                        last_activity = time.monotonic()
                    for turn in await self._get_messages_async(
                        session_id,
                        remaining_seconds=max(
                            total_deadline - time.monotonic(), 0.0
                        ),
                    ):
                        if append_projection(turn, "messages_recovery"):
                            last_activity = time.monotonic()
                    session = await self._retry_recovery_read(
                        session_id,
                        "get_session",
                        lambda: client.get_session(session_id),
                        remaining_seconds=max(
                            total_deadline - time.monotonic(), 0.0
                        ),
                    )
                    session_status = str(
                        session.raw.get("status") or session_status
                    )

                # 断链只代表观察通道失效，不代表远端任务结束。只要远端仍处于
                # 可运行状态，就重新订阅并用 REST snapshot 兜底，直到得到明确终态。
                while session_status in _RUNNING_SESSION_STATUSES and not error_message:
                    next_event: asyncio.Task[Any] | None = None
                    try:
                        subscription = client.subscribe(session_id)
                        async with subscription:
                            transport_disconnected = False
                            next_event = asyncio.create_task(
                                subscription.__anext__()
                            )
                            while (
                                session_status in _RUNNING_SESSION_STATUSES
                                and not error_message
                            ):
                                done, _ = await asyncio.wait(
                                    {next_event},
                                    timeout=max(
                                        self.config.progress_poll_interval_seconds,
                                        0.05,
                                    ),
                                )
                                if next_event in done:
                                    try:
                                        event = next_event.result()
                                    except StopAsyncIteration:
                                        transport_disconnected = True
                                        break
                                    except BladeChatError as exc:
                                        transport_disconnected = True
                                        logger.warning(
                                            "recovery subscription broke for %s: %s",
                                            session_id, exc,
                                        )
                                        break
                                    ts = _now_iso()
                                    last_activity = time.monotonic()
                                    last_event_at = ts
                                    if event.kind == "turn:end":
                                        append_projection(event.raw, "socket_recovery")
                                    else:
                                        _append_jsonl(events_path, {
                                            "kind": event.kind,
                                            "timestamp": ts,
                                            "raw": event.raw,
                                            "source": "socket_recovery",
                                        })
                                        events_count += 1
                                    if event.kind == "chat:end":
                                        # 同上：RawEvent 形态的 chat:end 无 .status，
                                        # 从 payload 兜底。
                                        _end_payload = event.payload if isinstance(getattr(event, "payload", None), dict) else {}
                                        session_status = (
                                            getattr(event, "status", None)
                                            or _end_payload.get("status")
                                            or "completed"
                                        )
                                        break
                                    next_event = asyncio.create_task(
                                        subscription.__anext__()
                                    )

                                await _refresh_recovery_snapshot()
                                if time.monotonic() >= total_deadline:
                                    error_message = (
                                        "total timeout during restart recovery after "
                                        f"{task.timeout_seconds:g}s"
                                    )
                                    break
                                if (
                                    time.monotonic() - last_activity
                                    >= self.config.inactivity_timeout_seconds
                                ):
                                    error_message = (
                                        "inactivity timeout during restart recovery after "
                                        f"{self.config.inactivity_timeout_seconds:g}s"
                                    )
                                    break
                    except BladeChatError as exc:
                        transport_disconnected = True
                        logger.warning(
                            "recovery subscription unavailable for %s: %s; "
                            "falling back to REST polling",
                            session_id,
                            exc,
                        )
                    except Exception as exc:
                        transport_disconnected = True
                        logger.warning(
                            "recovery subscription failed for %s; "
                            "falling back to REST polling: %s",
                            session_id,
                            exc,
                        )
                    finally:
                        if next_event is not None:
                            next_event.cancel()
                            with contextlib.suppress(
                                asyncio.CancelledError,
                                StopAsyncIteration,
                                BladeChatError,
                            ):
                                await next_event

                    if error_message:
                        # 与 run() 的看门狗对齐：恢复期超时也要显式 stop 远端
                        # session，否则它在服务端继续烧模型。
                        try:
                            await client.stop(session_id)
                        except Exception as exc:
                            logger.warning(
                                "stop blade session %s after recovery timeout failed: %s",
                                session_id,
                                exc,
                            )
                        if not self.config.keep_blade_session and preserve_session is not True:
                            cleanup_pending = True
                        break
                    if session_status not in _RUNNING_SESSION_STATUSES:
                        break
                    await _refresh_recovery_snapshot()
                    if time.monotonic() >= total_deadline:
                        error_message = (
                            "total timeout during restart recovery after "
                            f"{task.timeout_seconds:g}s"
                        )
                        break
                    if (
                        time.monotonic() - last_activity
                        >= self.config.inactivity_timeout_seconds
                    ):
                        error_message = (
                            "inactivity timeout during restart recovery after "
                            f"{self.config.inactivity_timeout_seconds:g}s"
                        )
                        break
                    await asyncio.sleep(
                        max(self.config.progress_poll_interval_seconds, 0.05)
                    )

                await _refresh_recovery_snapshot()
                # 终态或恢复超时后再做一次权威快照，确保断链期间产生的事件、
                # token 和状态不会只依赖最后一条 socket 消息。

                trace_rows = _extract_trace_from_history(
                    history_nodes,
                    attempt_id=task.attempt_id,
                    env_session_id=session_id,
                )
                trace_path = attempt_dir / "trace.jsonl"
                _append_unique_trace_rows(trace_path, trace_rows)
                artifact_sync = await self._recover_workspace_artifacts(
                    client,
                    session_id,
                    attempt_dir,
                    task,
                    overwrite_existing_files=True,
                )
                session_requires_followup = session_status in {
                    "interrupted",
                    "waiting_for_input",
                }
                if (
                    not self.config.keep_blade_session
                    and preserve_session is not True
                    and session_status in _TERMINAL_SESSION_STATUSES
                    and not session_requires_followup
                ):
                    cleanup_pending = not await self._cleanup(client, session_id)
                elif (
                    not self.config.keep_blade_session
                    and preserve_session is not True
                    and error_message is not None
                ):
                    cleanup_pending = True
                try:
                    persist_cleanup_state(cleanup_pending)
                except Exception as exc:
                    logger.warning(
                        "write recovery cleanup state failed for %s: %s",
                        session_id,
                        exc,
                    )
        except Exception as exc:
            if _is_not_found_error(exc):
                return AdapterResult(
                    attempt_id=task.attempt_id,
                    status="cancelled",
                    external_refs={
                        "blade_session_id": session_id,
                        "blade_base_url": self.config.base_url,
                        "recovered_after_restart": True,
                        "blade_session_status": "deleted",
                        "remote_session_terminated": True,
                        "remote_termination_reason": "deleted_externally",
                        "cleanup_pending": False,
                    },
                    error_code="remote_session_deleted",
                    error_message=(
                        "Blade session was deleted externally; recovery will not "
                        "recreate or reattach it"
                    ),
                    transport_status="disconnected",
                    events_count=events_count,
                    last_event_at=last_event_at,
                    thinking_count=thinking_count,
                )
            if not self.config.keep_blade_session and preserve_session is not True:
                cleanup_pending = True
                with contextlib.suppress(Exception):
                    persist_cleanup_state(True)
            classification = classify(
                f"{type(exc).__name__}: {exc}",
                phase="blade_recovery",
            )
            if transport_disconnected or classification.retryable:
                return AdapterResult(
                    attempt_id=task.attempt_id,
                    status="chat_failed",
                    external_refs={
                        "blade_session_id": session_id,
                        "blade_base_url": self.config.base_url,
                        "recovered_after_restart": True,
                        "cleanup_pending": cleanup_pending,
                    },
                    error_code="transport_reconnect_exhausted",
                    error_message=(
                        "transport recovery failed while collecting Blade session: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                    transport_status="disconnected",
                    events_count=events_count,
                    last_event_at=last_event_at,
                    thinking_count=thinking_count,
                )
            return AdapterResult(
                attempt_id=task.attempt_id,
                status="interrupted",
                external_refs={
                    "blade_session_id": session_id,
                    "blade_base_url": self.config.base_url,
                    "recovered_after_restart": True,
                },
                error_code="blade_recovery_failed",
                error_message=f"{type(exc).__name__}: {exc}",
                transport_status="disconnected",
                events_count=events_count,
                last_event_at=last_event_at,
                thinking_count=thinking_count,
            )

        input_tokens = output_tokens = 0
        for node in history_nodes:
            usage = node.get("usage") or {}
            input_tokens += usage_input_tokens(usage)
            output_tokens += usage_output_tokens(usage)

        transport_status = "disconnected" if transport_disconnected else "connected"
        if error_message and transport_disconnected:
            status = "chat_failed"
            error_code = "transport_reconnect_exhausted"
            error_message = (
                "transport recovery exhausted while Blade session remained "
                f"{session_status or 'unknown'}"
            )
        elif error_message:
            status = "timeout"
            error_code = (
                "agent_total_timeout"
                if "total timeout" in error_message
                else "agent_inactivity_timeout"
            )
        elif session_status in ("completed", "ok"):
            # "ok" 与 _classify_outcome 对 chat:end status 的成功判定对齐
            status = "completed"
            error_code = None
        elif session_status in ("cancelled", "canceled", "stopped"):
            status = "cancelled"
            error_code = "remote_session_terminated"
            error_message = (
                "Blade session was terminated externally with status "
                f"{session_status}"
            )
        elif session_status == "waiting_for_input":
            status = "interrupted"
            error_code = "blade_session_waiting_for_input"
            error_message = (
                "Blade session is waiting for explicit user input; "
                "recovery will not wait or answer automatically"
            )
        elif session_status == "interrupted":
            status = "interrupted"
            error_code = "blade_session_interrupted"
            error_message = (
                "Blade session is interrupted but remains resumable; "
                "recovery will not restart it automatically"
            )
        elif session_status in ("failed", "error"):
            status = "chat_failed"
            error_code = "chat_error"
            error_message = "Blade session failed before Octagon recovery completed"
        else:
            status = "interrupted"
            error_code = "blade_session_interrupted"
            error_message = f"Blade session status after restart: {session_status or 'unknown'}"

        return AdapterResult(
            attempt_id=task.attempt_id,
            status=status,
            external_refs={
                "blade_session_id": session_id,
                "blade_base_url": self.config.base_url,
                "recovered_after_restart": True,
                "blade_session_status": session_status,
                "remote_session_terminated": session_status
                in ("cancelled", "canceled", "stopped"),
                "session_requires_followup": session_status
                in ("interrupted", "waiting_for_input"),
                "artifact_sync": artifact_sync,
                "cleanup_pending": cleanup_pending,
            },
            error_code=error_code,
            error_message=error_message,
            transport_status=transport_status,
            events_count=events_count,
            last_event_at=last_event_at,
            thinking_count=thinking_count,
            token_usage={
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
            duration_ms=int((time.monotonic() - started) * 1000),
            security_meta=build_security_meta(
                execution_locus=self.capabilities.execution_locus,
                permission_mode="sandbox",
                workspace_root=None,
                sandbox_id=session_id,
                extra=BLADE_SANDBOX_SECURITY_EXTRA,
            ),
        )

    async def _upload_task_materials(
        self,
        client: BladeAgentClient,
        session_id: str,
        task: AdapterRunInput,
        data_path: Path,
        *,
        web_surface: bool = False,
    ) -> None:
        """上传任务附件；原生入口另传私有 attempt.json，网页入口不传。"""
        if not web_surface:
            attempt_json = json.dumps({
                "attempt_id": task.attempt_id,
                "env_base_url": (
                    self.config.sandbox_env_base_url or task.env_base_url
                ),
                "env_token": task.env_token,
            }, ensure_ascii=False)
            with tempfile.NamedTemporaryFile(
                mode="w", suffix="_attempt.json", delete=False, encoding="utf-8"
            ) as f:
                f.write(attempt_json)
                tmp_path = f.name
            try:
                await client.upload_file(
                    session_id, tmp_path,
                    dir_path=".octagon", remote_path="attempt.json",
                )
            finally:
                Path(tmp_path).unlink(missing_ok=True)

        uploaded = task.task_context.get("uploaded_files")
        if uploaded and isinstance(uploaded, list):
            workspace = Path(data_path).resolve() / "attempts" / task.attempt_id / "skill_workspace"
            for uf in uploaded:
                name = uf.get("name", "")
                src = workspace / name
                if not src.is_file():
                    # 与 dispatch._copy_uploads / ssh_claude 对齐：物料缺失硬失败，
                    # 否则 agent 会在空的 blade workspace 里搜文件浪费整个 attempt
                    raise FileNotFoundError(f"任务物料不在本地 workspace: {name} ({src})")
                await client.upload_file(session_id, str(src), remote_path=name)
                logger.info("upload material %s to blade workspace", name)

        material_files = task.task_context.get("_agent_material_files")
        if material_files and isinstance(material_files, list):
            workspace = Path(data_path).resolve() / "attempts" / task.attempt_id / "skill_workspace"
            for rel_raw in sorted({str(x) for x in material_files}):
                rel = Path(rel_raw)
                if rel.is_absolute() or ".." in rel.parts:
                    raise FileNotFoundError(f"非法 agent material 路径: {rel_raw}")
                src = workspace / rel
                if not src.is_file():
                    raise FileNotFoundError(f"agent material 不在本地 workspace: {rel_raw} ({src})")
                dir_path = "." if rel.parent == Path(".") else rel.parent.as_posix()
                await client.upload_file(
                    session_id,
                    str(src),
                    dir_path=dir_path,
                    remote_path=rel.name,
                )
                logger.info("upload agent material %s to blade workspace", rel_raw)

    def _fallback_thinking_from_messages(
        self, session_id: str, thinking_path: Path
    ) -> int:
        """从 GET /messages 兜底提取 thinking，写 thinking.jsonl，返回新增条数。

        SDK 无 messages 方法，直接调 REST。assistant 消息的 blocks 里 type=thinking
        的块结构同实时流（content 承载文本）。同步函数，由调用方 to_thread 包裹。
        """
        base = self.config.base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        resp = httpx.get(
            f"{base}/api/sessions/{session_id}/messages",
            headers=headers,
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
        msgs = data if isinstance(data, list) else data.get(
            "messages", data.get("items", data.get("data", []))
        )
        if not isinstance(msgs, list):
            return 0
        added = 0
        with thinking_path.open("a", encoding="utf-8") as fp:
            for msg in msgs:
                if not isinstance(msg, dict):
                    continue
                for block in msg.get("blocks") or []:
                    if not isinstance(block, dict) or block.get("type") != "thinking":
                        continue
                    content = block.get("content", "")
                    if not content:
                        continue
                    added += 1
                    fp.write(json.dumps({
                        "timestamp": msg.get("created_at") or _now_iso(),
                        "sequence": added,
                        "content": content,
                        "type": "thinking",
                        "source": "messages_fallback",
                    }, ensure_ascii=False) + "\n")
        return added

    async def _retry_recovery_read(
        self,
        session_id: str,
        operation: str,
        read: Any,
        *,
        remaining_seconds: float | None = None,
    ) -> Any:
        for attempt in range(1, _RECOVERY_READ_MAX_ATTEMPTS + 1):
            try:
                return await read()
            except Exception as exc:
                diagnostic = f"{type(exc).__name__}: {exc}"
                classification = classify(
                    diagnostic, phase=f"blade_recovery_{operation}"
                )
                if not should_retry(
                    classification,
                    attempt=attempt,
                    max_attempts=_RECOVERY_READ_MAX_ATTEMPTS,
                    remaining_seconds=remaining_seconds,
                ):
                    raise
                delay = retry_delay_seconds(classification, attempt)
                logger.warning(
                    "blade recovery %s failed transiently for %s "
                    "(%s, attempt %d/%d); retrying in %.1fs: %s",
                    operation,
                    session_id,
                    classification.code,
                    attempt,
                    _RECOVERY_READ_MAX_ATTEMPTS,
                    delay,
                    classification.summary,
                )
                await asyncio.sleep(delay)
        raise RuntimeError(f"unreachable recovery retry state: {operation}")

    async def _get_messages_async(
        self,
        session_id: str,
        *,
        remaining_seconds: float | None = None,
    ) -> list[dict[str, Any]]:
        base = self.config.base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        async with httpx.AsyncClient(
            headers=headers, timeout=self.config.request_timeout_seconds
        ) as http:
            for attempt in range(1, _RECOVERY_READ_MAX_ATTEMPTS + 1):
                try:
                    resp = await http.get(
                        f"{base}/api/sessions/{session_id}/messages"
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    break
                except httpx.HTTPError as exc:
                    diagnostic = f"{type(exc).__name__}: {exc}"
                    classification = classify(
                        diagnostic, phase="blade_messages_recovery"
                    )
                    if not should_retry(
                        classification,
                        attempt=attempt,
                        max_attempts=_RECOVERY_READ_MAX_ATTEMPTS,
                        remaining_seconds=remaining_seconds,
                    ):
                        raise
                    delay = retry_delay_seconds(classification, attempt)
                    logger.warning(
                        "blade messages read failed transiently for %s "
                        "(%s, attempt %d/%d); retrying in %.1fs: %s",
                        session_id,
                        classification.code,
                        attempt,
                        _RECOVERY_READ_MAX_ATTEMPTS,
                        delay,
                        classification.summary,
                    )
                    await asyncio.sleep(delay)
        if isinstance(data, list):
            messages = data
        elif isinstance(data, dict):
            messages = data.get("messages", data.get("items", data.get("data", [])))
        else:
            messages = []
        return [item for item in messages if isinstance(item, dict)]

    async def _download_priority_paths(
        self,
        client: BladeAgentClient,
        session_id: str,
        paths: list[str],
        *,
        download_root: Path,
        baseline_root: Path,
        errors: list[str],
        project_workspace: str = "",
    ) -> tuple[list[str], list[str]]:
        """定向下载 agent 编辑过的路径，并校验落地内容确实变了。

        「`download_file` 没抛异常」不等于「拿到了产物」：实测下载成功但文件
        内容仍是基线（mtime 停在物料拷贝时间）。远端路径前缀可能与本地不同，
        请求到的其实是另一个位置的同名文件。所以下载后比对 hash——与基线一致
        就视为未命中，换候选前缀再试（需求 5.1/5.2）。
        """
        got: list[str] = []
        missing: list[str] = []
        for rel in paths:
            target = download_root / rel
            # 基线必须取自**活的 workspace**，不能取 download_root：续聊/恢复
            # 时 download_root 是新建的空 staging 目录，摘要恒为 None，校验就
            # 完全失效——而那恰恰是这个校验要守的路径。
            # 文件不存在时为 None：那时任何内容都算命中（本来就没有）。
            baseline = _digest_of(baseline_root / rel)
            landed = False
            for candidate in _candidate_remote_paths(
                rel, project_workspace=project_workspace
            ):
                try:
                    content = await client.download_file(session_id, candidate)
                except Exception:
                    # 候选路径猜错会 404，这是预期内的试探，不记进 errors——
                    # errors 非空会让迭代评审路径把 attempt 判成 chat_failed。
                    continue
                if baseline is not None and _digest_bytes(content) == baseline:
                    # 内容没变 = 没真正拿到，继续试下一个候选。
                    continue
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                except OSError as exc:
                    errors.append(f"write {rel}: {exc}")
                    break
                got.append(rel)
                landed = True
                break
            if not landed:
                missing.append(rel)
        return got, missing

    async def _recover_workspace_artifacts(
        self,
        client: BladeAgentClient,
        session_id: str,
        attempt_dir: Path,
        task: AdapterRunInput,
        *,
        overwrite_existing_files: bool = False,
    ) -> dict[str, Any]:
        """递归拉取 blade workspace 到本地 skill_workspace。

        不再依赖硬编码文件名：能拉的全拉（跳过运行时内部目录），scorer 自己判定。
        首轮已存在且非空的本地文件默认跳过（uploaded 物料本来就在本地）。
        Session 续聊或恢复时远端工作区是当前版本权威来源，必须覆盖旧轮本地文件。
        """
        workspace_dir = attempt_dir / "skill_workspace"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        staging_dir = (
            Path(tempfile.mkdtemp(prefix=".skill_workspace.sync-", dir=attempt_dir))
            if overwrite_existing_files
            else None
        )
        download_root = staging_dir or workspace_dir
        overwrite_existing = {
            str(x)
            for x in (task.task_context.get("_agent_material_files") or [])
            if isinstance(x, str)
        }
        # Task uploads are copied locally before the Blade session starts. They
        # are seed inputs, not final artifacts: the agent may edit the same file
        # in the remote workspace (for example amortization.db). Allow the
        # remote copy to replace the local seed during recovery.
        for item in (task.task_context.get("uploaded_files") or []):
            if isinstance(item, str):
                name = Path(item).name
            elif isinstance(item, dict):
                name = str(item.get("name") or Path(str(item.get("path") or "")).name)
            else:
                continue
            if name and Path(name).name == name:
                overwrite_existing.add(name)

        listing: list[dict[str, Any]] = []
        downloaded: list[str] = []
        skipped_existing: list[str] = []
        errors: list[str] = []
        truncated = False

        # 优先回收：先按 agent 实际编辑过的路径定向下载，再进 BFS 兜底。
        # BFS 的 500 上限本身是合理的防爆保护，但它会把交付物挤掉——
        # 优先路径不受这个上限约束。
        # workspace_root 留空是故意的：blade 在沙盒内按 session 选定工作区
        # （/root/智能助手工作空间/<session>），adapter 事前不知具体子目录。
        # `normalize_path` 用工作区标记反查前缀，与安全扫描的做法一致。
        priority_paths, priority_source = agent_edited_paths(attempt_dir)
        priority_downloaded: list[str] = []
        priority_missing: list[str] = []
        if priority_paths:
            got, missing = await self._download_priority_paths(
                client,
                session_id,
                priority_paths,
                download_root=download_root,
                baseline_root=workspace_dir,
                errors=errors,
            )
            priority_downloaded.extend(got)
            priority_missing.extend(missing)
            downloaded.extend(got)

        queue: list[tuple[str, int]] = [(".", 0)]
        seen_files = 0
        while queue:
            dir_path, depth = queue.pop(0)
            try:
                entries = await client.list_dir(session_id, dir_path)
            except Exception as exc:
                errors.append(f"list {dir_path}: {exc}")
                continue
            for entry in entries:
                rel = entry.path or (
                    entry.name if dir_path == "." else f"{dir_path}/{entry.name}"
                )
                if entry.is_dir:
                    if entry.name in _ARTIFACT_SKIP_DIRS or entry.name.startswith("."):
                        continue
                    if depth + 1 <= _ARTIFACT_MAX_DEPTH:
                        queue.append((rel, depth + 1))
                    continue
                if entry.name.startswith("."):
                    continue
                seen_files += 1
                if seen_files > _ARTIFACT_MAX_FILES:
                    truncated = True
                    break
                listing.append({"path": rel})
                target = download_root / rel
                if rel in priority_set:
                    continue
                if (
                    staging_dir is None
                    and
                    target.exists()
                    and target.stat().st_size > 0
                    and rel not in overwrite_existing
                    and not overwrite_existing_files
                ):
                    skipped_existing.append(rel)
                    continue
                try:
                    content = await client.download_file(session_id, rel)
                except Exception as exc:
                    errors.append(f"download {rel}: {exc}")
                    continue
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                except OSError as exc:
                    errors.append(f"write {rel}: {exc}")
                    continue
                downloaded.append(rel)
            if truncated:
                break

        result = {
            "downloaded": downloaded,
            "priority_downloaded": priority_downloaded,
            "priority_source": priority_source,
            "skipped_existing": skipped_existing,
            "total_listed": len(listing),
            "errors": errors,
            # 这四个字段恒存在（需求 6.2）：排查低分时要一眼看出是
            # 「agent 没做」还是「没回收到」。truncated_at 为 None 表示没截断。
            "truncated_at": _ARTIFACT_MAX_FILES if truncated else None,
            "priority_downloaded": priority_downloaded,
            "priority_missing": priority_missing,
            "priority_source": "+".join(priority_source) if priority_source else "none",
        }
        if truncated:
            # 静默截断会被误读成"全量回收"，显式标注
            result["truncated_at"] = _ARTIFACT_MAX_FILES
            logger.warning(
                "artifact recovery truncated at %d files (session %s)",
                _ARTIFACT_MAX_FILES, session_id,
            )
        if staging_dir is not None:
            if errors or truncated:
                # staging 被整个丢弃 —— 包括优先阶段已经拉到的那些文件。
                # 若仍把它们记在 priority_downloaded 里，`artifact_recovery_failed`
                # 会判定回收成功，于是 attempt 拿着一个「对空工作区打出的分数」
                # 进矩阵，且不带 infrastructure 标记。产物确实没落地，记账就得
                # 照实说：全部转入 priority_missing。
                shutil.rmtree(staging_dir, ignore_errors=True)
                if priority_downloaded:
                    logger.warning(
                        "staging 丢弃使 %d 个优先产物未落地 (session %s)",
                        len(priority_downloaded), session_id,
                    )
                    for rel in priority_downloaded:
                        if rel in downloaded:
                            downloaded.remove(rel)
                    priority_missing.extend(priority_downloaded)
                    priority_downloaded.clear()
                    result["priority_downloaded"] = priority_downloaded
                    result["priority_missing"] = priority_missing
                    result["downloaded"] = downloaded
                    result["staging_discarded"] = True
            else:
                backup_dir = attempt_dir / f".skill_workspace.previous-{time.time_ns()}"
                try:
                    workspace_dir.rename(backup_dir)
                    staging_dir.rename(workspace_dir)
                except Exception:
                    if not workspace_dir.exists() and backup_dir.exists():
                        backup_dir.rename(workspace_dir)
                    shutil.rmtree(staging_dir, ignore_errors=True)
                    raise
                else:
                    shutil.rmtree(backup_dir, ignore_errors=True)
                    result["workspace_replaced"] = True
        return result

    async def _cleanup(self, client: BladeAgentClient, session_id: str | None) -> bool:
        if not session_id:
            return True

        for attempt in range(1, _CLEANUP_MAX_ATTEMPTS + 1):
            background_tasks_ok = True
            try:
                await self._stop_running_background_tasks(session_id)
            except Exception as exc:
                background_tasks_ok = False
                logger.warning(
                    "cleanup blade session %s background tasks failed "
                    "(attempt %d/%d): %s",
                    session_id,
                    attempt,
                    _CLEANUP_MAX_ATTEMPTS,
                    exc,
                )
            if self.config.keep_blade_session:
                if background_tasks_ok:
                    return True
                if attempt < _CLEANUP_MAX_ATTEMPTS:
                    await asyncio.sleep(float(attempt))
                    continue
                return False
            try:
                await client.delete_session(session_id)
            except Exception as exc:
                diagnostic = f"{type(exc).__name__}: {exc}"
                classification = classify(diagnostic, phase="blade_cleanup")
                logger.warning(
                    "cleanup blade session %s failed "
                    "(attempt %d/%d, %s): %s",
                    session_id,
                    attempt,
                    _CLEANUP_MAX_ATTEMPTS,
                    classification.code,
                    classification.summary,
                )
                if not should_retry(
                    classification,
                    attempt=attempt,
                    max_attempts=_CLEANUP_MAX_ATTEMPTS,
                    remaining_seconds=None,
                ):
                    return False
                await asyncio.sleep(retry_delay_seconds(classification, attempt))
                continue
            return True
        return False

    async def _stop_running_background_tasks(self, session_id: str) -> list[str]:
        base = self.config.base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        encoded_session_id = quote(session_id, safe="")
        async with httpx.AsyncClient(
            headers=headers,
            timeout=self.config.request_timeout_seconds,
        ) as http:
            response = await http.get(
                f"{base}/api/sessions/{encoded_session_id}/background-tasks"
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise TypeError(
                    "unexpected background tasks response: "
                    f"{type(payload).__name__}"
                )

            stopped: list[str] = []
            failed: list[str] = []
            for item in payload:
                if not isinstance(item, dict):
                    continue
                status = str(item.get("status") or "").lower()
                legacy_status = str(item.get("legacy_status") or "").lower()
                if (
                    status not in _RUNNING_BACKGROUND_TASK_STATUSES
                    and legacy_status != "running"
                ):
                    continue
                task_id = str(item.get("id") or "").strip()
                if not task_id:
                    continue
                encoded_task_id = quote(task_id, safe="")
                stop_response = await http.post(
                    f"{base}/api/sessions/{encoded_session_id}/background-tasks/"
                    f"{encoded_task_id}/stop"
                )
                if stop_response.status_code == 404:
                    continue
                try:
                    stop_response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    logger.warning(
                        "stop blade background task %s/%s failed: %s",
                        session_id,
                        task_id,
                        exc,
                    )
                    failed.append(task_id)
                    continue
                stopped.append(task_id)
            if failed:
                raise RuntimeError(
                    "failed to stop Blade background task(s): "
                    + ", ".join(failed)
                )
            return stopped


def _extract_trace_from_history(
    nodes: list[dict[str, Any]],
    *,
    attempt_id: str,
    env_session_id: str,
) -> list[dict[str, Any]]:
    """把 get_history 的 nodes 归一成 Octagon trace.jsonl 行。

    结构依据 blade-agent host/sessions/history.py list_history()（Phase 0 已实测验证，
    见 docs/specs/batch_benchmark/phase0_history_sample.json）：
    - assistant 节点的 tool_calls（OpenAI 格式）与后续 role=tool 节点的 content 配对；
      tool 节点不带 tool_call_id → 同一 loop_name 内按序 FIFO 配对
    - duration_ms 用 assistant 节点 → tool 结果节点的 timestamp 差近似
    - is_error 从结果内容启发式判断（exit_code 非 0 / Error 前缀）
    - 过滤 is_deprecated 节点
    """
    rows: list[dict[str, Any]] = []
    # loop_name -> 待配对的 (tool_call, assistant_ts) FIFO 队列
    pending: dict[str, list[tuple[dict[str, Any], str | None]]] = {}

    for node in nodes:
        if node.get("is_deprecated"):
            continue
        if node.get("kind") != "message":
            continue
        role = node.get("role")
        loop = str(node.get("loop_name") or "root")
        ts = node.get("timestamp")

        if role == "assistant" and node.get("tool_calls"):
            for tc in node["tool_calls"]:
                pending.setdefault(loop, []).append((tc, ts))
            continue

        if role == "tool":
            queue = pending.get(loop) or []
            tc, call_ts = queue.pop(0) if queue else ({}, None)
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            raw_args = fn.get("arguments", "")
            try:
                arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                if not isinstance(arguments, dict):
                    arguments = {"_raw": arguments}
            except ValueError:
                arguments = {"_raw": raw_args}
            result = node.get("content", "")
            row = {
                "timestamp": ts,
                "attempt_id": attempt_id,
                "env_session_id": env_session_id,
                "tool_name": fn.get("name") or "unknown",
                "arguments": arguments,
                "result": result,
                "is_error": _looks_like_error(result),
                "duration_ms": _ts_diff_ms(call_ts, ts),
                "loop_name": loop,
                "tool_call_id": tc.get("id") if isinstance(tc, dict) else None,
            }
            skill_call = _parse_blade_skill_run(arguments)
            if skill_call:
                row["skill_id"], row["skill_tool"], row["skill_args"] = skill_call
            rows.append(row)
    return rows


def _trace_row_identity(row: dict[str, Any]) -> str:
    tool_call_id = row.get("tool_call_id")
    if isinstance(tool_call_id, str) and tool_call_id:
        return "tool_call:" + "|".join(
            (
                str(row.get("env_session_id") or ""),
                str(row.get("loop_name") or "root"),
                tool_call_id,
            )
        )
    stable = {
        key: row.get(key)
        for key in (
            "env_session_id",
            "loop_name",
            "timestamp",
            "tool_name",
            "arguments",
            "result",
        )
    }
    payload = json.dumps(
        stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return "fallback:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _append_unique_trace_rows(path: Path, rows: list[dict[str, Any]]) -> int:
    identities: set[str] = set()
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            with contextlib.suppress(json.JSONDecodeError):
                existing = json.loads(line)
                if isinstance(existing, dict):
                    identities.add(_trace_row_identity(existing))
    added = 0
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            identity = _trace_row_identity(row)
            if identity in identities:
                continue
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            identities.add(identity)
            added += 1
    return added


def _parse_blade_skill_run(arguments: Any) -> tuple[str, str, dict[str, Any]] | None:
    """从 Bash command 里解析 `blade skill run` 的 skill 级语义。"""
    if not isinstance(arguments, dict):
        return None
    command = arguments.get("command")
    if not isinstance(command, str) or "blade skill run" not in command:
        return None
    m = _BLADE_SKILL_RUN_RE.search(command)
    if not m:
        return None
    skill_id, tool_name, raw_args = m.group(1), m.group(2), m.group(3)
    try:
        skill_args = json.loads(raw_args) if raw_args else {}
        if not isinstance(skill_args, dict):
            skill_args = {"_raw": skill_args}
    except ValueError:
        skill_args = {"_raw": raw_args}
    return skill_id, tool_name, skill_args


def _looks_like_error(result: Any) -> bool:
    if not isinstance(result, str):
        return False
    stripped = result.lstrip()
    if stripped.startswith("Error") or stripped.startswith("error:"):
        return True
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except ValueError:
            return False
        if isinstance(data, dict):
            exit_code = data.get("exit_code")
            if isinstance(exit_code, int) and exit_code != 0:
                return True
            if data.get("is_error") is True or data.get("error"):
                return True
    return False


def _ts_diff_ms(start: str | None, end: str | None) -> int:
    if not start or not end:
        return 0
    try:
        s = datetime.fromisoformat(str(start))
        e = datetime.fromisoformat(str(end))
    except ValueError:
        return 0
    diff = (e - s).total_seconds() * 1000
    return max(int(diff), 0)


def _classify_outcome(
    chat_end_status: str | None,
    error_message: str | None,
    finish_reason: str | None = None,
    *,
    unexpected_interaction: bool = False,
) -> str:
    # 未声明的交互请求：agent 停在等应答，任务不可能完成。语义是
    # "agent 要人介入而场景没准备"，不是超时——归 chat_failed 并配专用 error_code。
    if unexpected_interaction:
        return "chat_failed"
    if error_message and "timeout" in error_message.lower():
        return "timeout"
    if error_message and "socket disconnected" in error_message.lower():
        return "chat_failed"
    if error_message:
        return "chat_failed"
    if finish_reason == "terminal_tool":
        return "completed"
    if chat_end_status in ("ok", "completed"):
        return "completed"
    if chat_end_status == "failed":
        return "chat_failed"
    if chat_end_status == "paused":
        # 非 headless 下 paused 是"等交互应答"。走到这里说明应答循环没能收尾
        # （已声明的 turn 用尽仍在暂停），同样不是超时。
        return "chat_failed"
    if chat_end_status == "interrupted":
        return "chat_failed"
    if chat_end_status is None:
        return "timeout"
    return "chat_failed"


def _outcome_error_code(
    status: str,
    error_message: str | None,
    finish_reason: str | None,
    *,
    unexpected_interaction: bool = False,
) -> str | None:
    if status == "completed":
        return None
    if unexpected_interaction:
        return "unexpected_interaction"
    lowered = (error_message or "").lower()
    if "socket disconnected" in lowered:
        return "transport_reconnect_exhausted"
    if "inactivity timeout" in lowered:
        return "agent_inactivity_timeout"
    if "total timeout" in lowered:
        return "agent_total_timeout"
    if "timeout" in lowered:
        return "agent_timeout"
    if "finish_reason" in lowered or "流式响应未正常结束" in lowered:
        return "llm_stream_incomplete"
    return finish_reason or "chat_error"


def sync_blade_skills(envs_path: Path, skills_path: Path) -> list[str]:
    """把所有 env 的 blade_skill/ 同步到 <skills_path>/octagon/<env-name>/。

    路线 A 下这是旧 env 薄壳的注册通道：blade server 启动时把 <skills_path>
    加进 BLADE_SKILL_PATHS，薄壳即成为注册 skill（id = "octagon/<env-name>"），
    运行期不再向 session 上传。
    """
    envs_path = Path(envs_path)
    skills_path = Path(skills_path)
    target = skills_path / "octagon"
    tmp = skills_path / ".octagon.tmp"
    skills_path.mkdir(parents=True, exist_ok=True)
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)

    synced: list[str] = []
    if envs_path.is_dir():
        for env_dir in sorted(p for p in envs_path.iterdir() if p.is_dir()):
            blade_skill = env_dir / "blade_skill"
            if not blade_skill.is_dir():
                continue
            shutil.copytree(blade_skill, tmp / env_dir.name)
            synced.append(env_dir.name)

    if target.exists():
        shutil.rmtree(target)
    tmp.rename(target)
    logger.info("sync_blade_skills: %s envs -> %s", len(synced), target)
    return synced


# 软件工厂只在这里复用前端的项目标题规则；app-dev bootstrap 消息、工作区
# 准备与 skills 全部由当前 BA 的 /api/agent-board/projects 生成，Octagon 不留副本。
_PROJECT_TITLE_MAX = 36


def _project_prompt_title(prompt: str) -> str:
    """与软件工厂前端 ``projectPromptTitle`` 保持一致。"""
    text = " ".join(prompt.strip().split())
    return text[:_PROJECT_TITLE_MAX] if text else "新对话"


def _chat_intent(prompt: str) -> str:
    """普通聊天首页用用户输入前 20 个 Unicode 字符作为会话标题。"""
    text = prompt.strip() or "新会话"
    return text[:20]


def _render_prompt(
    task: AdapterRunInput,
    env: "AdapterEnv | None" = None,
    data_path: Path | None = None,
) -> str:
    """只传任务消息。工具引导交给 blade 真实 AGENTS.md / SKILL.md（路线 A）。"""
    parts: list[str] = []
    # 时间预算放最前（blade ChatSendPayload 无独立 system 通道，回落 message 顶部；
    # 语义是"框架级约束"，置顶更醒目）。None（不限时）不注入。
    notice = (
        time_budget_notice(task.timeout_seconds)
        if task.notify_model_of_timeout
        else None
    )
    if notice:
        parts += [notice, ""]

    parts.append(task.task_prompt)

    if task.task_context:
        context = prompt_context(task.task_context)
        if context:
            parts += ["", "上下文:", json.dumps(context, ensure_ascii=False, indent=2)]
    return "\n".join(parts)


def _render_turn_prompt(
    task: AdapterRunInput,
    turn: Any,
    env: "AdapterEnv | None" = None,
    data_path: Path | None = None,
) -> str:
    """渲染某一轮的消息。

    首轮（turn_index==0）走 `_render_prompt` 的完整渲染：time budget notice 与
    task context 只在首轮注入——后续轮重复宣称"本任务限时 X"会误导 agent，
    context 也已在同一 session 里。legacy 单轮因此与改造前逐字节一致。
    后续轮只发该轮 prompt 原文。
    """
    if turn.turn_index == 0:
        # legacy 轮的 prompt 就是 task_prompt，等价于旧路径；多轮首轮用该轮
        # prompt 替换任务消息，notice/context 渲染方式不变。
        if turn.prompt == task.task_prompt:
            return _render_prompt(task, env, data_path)
        parts: list[str] = []
        notice = (
            time_budget_notice(task.timeout_seconds)
            if task.notify_model_of_timeout
            else None
        )
        if notice:
            parts += [notice, ""]
        parts.append(turn.prompt or "")
        if task.task_context:
            context = prompt_context(task.task_context)
            if context:
                parts += [
                    "", "上下文:",
                    json.dumps(context, ensure_ascii=False, indent=2),
                ]
        return "\n".join(parts)
    return turn.prompt or ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _append_jsonl(path: Path, data: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, default=str) + "\n")


_TURN_TERMINAL_STATUSES = frozenset(
    {"completed", "ok", "failed", "error", "interrupted", "cancelled"}
)


def _turn_is_terminal(turn: dict[str, Any]) -> bool:
    """/messages 返回的 turn 是否已到终态。

    status 缺失视为终态（旧版 blade /messages 只返回已完成的 turn，部分行
    可能不带 status 字段）；只有显式的进行中状态才判非终态。
    """
    status = turn.get("status")
    if status is None:
        return True
    return str(status) in _TURN_TERMINAL_STATUSES


def _projection_identity(payload: dict[str, Any]) -> str:
    turn_id = payload.get("turn_id") or payload.get("id")
    if turn_id:
        return f"{payload.get('role', '')}:{turn_id}"
    return json.dumps(
        {
            "role": payload.get("role"),
            "status": payload.get("status"),
            "blocks": payload.get("blocks") or [],
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


def _write_progress(path: Path, progress: dict[str, Any]) -> None:
    progress["updated_at"] = _now_iso()
    _write_json(path, progress)


def _persist_cleanup_pending_checkpoint(
    attempt_dir: Path,
    session_id: str | None,
    external_refs: dict[str, Any] | None = None,
) -> None:
    if not session_id:
        return
    checkpoint_path = attempt_dir / "recovery.json"
    checkpoint: dict[str, Any] = {}
    if checkpoint_path.is_file():
        with contextlib.suppress(OSError, json.JSONDecodeError):
            loaded = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                checkpoint = loaded
    if external_refs:
        checkpoint.update(external_refs)
    checkpoint["blade_session_id"] = session_id
    checkpoint["cleanup_pending"] = True
    _write_json(checkpoint_path, checkpoint)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)
