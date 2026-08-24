"""Normalized Output schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ProtectedSpanRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    field_path: str
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    kind: Literal["number", "code", "command", "refusal", "tool_params"]
    content_hash: str


class NormalizedDocument(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["octagon-normalized-output-v1"]
    attempt_id: str
    source_hash: str
    pipeline_hash: str
    content: Any
    transform_manifest: dict[str, Any]
    protected_spans: tuple[ProtectedSpanRecord, ...]
    annotations: tuple[dict[str, Any], ...] = ()
