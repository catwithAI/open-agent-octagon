"""Offline production Rubric Evolution Loop.

This module never changes the active rubric. It freezes inputs, asks an analysis
provider to produce ``candidate_generated``/``no_change``/``insufficient_evidence``,
validates the result, and writes append-only artifacts for human publication.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from backend.db import _now_iso
from backend.experiments.hashing import canonical_hash
from backend.insights.providers import InsightProvider, InsightProviderError, invoke_generate
from backend.insights.workspace import EvidenceWorkspace

from .models import EvolutionBatch, RubricEvolutionResult
from .prompts import RUBRIC_EVOLUTION_PROMPT_VERSION, rubric_evolution_prompt

PIPELINE_VERSION = "octagon-rubric-evolution-pipeline-v1"


@dataclass(frozen=True)
class RubricEvolutionOutcome:
    status: str
    batch_id: str
    artifact_dir: str
    result: str | None = None
    candidate_version: str | None = None
    cache_hit: bool = False
    error_code: str | None = None
    error_message: str | None = None


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, default=str)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _parse_json(text: str) -> dict[str, Any]:
    value = text.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            value = "\n".join(lines[1:-1])
            if value.lstrip().startswith("json\n"):
                value = value.lstrip()[5:]
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("rubric evolution output must be a JSON object")
    return parsed


def _collect_refs(value: Any, *, field: str | None = None) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            refs.update(_collect_refs(item, field=str(key)))
    elif isinstance(value, list):
        for item in value:
            refs.update(_collect_refs(item, field=field))
    elif isinstance(value, str) and (
        field in {"anchor", "evidence_ref"}
        or field in {"evidence_refs", "contradicting_refs"}
        or value.startswith("octagon://")
    ):
        refs.add(value)
    return refs


def _validate_result_against_inputs(
    *,
    result: RubricEvolutionResult,
    batch: EvolutionBatch,
    current_rubric_version: str,
    available_refs: set[str],
) -> None:
    output_refs = {
        ref
        for diagnosis in result.diagnoses
        for ref in diagnosis.evidence_refs
    } | {
        ref for change in result.changes for ref in change.evidence_refs
    }
    invalid = sorted(output_refs - available_refs)
    if invalid:
        raise ValueError(f"rubric evolution returned invalid evidence refs: {invalid[:10]}")
    candidate = result.candidate_rubric
    if candidate is None:
        return
    if candidate.parent_version != current_rubric_version:
        raise ValueError("candidate parent_version does not match frozen current rubric")
    if candidate.scope != batch.scope:
        raise ValueError("candidate scope does not match evolution batch")
    if batch.scope == "environment" and candidate.env_name != batch.env_name:
        raise ValueError("candidate env_name does not match evolution batch")
    if candidate.proposed_version == candidate.parent_version:
        raise ValueError("candidate proposed_version must differ from parent_version")


def _artifact_root(data_path: Path, batch: EvolutionBatch, input_hash: str) -> Path:
    return (
        data_path
        / "rubric-evolution"
        / "batches"
        / batch.batch_id
        / input_hash.split(":")[-1][:24]
    )


async def evolve_rubric(
    *,
    data_path: Path,
    batch: EvolutionBatch,
    current_rubric: dict[str, Any],
    current_rubric_version: str,
    provider: InsightProvider,
    reviewed_analyses: list[dict[str, Any]] | None = None,
    raw_runtrace_evidence: list[dict[str, Any]] | None = None,
) -> RubricEvolutionOutcome:
    reviewed = reviewed_analyses or []
    raw_evidence = raw_runtrace_evidence or []
    provider_identity = {
        "provider": getattr(provider, "provider_name", provider.__class__.__name__),
        "model": getattr(provider, "model", None),
    }
    input_hash = canonical_hash({
        "pipeline_version": PIPELINE_VERSION,
        "prompt_version": RUBRIC_EVOLUTION_PROMPT_VERSION,
        "batch_hash": batch.input_hash,
        "current_rubric_version": current_rubric_version,
        "current_rubric": current_rubric,
        "reviewed_analyses": reviewed,
        "raw_runtrace_evidence": raw_evidence,
        **provider_identity,
    })
    root = _artifact_root(data_path, batch, input_hash)
    result_path = root / "evolution-result.json"
    if result_path.is_file():
        try:
            cached = RubricEvolutionResult.model_validate_json(
                result_path.read_text(encoding="utf-8")
            )
            return RubricEvolutionOutcome(
                status="ready",
                batch_id=batch.batch_id,
                artifact_dir=str(root),
                result=cached.result,
                candidate_version=(
                    cached.candidate_rubric.proposed_version
                    if cached.candidate_rubric else None
                ),
                cache_hit=True,
            )
        except (OSError, ValidationError):
            pass

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
        },
        {
            "current-rubric.json": "frozen current Product Rubric",
            "batch.json": "frozen Judge Record batch",
            "evidence/reviewed-analyses.json": "reviewed Process analyses, if any",
            "evidence/raw-runtrace-evidence.json": "frozen runtrace snapshots",
        },
    )
    prompt = rubric_evolution_prompt(
        batch=batch,
        current_rubric=current_rubric,
        evidence_catalog={"files": workspace.catalog()},
    )
    _atomic_json(root / "analysis-input.json", {
        "schema_version": "octagon-rubric-evolution-input-v1",
        "pipeline_version": PIPELINE_VERSION,
        "prompt_version": RUBRIC_EVOLUTION_PROMPT_VERSION,
        "input_hash": input_hash,
        "provider": provider_identity,
        "evidence_catalog": workspace.catalog(),
        "created_at": _now_iso(),
    })
    available_refs = _collect_refs([
        batch.model_dump(mode="json"), reviewed, raw_evidence
    ])
    try:
        generated = await invoke_generate(provider, prompt, workspace=workspace)
        _atomic_json(root / "raw-provider-output.json", {
            "provider": generated.provider,
            "model": generated.model,
            "text": generated.text,
            "input_tokens": generated.input_tokens,
            "output_tokens": generated.output_tokens,
            "cost": generated.cost,
            "received_at": _now_iso(),
        })
        result = RubricEvolutionResult.model_validate(_parse_json(generated.text))
        _validate_result_against_inputs(
            result=result,
            batch=batch,
            current_rubric_version=current_rubric_version,
            available_refs=available_refs,
        )
        payload = result.model_dump(mode="json", by_alias=True)
        _atomic_json(result_path, payload)
        generation = {
            "schema_version": "octagon-rubric-evolution-generation-v1",
            "pipeline_version": PIPELINE_VERSION,
            "batch_id": batch.batch_id,
            "input_hash": input_hash,
            "generated_at": _now_iso(),
            "publication_status": (
                "awaiting_human" if result.candidate_rubric else "not_applicable"
            ),
            "result": payload,
        }
        generation_hash = canonical_hash(generation)
        _atomic_json(
            root / "generations" / f"{generation_hash.split(':')[-1]}.json",
            generation,
        )
        return RubricEvolutionOutcome(
            status="ready",
            batch_id=batch.batch_id,
            artifact_dir=str(root),
            result=result.result,
            candidate_version=(
                result.candidate_rubric.proposed_version
                if result.candidate_rubric else None
            ),
        )
    except (InsightProviderError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        failure = {
            "schema_version": "octagon-rubric-evolution-failure-v1",
            "pipeline_version": PIPELINE_VERSION,
            "batch_id": batch.batch_id,
            "input_hash": input_hash,
            "error_code": (
                "rubric_evolution_provider_failed"
                if isinstance(exc, InsightProviderError)
                else "rubric_evolution_output_invalid"
            ),
            "error_message": str(exc)[:4000],
            "failed_at": _now_iso(),
        }
        _atomic_json(root / "evolution-failure.json", failure)
        return RubricEvolutionOutcome(
            status="failed",
            batch_id=batch.batch_id,
            artifact_dir=str(root),
            error_code=failure["error_code"],
            error_message=failure["error_message"],
        )
