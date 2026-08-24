"""Value objects passed across the mutator boundary."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class VariantSpec(FrozenModel):
    mutator_id: str = Field(min_length=1)
    mutator_version: str = Field(min_length=1)
    seed: int
    intensity: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict, repr=False)


class ProtectedSpan(FrozenModel):
    field_path: str = Field(pattern=r"^/")
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    kind: Literal["path", "url", "json", "code_ident", "exact_output", "canary"]
    content_hash: str

    @model_validator(mode="after")
    def _ordered(self) -> "ProtectedSpan":
        if self.end <= self.start:
            raise ValueError("protected span end must be greater than start")
        return self


class MutationInput(FrozenModel):
    source_task_id: str | None
    prompt: str = Field(repr=False)
    context: dict[str, Any] = Field(default_factory=dict, repr=False)
    constraints: dict[str, Any] = Field(default_factory=dict, repr=False)
    modalities: tuple[str, ...] = ("text",)
    source_hash: str
    protected_spans: tuple[ProtectedSpan, ...] = ()
    mutable_regions: tuple[str, ...] = ()


class Applicability(FrozenModel):
    status: Literal["supported", "unsupported", "warning"]
    reason: str | None = None
    warnings: tuple[str, ...] = ()


class VariantResult(FrozenModel):
    prompt: str = Field(repr=False)
    context_delta: dict[str, Any] = Field(default_factory=dict, repr=False)
    content_hash: str
    summary: dict[str, Any] = Field(default_factory=dict)
    warnings: tuple[str, ...] = ()
