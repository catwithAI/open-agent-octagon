from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,120}$")
    text: str = Field(min_length=1, max_length=12000)
    confidence: float = Field(ge=0, le=1)
    evidence_refs: list[str] = Field(default_factory=list, max_length=50)
    contradicting_refs: list[str] = Field(default_factory=list, max_length=50)
    alternative_explanations: list[str] = Field(default_factory=list, max_length=20)
    limitations: list[str] = Field(default_factory=list, max_length=20)


class AttemptAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["octagon-attempt-analysis-v1"]
    attempt_id: str
    summary: str = Field(min_length=1, max_length=20000)
    claims: list[Claim] = Field(default_factory=list, max_length=100)


class ComparisonAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["octagon-cross-agent-analysis-v1"]
    run_id: str
    summary: str = Field(min_length=1, max_length=20000)
    claims: list[Claim] = Field(default_factory=list, max_length=150)
    recommended_next_steps: list[str] = Field(default_factory=list, max_length=30)


class RejectedClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: str
    reason: str = Field(min_length=1, max_length=4000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=50)


class Critique(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["octagon-evidence-critique-v1"]
    accepted_claim_ids: list[str] = Field(default_factory=list)
    rejected_claims: list[RejectedClaim] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list, max_length=100)
