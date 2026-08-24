"""Deterministic active evolution loop.

Assumes a good evaluation environment already produced frozen official scores,
reviewed claims, and candidate snapshots. This module never calls a model and
never publishes an official rubric.

    reviewed claim
      → gap
      → hypothesis
      → compiled proposal (restricted patches only)
      → frozen corpus
      → gold-label compile precheck
      → hard gates on newly introduced FP/FN
      → dimension report
      → research memory
      → human decision (shadow / reject / rollback)

Passive scheduler output (`candidate_generated`) is an optional seed. The
authoritative path is this offline loop.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.db import _now_iso
from backend.experiments.hashing import canonical_hash

from .models import (
    CandidateRubric,
    JudgeCheckRecord,
    RubricChange,
    RubricCheckSpec,
    RubricCriteria,
)
from .scoring import compute_rubric_score_views

SCHEMA_GAP = "octagon-rubric-gap-v1"
SCHEMA_HYPOTHESIS = "octagon-rubric-hypothesis-v1"
SCHEMA_PROPOSAL = "octagon-rubric-proposal-v1"
SCHEMA_CORPUS = "octagon-rubric-corpus-v1"
SCHEMA_REPLAY = "octagon-rubric-offline-replay-v1"
SCHEMA_REPORT = "octagon-rubric-evolution-report-v1"
SCHEMA_MEMORY = "octagon-rubric-research-memory-v1"
SCHEMA_DECISION = "octagon-rubric-promotion-decision-v1"

OWNER_LABELS = frozenset({
    "candidate", "evaluator", "infrastructure", "protocol", "unknown",
})
SLICE_LABELS = frozenset({
    "clear_success",
    "clear_failure",
    "partial_completion",
    "gaming_attempt",
    "alternative_valid_path",
    "evidence_missing",
    "evaluator_failure",
    "repeat_run",
    "cross_agent_same_model",
    "historical_regression",
})
ALLOWED_CHANGE_TYPES = frozenset({
    "add_check", "clarify_check", "remove_check", "change_weight",
})
FORBIDDEN_CHANGE_TYPES = frozenset({"aggregation_patch", "arbitrary_code"})
TERMINAL_DECISIONS = frozenset({
    "approved_for_shadow", "rejected", "rolled_back", "needs_review",
})

PromotionState = Literal[
    "draft",
    "evidence_checked",
    "compiled",
    "replaying",
    "validation_failed",
    "needs_review",
    "approved_for_shadow",
    "rejected",
    "rolled_back",
]


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, default=str)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _short_id(prefix: str, payload: Any) -> str:
    digest = canonical_hash(payload).split(":")[-1]
    return f"{prefix}_{digest[:20]}"


def _require_refs(refs: list[str], *, field: str) -> None:
    if not refs:
        raise ValueError(f"{field} requires at least one evidence_ref")
    invalid = [item for item in refs if not str(item).startswith("octagon://")]
    if invalid:
        raise ValueError(f"{field} has non-anchor refs: {invalid[:5]}")


class RubricGap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["octagon-rubric-gap-v1"] = SCHEMA_GAP
    gap_id: str
    env_name: str
    rubric_version: str
    gap_type: Literal[
        "false_positive",
        "false_negative",
        "false_negative_attribution",
        "overlap",
        "unstable",
        "leaky",
        "ambiguous_specification",
    ]
    claim: str = Field(min_length=1, max_length=12000)
    confidence: float = Field(ge=0, le=1)
    evidence_refs: list[str] = Field(min_length=1, max_length=200)
    contradicting_refs: list[str] = Field(default_factory=list, max_length=200)
    affected_capability: str = Field(min_length=1, max_length=300)
    owner_hypothesis: Literal[
        "candidate", "evaluator", "infrastructure", "protocol", "unknown",
    ]
    alternative_explanations: list[str] = Field(default_factory=list, max_length=20)
    recommended_next_step: str = Field(min_length=1, max_length=4000)
    source_claim_ids: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def validate_refs(self) -> "RubricGap":
        _require_refs(self.evidence_refs, field="gap.evidence_refs")
        return self


class RubricHypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["octagon-rubric-hypothesis-v1"] = SCHEMA_HYPOTHESIS
    hypothesis_id: str
    gap_id: str
    claim: str = Field(min_length=1, max_length=12000)
    expected_improvement: dict[str, str] = Field(min_length=1, max_length=20)
    risk: list[str] = Field(default_factory=list, max_length=20)
    required_corpus_slices: list[str] = Field(min_length=1, max_length=20)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=20)
    evidence_refs: list[str] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_slices(self) -> "RubricHypothesis":
        unknown = sorted(set(self.required_corpus_slices) - SLICE_LABELS)
        if unknown:
            raise ValueError(f"unknown corpus slices: {unknown}")
        _require_refs(self.evidence_refs, field="hypothesis.evidence_refs")
        return self


class RestrictedPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    change_id: str
    change_type: Literal["add_check", "clarify_check", "remove_check", "change_weight"]
    target_check_id: str | None = None
    summary: str = Field(min_length=1, max_length=12000)
    rationale: str = Field(min_length=1, max_length=12000)
    evidence_refs: list[str] = Field(min_length=1, max_length=200)
    check: RubricCheckSpec | None = None
    weight: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_patch(self) -> "RestrictedPatch":
        if self.change_type == "add_check" and self.check is None:
            raise ValueError("add_check requires a check spec")
        if self.change_type in {"clarify_check", "remove_check", "change_weight"}:
            if not self.target_check_id:
                raise ValueError(f"{self.change_type} requires target_check_id")
        if self.change_type == "change_weight" and self.weight is None:
            raise ValueError("change_weight requires weight")
        _require_refs(self.evidence_refs, field="patch.evidence_refs")
        return self


class RubricProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["octagon-rubric-proposal-v1"] = SCHEMA_PROPOSAL
    proposal_id: str
    gap_id: str
    hypothesis_id: str
    parent_rubric: CandidateRubric
    candidate_rubric: CandidateRubric
    patches: list[RestrictedPatch] = Field(min_length=1, max_length=50)
    state: PromotionState = "compiled"
    created_at: str


class GoldLabel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_id: str
    expected_result: Literal["pass", "partial", "fail", "not_applicable", "unknown"]
    owner: Literal["candidate", "evaluator", "infrastructure", "protocol", "unknown"]
    awarded: float | None = Field(default=None, ge=0)
    note: str = ""


class CorpusCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    attempt_id: str
    env_name: str
    slice: str
    snapshot_hash: str
    evidence_snapshot: dict[str, Any]
    parent_checks: list[JudgeCheckRecord] = Field(min_length=1)
    gold_labels: list[GoldLabel] = Field(min_length=1)
    agent_alias: str = "anonymous"
    candidate_observed: list[JudgeCheckRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_case(self) -> "CorpusCase":
        if self.slice not in SLICE_LABELS:
            raise ValueError(f"unknown slice: {self.slice}")
        if self.agent_alias != "anonymous" and not self.agent_alias.startswith("agent-"):
            raise ValueError("corpus cases must be identity-blinded")
        return self


class FrozenCorpus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["octagon-rubric-corpus-v1"] = SCHEMA_CORPUS
    corpus_id: str
    env_name: str
    parent_version: str
    cases: list[CorpusCase] = Field(min_length=1, max_length=500)
    input_hash: str
    frozen_at: str


class GateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gate_id: str
    passed: bool
    detail: str


class CheckReplayRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    check_id: str
    slice: str
    parent_result: str
    candidate_result: str
    gold_result: str
    gold_owner: str
    parent_match: bool
    candidate_match: bool
    improved: bool
    regressed: bool


class OfflineReplayOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["octagon-rubric-offline-replay-v1"] = SCHEMA_REPLAY
    replay_id: str
    proposal_id: str
    corpus_id: str
    rows: list[CheckReplayRow]
    parent_gold_matches: int
    candidate_gold_matches: int
    new_false_positives: int
    new_false_negatives: int
    infra_as_candidate_errors: int
    input_hash: str


class EvolutionReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["octagon-rubric-evolution-report-v1"] = SCHEMA_REPORT
    report_id: str
    proposal_id: str
    summary: str
    gates: list[GateResult]
    replay: OfflineReplayOutcome
    recommend: Literal["approved_for_shadow", "rejected", "needs_review"]
    reasons: list[str]


class ResearchMemoryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["octagon-rubric-research-memory-v1"] = SCHEMA_MEMORY
    memory_id: str
    proposal_id: str
    decision: str
    actor: str
    reasons: list[str]
    evidence_refs: list[str]
    created_at: str


class PromotionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["octagon-rubric-promotion-decision-v1"] = SCHEMA_DECISION
    proposal_id: str
    from_state: PromotionState
    to_state: PromotionState
    actor: str
    reasons: list[str]
    created_at: str


def mine_gap_from_claim(
    *,
    env_name: str,
    rubric_version: str,
    claim: dict[str, Any],
    owner_hypothesis: str = "evaluator",
    gap_type: str = "false_positive",
    affected_capability: str,
    recommended_next_step: str,
) -> RubricGap:
    """Turn one Evidence-Critic-accepted claim into a Gap. No model."""
    claim_id = str(claim.get("id") or "").strip()
    text = str(claim.get("text") or "").strip()
    refs = [str(item) for item in (claim.get("evidence_refs") or []) if str(item).strip()]
    if not claim_id or not text:
        raise ValueError("accepted claim requires id and text")
    if owner_hypothesis not in OWNER_LABELS:
        raise ValueError(f"invalid owner_hypothesis: {owner_hypothesis}")
    payload = {
        "env_name": env_name,
        "rubric_version": rubric_version,
        "claim_id": claim_id,
        "text": text,
        "gap_type": gap_type,
    }
    return RubricGap(
        gap_id=_short_id("rgap", payload),
        env_name=env_name,
        rubric_version=rubric_version,
        gap_type=gap_type,  # type: ignore[arg-type]
        claim=text,
        confidence=float(claim.get("confidence") or 0.5),
        evidence_refs=refs,
        contradicting_refs=[
            str(item) for item in (claim.get("contradicting_refs") or []) if str(item).strip()
        ],
        affected_capability=affected_capability,
        owner_hypothesis=owner_hypothesis,  # type: ignore[arg-type]
        alternative_explanations=list(claim.get("alternative_explanations") or []),
        recommended_next_step=recommended_next_step,
        source_claim_ids=[claim_id],
    )


def build_hypothesis(
    *,
    gap: RubricGap,
    claim: str,
    expected_improvement: dict[str, str],
    required_corpus_slices: list[str],
    acceptance_criteria: list[str],
    risk: list[str] | None = None,
) -> RubricHypothesis:
    payload = {"gap_id": gap.gap_id, "claim": claim, "slices": required_corpus_slices}
    return RubricHypothesis(
        hypothesis_id=_short_id("rhyp", payload),
        gap_id=gap.gap_id,
        claim=claim,
        expected_improvement=expected_improvement,
        risk=list(risk or []),
        required_corpus_slices=required_corpus_slices,
        acceptance_criteria=acceptance_criteria,
        evidence_refs=list(gap.evidence_refs),
    )


def _criteria(**kwargs: str) -> RubricCriteria:
    return RubricCriteria.model_validate({
        "pass": kwargs.get("pass", "evidence supports pass"),
        "partial": kwargs.get("partial", "evidence supports partial"),
        "fail": kwargs.get("fail", "evidence supports fail"),
        "not_applicable": kwargs.get("not_applicable", "check does not apply"),
        "unknown": kwargs.get(
            "unknown",
            "evidence missing, conflicting, or infrastructure failure; never score as fail",
        ),
    })


def _index_checks(rubric: CandidateRubric) -> dict[str, RubricCheckSpec]:
    return {item.check_id: item for item in rubric.checks}


def compile_proposal(
    *,
    parent: CandidateRubric,
    gap: RubricGap,
    hypothesis: RubricHypothesis,
    patches: list[RestrictedPatch | RubricChange | dict[str, Any]],
    proposed_version: str | None = None,
) -> RubricProposal:
    """Apply restricted patches. Rejects arbitrary code and aggregation."""
    parsed: list[RestrictedPatch] = []
    for raw in patches:
        if isinstance(raw, RestrictedPatch):
            parsed.append(raw)
            continue
        if isinstance(raw, RubricChange):
            raise ValueError(
                "RubricChange is a diagnosis, not a compiler patch; "
                "pass RestrictedPatch instead"
            )
        data = dict(raw)
        if data.get("change_type") in FORBIDDEN_CHANGE_TYPES:
            raise ValueError(f"forbidden patch type: {data.get('change_type')}")
        parsed.append(RestrictedPatch.model_validate(data))
    if not parsed:
        raise ValueError("compiler requires at least one restricted patch")

    checks = list(parent.checks)
    by_id = _index_checks(parent)
    for patch in parsed:
        if patch.change_type == "add_check":
            assert patch.check is not None
            if patch.check.check_id in by_id:
                raise ValueError(f"add_check duplicates {patch.check.check_id}")
            if not patch.check.criteria.unknown.strip():
                raise ValueError("every check must define unknown")
            checks.append(patch.check)
            by_id[patch.check.check_id] = patch.check
        elif patch.change_type == "remove_check":
            checks = [item for item in checks if item.check_id != patch.target_check_id]
            by_id.pop(patch.target_check_id, None)
        elif patch.change_type == "clarify_check":
            current = by_id.get(patch.target_check_id or "")
            if current is None:
                raise ValueError(f"clarify_check target missing: {patch.target_check_id}")
            replacement = patch.check or current.model_copy(
                update={"description": f"{current.description}\n{patch.summary}"}
            )
            if replacement.check_id != current.check_id:
                raise ValueError("clarify_check cannot rename check_id")
            checks = [
                replacement if item.check_id == current.check_id else item
                for item in checks
            ]
            by_id[current.check_id] = replacement
        elif patch.change_type == "change_weight":
            current = by_id.get(patch.target_check_id or "")
            if current is None:
                raise ValueError(f"change_weight target missing: {patch.target_check_id}")
            replacement = current.model_copy(update={"weight": patch.weight})
            checks = [
                replacement if item.check_id == current.check_id else item
                for item in checks
            ]
            by_id[current.check_id] = replacement

    if not checks:
        raise ValueError("compiled rubric would have no checks")
    version = proposed_version or _short_id("rubric", {
        "parent": parent.proposed_version,
        "patches": [item.model_dump(mode="json") for item in parsed],
    })
    candidate = parent.model_copy(update={
        "parent_version": parent.proposed_version,
        "proposed_version": version,
        "checks": checks,
    })
    proposal_id = _short_id("rprop", {
        "gap": gap.gap_id,
        "hypothesis": hypothesis.hypothesis_id,
        "candidate": candidate.model_dump(mode="json", by_alias=True),
    })
    return RubricProposal(
        proposal_id=proposal_id,
        gap_id=gap.gap_id,
        hypothesis_id=hypothesis.hypothesis_id,
        parent_rubric=parent,
        candidate_rubric=candidate,
        patches=parsed,
        state="compiled",
        created_at=_now_iso(),
    )


def freeze_corpus(
    *,
    env_name: str,
    parent_version: str,
    cases: list[CorpusCase | dict[str, Any]],
    hypothesis: RubricHypothesis | None = None,
) -> FrozenCorpus:
    parsed = [
        item if isinstance(item, CorpusCase) else CorpusCase.model_validate(item)
        for item in cases
    ]
    if hypothesis is not None:
        present = {item.slice for item in parsed}
        missing = [item for item in hypothesis.required_corpus_slices if item not in present]
        if missing:
            raise ValueError(f"corpus missing required slices: {missing}")
    payload = {
        "env_name": env_name,
        "parent_version": parent_version,
        "cases": [item.model_dump(mode="json") for item in parsed],
    }
    frozen_at = _now_iso()
    input_hash = canonical_hash(payload)
    return FrozenCorpus(
        corpus_id=_short_id("rcorp", payload),
        env_name=env_name,
        parent_version=parent_version,
        cases=parsed,
        input_hash=input_hash,
        frozen_at=frozen_at,
    )


def _gold_map(case: CorpusCase) -> dict[str, GoldLabel]:
    mapping = {item.check_id: item for item in case.gold_labels}
    if len(mapping) != len(case.gold_labels):
        raise ValueError(f"duplicate gold labels in {case.case_id}")
    return mapping


def _execute_deterministic(
    rubric: CandidateRubric,
    case: CorpusCase,
    *,
    parent_by_id: dict[str, JudgeCheckRecord],
) -> list[JudgeCheckRecord]:
    """Gold precheck only. Not a scorer or ProviderRubricExecutor.

    Unchanged check ids reuse the parent result. New checks use gold labels
    unless candidate_observed injects an independent executor row.
    """
    gold = _gold_map(case)
    observed = {item.check_id: item for item in case.candidate_observed}
    produced: list[JudgeCheckRecord] = []
    for spec in rubric.checks:
        if spec.check_id in observed:
            row = observed[spec.check_id]
            produced.append(row.model_copy(update={"maximum": spec.weight}))
            continue
        if spec.check_id in parent_by_id:
            baseline = parent_by_id[spec.check_id]
            produced.append(baseline.model_copy(update={"maximum": spec.weight}))
            continue
        label = gold.get(spec.check_id)
        if label is None:
            produced.append(JudgeCheckRecord(
                check_id=spec.check_id,
                result="unknown",
                awarded=None,
                maximum=spec.weight,
                evidence_refs=[],
                unknown_reason="evidence_missing",
                detail="no gold label for new check",
            ))
            continue
        awarded = label.awarded
        if label.expected_result == "unknown":
            awarded = None
        elif awarded is None and label.expected_result == "pass":
            awarded = spec.weight
        elif awarded is None and label.expected_result == "fail":
            awarded = 0.0
        elif awarded is None and label.expected_result == "partial":
            awarded = spec.weight / 2
        elif label.expected_result == "not_applicable":
            awarded = None
        produced.append(JudgeCheckRecord(
            check_id=spec.check_id,
            result=label.expected_result,
            awarded=awarded,
            maximum=spec.weight,
            evidence_refs=list(case.evidence_snapshot.get("evidence_refs") or []),
            unknown_reason=(
                "infrastructure_failure" if label.owner == "infrastructure" and label.expected_result == "unknown"
                else ("evidence_missing" if label.expected_result == "unknown" else None)
            ),
            detail=label.note,
        ))
    return produced


def replay_parent_and_candidate(
    *,
    proposal: RubricProposal,
    corpus: FrozenCorpus,
) -> OfflineReplayOutcome:
    rows: list[CheckReplayRow] = []
    new_fp = 0
    new_fn = 0
    infra_errors = 0
    parent_matches = 0
    candidate_matches = 0
    for case in corpus.cases:
        parent_by_id = {item.check_id: item for item in case.parent_checks}
        gold = _gold_map(case)
        candidate_checks = _execute_deterministic(
            proposal.candidate_rubric, case, parent_by_id=parent_by_id,
        )
        candidate_by_id = {item.check_id: item for item in candidate_checks}
        check_ids = sorted(
            set(parent_by_id) | set(candidate_by_id) | set(gold)
        )
        for check_id in check_ids:
            parent_result = parent_by_id[check_id].result if check_id in parent_by_id else "absent"
            candidate_result = (
                candidate_by_id[check_id].result if check_id in candidate_by_id else "absent"
            )
            label = gold.get(check_id)
            gold_result = label.expected_result if label else "unlabeled"
            gold_owner = label.owner if label else "unknown"
            parent_match = bool(label) and parent_result == gold_result
            candidate_match = bool(label) and candidate_result == gold_result
            if parent_match:
                parent_matches += 1
            if candidate_match:
                candidate_matches += 1
            improved = candidate_match and not parent_match and bool(label)
            regressed = parent_match and not candidate_match and bool(label)
            # Count only errors introduced or kept by the *candidate* relative to gold.
            # Parent leftover FPs (parent pass, gold fail) are the reason we evolve;
            # they must not veto a proposal that already fails those cases.
            candidate_positive = candidate_result in {"pass", "partial"}
            parent_positive = parent_result in {"pass", "partial"}
            if (
                label
                and gold_owner == "candidate"
                and gold_result == "fail"
                and candidate_positive
                and (parent_result == "absent" or not parent_positive)
            ):
                new_fp += 1
            if (
                label
                and gold_owner == "candidate"
                and gold_result == "pass"
                and candidate_result == "fail"
                and (parent_result == "absent" or parent_positive)
            ):
                new_fn += 1
            if gold_owner == "infrastructure" and candidate_result == "fail":
                infra_errors += 1
            rows.append(CheckReplayRow(
                case_id=case.case_id,
                check_id=check_id,
                slice=case.slice,
                parent_result=str(parent_result),
                candidate_result=str(candidate_result),
                gold_result=gold_result,
                gold_owner=gold_owner,
                parent_match=parent_match,
                candidate_match=candidate_match,
                improved=improved,
                regressed=regressed,
            ))
    payload = {
        "proposal_id": proposal.proposal_id,
        "corpus_id": corpus.corpus_id,
        "rows": [item.model_dump(mode="json") for item in rows],
    }
    return OfflineReplayOutcome(
        replay_id=_short_id("rrep", payload),
        proposal_id=proposal.proposal_id,
        corpus_id=corpus.corpus_id,
        rows=rows,
        parent_gold_matches=parent_matches,
        candidate_gold_matches=candidate_matches,
        new_false_positives=new_fp,
        new_false_negatives=new_fn,
        infra_as_candidate_errors=infra_errors,
        input_hash=canonical_hash(payload),
    )


def validate_proposal(
    *,
    proposal: RubricProposal,
    corpus: FrozenCorpus,
    replay: OfflineReplayOutcome,
) -> list[GateResult]:
    gates: list[GateResult] = []

    def add(gate_id: str, passed: bool, detail: str) -> None:
        gates.append(GateResult(gate_id=gate_id, passed=passed, detail=detail))

    add(
        "schema_valid",
        True,
        "gap/hypothesis/proposal/corpus already validated by schema",
    )
    unknown_ok = all(
        spec.criteria.unknown.strip() for spec in proposal.candidate_rubric.checks
    )
    add("all_checks_support_unknown", unknown_ok, "every check defines unknown")

    leak_hits = 0
    for case in corpus.cases:
        blob = json.dumps(case.evidence_snapshot, ensure_ascii=False).lower()
        if "gold patch" in blob or "verify_issue.py" in blob or "hidden oracle" in blob:
            leak_hits += 1
    add("oracle_leak_incidents", leak_hits == 0, f"leak_hits={leak_hits}")

    identity_ok = all(case.agent_alias.startswith("agent-") or case.agent_alias == "anonymous" for case in corpus.cases)
    add("identity_blinded", identity_ok, "corpus agent aliases are blinded")

    ranking_ok = not any(
        "rank" in patch.rationale.lower() or "leaderboard" in patch.rationale.lower()
        for patch in proposal.patches
    )
    add("not_rank_optimized", ranking_ok, "patches must not cite ranking")

    add(
        "infra_not_scored_as_candidate",
        replay.infra_as_candidate_errors == 0,
        f"infra_as_candidate_errors={replay.infra_as_candidate_errors}",
    )
    add(
        "no_new_false_positives",
        replay.new_false_positives == 0,
        f"new_false_positives={replay.new_false_positives}",
    )
    add(
        "no_new_false_negatives",
        replay.new_false_negatives == 0,
        f"new_false_negatives={replay.new_false_negatives}",
    )
    add(
        "gold_match_not_worse",
        replay.candidate_gold_matches >= replay.parent_gold_matches,
        (
            f"parent={replay.parent_gold_matches} "
            f"candidate={replay.candidate_gold_matches}"
        ),
    )
    add(
        "gold_precheck_only",
        True,
        "this replay compares compiled labels to gold; it is not scorer/executor proof",
    )
    return gates


def write_report(*, proposal: RubricProposal, replay: OfflineReplayOutcome, gates: list[GateResult]) -> EvolutionReport:
    failed = [item for item in gates if not item.passed]
    improved = sum(1 for row in replay.rows if row.improved)
    regressed = sum(1 for row in replay.rows if row.regressed)
    if failed:
        recommend: Literal["approved_for_shadow", "rejected", "needs_review"] = "rejected"
        reasons = [f"{item.gate_id}: {item.detail}" for item in failed]
    elif improved == 0:
        recommend = "needs_review"
        reasons = [
            "gold precheck passed but candidate did not improve gold matches; "
            "clarify_check/change_weight still need frozen-evidence executor replay"
        ]
    else:
        recommend = "approved_for_shadow"
        reasons = [
            f"gold_precheck_improved={improved}",
            f"regressed={regressed}",
            f"gold_matches {replay.parent_gold_matches} -> {replay.candidate_gold_matches}",
            "approved_for_shadow is compile/precheck only; run scorer or ProviderRubricExecutor before publication",
        ]
    summary = (
        f"proposal {proposal.proposal_id} vs parent "
        f"{proposal.parent_rubric.proposed_version}: "
        f"{recommend}; gold_precheck improved={improved} regressed={regressed}"
    )
    return EvolutionReport(
        report_id=_short_id("rrepdoc", {
            "proposal": proposal.proposal_id, "replay": replay.replay_id,
        }),
        proposal_id=proposal.proposal_id,
        summary=summary,
        gates=gates,
        replay=replay,
        recommend=recommend,
        reasons=reasons,
    )


def record_memory(
    *,
    proposal: RubricProposal,
    decision: str,
    actor: str,
    reasons: list[str],
    evidence_refs: list[str],
) -> ResearchMemoryRecord:
    if not actor.strip():
        raise ValueError("memory actor is required")
    if decision not in TERMINAL_DECISIONS:
        raise ValueError(f"memory only records terminal decisions, got {decision}")
    created_at = _now_iso()
    payload = {
        "proposal_id": proposal.proposal_id,
        "decision": decision,
        "actor": actor,
        "reasons": reasons,
        "created_at": created_at,
    }
    return ResearchMemoryRecord(
        memory_id=_short_id("rmem", payload),
        proposal_id=proposal.proposal_id,
        decision=decision,
        actor=actor.strip(),
        reasons=reasons,
        evidence_refs=evidence_refs,
        created_at=created_at,
    )


def apply_human_decision(
    *,
    proposal: RubricProposal,
    report: EvolutionReport,
    actor: str,
    decision: Literal["approved_for_shadow", "rejected", "rolled_back", "needs_review"] | None = None,
) -> tuple[PromotionDecision, ResearchMemoryRecord]:
    """Human gate. Shadow is the highest automatic recommendation.

    Official publication remains `store.publish_candidate_sync`, which still
    refuses environment candidates without an official executor.
    """
    if not actor.strip():
        raise ValueError("promotion actor is required")
    target = decision or report.recommend
    if target == "approved_for_shadow" and report.recommend == "rejected":
        raise ValueError("cannot approve a proposal that failed hard gates")
    created_at = _now_iso()
    promotion = PromotionDecision(
        proposal_id=proposal.proposal_id,
        from_state=proposal.state,
        to_state=target,
        actor=actor.strip(),
        reasons=list(report.reasons),
        created_at=created_at,
    )
    memory = record_memory(
        proposal=proposal,
        decision=target,
        actor=actor,
        reasons=list(report.reasons),
        evidence_refs=[ref for patch in proposal.patches for ref in patch.evidence_refs],
    )
    return promotion, memory


def persist_proposal_bundle(
    *,
    data_path: Path,
    gap: RubricGap,
    hypothesis: RubricHypothesis,
    proposal: RubricProposal,
    corpus: FrozenCorpus,
    replay: OfflineReplayOutcome,
    report: EvolutionReport,
    promotion: PromotionDecision | None = None,
    memory: ResearchMemoryRecord | None = None,
) -> Path:
    root = data_path / "rubric-evolution" / "proposals" / proposal.proposal_id
    _atomic_json(root / "source-gap.json", gap.model_dump(mode="json"))
    _atomic_json(root / "hypothesis.json", hypothesis.model_dump(mode="json"))
    _atomic_json(root / "proposal.json", proposal.model_dump(mode="json", by_alias=True))
    _atomic_json(root / "parent-rubric.json", proposal.parent_rubric.model_dump(mode="json", by_alias=True))
    _atomic_json(
        root / "candidate-rubric.json",
        proposal.candidate_rubric.model_dump(mode="json", by_alias=True),
    )
    _atomic_json(root / "corpus-manifest.json", corpus.model_dump(mode="json"))
    _atomic_json(root / "offline-replay.json", replay.model_dump(mode="json"))
    _atomic_json(root / "evolution-report.json", report.model_dump(mode="json"))
    if promotion is not None:
        _atomic_json(root / "promotion-decision.json", promotion.model_dump(mode="json"))
    if memory is not None:
        memory_root = data_path / "rubric-evolution" / "memory"
        _atomic_json(root / "research-memory.json", memory.model_dump(mode="json"))
        _atomic_json(memory_root / f"{memory.memory_id}.json", memory.model_dump(mode="json"))
    return root


def run_active_evolution(
    *,
    data_path: Path,
    env_name: str,
    parent: CandidateRubric,
    claim: dict[str, Any],
    patches: list[RestrictedPatch | dict[str, Any]],
    cases: list[CorpusCase | dict[str, Any]],
    affected_capability: str,
    recommended_next_step: str,
    hypothesis_claim: str,
    expected_improvement: dict[str, str],
    required_corpus_slices: list[str],
    acceptance_criteria: list[str],
    actor: str,
    gap_type: str = "false_positive",
    owner_hypothesis: str = "evaluator",
    risk: list[str] | None = None,
    auto_decide: bool = True,
) -> dict[str, Any]:
    """End-to-end path with frozen fixtures. No LLM, no official publish."""
    gap = mine_gap_from_claim(
        env_name=env_name,
        rubric_version=parent.proposed_version,
        claim=claim,
        owner_hypothesis=owner_hypothesis,
        gap_type=gap_type,
        affected_capability=affected_capability,
        recommended_next_step=recommended_next_step,
    )
    hypothesis = build_hypothesis(
        gap=gap,
        claim=hypothesis_claim,
        expected_improvement=expected_improvement,
        required_corpus_slices=required_corpus_slices,
        acceptance_criteria=acceptance_criteria,
        risk=risk,
    )
    proposal = compile_proposal(
        parent=parent, gap=gap, hypothesis=hypothesis, patches=patches,
    )
    corpus = freeze_corpus(
        env_name=env_name,
        parent_version=parent.proposed_version,
        cases=cases,
        hypothesis=hypothesis,
    )
    replay = replay_parent_and_candidate(proposal=proposal, corpus=corpus)
    gates = validate_proposal(proposal=proposal, corpus=corpus, replay=replay)
    report = write_report(proposal=proposal, replay=replay, gates=gates)
    promotion = memory = None
    if auto_decide:
        promotion, memory = apply_human_decision(
            proposal=proposal, report=report, actor=actor,
        )
    artifact_dir = persist_proposal_bundle(
        data_path=data_path,
        gap=gap,
        hypothesis=hypothesis,
        proposal=proposal,
        corpus=corpus,
        replay=replay,
        report=report,
        promotion=promotion,
        memory=memory,
    )
    return {
        "gap_id": gap.gap_id,
        "hypothesis_id": hypothesis.hypothesis_id,
        "proposal_id": proposal.proposal_id,
        "corpus_id": corpus.corpus_id,
        "replay_id": replay.replay_id,
        "recommend": report.recommend,
        "artifact_dir": str(artifact_dir),
        "parent_gold_matches": replay.parent_gold_matches,
        "candidate_gold_matches": replay.candidate_gold_matches,
        "official_scores_mutated": False,
        "published": False,
    }


# Convenience for tests and scripts that need a tiny valid parent rubric.
def make_parent_rubric(
    *,
    env_name: str,
    version: str = "rubric-v1",
    check_id: str = "validation",
    weight: float = 10,
    description: str = "Award validation if a test-runner command appears in trace",
) -> CandidateRubric:
    return CandidateRubric(
        schema_version="octagon-evolved-rubric-v1",
        evolution_domain="product",
        rubric_id=f"rub_{env_name}_{version}",
        parent_version="bootstrap",
        proposed_version=version,
        scope="environment",
        env_name=env_name,
        checks=[RubricCheckSpec(
            check_id=check_id,
            title=check_id,
            description=description,
            weight=weight,
            evidence_requirements=["trace"],
            criteria=_criteria(
                **{
                    "pass": "trace contains a successful repository test-runner command",
                    "fail": "trace contains no successful repository test-runner command",
                }
            ),
        )],
    )
