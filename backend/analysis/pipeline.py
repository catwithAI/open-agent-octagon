"""Replayable three-stage black-box analysis pipeline."""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from backend.db import _now_iso
from backend.experiments.hashing import canonical_hash
from backend.insights.jsonutil import parse_json_object
from backend.insights.providers import InsightProvider, InsightProviderError, invoke_generate
from backend.insights.workspace import EvidenceWorkspace

from .models import AttemptAnalysis, ComparisonAnalysis, Critique
from .prompts import (
    ATTEMPT_PROMPT_VERSION,
    COMPARISON_PROMPT_VERSION,
    CRITIC_PROMPT_VERSION,
    attempt_prompt,
    comparison_prompt,
    critic_prompt,
)
from .snapshot import build_run_analysis_snapshot

PIPELINE_VERSION = "octagon-blackbox-analysis-pipeline-v1"
logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class AnalysisOutcome:
    status: str
    run_id: str
    snapshot_hash: str
    artifact_dir: str
    accepted_claim_count: int
    rejected_claim_count: int
    provider_calls: int = 0
    cache_hits: int = 0
    error_code: str | None = None
    error_message: str | None = None


def _parse_json(text: str) -> dict[str, Any]:
    return parse_json_object(text)


def _wrap_bare_claim(parsed: dict[str, Any], *, stage: str) -> dict[str, Any]:
    """If the model returned one claim object, wrap it in the Attempt envelope."""
    if parsed.get("schema_version") or "claims" in parsed:
        return parsed
    if not {"id", "text"}.issubset(parsed):
        return parsed
    if not stage.startswith("attempt-"):
        return parsed
    return {
        "schema_version": "octagon-attempt-analysis-v1",
        "attempt_id": stage.removeprefix("attempt-"),
        "summary": str(parsed.get("text") or ""),
        "claims": [parsed],
    }


_STAGE_SCHEMA_VERSIONS = {
    "attempt": "octagon-attempt-analysis-v1",
    "comparison": "octagon-cross-agent-analysis-v1",
    "critic": "octagon-evidence-critique-v1",
}


def _fill_missing_schema_version(parsed: dict[str, Any], *, stage: str) -> dict[str, Any]:
    """Stamp the envelope version when the body is otherwise complete."""
    if parsed.get("schema_version"):
        return parsed
    if stage.startswith("attempt-"):
        key = "attempt"
    elif stage == "comparison":
        key = "comparison"
    elif stage == "critic":
        key = "critic"
    else:
        return parsed
    if key == "critic" and "accepted_claim_ids" not in parsed and "rejected_claims" not in parsed:
        return parsed
    if key == "comparison" and "run_id" not in parsed and "claims" not in parsed:
        return parsed
    if key == "attempt" and "attempt_id" not in parsed and "claims" not in parsed:
        return parsed
    filled = dict(parsed)
    filled["schema_version"] = _STAGE_SCHEMA_VERSIONS[key]
    return filled


def _normalize_analysis_shape(
    parsed: dict[str, Any], *, stage: str = "",
) -> dict[str, Any]:
    """Normalize only representation-equivalent list fields, never meaning."""
    candidate = _fill_missing_schema_version(
        _wrap_bare_claim(dict(parsed), stage=stage), stage=stage
    )
    claims = candidate.get("claims")
    if isinstance(claims, list):
        normalized_claims = []
        for raw in claims:
            if not isinstance(raw, dict):
                normalized_claims.append(raw)
                continue
            claim = dict(raw)
            # Models sometimes emit {..., "id": "claim.id"} after closing
            # nested arrays, which is invalid JSON. If a trailing sibling leaked
            # into this object as a second id-like field, keep the explicit id.
            if not claim.get("id") and claim.get("claim_id"):
                claim["id"] = claim.pop("claim_id")
            for field in (
                "evidence_refs", "contradicting_refs", "alternative_explanations", "limitations"
            ):
                value = claim.get(field)
                if isinstance(value, str):
                    claim[field] = [value] if value else []
                elif value is None:
                    claim[field] = []
            normalized_claims.append(claim)
        candidate["claims"] = normalized_claims
    rejected_claims = candidate.get("rejected_claims")
    if isinstance(rejected_claims, list):
        normalized_rejections = []
        for raw in rejected_claims:
            if isinstance(raw, dict) and "claim_id" not in raw and "id" in raw:
                raw = {**raw, "claim_id": raw["id"]}
                raw.pop("id", None)
            normalized_rejections.append(raw)
        candidate["rejected_claims"] = normalized_rejections
    for field in ("recommended_next_steps", "accepted_claim_ids", "limitations"):
        value = candidate.get(field)
        if isinstance(value, str):
            candidate[field] = [value] if value else []
        elif value is None and field in candidate:
            candidate[field] = []
    return candidate


def _write_stage_workspace(
    root: Path,
    *,
    stage: str,
    files: dict[str, Any],
) -> EvidenceWorkspace:
    workspace_root = root / "workspaces" / stage
    workspace_root.mkdir(parents=True, exist_ok=True)
    mapping: dict[str, Path] = {}
    descriptions: dict[str, str] = {}
    for relative, value in files.items():
        path = workspace_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(value, Path):
            path.write_bytes(value.read_bytes())
        else:
            path.write_text(
                json.dumps(value, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        mapping[relative] = path
        descriptions[relative] = f"frozen {relative}"
    return EvidenceWorkspace(mapping, descriptions)


def _write_raw_provider_output(path: Path, *, prompt: str, result: Any, error: str | None = None) -> None:
    payload = {
        "provider": getattr(result, "provider", None),
        "model": getattr(result, "model", None),
        "text": getattr(result, "text", None),
        "input_tokens": getattr(result, "input_tokens", None),
        "output_tokens": getattr(result, "output_tokens", None),
        "cost": getattr(result, "cost", None),
        "prompt_chars": len(prompt),
        "received_at": _now_iso(),
    }
    if error is not None:
        payload["error"] = error[:4000]
    _atomic_json(path, payload)


def _empty_process_analysis(value: Any) -> bool:
    claims = getattr(value, "claims", None)
    summary = getattr(value, "summary", None)
    if claims is not None and len(claims) == 0:
        return True
    return isinstance(summary, str) and not summary.strip()


async def _generate(
    provider: InsightProvider,
    prompt: str,
    model: type[T],
    workspace: EvidenceWorkspace | None = None,
    raw_output_path: Path | None = None,
    stage: str = "",
    retry_empty: bool = True,
) -> tuple[T, dict[str, Any]]:
    result = await invoke_generate(provider, prompt, workspace=workspace)
    if raw_output_path is not None:
        _write_raw_provider_output(raw_output_path, prompt=prompt, result=result)
    try:
        parsed = _normalize_analysis_shape(
            _parse_json(result.text), stage=stage,
        )
        value = model.model_validate(parsed)
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as exc:
        if raw_output_path is not None:
            _write_raw_provider_output(
                raw_output_path, prompt=prompt, result=result, error=str(exc)
            )
        raise
    if retry_empty and workspace is not None and _empty_process_analysis(value):
        if raw_output_path is not None:
            _write_raw_provider_output(
                raw_output_path, prompt=prompt, result=result,
                error="process analysis returned an empty envelope",
            )
        followup = (
            prompt
            + "\n上一轮 claims/summary 为空。必须先读取 attempt.json 与 "
            + "anchors.json，再返回非空 summary 和至少一条带真实 "
            + "evidence_refs 的 claim。不要交空壳。"
        )
        suffix = raw_output_path.with_name(
            raw_output_path.stem + "-retry.json"
        ) if raw_output_path is not None else None
        return await _generate(
            provider, followup, model, workspace,
            raw_output_path=suffix, stage=stage, retry_empty=False,
        )
    return value, {
        "provider": result.provider,
        "model": result.model,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "cost": result.cost,
        "cache_hit": False,
    }


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, default=str)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _provider_identity(provider: InsightProvider) -> dict[str, Any]:
    return {
        "provider": getattr(provider, "provider_name", provider.__class__.__name__),
        "model": getattr(provider, "model", None),
    }


def _stage_input_hash(
    *, stage: str, prompt_version: str, prompt: str, provider: InsightProvider,
    workspace: EvidenceWorkspace | None = None,
) -> str:
    return canonical_hash({
        "pipeline_version": PIPELINE_VERSION,
        "stage": stage,
        "prompt_version": prompt_version,
        "prompt": prompt,
        "workspace_digest": None if workspace is None else workspace.digest(),
        **_provider_identity(provider),
    })


def _stage_cache_path(root: Path, stage: str, input_hash: str) -> Path:
    safe_stage = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in stage)
    return root / "stage-cache" / f"{safe_stage}-{input_hash.split(':')[-1][:24]}.json"


async def _generate_cached(
    *,
    root: Path,
    stage: str,
    prompt_version: str,
    prompt: str,
    provider: InsightProvider,
    model: type[T],
    metrics: dict[str, int],
    workspace: EvidenceWorkspace | None = None,
) -> tuple[T, dict[str, Any]]:
    input_hash = _stage_input_hash(
        stage=stage, prompt_version=prompt_version, prompt=prompt,
        provider=provider, workspace=workspace,
    )
    path = _stage_cache_path(root, stage, input_hash)
    if path.is_file():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if cached.get("input_hash") == input_hash:
                value = model.model_validate(cached["value"])
                metadata = dict(cached.get("generator") or {})
                metadata.update({"cache_hit": True, "input_hash": input_hash})
                metrics["cache_hits"] += 1
                return value, metadata
        except (OSError, KeyError, json.JSONDecodeError, ValidationError, TypeError):
            # Corrupt/stale cache is ignored; the provider call rebuilds it.
            pass
    metrics["provider_calls"] += 1
    raw_output_path = root / "raw-provider-outputs" / f"{stage}.json"
    value, metadata = await _generate(
        provider, prompt, model, workspace,
        raw_output_path=raw_output_path, stage=stage,
    )
    metadata["input_hash"] = input_hash
    _atomic_json(path, {
        "schema_version": "octagon-analysis-stage-cache-v1",
        "stage": stage,
        "input_hash": input_hash,
        "value": value.model_dump(mode="json"),
        "generator": metadata,
        "created_at": _now_iso(),
    })
    return value, metadata


def _claim_gate(
    analyses: list[dict[str, Any]], comparison: dict[str, Any], anchors: set[str]
) -> tuple[set[str], list[dict[str, str]]]:
    valid_ids: set[str] = set()
    rejected: list[dict[str, str]] = []
    seen: set[str] = set()
    all_claims = [
        claim
        for analysis in analyses
        for claim in analysis.get("claims", [])
    ] + list(comparison.get("claims", []))
    for claim in all_claims:
        claim_id = str(claim.get("id") or "")
        if not claim_id or claim_id in seen:
            rejected.append({"claim_id": claim_id or "<missing>", "reason": "missing_or_duplicate_claim_id"})
            continue
        seen.add(claim_id)
        refs = list(claim.get("evidence_refs") or [])
        invalid = sorted(set(refs) - anchors)
        if invalid:
            rejected.append({"claim_id": claim_id, "reason": "invalid_evidence_refs"})
        elif not refs:
            rejected.append({"claim_id": claim_id, "reason": "claim_has_no_evidence"})
        else:
            valid_ids.add(claim_id)
    return valid_ids, rejected


def _artifact_dir(data_path: Path, run_id: str, snapshot_hash: str) -> Path:
    return data_path / "analysis" / "runs" / run_id / snapshot_hash[:20]


async def analyze_run(
    *,
    db_path: Path,
    data_path: Path,
    run_id: str,
    provider: InsightProvider,
    per_source_limit: int = 500,
    attempt_ids: set[str] | None = None,
) -> AnalysisOutcome:
    snapshot = build_run_analysis_snapshot(
        db_path=db_path,
        data_path=data_path,
        run_id=run_id,
        per_source_limit=per_source_limit,
        attempt_ids=attempt_ids,
    )
    from backend.rubric_evolution.cross_layer import get_active_process_contract_sync

    process_rubric_version, process_contract = get_active_process_contract_sync(db_path)
    # Supply only executable diagnostic rules. Registry provenance, historical
    # prompt-version strings and JSON Schemas are frozen in artifacts but must not
    # leak into stage routing or become instructions inside another stage.
    process_execution_contract = {
        key: process_contract[key]
        for key in (
            "schema_version", "evolution_domain", "scope_key", "purpose",
            "dimensions", "constraints", "global_constraints",
        )
        if key in process_contract
    }
    root = _artifact_dir(data_path, run_id, snapshot["snapshot_hash"])
    _atomic_json(root / "analysis-snapshot.json", snapshot)
    provider_events: list[dict[str, Any]] = []
    stage_metrics = {"provider_calls": 0, "cache_hits": 0}
    try:
        attempt_analyses: list[dict[str, Any]] = []
        for attempt in snapshot["attempts"]:
            attempt_id = str(attempt["metadata"]["id"])
            workspace = _write_stage_workspace(
                root,
                stage=f"attempt-{attempt_id}",
                files={
                    "task.json": snapshot["task"],
                    "attempt.json": attempt,
                    "anchors.json": snapshot["anchors"],
                    "process-contract.json": process_execution_contract,
                },
            )
            prompt = attempt_prompt(
                snapshot, attempt, process_execution_contract,
                evidence_catalog={"files": workspace.catalog()},
            )
            analysis, metadata = await _generate_cached(
                root=root,
                stage=f"attempt-{attempt_id}",
                prompt_version=ATTEMPT_PROMPT_VERSION,
                prompt=prompt,
                provider=provider,
                model=AttemptAnalysis,
                metrics=stage_metrics,
                workspace=workspace,
            )
            if analysis.attempt_id != attempt["metadata"]["id"]:
                raise ValueError("attempt analysis returned a different attempt_id")
            attempt_analyses.append(analysis.model_dump(mode="json"))
            provider_events.append({"stage": "attempt", "attempt_id": analysis.attempt_id, **metadata})

        comparison_workspace = _write_stage_workspace(
            root,
            stage="comparison",
            files={
                "run.json": snapshot["run"],
                "task.json": snapshot["task"],
                "attempts.json": snapshot["attempts"],
                "anchors.json": snapshot["anchors"],
                "attempt-analyses.json": attempt_analyses,
                "process-contract.json": process_execution_contract,
            },
        )
        comparison_rendered = comparison_prompt(
            snapshot, attempt_analyses, process_execution_contract,
            evidence_catalog={"files": comparison_workspace.catalog()},
        )
        comparison_model, metadata = await _generate_cached(
            root=root,
            stage="comparison",
            prompt_version=COMPARISON_PROMPT_VERSION,
            prompt=comparison_rendered,
            provider=provider,
            model=ComparisonAnalysis,
            metrics=stage_metrics,
            workspace=comparison_workspace,
        )
        if comparison_model.run_id != run_id:
            raise ValueError("comparison returned a different run_id")
        comparison = comparison_model.model_dump(mode="json")
        provider_events.append({"stage": "comparison", **metadata})

        valid_ids, deterministic_rejections = _claim_gate(
            attempt_analyses, comparison, set(snapshot["anchors"])
        )
        critic_workspace = _write_stage_workspace(
            root,
            stage="critic",
            files={
                "anchors.json": snapshot["anchors"],
                "attempts.json": snapshot["attempts"],
                "attempt-analyses.json": attempt_analyses,
                "comparison.json": comparison,
                "process-contract.json": process_execution_contract,
            },
        )
        critic_rendered = critic_prompt(
            snapshot, attempt_analyses, comparison, process_execution_contract,
            evidence_catalog={"files": critic_workspace.catalog()},
        )
        critique_model, metadata = await _generate_cached(
            root=root,
            stage="critic",
            prompt_version=CRITIC_PROMPT_VERSION,
            prompt=critic_rendered,
            provider=provider,
            model=Critique,
            metrics=stage_metrics,
            workspace=critic_workspace,
        )
        provider_events.append({"stage": "critic", **metadata})
        critique = critique_model.model_dump(mode="json")
        rejected_by_critic = {
            item["claim_id"]: item["reason"] for item in critique["rejected_claims"]
        }
        accepted = [
            claim_id for claim_id in critique["accepted_claim_ids"]
            if claim_id in valid_ids and claim_id not in rejected_by_critic
        ]
        rejected = list(deterministic_rejections)
        rejected.extend(
            {"claim_id": claim_id, "reason": reason}
            for claim_id, reason in rejected_by_critic.items()
            if claim_id in valid_ids
        )
        decided = set(accepted) | {item["claim_id"] for item in rejected}
        rejected.extend(
            {"claim_id": claim_id, "reason": "critic_did_not_accept_claim"}
            for claim_id in sorted(valid_ids - decided)
        )
        final = {
            "schema_version": "octagon-reviewed-blackbox-analysis-v1",
            "pipeline_version": PIPELINE_VERSION,
            "run_id": run_id,
            "snapshot_hash": snapshot["snapshot_hash"],
            "attempt_analyses": attempt_analyses,
            "comparison": comparison,
            "critique": critique,
            "accepted_claim_ids": sorted(set(accepted)),
            "rejected_claims": rejected,
            "generator": {
                "attempt_prompt_version": ATTEMPT_PROMPT_VERSION,
                "comparison_prompt_version": COMPARISON_PROMPT_VERSION,
                "critic_prompt_version": CRITIC_PROMPT_VERSION,
                "process_rubric_version": process_rubric_version,
                "process_contract_hash": canonical_hash(process_contract),
                "calls": provider_events,
                "completed_at": _now_iso(),
            },
        }
        final["analysis_hash"] = canonical_hash({
            "pipeline_version": PIPELINE_VERSION,
            "snapshot_hash": snapshot["snapshot_hash"],
            "attempt_analyses": attempt_analyses,
            "comparison": comparison,
            "critique": critique,
            "accepted_claim_ids": final["accepted_claim_ids"],
            "rejected_claims": rejected,
            "provider": _provider_identity(provider),
            "prompt_versions": {
                "attempt": ATTEMPT_PROMPT_VERSION,
                "comparison": COMPARISON_PROMPT_VERSION,
                "critic": CRITIC_PROMPT_VERSION,
            },
            "process_rubric_version": process_rubric_version,
            "process_contract_hash": canonical_hash(process_contract),
        })
        _atomic_json(root / "attempt-analyses.json", attempt_analyses)
        _atomic_json(root / "cross-agent-analysis.json", comparison)
        _atomic_json(root / "evidence-critique.json", critique)
        generation_path = (
            root / "generations" /
            f"{final['analysis_hash'].split(':')[-1]}.json"
        )
        if not generation_path.exists():
            _atomic_json(generation_path, final)
        # Mutable convenience projection; immutable generations remain authoritative.
        _atomic_json(root / "reviewed-analysis.json", final)
        # Derived Process/Association collection is fail-open: analysis artifacts
        # remain authoritative even if the evolution stores are unavailable.
        try:
            from backend.rubric_evolution.cross_layer import record_process_analysis_sync

            record_process_analysis_sync(
                db_path=db_path,
                snapshot=snapshot,
                reviewed_analysis=final,
            )
        except Exception:
            logger.exception(
                "failed to collect Process Judge/Association records run_id=%s",
                run_id,
            )
        return AnalysisOutcome(
            status="ready",
            run_id=run_id,
            snapshot_hash=snapshot["snapshot_hash"],
            artifact_dir=str(root),
            accepted_claim_count=len(set(accepted)),
            rejected_claim_count=len(rejected),
            provider_calls=stage_metrics["provider_calls"],
            cache_hits=stage_metrics["cache_hits"],
        )
    except (InsightProviderError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        failure = {
            "schema_version": "octagon-blackbox-analysis-failure-v1",
            "pipeline_version": PIPELINE_VERSION,
            "run_id": run_id,
            "snapshot_hash": snapshot["snapshot_hash"],
            "error_code": (
                "analysis_provider_failed"
                if isinstance(exc, InsightProviderError)
                else "analysis_output_invalid"
            ),
            "error_message": str(exc)[:2000],
            "generator_calls": provider_events,
            "failed_at": _now_iso(),
        }
        _atomic_json(root / "analysis-failure.json", failure)
        return AnalysisOutcome(
            status="failed",
            run_id=run_id,
            snapshot_hash=snapshot["snapshot_hash"],
            artifact_dir=str(root),
            accepted_claim_count=0,
            rejected_claim_count=0,
            provider_calls=stage_metrics["provider_calls"],
            cache_hits=stage_metrics["cache_hits"],
            error_code=failure["error_code"],
            error_message=failure["error_message"],
        )
