"""Map accepted Process findings to Product rubric opportunities.

Design constraints (self-evolving-rubric-design.md):

- Process findings never mutate official Product scores.
- Association hypotheses stay correlational.
- Tool preference / Skill path / PocketBase usage is not a Product deduction.
- Infrastructure recovery is a confounder, not a candidate capability gap.
- Only Critic-accepted claims may seed a Product opportunity.
- The output is an Opportunity for the Product loop, not a published rubric.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.experiments.hashing import canonical_hash

from .active_loop import RubricGap, mine_gap_from_claim
from .loop_models import AssociationHypothesisSpec

SCHEMA_MAPPING = "octagon-process-product-mapping-v1"
SCHEMA_OPPORTUNITY = "octagon-product-evolution-opportunity-v1"
SUPPORTED_ENVS = frozenset({
    "user-programming-scenario-document-review-formatting",
    "user-programming-scenario-document-review-formatting-multiturn",
})

# Process claim slugs that may justify a Product measurement gap.
_PRODUCT_FALSE_POSITIVE_SLUGS = (
    "validation_false_positive",
    "shallow_completion_basis",
    "completion_basis",
    "claimed_validation_not_run",
    "ui_basis",
    "validation_basis_text_visibility",
    "hardcoded_core",
    "demo_shell_over_engine",
    "task_understanding_shell",
    "static_shell",
    "completion_unfinished",
)
# Process findings that explain behavior but must not become Product deductions.
_PROCESS_ONLY_SLUGS = (
    "error_recovery",
    "infrastructure_recovery",
    "infra_pivot",
    "infra_shell",
    "subagent",
    "persistence_proxy",
    "context_first",
    "setup_reading",
    "single_attempt",
    "single_trace",
    "no_second_trajectory",
)

_SHELL_DIMENSIONS = (
    "requirement_alignment",
    "document_workflow_completeness",
    "ui_interaction_quality",
)


class ProcessProductLink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    process_claim_id: str
    product_dimension: str
    product_value: int | float | None
    mapping_kind: Literal[
        "false_positive",
        "missing_coverage",
        "confounder_only",
        "insufficient_alignment",
    ]
    reason: str
    evidence_refs: list[str] = Field(min_length=1, max_length=50)


class ProductEvolutionOpportunity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["octagon-product-evolution-opportunity-v1"] = SCHEMA_OPPORTUNITY
    opportunity_id: str
    env_name: str
    attempt_ids: list[str]
    gap_type: Literal["false_positive", "missing_coverage"]
    affected_capability: str
    claim: str
    evidence_refs: list[str]
    source_process_claim_ids: list[str]
    recommended_next_step: str
    official_scores_mutated: bool = False


class ProcessProductMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["octagon-process-product-mapping-v1"] = SCHEMA_MAPPING
    env_name: str
    case_ids: list[str]
    links: list[ProcessProductLink]
    hypotheses: list[AssociationHypothesisSpec]
    opportunities: list[ProductEvolutionOpportunity]
    skipped_claim_ids: list[str]
    limitations: list[str]

    @model_validator(mode="after")
    def validate_safety(self) -> "ProcessProductMapping":
        if any(item.official_scores_mutated for item in self.opportunities):
            raise ValueError("mapping must not mutate official scores")
        if any(item.causal_status != "correlational" for item in self.hypotheses):
            raise ValueError("association hypotheses must stay correlational")
        return self


def _slug(claim_id: str) -> str:
    return str(claim_id).split(".", 1)[-1].lower()


def _is_process_only(claim_id: str, text: str) -> bool:
    slug = _slug(claim_id)
    blob = f"{slug} {text}".lower()
    if any(token in slug for token in _PROCESS_ONLY_SLUGS):
        return True
    preference = (
        "pocketbase", "start-app", "pnpm", "vite", "skill path",
        "工具名", "工具偏好",
    )
    if any(token in blob for token in preference) and not any(
        token in slug for token in _PRODUCT_FALSE_POSITIVE_SLUGS
    ):
        return True
    return False


def _is_product_signal(claim_id: str, text: str) -> bool:
    slug = _slug(claim_id)
    if any(token in slug for token in _PRODUCT_FALSE_POSITIVE_SLUGS):
        return True
    blob = text.lower()
    return any(token in blob for token in (
        "硬编码", "假阳性", "固定通过", "演示数据", "ui 壳", "界面壳",
        "没有解析", "未真实解析", "完成依据",
    ))


def _dimension_value(scores: list[dict[str, Any]], name: str) -> int | float | None:
    for row in scores:
        if str(row.get("dimension")) == name:
            try:
                return float(row["value"])
            except (KeyError, TypeError, ValueError):
                return None
    return None


def _claim_map(analysis: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item["id"]): item
        for item in (analysis.get("claims") or [])
        if isinstance(item, dict) and item.get("id")
    }


def map_process_to_product(
    *,
    env_name: str,
    cases: list[dict[str, Any]],
) -> ProcessProductMapping:
    """Deterministic mapping. ``cases`` are aligned Product/Process records."""
    if env_name not in SUPPORTED_ENVS:
        return ProcessProductMapping(
            env_name=env_name,
            case_ids=[str(item.get("attempt_id") or "") for item in cases],
            links=[],
            hypotheses=[],
            opportunities=[],
            skipped_claim_ids=[],
            limitations=[
                f"mapping strategy is env-specific; {env_name} is not in SUPPORTED_ENVS",
                "does not rewrite official scores",
            ],
        )
    links: list[ProcessProductLink] = []
    skipped: list[str] = []
    opportunity_groups: dict[str, list[ProcessProductLink]] = {}
    supporting: list[str] = []
    contradicting: list[str] = []

    for case in cases:
        attempt_id = str(case.get("attempt_id") or "")
        product = case.get("product_outcome") or {}
        findings = case.get("process_findings") or {}
        accepted = set(findings.get("accepted_claim_ids") or [])
        claims = _claim_map({"claims": findings.get("claims") or []})
        scores = list(product.get("scores") or [])
        alignment = _dimension_value(scores, "requirement_alignment")
        workflow = _dimension_value(scores, "document_workflow_completeness")
        high_looking = any(
            value is not None and value >= 40
            for value in (alignment, workflow)
        )
        for claim_id, claim in claims.items():
            text = str(claim.get("text") or "")
            refs = [str(item) for item in (claim.get("evidence_refs") or []) if str(item).startswith("octagon://")]
            if claim_id not in accepted:
                skipped.append(claim_id)
                continue
            if not refs:
                skipped.append(claim_id)
                continue
            if _is_process_only(claim_id, text):
                links.append(ProcessProductLink(
                    process_claim_id=claim_id,
                    product_dimension="none",
                    product_value=None,
                    mapping_kind="confounder_only",
                    reason="process-only finding; tool/infra preference is not a Product deduction",
                    evidence_refs=refs[:10],
                ))
                continue
            if not _is_product_signal(claim_id, text):
                links.append(ProcessProductLink(
                    process_claim_id=claim_id,
                    product_dimension="none",
                    product_value=None,
                    mapping_kind="insufficient_alignment",
                    reason="accepted process claim does not name a Product measurement error",
                    evidence_refs=refs[:10],
                ))
                continue
            dimension = "requirement_alignment"
            value = alignment
            if workflow is not None and (alignment is None or workflow >= (alignment or 0)):
                dimension = "document_workflow_completeness"
                value = workflow
            kind: Literal["false_positive", "missing_coverage"] = (
                "false_positive" if high_looking else "missing_coverage"
            )
            link = ProcessProductLink(
                process_claim_id=claim_id,
                product_dimension=dimension,
                product_value=value,
                mapping_kind=kind,
                reason=(
                    f"accepted process claim {claim_id} explains a Product "
                    f"{kind} on {dimension}={value}"
                ),
                evidence_refs=refs[:10],
            )
            links.append(link)
            opportunity_groups.setdefault(kind, []).append(link)
            supporting.append(attempt_id)

        if alignment is not None and alignment < 40 and workflow is not None and workflow < 40:
            contradicting.append(attempt_id)

    hypotheses: list[AssociationHypothesisSpec] = []
    product_links = [
        item for item in links
        if item.mapping_kind in {"false_positive", "missing_coverage"}
    ]
    if product_links and supporting:
        payload = {
            "env_name": env_name,
            "kind": "shell_completion_basis",
            "attempts": sorted(set(supporting)),
        }
        hypotheses.append(AssociationHypothesisSpec(
            hypothesis_id="ahyp_" + canonical_hash(payload).split(":")[-1][:16],
            behavior_pattern=(
                "Agent treats UI/PocketBase shell plus hardcoded or click-through "
                "status text as the completion basis"
            ),
            associated_product_outcome=(
                "Product dimensions that count domain-looking coverage stay mid/high "
                "while core document operations are absent"
            ),
            supporting_case_ids=sorted(set(supporting)),
            contradicting_case_ids=sorted(set(contradicting) - set(supporting)),
            confounders=[
                "offline dependency failure",
                "start-app / PocketBase skill path",
                "single-attempt compare_mode metadata",
                "source-only vs full judge protocol",
            ],
            applicability={
                "env_name": env_name,
                "requires_accepted_process_claims": True,
            },
            causal_status="correlational",
            falsification_test=(
                "A candidate that still uses PocketBase/start-app but actually "
                "parses Word/Excel/PDF and lets rules change results must not "
                "be labelled a Product false positive"
            ),
            proposed_intervention=(
                "Add a Product check that core OBJ-DIFF/FORMAT/DATA must pass "
                "on real files; do not deduct for PocketBase itself"
            ),
        ))

    opportunities: list[ProductEvolutionOpportunity] = []
    for kind, group in opportunity_groups.items():
        refs = []
        claim_ids = []
        for item in group:
            refs.extend(item.evidence_refs)
            claim_ids.append(item.process_claim_id)
        uniq_refs = list(dict.fromkeys(refs))
        payload = {"env": env_name, "kind": kind, "claims": claim_ids}
        attempt_ids = sorted(set(supporting))
        opportunities.append(ProductEvolutionOpportunity(
            opportunity_id="opp_" + canonical_hash(payload).split(":")[-1][:16],
            env_name=env_name,
            attempt_ids=attempt_ids,
            gap_type=kind,  # type: ignore[arg-type]
            affected_capability="core_document_review",
            claim=(
                "Accepted Process findings show the Product rubric awarded "
                "domain-looking coverage while the completion basis was a UI "
                "shell or hardcoded status text."
            ),
            evidence_refs=uniq_refs[:20],
            source_process_claim_ids=sorted(set(claim_ids)),
            recommended_next_step=(
                "feed this opportunity into the Product active loop as a "
                "restricted add_check; do not publish automatically"
            ),
            official_scores_mutated=False,
        ))

    return ProcessProductMapping(
        env_name=env_name,
        case_ids=[str(item.get("attempt_id") or "") for item in cases],
        links=links,
        hypotheses=hypotheses,
        opportunities=opportunities,
        skipped_claim_ids=sorted(set(skipped)),
        limitations=[
            "mapping is correlational",
            "does not rewrite official scores",
            "does not treat Skill/tool preference as a Product fail",
            "single-env historical sample cannot prove causation",
        ],
    )


def cases_from_reviewed_analysis(
    *,
    snapshot: dict[str, Any],
    reviewed: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build aligned cases from frozen analysis artifacts. No DB write."""
    accepted = set(reviewed.get("accepted_claim_ids") or [])
    analyses = {
        str((item.get("attempt_id") or "")): item
        for item in (reviewed.get("attempt_analyses") or [])
        if isinstance(item, dict)
    }
    cases = []
    for attempt in snapshot.get("attempts") or []:
        metadata = attempt.get("metadata") or {}
        attempt_id = str(metadata.get("id") or "")
        analysis = analyses.get(attempt_id) or {"claims": []}
        claims = list(analysis.get("claims") or [])
        claim_ids = {str(item.get("id")) for item in claims if isinstance(item, dict)}
        cases.append({
            "attempt_id": attempt_id,
            "env_name": (snapshot.get("run") or {}).get("env_name"),
            "product_outcome": {
                "score_total": metadata.get("score_total"),
                "scores": attempt.get("scores") or [],
                "status": metadata.get("status"),
                "execution_status": metadata.get("execution_status"),
            },
            "process_findings": {
                "claims": claims,
                "accepted_claim_ids": sorted(claim_ids & accepted),
            },
            "controls": {
                "agent_name": metadata.get("agent_name"),
                "model": metadata.get("model"),
            },
        })
    return cases


def opportunities_to_gaps(
    mapping: ProcessProductMapping,
    *,
    rubric_version: str,
) -> list[RubricGap]:
    """Compile Product Gaps from mapping opportunities. Still not a publish."""
    gaps: list[RubricGap] = []
    for item in mapping.opportunities:
        gaps.append(mine_gap_from_claim(
            env_name=item.env_name,
            rubric_version=rubric_version,
            claim={
                "id": item.opportunity_id,
                "text": item.claim,
                "confidence": 0.8,
                "evidence_refs": item.evidence_refs,
            },
            owner_hypothesis="evaluator",
            gap_type=("false_positive" if item.gap_type == "false_positive" else "false_negative"),
            affected_capability=item.affected_capability,
            recommended_next_step=item.recommended_next_step,
        ))
    return gaps


def persist_mapping(path: Path, mapping: ProcessProductMapping) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(mapping.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path
