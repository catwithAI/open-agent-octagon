"""Research feedback value objects."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FeedbackCreate(FrozenModel):
    target_type: Literal[
        "profile-recommendation", "insight", "profile", "experiment-annotation"
    ]
    target_id: str = Field(min_length=1)
    scope: dict[str, Any] = Field(default_factory=dict)
    signal: Literal["helpful", "unhelpful", "accepted", "rejected", "annotation"]
    reason: str | None = Field(default=None, max_length=200)
    rationale: str | None = Field(default=None, max_length=4000, repr=False)
    actor: str = Field(min_length=1, max_length=200)
    supersedes: str | None = None


class ResearchFeedback(FrozenModel):
    schema_version: Literal["octagon-feedback-v1"]
    id: str
    target_type: str
    target_id: str
    scope: dict[str, Any]
    signal: str
    reason: str | None
    rationale: str | None = Field(repr=False)
    actor: str
    supersedes: str | None
    created_at: str
