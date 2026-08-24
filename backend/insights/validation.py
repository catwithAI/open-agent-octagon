"""Insight report schema and evidence-provenance normalization."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


SECTION_NAMES = (
    "consensus",
    "divergence",
    "first_deviation",
    "recovery",
    "suggested_probes",
)


class BuilderDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)
    rationale: str | None = Field(default=None, max_length=4000)
    protocol_patch: dict[str, Any] = Field(default_factory=dict)


class InsightStatement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,120}$")
    kind: Literal["evidence-backed", "inference", "unavailable"]
    text: str = Field(min_length=1, max_length=12000)
    anchors: list[str] = Field(default_factory=list, max_length=50)
    builder_draft: BuilderDraft | None = None


class InsightSections(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consensus: list[InsightStatement] = Field(default_factory=list)
    divergence: list[InsightStatement] = Field(default_factory=list)
    first_deviation: list[InsightStatement] = Field(default_factory=list)
    recovery: list[InsightStatement] = Field(default_factory=list)
    suggested_probes: list[InsightStatement] = Field(default_factory=list)


class InsightReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["octagon-insight-v1"]
    sections: InsightSections
    limitations: list[str] = Field(default_factory=list, max_length=200)
    validation_events: list[dict[str, Any]] = Field(default_factory=list)


def validate_report(raw: dict[str, Any], bundle: dict[str, Any]) -> InsightReport:
    """Validate shape and downgrade unverifiable claims statement-by-statement."""

    candidate = dict(raw)
    candidate.setdefault("validation_events", [])
    report = InsightReport.model_validate(candidate)
    available = {
        uri
        for uri, resolution in bundle.get("anchors", {}).items()
        if isinstance(resolution, dict)
        and resolution.get("status") in {"resolved", "redacted"}
    }
    seen_ids: set[str] = set()
    events = list(report.validation_events)
    limitations = list(report.limitations)
    for section_name in SECTION_NAMES:
        statements = getattr(report.sections, section_name)
        for statement in statements:
            if statement.id in seen_ids:
                raise ValueError(f"duplicate insight statement id: {statement.id}")
            seen_ids.add(statement.id)
            valid = [anchor for anchor in statement.anchors if anchor in available]
            invalid = sorted(set(statement.anchors) - set(valid))
            if invalid:
                events.append(
                    {
                        "statement_id": statement.id,
                        "event": "invalid_anchors_removed",
                        "count": len(invalid),
                    }
                )
            statement.anchors = valid
            if statement.kind == "evidence-backed" and not valid:
                statement.kind = "inference"
                events.append(
                    {
                        "statement_id": statement.id,
                        "event": "downgraded_to_inference",
                        "reason": "no_resolvable_bundle_anchor",
                    }
                )
                limitation = (
                    f"statement {statement.id} was downgraded because its evidence "
                    "anchor could not be verified"
                )
                if limitation not in limitations:
                    limitations.append(limitation)
            if section_name == "suggested_probes" and statement.builder_draft is None:
                raise ValueError(
                    f"suggested probe {statement.id} requires a safe builder_draft"
                )
            if section_name != "suggested_probes" and statement.builder_draft is not None:
                raise ValueError(
                    f"builder_draft is only allowed in suggested_probes: {statement.id}"
                )
    report.validation_events = events
    report.limitations = limitations
    return report
