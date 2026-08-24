"""API IO 模型(Pydantic v2)。

只放"对外或对 DB 序列化"用的模型。env loader 自己的 `Task` dataclass 是
内部数据结构,与本文件的 `TaskModel` 字段对应但不强耦合——单进程范围内,env
loader 的 Task 直接用 `to_db_row()` 落 `tasks` 表,反向通过 `TaskModel.from_row`
读出来。

`AttemptModel.external_refs_json` 是 string 字段(DB 存原始 JSON),API
响应用 `external_refs` 解析后的 dict。注意:**永远不要把 env_token 放进
external_refs**——`backend.db._validate_external_refs` 会拦截。
"""

from __future__ import annotations

import json
from typing import Any, Literal, get_args

from pydantic import BaseModel, Field, model_validator


class ApiError(BaseModel):
    """Error body used only by research-capability APIs."""

    code: str
    message: str
    request_id: str
    subsystem: str
    retryable: bool = False

# ---------- Task ----------------------------------------------------------


class TaskModel(BaseModel):
    id: str
    env_name: str
    prompt: str
    context: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = 600
    source: Literal["file", "adhoc"] = "file"
    created_at: str = ""

    def to_db_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "env_name": self.env_name,
            "prompt": self.prompt,
            "context_json": json.dumps(self.context, ensure_ascii=False),
            "constraints_json": json.dumps(self.constraints, ensure_ascii=False),
            "timeout_seconds": self.timeout_seconds,
            "source": self.source,
            "created_at": self.created_at,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "TaskModel":
        return cls(
            id=row["id"],
            env_name=row["env_name"],
            prompt=row["prompt"],
            context=json.loads(row.get("context_json") or "{}"),
            constraints=json.loads(row.get("constraints_json") or "{}"),
            timeout_seconds=row.get("timeout_seconds", 600),
            source=row.get("source", "file"),
            created_at=row.get("created_at", ""),
        )


# ---------- Run -----------------------------------------------------------

# `cancelled` 与 AttemptStatus 同理：用户主动停止是独立终态，不是
# failed 的子类。Run 层缺这个状态时，一次 Stop 会在 attempt 上是 cancelled、
# 在 run 上是 failed、在 cell 上又是 cancelled——同一个操作在三层表现不一致，
# 正是某些边界情况下需要人工二次修复的根源。
RunStatus = Literal[
    "queued", "running", "scoring", "completed", "failed", "cancelled"
]


class RunModel(BaseModel):
    id: str
    task_id: str
    env_name: str
    status: RunStatus = "queued"
    created_at: str = ""
    started_at: str | None = None
    ended_at: str | None = None


# ---------- Attempt -------------------------------------------------------

AttemptStatus = Literal[
    "queued",
    "starting_blade_session",
    "running",
    "scoring",
    # 用户主动停止：**独立终态**，
    # 不复用 timeout/scoring_failed。聚合与失败率先按 status 分类，借用
    # 任何失败态都会把一次操作决定报告成系统故障——借 timeout 会算成
    # 「Agent 没做完」，借 scoring_failed 会算成「判分设施坏了」。
    "cancelled",
    "interrupted",
    "completed",
    "gave_up",
    "timeout",
    "blade_service_unavailable",
    "auth_failed",
    "session_create_failed",
    "session_socket_overflow",
    "server_unreachable",
    "provider_quota_exhausted",
    "chat_failed",
    "scoring_failed",
    "cli_not_found",
    "cli_error",
    # capture 基础设施独立终态：strict 模式下改写型 wire source 无法
    # ready，agent 未启动；不复用任何 agent/blade 状态。
    "capture_infrastructure_failed",
    # outbound LLM request used a model other than attempts.model.  This is a
    # platform comparison-integrity failure, never an agent-quality result.
    "model_integrity_failed",
    "input_snapshot_missing",
]

# 非终态：attempt 仍在活跃流转、可继续接受工具调用。集合极小且稳定；终态由
# AttemptStatus 全集扣除它推导，这样 AttemptStatus 新增任何失败终态都自动纳入
# 终态，杜绝各处手抄终态集合时漏项（曾漏 cli_not_found/cli_error/
# capture_infrastructure_failed，使这些终态仍能凭原 token 调用工具改 env DB）。
NON_TERMINAL_ATTEMPT_STATUSES: frozenset[str] = frozenset(
    {
        "queued",
        "starting_blade_session",
        "running",
    }
)
# terminal = 所有已声明状态 - 非终态。用 typing.get_args 从 Literal 取全集，单一真源。
TERMINAL_ATTEMPT_STATUSES: frozenset[str] = (
    frozenset(get_args(AttemptStatus)) - NON_TERMINAL_ATTEMPT_STATUSES
)

TransportStatus = Literal["unknown", "connected", "disconnected", "not_applicable"]


class AttemptModel(BaseModel):
    id: str
    run_id: str
    task_id: str
    env_name: str
    agent_name: str = "blade-agent"
    # 该 attempt 请求使用的模型（multi-model / per-agent 场景同一 run 下各不相同）
    model: str | None = None
    status: AttemptStatus = "queued"
    transport_status: TransportStatus = "unknown"
    env_session_id: str
    env_token_hash: str
    external_refs: dict[str, Any] = Field(default_factory=dict)
    event_count: int = 0
    last_event_at: str | None = None
    thinking_count: int = 0
    tool_call_count: int = 0
    token_usage: dict[str, int] = Field(default_factory=dict)
    cost_estimate: float | None = None
    duration_ms: int = 0
    score_total: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    # status remains the failure fact; these are structured diagnostic facets.
    failure_kind: str | None = None
    retryable: bool | None = None
    started_at: str | None = None
    ended_at: str | None = None
    created_at: str = ""
    # 安全维度（与 score_total 并列，不合并）。② 场合快照 + ①③ 事件汇总。
    execution_locus: str | None = None
    permission_mode: str | None = None
    workspace_root: str | None = None
    security_event_count: int = 0
    security_max_severity: str | None = None
    security_hitl: dict[str, Any] = Field(default_factory=dict)
    security_reaction: str | None = None
    # 本次安全扫描各通道的采集来源：区分「真干净」与「未采集」。
    security_coverage: dict[str, Any] = Field(default_factory=dict)
    execution_status: str = "queued"
    execution_started_at: str | None = None
    execution_ended_at: str | None = None
    execution_deadline_at: str | None = None
    execution_error_code: str | None = None
    execution_error_message: str | None = None
    scoring_status: str = "not_ready"
    scoring_queued_at: str | None = None
    scoring_started_at: str | None = None
    scoring_ended_at: str | None = None
    scoring_deadline_at: str | None = None
    scoring_error_code: str | None = None
    scoring_error_message: str | None = None

    @model_validator(mode="after")
    def _no_token_in_external_refs(self) -> "AttemptModel":
        leaks = {"env_token", "env_token_hash"} & set(self.external_refs)
        if leaks:
            raise ValueError(
                f"external_refs 禁止包含敏感字段: {sorted(leaks)}"
            )
        return self

    def to_db_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "env_name": self.env_name,
            "agent_name": self.agent_name,
            "model": self.model,
            "status": self.status,
            "transport_status": self.transport_status,
            "env_session_id": self.env_session_id,
            "env_token_hash": self.env_token_hash,
            "external_refs_json": json.dumps(self.external_refs, ensure_ascii=False),
            "event_count": self.event_count,
            "last_event_at": self.last_event_at,
            "thinking_count": self.thinking_count,
            "tool_call_count": self.tool_call_count,
            "token_usage_json": json.dumps(self.token_usage, ensure_ascii=False),
            "cost_estimate": self.cost_estimate,
            "duration_ms": self.duration_ms,
            "score_total": self.score_total,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "failure_kind": self.failure_kind,
            "retryable": self.retryable,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "created_at": self.created_at,
            "execution_locus": self.execution_locus,
            "permission_mode": self.permission_mode,
            "workspace_root": self.workspace_root,
            "security_event_count": self.security_event_count,
            "security_max_severity": self.security_max_severity,
            "security_hitl_json": json.dumps(self.security_hitl, ensure_ascii=False),
            "security_reaction": self.security_reaction,
            "security_coverage_json": json.dumps(
                self.security_coverage, ensure_ascii=False
            ),
            "execution_status": self.execution_status,
            "execution_started_at": self.execution_started_at,
            "execution_ended_at": self.execution_ended_at,
            "execution_deadline_at": self.execution_deadline_at,
            "execution_error_code": self.execution_error_code,
            "execution_error_message": self.execution_error_message,
            "scoring_status": self.scoring_status,
            "scoring_queued_at": self.scoring_queued_at,
            "scoring_started_at": self.scoring_started_at,
            "scoring_ended_at": self.scoring_ended_at,
            "scoring_deadline_at": self.scoring_deadline_at,
            "scoring_error_code": self.scoring_error_code,
            "scoring_error_message": self.scoring_error_message,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "AttemptModel":
        return cls(
            id=row["id"],
            run_id=row["run_id"],
            task_id=row["task_id"],
            env_name=row["env_name"],
            agent_name=row.get("agent_name", "blade-agent"),
            model=row.get("model"),
            status=row.get("status", "queued"),
            transport_status=row.get("transport_status", "unknown"),
            env_session_id=row.get("env_session_id", ""),
            env_token_hash=row.get("env_token_hash", ""),
            external_refs=json.loads(row.get("external_refs_json") or "{}"),
            event_count=row.get("event_count", 0),
            last_event_at=row.get("last_event_at"),
            thinking_count=row.get("thinking_count", 0),
            tool_call_count=row.get("tool_call_count", 0),
            token_usage=json.loads(row.get("token_usage_json") or "{}"),
            cost_estimate=row.get("cost_estimate"),
            duration_ms=row.get("duration_ms", 0),
            score_total=row.get("score_total"),
            error_code=row.get("error_code"),
            error_message=row.get("error_message"),
            failure_kind=row.get("failure_kind"),
            retryable=(
                bool(row["retryable"]) if row.get("retryable") is not None else None
            ),
            started_at=row.get("started_at"),
            ended_at=row.get("ended_at"),
            created_at=row.get("created_at", ""),
            execution_locus=row.get("execution_locus"),
            permission_mode=row.get("permission_mode"),
            workspace_root=row.get("workspace_root"),
            security_event_count=row.get("security_event_count", 0),
            security_max_severity=row.get("security_max_severity"),
            security_hitl=json.loads(row.get("security_hitl_json") or "{}"),
            security_reaction=row.get("security_reaction"),
            security_coverage=json.loads(row.get("security_coverage_json") or "{}"),
            execution_status=row.get("execution_status", "queued"),
            execution_started_at=row.get("execution_started_at"),
            execution_ended_at=row.get("execution_ended_at"),
            execution_deadline_at=row.get("execution_deadline_at"),
            execution_error_code=row.get("execution_error_code"),
            execution_error_message=row.get("execution_error_message"),
            scoring_status=row.get("scoring_status", "not_ready"),
            scoring_queued_at=row.get("scoring_queued_at"),
            scoring_started_at=row.get("scoring_started_at"),
            scoring_ended_at=row.get("scoring_ended_at"),
            scoring_deadline_at=row.get("scoring_deadline_at"),
            scoring_error_code=row.get("scoring_error_code"),
            scoring_error_message=row.get("scoring_error_message"),
        )


# ---------- Score ---------------------------------------------------------


class ScoreModel(BaseModel):
    id: int | None = None
    attempt_id: str
    dimension: str
    value: int
    detail: str = ""
