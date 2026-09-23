"""Vendored ATIF-v1.7 trajectory schema（Agent Trajectory Interchange Format）。

移植自 Harbor RFC 0001（``/home/yang/bench/harbor/rfcs/0001-trajectory-format.md``
与 ``@openagentsinc/atif`` 的 emit 类型）的 Pydantic 模型，自包含、不依赖 harbor
包。这是「归因」用的**对外对话流契约**，独立于 agent-octagon 的 wire/trajectory
数据结构——产出可以被 Harbor 的 ``trajectory_validator`` 校验通过。

校验规则对齐 RFC §II：
- ``step_id`` 从 1 连续递增（Trajectory 级）；
- 同一步里 ``observation.results[].source_call_id`` 必须能配对到该步的
  ``tool_calls``（null 允许——非标准工具调用或系统事件）；
- ``source != "agent"`` 的 step 不得带 agent 专属字段（model_name /
  reasoning_effort / reasoning_content / tool_calls / metrics）；
- ``llm_call_count=0`` 的 agent step 是确定性分发，不得带 metrics/reasoning_content；
- ``timestamp`` 必须是 ISO 8601；
- 嵌入的 ``subagent_trajectories`` 必须各带唯一的 ``trajectory_id``。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ATIF_SCHEMA_VERSION = "ATIF-v1.7"


class ImageSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    media_type: Literal["image/jpeg", "image/png", "image/gif", "image/webp"]
    path: str


class ContentPart(BaseModel):
    """v1.6+ 多模态内容块。``text`` 与 ``source`` 互斥（由 type 决定）。"""

    model_config = ConfigDict(extra="forbid")

    type: Literal["text", "image"]
    text: str | None = Field(default=None, min_length=0)
    source: ImageSource | None = None

    @model_validator(mode="after")
    def _text_or_image(self) -> "ContentPart":
        if self.type == "text" and self.source is not None:
            raise ValueError("text content part must not carry an image source")
        if self.type == "image" and self.text is not None:
            raise ValueError("image content part must not carry text")
        if self.type == "text" and self.text is None:
            raise ValueError("text content part requires text")
        if self.type == "image" and self.source is None:
            raise ValueError("image content part requires source")
        return self


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_call_id: str = Field(min_length=1)
    function_name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    extra: dict[str, Any] | None = None


class SubagentTrajectoryRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trajectory_id: str | None = None
    trajectory_path: str | None = None
    session_id: str | None = None
    extra: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _resolvable(self) -> "SubagentTrajectoryRef":
        if not self.trajectory_id and not self.trajectory_path:
            raise ValueError(
                "at least one of trajectory_id / trajectory_path must be set"
            )
        return self


class ObservationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_call_id: str | None = None
    content: str | list[ContentPart] | None = None
    subagent_trajectory_ref: list[SubagentTrajectoryRef] | None = None
    extra: dict[str, Any] | None = None


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    results: list[ObservationResult]


class Metrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_tokens: int | None = None
    cost_usd: float | None = None
    prompt_token_ids: list[int] | None = None
    completion_token_ids: list[int] | None = None
    logprobs: list[float] | None = None
    extra: dict[str, Any] | None = None


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: int = Field(ge=1)
    timestamp: str | None = None
    source: Literal["system", "user", "agent"]
    model_name: str | None = None
    reasoning_effort: str | float | None = None
    message: str | list[ContentPart] = ""
    reasoning_content: str | None = None
    tool_calls: list[ToolCall] | None = None
    observation: Observation | None = None
    metrics: Metrics | None = None
    llm_call_count: int | None = Field(default=None, ge=0)
    is_copied_context: bool | None = None
    extra: dict[str, Any] | None = None

    @field_validator("timestamp")
    @classmethod
    def _valid_timestamp(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"Invalid ISO 8601 timestamp: {v!r}") from exc
        return v

    @model_validator(mode="after")
    def _agent_only_fields(self) -> "Step":
        if self.source == "agent":
            return self
        for field in (
            "model_name",
            "reasoning_effort",
            "reasoning_content",
            "tool_calls",
            "metrics",
        ):
            if getattr(self, field) is not None:
                raise ValueError(
                    f"field {field!r} only applies when source='agent', "
                    f"but source={self.source!r}"
                )
        return self

    @model_validator(mode="after")
    def _llm_call_count_zero(self) -> "Step":
        if self.llm_call_count == 0 and self.source == "agent":
            for field in ("metrics", "reasoning_content"):
                if getattr(self, field) is not None:
                    raise ValueError(
                        f"field {field!r} must be absent when "
                        "llm_call_count=0 (deterministic dispatch)"
                    )
        return self


class Agent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    model_name: str | None = None
    tool_definitions: list[dict[str, Any]] | None = None
    extra: dict[str, Any] | None = None


class FinalMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_prompt_tokens: int | None = None
    total_completion_tokens: int | None = None
    total_cached_tokens: int | None = None
    total_cost_usd: float | None = None
    total_steps: int | None = None
    extra: dict[str, Any] | None = None


class Trajectory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["ATIF-v1.7"] = ATIF_SCHEMA_VERSION
    session_id: str | None = None
    trajectory_id: str | None = None
    agent: Agent
    steps: list[Step]
    notes: str | None = None
    final_metrics: FinalMetrics | None = None
    continued_trajectory_ref: str | None = None
    extra: dict[str, Any] | None = None
    subagent_trajectories: list["Trajectory"] | None = None

    @model_validator(mode="after")
    def _sequential_step_ids(self) -> "Trajectory":
        for i, step in enumerate(self.steps):
            expected = i + 1
            if step.step_id != expected:
                raise ValueError(
                    f"steps[{i}].step_id: expected {expected} (sequential from 1), "
                    f"got {step.step_id}"
                )
        return self

    @model_validator(mode="after")
    def _tool_call_references(self) -> "Trajectory":
        for step in self.steps:
            if step.observation is None:
                continue
            ids = {tc.tool_call_id for tc in step.tool_calls or []}
            for result in step.observation.results:
                if result.source_call_id is not None and result.source_call_id not in ids:
                    raise ValueError(
                        f"step {step.step_id} observation references source_call_id "
                        f"{result.source_call_id!r} not in that step's tool_calls"
                    )
        return self

    @model_validator(mode="after")
    def _embedded_subagent_ids(self) -> "Trajectory":
        if not self.subagent_trajectories:
            return self
        seen: set[str] = set()
        for i, sub in enumerate(self.subagent_trajectories):
            if sub.trajectory_id is None:
                raise ValueError(
                    f"subagent_trajectories[{i}].trajectory_id is required for "
                    f"embedded subagents (agent.name={sub.agent.name!r})"
                )
            if sub.trajectory_id in seen:
                raise ValueError(
                    f"subagent_trajectories[{i}].trajectory_id {sub.trajectory_id!r} "
                    "not unique"
                )
            seen.add(sub.trajectory_id)
        return self

    def to_json_dict(self) -> dict[str, Any]:
        """exclude_none 序列化，与 Harbor 的 ``to_json_dict()`` 行为一致。"""
        return self.model_dump(exclude_none=True, mode="json")
