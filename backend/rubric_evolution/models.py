from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

JudgeResult = Literal["pass", "partial", "fail", "not_applicable", "unknown"]
UnknownReason = Literal[
    "evidence_missing",
    "conflicting_evidence",
    "rubric_ambiguous",
    "judge_execution_invalid",
    "unsupported_edge_case",
    "infrastructure_failure",
    "other",
]
EvolutionScope = Literal["environment", "cross_environment"]
EvolutionDecision = Literal[
    "candidate_generated", "no_change", "insufficient_evidence"
]


class JudgeCheckRecord(BaseModel):
    """One check result emitted by an ordinary judge execution.

    ``unknown`` is a normal Rubric result, not a synonym for failure.  It has no
    awarded score, but its maximum weight remains visible for the score that
    includes unknown checks in the denominator.
    """

    model_config = ConfigDict(extra="forbid")

    check_id: str = Field(min_length=1, max_length=200)
    result: JudgeResult
    awarded: float | None = Field(default=None, ge=0)
    maximum: float = Field(gt=0)
    evidence_refs: list[str] = Field(default_factory=list, max_length=100)
    unknown_reason: UnknownReason | None = None
    detail: str = Field(default="", max_length=12000)

    @model_validator(mode="after")
    def validate_result_score(self) -> "JudgeCheckRecord":
        if self.result == "unknown":
            if self.awarded is not None:
                raise ValueError("unknown check must not carry an awarded score")
            if self.unknown_reason is None:
                raise ValueError("unknown check requires unknown_reason")
        else:
            if self.unknown_reason is not None:
                raise ValueError("unknown_reason is only valid for unknown checks")
            if self.result == "not_applicable":
                if self.awarded is not None:
                    raise ValueError("not_applicable check must not carry an awarded score")
            elif self.awarded is None:
                raise ValueError(f"{self.result} check requires an awarded score")
        if self.awarded is not None and self.awarded > self.maximum:
            raise ValueError("awarded score cannot exceed maximum")
        return self


class JudgeRecord(BaseModel):
    """A complete judge execution for one Attempt.

    Retries intentionally remain separate records and all count toward batch
    thresholds, even when they share an ``attempt_id``.
    """

    model_config = ConfigDict(extra="forbid")

    record_id: str = Field(min_length=1, max_length=200)
    env_name: str = Field(min_length=1, max_length=300)
    rubric_version: str = Field(min_length=1, max_length=300)
    run_id: str = Field(min_length=1, max_length=200)
    attempt_id: str = Field(min_length=1, max_length=200)
    judge_execution_id: str = Field(min_length=1, max_length=200)
    created_at: str
    valid_execution: bool = True
    checks: list[JudgeCheckRecord] = Field(default_factory=list, max_length=1000)
    execution_error: str | None = Field(default=None, max_length=4000)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_execution(self) -> "JudgeRecord":
        if self.valid_execution and not self.checks:
            raise ValueError("valid judge execution requires at least one check")
        if not self.valid_execution and not self.execution_error:
            raise ValueError("invalid judge execution requires execution_error")
        return self


class EvolutionBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["octagon-rubric-evolution-batch-v1"]
    batch_id: str
    scope: EvolutionScope
    env_name: str | None = None
    rubric_versions: list[str]
    threshold: int
    trigger_record_count: int
    overlap_record_count: int
    unique_attempt_count: int
    unknown_record_count: int
    records: list[JudgeRecord]
    input_hash: str
    frozen_at: str

    @model_validator(mode="after")
    def validate_scope(self) -> "EvolutionBatch":
        if self.scope == "environment" and not self.env_name:
            raise ValueError("environment batch requires env_name")
        if self.trigger_record_count < self.threshold:
            raise ValueError("ready batch must meet its trigger threshold")
        if self.overlap_record_count > len(self.records):
            raise ValueError("overlap_record_count exceeds records")
        return self


class EvolutionBatchPreparation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ready", "waiting"]
    scope: EvolutionScope
    threshold: int
    valid_new_record_count: int
    excluded_invalid_record_count: int
    batch: EvolutionBatch | None = None


class RubricCriteria(BaseModel):
    """Executable result contract. Unknown is mandatory for every check."""

    model_config = ConfigDict(extra="forbid")

    pass_: str = Field(alias="pass", min_length=1, max_length=12000)
    partial: str = Field(min_length=1, max_length=12000)
    fail: str = Field(min_length=1, max_length=12000)
    not_applicable: str = Field(min_length=1, max_length=12000)
    unknown: str = Field(min_length=1, max_length=12000)


class RubricCheckSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=1000)
    description: str = Field(min_length=1, max_length=12000)
    weight: float = Field(gt=0)
    evidence_requirements: list[str] = Field(default_factory=list, max_length=100)
    criteria: RubricCriteria


class CandidateRubric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["octagon-evolved-rubric-v1"]
    evolution_domain: Literal["product"] = "product"
    rubric_id: str = Field(min_length=1, max_length=300)
    parent_version: str = Field(min_length=1, max_length=300)
    proposed_version: str = Field(min_length=1, max_length=300)
    scope: EvolutionScope
    env_name: str | None = None
    checks: list[RubricCheckSpec] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_unique_checks(self) -> "CandidateRubric":
        ids = [item.check_id for item in self.checks]
        if len(ids) != len(set(ids)):
            raise ValueError("candidate rubric check_id values must be unique")
        if self.scope == "environment" and not self.env_name:
            raise ValueError("environment candidate rubric requires env_name")
        return self


class RubricDiagnosis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    diagnosis_id: str = Field(min_length=1, max_length=200)
    problem_type: Literal[
        "missing_coverage",
        "ambiguous_specification",
        "distortion",
        "overlap",
        "unknown",
    ]
    summary: str = Field(min_length=1, max_length=12000)
    current_rubric_refs: list[str] = Field(default_factory=list, max_length=100)
    evidence_refs: list[str] = Field(default_factory=list, max_length=200)
    limitations: list[str] = Field(default_factory=list, max_length=100)


class RubricChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    change_id: str = Field(min_length=1, max_length=200)
    change_type: Literal[
        "add_check", "clarify_check", "remove_check", "change_weight", "other"
    ]
    target_check_id: str | None = Field(default=None, max_length=200)
    summary: str = Field(min_length=1, max_length=12000)
    rationale: str = Field(min_length=1, max_length=12000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=200)


class RubricEvolutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["octagon-rubric-evolution-result-v1"]
    result: EvolutionDecision
    summary: str = Field(min_length=1, max_length=20000)
    diagnoses: list[RubricDiagnosis] = Field(default_factory=list, max_length=200)
    changes: list[RubricChange] = Field(default_factory=list, max_length=200)
    candidate_rubric: CandidateRubric | None = None

    @model_validator(mode="after")
    def validate_decision(self) -> "RubricEvolutionResult":
        if self.result == "candidate_generated":
            if self.candidate_rubric is None or not self.changes:
                raise ValueError("candidate_generated requires candidate_rubric and changes")
        elif self.candidate_rubric is not None or self.changes:
            raise ValueError(f"{self.result} must not include candidate rubric changes")
        return self


class RubricScoreView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score_with_unknown: float
    normalized_score_without_unknown: float | None
    resolved_points: float
    total_points_with_unknown: float
    total_points_without_unknown: float
    unknown_count: int
    unknown_weight: float
