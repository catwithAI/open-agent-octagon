"""Validated immutable DTOs for Experiment and RunGroup planning."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .hashing import canonical_hash
from backend.wire.policy import CapturePolicy
from backend.security_policy import assert_secret_free

PROTOCOL_SCHEMA_VERSION = "octagon-experiment-protocol-v1"
VARIANT_SPEC_SCHEMA_VERSION = "octagon-variant-spec-v1"
RUN_GROUP_PLAN_SCHEMA_VERSION = "octagon-run-group-plan-v1"
MATRIX_CELL_SCHEMA_VERSION = "octagon-matrix-cell-v1"
LEADER_CONFIG_SCHEMA_VERSION = "octagon-leader-config-v1"


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AgentModel(FrozenModel):
    agent: str = Field(min_length=1)
    model: str | None = None


class VariantSpec(FrozenModel):
    schema_version: Literal["octagon-variant-spec-v1"]
    mutator_id: str = Field(min_length=1, alias="mutator")
    mutator_version: str = Field(min_length=1, alias="version")
    seed: int
    intensity: str = Field(default="default", min_length=1)
    params: dict[str, Any] = Field(default_factory=dict, repr=False)

    model_config = ConfigDict(
        frozen=True, extra="forbid", populate_by_name=True
    )


class LeaderConfig(FrozenModel):
    schema_version: Literal["octagon-leader-config-v1"]
    scope: Literal["variant-repeat"]
    metric: Literal["task_score"]
    tie_break: Literal["duration", "candidate_id"]


class ProtocolLimits(FrozenModel):
    max_cells: int = Field(ge=1, le=1000)
    max_attempts: int | None = Field(default=None, ge=1, le=32000)


class ExperimentProtocol(FrozenModel):
    schema_version: Literal["octagon-experiment-protocol-v1"]
    compare_mode: Literal["multi-agent", "same-model", "multi-model"]
    profile: dict[str, Any] | None = Field(default=None, repr=False)
    agents: tuple[AgentModel, ...] = Field(min_length=1)
    variant_specs: tuple[VariantSpec, ...] = Field(min_length=1)
    repeats: int = Field(ge=1, le=1000)
    execution: Literal["serial", "parallel"]
    # Reserved in P0; the deployment-level lease remains authoritative.
    max_concurrency: int | None = Field(default=None, ge=1, le=32)
    timeout_seconds: int | None = Field(default=None, gt=0)
    # timeout_seconds 始终是平台侧墙钟上限；默认不把该机制透露给模型，
    # 从而模拟真实用户的耐心，而不是显式限时任务。
    notify_model_of_timeout: bool = False
    capture_policy: CapturePolicy
    leader: LeaderConfig
    # Requests may omit this server-owned field. ProtocolNormalizer always
    # replaces it with a sanitized catalog snapshot before hashing/persistence.
    catalog_snapshot: dict[str, Any] = Field(default_factory=dict, repr=False)
    limits: ProtocolLimits

    @model_validator(mode="after")
    def _validate_matrix(self) -> "ExperimentProtocol":
        secret_scan = self.model_dump(mode="python", by_alias=True)
        # catalog_snapshot is server-owned and ProtocolNormalizer always replaces
        # untrusted client input. Ignore it here so malicious/stale client fields
        # cannot alter validation behavior; the rebuilt projection is covered by
        # CatalogSnapshotBuilder's explicit allowlist.
        secret_scan.pop("catalog_snapshot", None)
        assert_secret_free(secret_scan, label="experiment protocol")
        candidates = [(item.agent, item.model) for item in self.agents]
        if len(candidates) != len(set(candidates)):
            raise ValueError("protocol agent/model candidates must be unique")
        variant_keys = [
            (
                spec.mutator_id,
                spec.mutator_version,
                spec.seed,
                canonical_hash(spec.params),
            )
            for spec in self.variant_specs
        ]
        if len(variant_keys) != len(set(variant_keys)):
            raise ValueError("protocol variant_specs must be unique")
        baseline_count = sum(
            spec.mutator_id == "baseline" for spec in self.variant_specs
        )
        if baseline_count != 1:
            raise ValueError("protocol requires exactly one baseline variant")
        cells = len(self.variant_specs) * self.repeats
        if cells > self.limits.max_cells:
            raise ValueError(
                f"matrix has {cells} cells, exceeds max_cells={self.limits.max_cells}"
            )
        attempts = cells * len(self.agents)
        if self.limits.max_attempts is not None and attempts > self.limits.max_attempts:
            raise ValueError(
                f"matrix has {attempts} attempts, exceeds "
                f"max_attempts={self.limits.max_attempts}"
            )
        if self.compare_mode == "same-model" and len(self.agents) < 2:
            raise ValueError("same-model protocol requires at least two agents")
        if self.compare_mode != "multi-model":
            agent_names = [item.agent for item in self.agents]
            if len(agent_names) != len(set(agent_names)):
                raise ValueError("protocol agents must be unique")
        if self.compare_mode == "multi-model":
            agent_names = {item.agent for item in self.agents}
            if len(self.agents) < 2 or len(agent_names) != 1:
                raise ValueError(
                    "multi-model protocol requires one agent and at least two models"
                )
            if any(item.model is None for item in self.agents):
                raise ValueError("multi-model protocol requires explicit models")
        return self

    @property
    def cell_count(self) -> int:
        return len(self.variant_specs) * self.repeats

    @property
    def attempt_count(self) -> int:
        return self.cell_count * len(self.agents)


CellStatus = Literal[
    "queued", "provisioning", "running", "completed", "partial", "failed", "cancelled"
]
TERMINAL_CELL_STATUSES = frozenset({"completed", "partial", "failed", "cancelled"})


class MatrixCell(FrozenModel):
    schema_version: Literal["octagon-matrix-cell-v1"]
    id: str = Field(pattern=r"^cell_[0-9a-f]{20}$")
    variant_id: str = Field(pattern=r"^var_[0-9a-f]{20}$")
    repeat_index: int = Field(ge=0)
    status: CellStatus = "queued"
    run_id: str | None = None
    error_code: str | None = None


class RunGroupPlan(FrozenModel):
    schema_version: Literal["octagon-run-group-plan-v1"]
    strategy: Literal["full-matrix", "retry", "forensic"]
    cells: tuple[MatrixCell, ...] = Field(min_length=1)
    stop_policy: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _unique_cells(self) -> "RunGroupPlan":
        ids = [cell.id for cell in self.cells]
        coordinates = [(cell.variant_id, cell.repeat_index) for cell in self.cells]
        if len(ids) != len(set(ids)):
            raise ValueError("run group plan contains duplicate cell IDs")
        if len(coordinates) != len(set(coordinates)):
            raise ValueError("run group plan contains duplicate variant/repeat cells")
        return self
