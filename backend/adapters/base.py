"""Adapter 接口与共享数据结构。

初期只实现 BladeServiceAdapter,但接口先固定下来,runner 调度只看 Protocol,
不绑定具体实现,后续接 Claude / Codex 时直接加新文件即可。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

from ..wire.injection import WireInjection

logger = logging.getLogger(__name__)


def build_security_meta(
    *,
    execution_locus: str,
    permission_mode: str | None,
    workspace_root: str | None,
    sandbox_image: str | None = None,
    sandbox_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """执行场合快照。各 adapter 在已知启动参数处直接填，不做事后推断。

    - execution_locus: docker-sandbox / host / remote-host
    - permission_mode: 启动 CLI 时实际传入的权限/审批 flag 原文
    - workspace_root: agent 被授权工作的目录边界（供 target 判定）
    """
    meta: dict[str, Any] = {
        "execution_locus": execution_locus,
        "permission_mode": permission_mode,
        "workspace_root": workspace_root,
    }
    if sandbox_image:
        meta["sandbox_image"] = sandbox_image
    if sandbox_id:
        meta["sandbox_id"] = sandbox_id
    # 执行场合的附加字段（agent_version / egress_policy / server_side_network /
    # sandbox_shared …），由 launcher 或 adapter 按实际启动参数提供。
    for key, value in (extra or {}).items():
        if value is not None and key not in meta:
            meta[key] = value
    return meta


# adapter 自身的联网需求三态：
# 单一 bool 太粗——blade-agent server 本身可离线运行，但 Octagon 需要 TCP 连接
# 一个已运行的本机/局域网实例（local_service）；CC/Codex 走公网 API
# （public_internet）。两者是不同性质的依赖，不能混为一谈。
NetworkRequirement = Literal["none", "local_service", "public_internet"]


@dataclass(frozen=True)
class AdapterCapabilities:
    """adapter 能力静态声明。

    只做声明与展示：`execution_locus` 是 `build_security_meta()` 的权威取值
    来源（不再在各 adapter 调用点写字面量）；`network_required`/
    `system_requires` 第一版只声明不消费——不驱动任何运行时调度/门禁决策
    （沿用 "locus 只判定、只显示"原则）。
    `system_requires` 是 adapter 级本机二进制依赖，区别于 env 级
    `meta.yaml prerequisites`。
    """

    execution_locus: Literal["host", "docker-sandbox", "remote-host"]
    network_required: NetworkRequirement
    system_requires: tuple[str, ...] = ()
    # 运行中交互应答能力：能否在 agent
    # 等待 AskUserQuestion 类交互时提交应答。当前三个 adapter 均为 False
    # （Blade 待确认应答 API 后打开；CC `-p`/codex exec 无此通道）。
    # dispatch 对含 answer_interaction turn 的 conversation 按此 fail fast。
    interaction_answer: bool = False
    # 是否支持同一逻辑 session 在正常 turn 结束后由平台动态决定下一条消息。
    iterative_session: bool = False


class IterationTurnHandler(Protocol):
    async def on_turn_completed(
        self, *, producer_session_id: str | None
    ) -> Any: ...

    async def resume_after_restart(
        self, *, producer_session_id: str | None
    ) -> Any: ...

    def mark_feedback_sending(self, decision: Any) -> None: ...

    def mark_feedback_delivered(self, decision: Any) -> None: ...

    def finalize_last_successful_submission(self) -> bool: ...


@dataclass
class McpServerSpec:
    """场景显式提供的 MCP stdio 入口。

    adapter 只负责把这个声明翻译成对应 CLI 的配置，不得根据 ``env_name``
    猜测或合成 server。``cwd`` 是场景命令的解析基准目录。
    """

    name: str
    command: str
    args: tuple[str, ...] = ()
    cwd: str | None = None


# ---------- Conversation turn ----------

# interaction 专用于 answer_interaction 轮；其余四种对应实验阶段语义。
TurnPurpose = Literal["setup", "pressure", "probe", "task", "interaction"]
TurnAction = Literal["send_message", "answer_interaction"]


@dataclass(frozen=True)
class InteractionWaitFor:
    """描述 answer_interaction 轮要应答的交互请求。

    driver 收到 producer 的交互请求事件时按 tool_name（必要时加 question_key
    消歧）匹配；匹配不到任何已声明轮次的交互请求是 unexpected interaction，
    走失败态而不是替 agent 猜答案。
    """

    tool_name: str  # 如 "builtin:AskUserQuestion"
    question_key: str | None = None


@dataclass(frozen=True)
class ConversationTurn:
    """一轮 conversation 输入。

    两种形状由 action 区分：send_message 必须有 prompt；answer_interaction
    必须有 wait_for + answer（静态应答，场景作者在任务定义阶段写死）。互斥
    校验在 backend.conversation.plan 里做，这里只是数据载体。
    """

    turn_id: str
    turn_index: int
    action: TurnAction = "send_message"
    purpose: TurnPurpose = "task"
    score_after: bool = False
    prompt: str | None = None
    wait_for: InteractionWaitFor | None = None
    answer: dict[str, Any] | None = None


@dataclass
class AdapterRunInput:
    """单次 attempt 的最小输入。

    我们不直接把 backend.models 的整 dataclass / Pydantic 模型塞进 adapter,
    而是用一个轻量结构,避免 adapter 依赖 backend 内部 schema。

    `env_token` 是明文,由 runner 生成并传入。adapter 写到 blade workspace
    的 `.octagon/attempt.json` 里供 blade skill `tools.py` 读取,**不**得回写
    到任何持久化通道。
    """

    attempt_id: str
    task_id: str
    task_prompt: str
    task_context: dict[str, Any]
    # None → 不限时：不注入时间预算文案、执行层不启用 wait_for/deadline。
    timeout_seconds: int | None
    env_name: str
    env_skill_id: str  # "octagon/<env_name>"
    env_token: str  # 明文,只用于写 attempt.json
    env_base_url: str  # Octagon Env Attempt Server 的对外地址
    # 执行层始终按 timeout_seconds 截止；仅此开关为 True 时才向 agent
    # 注入时间预算文案。普通运行保持历史默认，实验协议会显式传入其配置。
    notify_model_of_timeout: bool = True
    # 所属 run。成本核算用它查 run 专属上游 key（backend/cost/credential.py）——
    # 该 key 由 run 内全部 session 与 judge 共用，其累计实扣差值就是这次 run 的
    # 资金口径。为 None 时 adapter 回落到 provider 配置的 key（审计降级为上界）。
    run_id: str | None = None
    # 由场景 meta.yaml 的 entrypoints.mcp 显式声明；空 tuple 表示场景不提供 MCP。
    mcp_servers: tuple[McpServerSpec, ...] = ()
    # wire 观测注入：lifecycle 合并所有 source 后的最终注入。
    # 默认零注入,enabled=False 时 adapter 行为与 wire 层不存在时完全一致。
    # capture_token 字段自身 repr=False,不会经由本 dataclass 泄漏。
    wire_injection: WireInjection = field(default_factory=WireInjection)
    # 多轮 conversation。空 tuple = 历史
    # 单轮行为：adapter 用 task_prompt + task_context 渲染一条消息，与本字段
    # 出现之前完全一致。非空时由 backend.conversation.plan.effective_conversation
    # 校验并消费；send_message 之外的轮次形状见 ConversationTurn。
    conversation_turns: tuple[ConversationTurn, ...] = ()
    # 动态产品返工控制器。None 保持所有历史单轮/静态 conversation 行为。
    iteration_turn_handler: IterationTurnHandler | None = None
    # 动态续聊消息被远端接受后的本地确认钩子。Blade 在收到该 chat 的首个
    # transport event 时调用；None 保持所有历史行为。
    prompt_delivery_handler: Callable[[], Any] | None = None


_EVALUATION_METADATA_CONTEXT_KEYS = frozenset(
    {"source_trace", "scenario_id", "adaptation"}
)


def prompt_context(task_context: dict[str, Any]) -> dict[str, Any]:
    """把 task_context 转成给 agent 看的上下文。

    `uploaded_files` 替换成「工作目录下的输入文件」文件名清单——物料已由
    dispatch/adapter 落到 agent 工作目录，agent 用文件名访问即可，
    不暴露宿主机路径。来源追踪、场景标识和改编说明属于评测 provenance，
    不得进入候选 prompt。三个 adapter 的 prompt 渲染共用，保证对比公平。
    """
    context = {
        k: v
        for k, v in task_context.items()
        if k != "uploaded_files"
        and k not in _EVALUATION_METADATA_CONTEXT_KEYS
        and not k.startswith("_")
    }
    uploaded = task_context.get("uploaded_files")
    if uploaded and isinstance(uploaded, list):
        names = [uf.get("name", "") for uf in uploaded if uf.get("name")]
        if names:
            context["工作目录下的输入文件"] = names
    return context


def _format_budget(seconds: int) -> str:
    """把秒数格式化成给 agent 看的自然时长（整分钟优先，否则带秒）。"""
    if seconds % 60 == 0:
        return f"{seconds // 60} 分钟"
    if seconds < 60:
        return f"{seconds} 秒"
    return f"{seconds // 60} 分 {seconds % 60} 秒"


def time_budget_notice(timeout_seconds: int | None) -> str | None:
    """时间预算文案（三个 adapter 共用，保证对比公平）。

    目的是测「单位时间能力上限」：告知 agent 总时长，并引导先产出可提交
    结果、再用剩余时间迭代优化。`None`（不限时）返回 None——不注入任何
    时间约束，agent 行为与该能力不存在时完全一致。
    """
    if timeout_seconds is None or timeout_seconds <= 0:
        return None
    budget = _format_budget(int(timeout_seconds))
    return (
        f"本任务限时 {budget}。请合理分配时间："
        "先尽快产出一个可用/可提交的结果，再用剩余时间迭代优化以争取更高分。"
        "时间到评测即结束，请确保届时已有最好结果。"
    )


@dataclass
class AdapterResult:
    attempt_id: str
    status: str
    external_refs: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    transport_status: str = "not_applicable"
    events_count: int = 0
    last_event_at: str | None = None
    thinking_count: int = 0
    tool_call_count: int = 0
    token_usage: dict[str, int] = field(default_factory=dict)
    duration_ms: int = 0
    # 执行场合快照：execution_locus / permission_mode / workspace_root / sandbox_*。
    # 各 adapter 在已知启动参数处填（Phase 2）；缺省空 dict，安全扫描回落默认值。
    security_meta: dict[str, Any] = field(default_factory=dict)
    # 多轮 conversation 摘要。
    # 单轮/legacy attempt 保持空 dict；多轮 adapter 用
    # backend.conversation.summary.summarize_conversation 填充。
    conversation_summary: dict[str, Any] = field(default_factory=dict)


class AgentAdapter(Protocol):
    @property
    def capabilities(self) -> AdapterCapabilities:
        """adapter 能力静态声明：类属性或简单 property 均可，
        不引入运行时协商。"""
        ...

    async def run(
        self,
        task: AdapterRunInput,
        env: Any,
        data_path: Path,
    ) -> AdapterResult:
        """运行一次 attempt。

        - 必须自行处理超时 / 失败分类,不允许向上抛异常(底层崩溃也要包成
          `error_code`+`error_message`,status 设为合理 terminal)。
        - 写 `events.jsonl` 到 `<data_path>/attempts/{attempt_id}/`
          （历史文档曾写 `blade_events.jsonl`，实际落盘文件名是
          `events.jsonl`，见 blade_service.py:192）。
        - 不写 env DB / trace.jsonl(那是 Env Attempt Server 的职责)。
        """
        ...
