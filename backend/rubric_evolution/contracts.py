"""Four independently acceptible evolution contracts.

Only contract 3 calls a model. The other three are deterministic and must be
replayable without a provider.

1. Evidence freeze
   Official scores and reviewed analyses become append-only records.
2. Batch / workspace freeze
   Ready records become a hashed workspace. Same inputs, same files.
3. Candidate generation
   LLM only. Not part of this module.
4. Accept / replay
   Validate a result against the frozen workspace; recompute score views;
   persist membership. No model, no official-score mutation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from backend.db import _now_iso, _open_sync
from backend.experiments.hashing import canonical_hash
from backend.insights.workspace import EvidenceWorkspace

from .batching import prepare_evolution_batch
from .collector import (
    project_historical_score_sync,
    project_historical_scores_sync,
    record_evaluation_outcome_sync,
)
from .cross_layer import (
    PROCESS_SCOPE_KEY,
    backfill_process_analysis_artifacts_sync,
    record_process_analysis_sync,
)
from .loop_models import AssociationEvolutionResult, ProcessEvolutionResult
from .models import EvolutionBatch, JudgeRecord, RubricEvolutionResult
from .pipeline import (
    PIPELINE_VERSION,
    _artifact_root,
    _atomic_json,
    _collect_refs,
    _validate_result_against_inputs,
)
from .prompts import RUBRIC_EVOLUTION_PROMPT_VERSION
from .scoring import compute_rubric_score_views
from .store import persist_batch_membership_sync


@dataclass(frozen=True)
class FrozenWorkspace:
    domain: str
    batch_id: str
    input_hash: str
    artifact_dir: Path
    catalog: list[dict[str, Any]]
    digest: str
    available_refs: frozenset[str]


def replay_product_evidence_sync(
    *,
    db_path: Path,
    attempt_id: str,
    scores: list[dict[str, Any]],
    evaluation_manifest: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> str:
    """Re-collect one committed official score as a Judge Record."""
    return record_evaluation_outcome_sync(
        db_path=db_path,
        attempt_id=attempt_id,
        scores=scores,
        evaluation_manifest=evaluation_manifest,
        metadata=metadata,
    )


def project_historical_product_evidence_sync(
    *,
    db_path: Path,
    attempt_id: str | None = None,
    env_name: str | None = None,
    limit: int = 10000,
) -> dict[str, Any]:
    """Project historical official scores without rewriting source rows."""
    if attempt_id is not None:
        record_id = project_historical_score_sync(
            db_path=db_path, attempt_id=attempt_id
        )
        return {"record_id": record_id, "inserted": 1}
    return project_historical_scores_sync(
        db_path=db_path, env_name=env_name, limit=limit
    )


def replay_process_evidence_sync(
    *,
    db_path: Path,
    data_path: Path | None = None,
    snapshot: dict[str, Any] | None = None,
    reviewed_analysis: dict[str, Any] | None = None,
) -> dict[str, int]:
    """Re-collect Process/Association records from frozen analysis artifacts."""
    if snapshot is not None and reviewed_analysis is not None:
        inserted = record_process_analysis_sync(
            db_path=db_path,
            snapshot=snapshot,
            reviewed_analysis=reviewed_analysis,
        )
        return {
            "process_records": inserted[0],
            "association_cases": inserted[1],
        }
    if data_path is None:
        raise ValueError("replay_process_evidence_sync requires artifacts or data_path")
    return backfill_process_analysis_artifacts_sync(
        db_path=db_path, data_path=data_path
    )


def freeze_product_workspace(
    *,
    data_path: Path,
    batch: EvolutionBatch,
    current_rubric: dict[str, Any],
    current_rubric_version: str,
    reviewed_analyses: list[dict[str, Any]] | None = None,
    raw_runtrace_evidence: list[dict[str, Any]] | None = None,
) -> FrozenWorkspace:
    """Write the Product evolution workspace without calling a model."""
    reviewed = reviewed_analyses or []
    raw_evidence = raw_runtrace_evidence or []
    input_hash = canonical_hash({
        "pipeline_version": PIPELINE_VERSION,
        "prompt_version": RUBRIC_EVOLUTION_PROMPT_VERSION,
        "batch_hash": batch.input_hash,
        "current_rubric_version": current_rubric_version,
        "current_rubric": current_rubric,
        "reviewed_analyses": reviewed,
        "raw_runtrace_evidence": raw_evidence,
    })
    root = _artifact_root(data_path, batch, input_hash)
    root.mkdir(parents=True, exist_ok=True)
    _atomic_json(root / "batch.json", batch.model_dump(mode="json"))
    _atomic_json(root / "current-rubric.json", {
        "rubric_version": current_rubric_version,
        "rubric": current_rubric,
    })
    evidence_dir = root / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    _atomic_json(evidence_dir / "reviewed-analyses.json", reviewed)
    _atomic_json(evidence_dir / "raw-runtrace-evidence.json", raw_evidence)
    workspace = EvidenceWorkspace(
        {
            "current-rubric.json": root / "current-rubric.json",
            "batch.json": root / "batch.json",
            "evidence/reviewed-analyses.json": evidence_dir / "reviewed-analyses.json",
            "evidence/raw-runtrace-evidence.json": evidence_dir / "raw-runtrace-evidence.json",
        }
    )
    catalog = workspace.catalog()
    _atomic_json(root / "analysis-input.json", {
        "schema_version": "octagon-rubric-evolution-input-v1",
        "pipeline_version": PIPELINE_VERSION,
        "prompt_version": RUBRIC_EVOLUTION_PROMPT_VERSION,
        "input_hash": input_hash,
        "evidence_catalog": catalog,
        "created_at": _now_iso(),
    })
    refs = _collect_refs([batch.model_dump(mode="json"), reviewed, raw_evidence])
    return FrozenWorkspace(
        domain="product",
        batch_id=batch.batch_id,
        input_hash=input_hash,
        artifact_dir=root,
        catalog=catalog,
        digest=workspace.digest(),
        available_refs=frozenset(refs),
    )


def freeze_process_workspace(
    *,
    data_path: Path,
    trigger: list[dict[str, Any]],
    overlap: list[dict[str, Any]],
    contract: dict[str, Any],
    parent_version: str,
) -> FrozenWorkspace:
    batch_payload = {
        "domain": "process",
        "scope_key": PROCESS_SCOPE_KEY,
        "parent_version": parent_version,
        "trigger_ids": [row["id"] for row in trigger],
        "overlap_ids": [row["id"] for row in overlap],
    }
    batch_id = "peb_" + canonical_hash(batch_payload).split(":")[-1][:24]
    root = data_path / "process-evolution" / "batches" / batch_id
    compact = [{
        "record_id": row["id"],
        "env_name": row["env_name"],
        "claims": json.loads(row["claims_json"]) if isinstance(row.get("claims_json"), str) else row.get("claims"),
        "accepted_claim_ids": (
            json.loads(row["accepted_claim_ids_json"])
            if isinstance(row.get("accepted_claim_ids_json"), str)
            else row.get("accepted_claim_ids")
        ),
        "rejected_claims": (
            json.loads(row["rejected_claims_json"])
            if isinstance(row.get("rejected_claims_json"), str)
            else row.get("rejected_claims")
        ),
        "metadata": (
            json.loads(row["metadata_json"])
            if isinstance(row.get("metadata_json"), str)
            else row.get("metadata")
        ),
    } for row in trigger + overlap]
    _atomic_json(root / "batch.json", batch_payload)
    _atomic_json(root / "evidence" / "process-records.json", compact)
    _atomic_json(root / "evidence" / "current-contract.json", contract)
    workspace = EvidenceWorkspace({
        "batch.json": root / "batch.json",
        "evidence/current-contract.json": root / "evidence" / "current-contract.json",
        "evidence/process-records.json": root / "evidence" / "process-records.json",
    })
    return FrozenWorkspace(
        domain="process",
        batch_id=batch_id,
        input_hash=canonical_hash(batch_payload),
        artifact_dir=root,
        catalog=workspace.catalog(),
        digest=workspace.digest(),
        available_refs=frozenset(batch_payload["trigger_ids"] + batch_payload["overlap_ids"]),
    )


def freeze_association_workspace(
    *,
    data_path: Path,
    trigger: list[dict[str, Any]],
    overlap: list[dict[str, Any]],
) -> FrozenWorkspace:
    batch_payload = {
        "domain": "association",
        "trigger_ids": [row["id"] for row in trigger],
        "overlap_ids": [row["id"] for row in overlap],
    }
    batch_id = "aeb_" + canonical_hash(batch_payload).split(":")[-1][:24]
    root = data_path / "association-evolution" / "batches" / batch_id
    compact = [{
        "case_id": row["id"],
        "env_name": row["env_name"],
        "product_outcome": (
            json.loads(row["product_outcome_json"])
            if isinstance(row.get("product_outcome_json"), str)
            else row.get("product_outcome")
        ),
        "process_findings": (
            json.loads(row["process_findings_json"])
            if isinstance(row.get("process_findings_json"), str)
            else row.get("process_findings")
        ),
        "controls": (
            json.loads(row["controls_json"])
            if isinstance(row.get("controls_json"), str)
            else row.get("controls")
        ),
    } for row in trigger + overlap]
    _atomic_json(root / "batch.json", batch_payload)
    _atomic_json(root / "evidence" / "association-cases.json", compact)
    workspace = EvidenceWorkspace({
        "batch.json": root / "batch.json",
        "evidence/association-cases.json": root / "evidence" / "association-cases.json",
    })
    return FrozenWorkspace(
        domain="association",
        batch_id=batch_id,
        input_hash=canonical_hash(batch_payload),
        artifact_dir=root,
        catalog=workspace.catalog(),
        digest=workspace.digest(),
        available_refs=frozenset(batch_payload["trigger_ids"] + batch_payload["overlap_ids"]),
    )


def validate_product_result(
    *,
    result: RubricEvolutionResult | dict[str, Any],
    batch: EvolutionBatch,
    current_rubric_version: str,
    available_refs: set[str] | frozenset[str],
) -> RubricEvolutionResult:
    try:
        parsed = (
            result if isinstance(result, RubricEvolutionResult)
            else RubricEvolutionResult.model_validate(result)
        )
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc
    _validate_result_against_inputs(
        result=parsed,
        batch=batch,
        current_rubric_version=current_rubric_version,
        available_refs=set(available_refs),
    )
    return parsed


def validate_process_result(
    *,
    result: ProcessEvolutionResult | dict[str, Any],
    allowed_record_ids: set[str] | frozenset[str],
    parent_version: str,
) -> ProcessEvolutionResult:
    try:
        parsed = (
            result if isinstance(result, ProcessEvolutionResult)
            else ProcessEvolutionResult.model_validate(result)
        )
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc
    if not set(parsed.evidence_record_ids).issubset(set(allowed_record_ids)):
        raise ValueError("Process result cited records outside the frozen batch")
    if parsed.candidate is not None:
        if parsed.candidate.parent_version != parent_version:
            raise ValueError("Process candidate parent_version is stale")
        if parsed.candidate.scope_key != PROCESS_SCOPE_KEY:
            raise ValueError("Process candidate scope_key is invalid")
    return parsed


def validate_association_result(
    *,
    result: AssociationEvolutionResult | dict[str, Any],
    allowed_case_ids: set[str] | frozenset[str],
) -> AssociationEvolutionResult:
    try:
        parsed = (
            result if isinstance(result, AssociationEvolutionResult)
            else AssociationEvolutionResult.model_validate(result)
        )
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc
    for hypothesis in parsed.hypotheses:
        refs = set(hypothesis.supporting_case_ids + hypothesis.contradicting_case_ids)
        if not refs.issubset(set(allowed_case_ids)):
            raise ValueError("Association hypothesis cited Cases outside the batch")
        if hypothesis.causal_status != "correlational":
            raise ValueError("Association hypothesis must remain correlational")
    return parsed


def replay_score_views(records: list[JudgeRecord]) -> list[dict[str, Any]]:
    """Recompute numeric views from frozen Judge Records. Official scores stay put."""
    views = []
    for record in records:
        view = compute_rubric_score_views(record.checks)
        views.append({
            "record_id": record.record_id,
            "attempt_id": record.attempt_id,
            "score_with_unknown": view.score_with_unknown,
            "normalized_score_without_unknown": view.normalized_score_without_unknown,
        })
    return views


def persist_product_batch_once(
    *, db_path: Path, batch: EvolutionBatch, env_name: str, rubric_version: str
) -> None:
    persist_batch_membership_sync(
        db_path, batch, scope_key=f"environment:{env_name}:{rubric_version}"
    )


def prepare_environment_batch(
    records: list[JudgeRecord],
    *,
    env_name: str,
    threshold: int,
    previous: list[JudgeRecord] | None = None,
):
    return prepare_evolution_batch(
        new_records=records,
        previous_records=previous or [],
        scope="environment",
        env_name=env_name,
        threshold=threshold,
    )
