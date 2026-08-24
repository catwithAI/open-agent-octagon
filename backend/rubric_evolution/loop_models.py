"""Contracts for the Process and Association evolution directions."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

EvolutionDecision = Literal["candidate_generated", "no_change", "insufficient_evidence"]


class ProcessDimensionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimension_id: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=1, max_length=4000)
    required_evidence: list[str] = Field(default_factory=list, max_length=50)
    output_fields: list[str] = Field(default_factory=list, max_length=50)
    unknown_when: str = Field(min_length=1, max_length=4000)
    attribution_guardrails: list[str] = Field(default_factory=list, max_length=50)


class ProcessRubricCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["octagon-process-rubric-v1"]
    evolution_domain: Literal["process"] = "process"
    scope_key: str = Field(min_length=1, max_length=300)
    parent_version: str = Field(min_length=1, max_length=300)
    proposed_version: str = Field(min_length=1, max_length=300)
    dimensions: list[ProcessDimensionSpec] = Field(min_length=1, max_length=100)
    global_constraints: list[str] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_contract(self) -> "ProcessRubricCandidate":
        ids = [item.dimension_id for item in self.dimensions]
        if len(ids) != len(set(ids)):
            raise ValueError("Process dimension_id values must be unique")
        if self.parent_version == self.proposed_version:
            raise ValueError("Process proposed_version must differ from parent_version")
        required = {
            "claims must cite frozen evidence anchors",
            "insufficient evidence must remain explicit",
            "process findings never directly mutate product scores or rewards",
        }
        normalized = {item.lower() for item in self.global_constraints}
        if not all(any(fragment in item for item in normalized) for fragment in required):
            raise ValueError("Process candidate removed a mandatory safety invariant")
        return self


class ProcessEvolutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["octagon-process-evolution-result-v1"]
    result: EvolutionDecision
    summary: str = Field(min_length=1, max_length=20000)
    evidence_record_ids: list[str] = Field(default_factory=list, max_length=1000)
    limitations: list[str] = Field(default_factory=list, max_length=100)
    candidate: ProcessRubricCandidate | None = None

    @model_validator(mode="after")
    def validate_result(self) -> "ProcessEvolutionResult":
        if self.result == "candidate_generated" and self.candidate is None:
            raise ValueError("candidate_generated requires a Process candidate")
        if self.result != "candidate_generated" and self.candidate is not None:
            raise ValueError(f"{self.result} must not include a Process candidate")
        return self


class AssociationHypothesisSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hypothesis_id: str = Field(min_length=1, max_length=200)
    behavior_pattern: str = Field(min_length=1, max_length=8000)
    associated_product_outcome: str = Field(min_length=1, max_length=8000)
    supporting_case_ids: list[str] = Field(min_length=1, max_length=1000)
    contradicting_case_ids: list[str] = Field(default_factory=list, max_length=1000)
    confounders: list[str] = Field(min_length=1, max_length=100)
    applicability: dict[str, Any] = Field(default_factory=dict)
    causal_status: Literal["correlational"] = "correlational"
    falsification_test: str = Field(min_length=1, max_length=8000)
    proposed_intervention: str | None = Field(default=None, max_length=8000)


class AssociationEvolutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["octagon-association-evolution-result-v1"]
    result: EvolutionDecision
    summary: str = Field(min_length=1, max_length=20000)
    hypotheses: list[AssociationHypothesisSpec] = Field(default_factory=list, max_length=100)
    limitations: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_result(self) -> "AssociationEvolutionResult":
        if self.result == "candidate_generated" and not self.hypotheses:
            raise ValueError("candidate_generated requires Association hypotheses")
        if self.result != "candidate_generated" and self.hypotheses:
            raise ValueError(f"{self.result} must not include hypotheses")
        return self
