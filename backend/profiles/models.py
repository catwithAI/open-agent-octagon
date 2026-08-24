"""Strict, secret-free Profile schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.experiments.models import LeaderConfig, VariantSpec
from backend.wire.policy import CapturePolicy
from backend.security_policy import assert_secret_free


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ProfileApplicability(FrozenModel):
    categories: tuple[str, ...] = ()
    modalities: tuple[str, ...] = ("text",)


class ProfileProtocol(FrozenModel):
    repeats: int = Field(ge=1, le=1000)
    variants: tuple[VariantSpec, ...] = Field(min_length=1)
    timeout_seconds: int = Field(gt=0, le=86400)
    execution: Literal["serial", "parallel"]
    max_concurrency: int | None = Field(default=None, ge=1, le=32)
    capture_policy: CapturePolicy
    leader: LeaderConfig

    @model_validator(mode="after")
    def one_baseline(self) -> "ProfileProtocol":
        if sum(item.mutator_id == "baseline" for item in self.variants) != 1:
            raise ValueError("profile requires exactly one baseline variant")
        return self


class ProfileLimits(FrozenModel):
    max_cells: int = Field(ge=1, le=1000)
    max_attempts: int | None = Field(default=None, ge=1, le=32000)


class ResearchProfile(FrozenModel):
    schema_version: Literal["octagon-profile-v1"]
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    version: str = Field(pattern=r"^[0-9]+(?:\.[0-9]+){0,2}$")
    label: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=2000)
    applies_to: ProfileApplicability
    protocol: ProfileProtocol
    limits: ProfileLimits
    provider_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def bounded_matrix(self) -> "ResearchProfile":
        cells = len(self.protocol.variants) * self.protocol.repeats
        if cells > self.limits.max_cells:
            raise ValueError("profile matrix exceeds its own max_cells")
        assert_secret_free(self.model_dump(mode="python"), label="profile")
        if self.id == "forensic" and self.limits.max_cells > 24:
            raise ValueError("forensic profile must remain limited to at most 24 cells")
        return self
