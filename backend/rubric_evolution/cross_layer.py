"""Process-Rubric and cross-layer record foundations.

These records never update Product scores. They provide independent Process
Judge evidence and paired Product/Process cases for later association learning.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from backend.analysis.models import AttemptAnalysis, ComparisonAnalysis, Critique
from backend.analysis.prompts import (
    ATTEMPT_PROMPT_VERSION,
    COMPARISON_PROMPT_VERSION,
    CRITIC_PROMPT_VERSION,
)
from backend.db import _init_db_sync, _now_iso, _open_sync
from backend.experiments.hashing import canonical_hash

PROCESS_SCOPE_KEY = "global:runtrace-analysis"


def _process_contract() -> dict[str, Any]:
    skill_path = Path(__file__).parents[1] / "analysis" / "blade_skill" / "SKILL.md"
    skill = skill_path.read_text(encoding="utf-8")
    return {
        "schema_version": "octagon-process-rubric-v1",
        "evolution_domain": "process",
        "scope_key": PROCESS_SCOPE_KEY,
        "purpose": "Explain why an Agent succeeded or failed without changing official scores.",
        "dimensions": [
            "task_understanding",
            "decision_divergence",
            "error_recovery",
            "validation_discipline",
            "completion_basis",
            "evidence_grounding",
            "alternative_explanations",
            "limitations",
            "infrastructure_attribution",
        ],
        "constraints": [
            "every claim must cite frozen evidence anchors",
            "temporal order is not automatically causation",
            "infrastructure failures must not be attributed to the candidate",
            "insufficient evidence must remain explicit",
            "process findings never directly mutate Product scores or rewards",
        ],
        "prompt_versions": {
            "attempt": ATTEMPT_PROMPT_VERSION,
            "comparison": COMPARISON_PROMPT_VERSION,
            "critic": CRITIC_PROMPT_VERSION,
        },
        "output_schemas": {
            "attempt": AttemptAnalysis.model_json_schema(),
            "comparison": ComparisonAnalysis.model_json_schema(),
            "critic": Critique.model_json_schema(),
        },
        "skill": {"path": "backend/analysis/blade_skill/SKILL.md", "content": skill},
    }


def register_process_rubric_v1_sync(db_path: Path) -> str:
    """Register the existing Runtrace Judge contract as Process Rubric V1."""
    _init_db_sync(db_path)
    contract = _process_contract()
    contract_hash = canonical_hash(contract)
    version = "process:" + contract_hash.split(":")[-1][:16]
    contract_id = "contract_" + contract_hash.split(":")[-1][:24]
    now = _now_iso()
    with _open_sync(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT OR IGNORE INTO evolution_contract_versions("
            "id,evolution_domain,scope_key,version,parent_version,status,contract_json,"
            "contract_hash,source_batch_id,created_at,published_at,published_by) "
            "VALUES(?,'process',?,?,'bootstrap','published',?,?,NULL,?,?,?)",
            (
                contract_id,
                PROCESS_SCOPE_KEY,
                version,
                json.dumps(contract, ensure_ascii=False, sort_keys=True),
                contract_hash,
                now,
                now,
                "system:runtrace-contract-bootstrap",
            ),
        )
        active = conn.execute(
            "SELECT version FROM evolution_active_contracts "
            "WHERE evolution_domain='process' AND scope_key=?",
            (PROCESS_SCOPE_KEY,),
        ).fetchone()
        if active is None:
            conn.execute(
                "INSERT INTO evolution_active_contracts("
                "evolution_domain,scope_key,version,contract_id,updated_at) "
                "VALUES('process',?,?,?,?)",
                (PROCESS_SCOPE_KEY, version, contract_id, now),
            )
        else:
            version = str(active[0])
        conn.commit()
    return version


def register_legacy_process_contract_sync(
    db_path: Path,
    *,
    pipeline_version: str,
    prompt_versions: dict[str, Any],
) -> str:
    """Map pre-versioned analysis artifacts to an immutable legacy contract."""
    normalized_prompts = {
        key: str(prompt_versions.get(key) or "")
        for key in ("attempt", "comparison", "critic")
    }
    if not pipeline_version or not all(normalized_prompts.values()):
        raise ValueError("legacy Process artifact lacks frozen prompt provenance")
    contract = {
        "schema_version": "octagon-legacy-process-rubric-v1",
        "evolution_domain": "process",
        "scope_key": PROCESS_SCOPE_KEY,
        "pipeline_version": pipeline_version,
        "prompt_versions": normalized_prompts,
        "provenance": "inferred_from_pre_process_version_artifact",
        "active": False,
    }
    contract_hash = canonical_hash(contract)
    version = "process-legacy:" + contract_hash.split(":")[-1][:16]
    contract_id = "contract_" + contract_hash.split(":")[-1][:24]
    now = _now_iso()
    with _open_sync(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO evolution_contract_versions("
            "id,evolution_domain,scope_key,version,parent_version,status,contract_json,"
            "contract_hash,source_batch_id,created_at,published_at,published_by) "
            "VALUES(?,'process',?,?,'legacy','published',?,?,NULL,?,?,?)",
            (
                contract_id, PROCESS_SCOPE_KEY, version,
                json.dumps(contract, ensure_ascii=False, sort_keys=True),
                contract_hash, now, now, "system:legacy-process-provenance",
            ),
        )
        conn.commit()
    return version


def get_active_process_contract_sync(db_path: Path) -> tuple[str, dict[str, Any]]:
    _init_db_sync(db_path)
    with _open_sync(db_path) as conn:
        row = conn.execute(
            "SELECT ac.version,cv.contract_json FROM evolution_active_contracts ac "
            "JOIN evolution_contract_versions cv ON cv.id=ac.contract_id "
            "WHERE ac.evolution_domain='process' AND ac.scope_key=?",
            (PROCESS_SCOPE_KEY,),
        ).fetchone()
    if row is None:
        register_process_rubric_v1_sync(db_path)
        return get_active_process_contract_sync(db_path)
    return str(row[0]), json.loads(row[1])


def get_active_process_rubric_version_sync(db_path: Path) -> str:
    return get_active_process_contract_sync(db_path)[0]


def reconcile_unversioned_process_evidence_sync(db_path: Path) -> dict[str, int]:
    """Quarantine records created by the old current-version fallback."""
    _init_db_sync(db_path)
    quarantined_records = 0
    quarantined_cases = 0
    with _open_sync(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id,metadata_json FROM process_judge_records WHERE valid_execution=1"
        ).fetchall()
        for row in rows:
            metadata = json.loads(row["metadata_json"] or "{}")
            if metadata.get("process_version_provenance"):
                continue
            metadata["process_version_provenance"] = "quarantined_missing_provenance"
            metadata["invalid_reason"] = "artifact_did_not_freeze_process_rubric_version"
            conn.execute(
                "UPDATE process_judge_records SET valid_execution=0,metadata_json=? WHERE id=?",
                (json.dumps(metadata, ensure_ascii=False, sort_keys=True), row["id"]),
            )
            # A corrected legacy mapping may reuse this record after backfill, so
            # remove old Batch consumption produced from invalid provenance.
            conn.execute(
                "DELETE FROM process_evolution_batch_members WHERE record_id=?",
                (row["id"],),
            )
            quarantined_records += 1
        cursor = conn.execute(
            "UPDATE association_cases SET valid_evidence=0,"
            "invalid_reason='process_rubric_provenance_missing' "
            "WHERE valid_evidence=1 AND NOT EXISTS ("
            "SELECT 1 FROM process_judge_records p WHERE p.attempt_id=association_cases.attempt_id "
            "AND p.process_rubric_version=association_cases.process_rubric_version "
            "AND p.valid_execution=1)"
        )
        quarantined_cases = max(cursor.rowcount, 0)
        conn.commit()
    return {
        "process_records": quarantined_records,
        "association_cases": quarantined_cases,
    }


def backfill_process_analysis_artifacts_sync(
    *, db_path: Path, data_path: Path, limit: int = 10000
) -> dict[str, int]:
    """Idempotently collect existing reviewed Runtrace analyses."""
    process_records = 0
    association_cases = 0
    failures = 0
    paths = sorted(
        (data_path / "analysis" / "runs").glob("*/*/reviewed-analysis.json")
    )[:max(1, min(limit, 100000))]
    for reviewed_path in paths:
        snapshot_path = reviewed_path.parent / "analysis-snapshot.json"
        if not snapshot_path.is_file():
            failures += 1
            continue
        try:
            reviewed = json.loads(reviewed_path.read_text(encoding="utf-8"))
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            inserted_process, inserted_association = record_process_analysis_sync(
                db_path=db_path,
                snapshot=snapshot,
                reviewed_analysis=reviewed,
            )
            process_records += inserted_process
            association_cases += inserted_association
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            failures += 1
    return {
        "artifacts": len(paths),
        "process_records": process_records,
        "association_cases": association_cases,
        "failures": failures,
    }


def list_evolution_contracts_sync(db_path: Path) -> list[dict[str, Any]]:
    _init_db_sync(db_path)
    with _open_sync(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT cv.id,cv.evolution_domain,cv.scope_key,cv.version,cv.parent_version,"
            "cv.status,cv.contract_hash,cv.source_batch_id,cv.created_at,cv.published_at,"
            "cv.published_by,CASE WHEN ac.contract_id=cv.id THEN 1 ELSE 0 END AS active "
            "FROM evolution_contract_versions cv LEFT JOIN evolution_active_contracts ac "
            "ON ac.contract_id=cv.id ORDER BY cv.created_at DESC,cv.id"
        ).fetchall()
    return [dict(row) for row in rows]


def list_association_hypotheses_sync(db_path: Path) -> list[dict[str, Any]]:
    _init_db_sync(db_path)
    with _open_sync(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id,version,parent_version,status,hypothesis_hash,source_batch_id,"
            "created_at,published_at,published_by FROM association_hypotheses "
            "ORDER BY created_at DESC,id"
        ).fetchall()
    return [dict(row) for row in rows]


def list_cross_layer_counts_sync(db_path: Path) -> dict[str, Any]:
    _init_db_sync(db_path)
    with _open_sync(db_path) as conn:
        process = conn.execute(
            "SELECT COUNT(*),COUNT(DISTINCT attempt_id),COUNT(DISTINCT env_name) "
            "FROM process_judge_records WHERE valid_execution=1"
        ).fetchone()
        association = conn.execute(
            "SELECT COUNT(*),COUNT(DISTINCT attempt_id),COUNT(DISTINCT env_name) "
            "FROM association_cases WHERE valid_evidence=1"
        ).fetchone()
        hypotheses = conn.execute(
            "SELECT COUNT(*) FROM association_hypotheses"
        ).fetchone()
    return {
        "process": {
            "valid_records": process[0],
            "unique_attempts": process[1],
            "environments": process[2],
        },
        "association": {
            "cases": association[0],
            "unique_attempts": association[1],
            "environments": association[2],
            "hypotheses": hypotheses[0],
        },
    }


def record_process_analysis_sync(
    *, db_path: Path, snapshot: dict[str, Any], reviewed_analysis: dict[str, Any]
) -> tuple[int, int]:
    """Append Process Judge records and aligned Product/Process cases.

    Idempotency is based on ``attempt_id + analysis_hash`` and ``case_hash``.
    """
    _init_db_sync(db_path)
    generator = reviewed_analysis.get("generator") or {}
    explicit_process_version = generator.get("process_rubric_version")
    if explicit_process_version:
        process_version = str(explicit_process_version)
        with _open_sync(db_path) as conn:
            known = conn.execute(
                "SELECT 1 FROM evolution_contract_versions "
                "WHERE evolution_domain='process' AND version=?",
                (process_version,),
            ).fetchone()
        if known is None:
            raise ValueError("Process artifact cites an unknown Rubric version")
        provenance = "explicit_process_rubric_version"
    else:
        process_version = register_legacy_process_contract_sync(
            db_path,
            pipeline_version=str(reviewed_analysis.get("pipeline_version") or ""),
            prompt_versions={
                "attempt": generator.get("attempt_prompt_version"),
                "comparison": generator.get("comparison_prompt_version"),
                "critic": generator.get("critic_prompt_version"),
            },
        )
        provenance = "legacy_inferred_from_frozen_prompt_versions"
    analysis_hash = str(reviewed_analysis["analysis_hash"])
    accepted = set(reviewed_analysis.get("accepted_claim_ids") or [])
    rejected = reviewed_analysis.get("rejected_claims") or []
    rejected_by_id: dict[str, list[dict[str, Any]]] = {}
    for item in rejected:
        if isinstance(item, dict):
            rejected_by_id.setdefault(str(item.get("claim_id") or ""), []).append(item)
    attempts_by_id = {
        str(item.get("attempt_id")): item
        for item in reviewed_analysis.get("attempt_analyses") or []
        if isinstance(item, dict)
    }
    now = _now_iso()
    process_inserted = 0
    association_inserted = 0
    with _open_sync(db_path) as conn:
        conn.row_factory = None
        for snapshot_attempt in snapshot.get("attempts") or []:
            metadata = snapshot_attempt.get("metadata") or {}
            attempt_id = str(metadata.get("id") or "")
            if not attempt_id:
                continue
            analysis = attempts_by_id.get(attempt_id, {"claims": []})
            claims = analysis.get("claims") or []
            claim_ids = {
                str(item.get("id")) for item in claims if isinstance(item, dict)
            }
            attempt_rejections = [
                item for claim_id in claim_ids for item in rejected_by_id.get(claim_id, [])
            ]
            record_hash = canonical_hash({
                "attempt_id": attempt_id,
                "analysis_hash": analysis_hash,
                "process_rubric_version": process_version,
            })
            record_id = "prec_" + record_hash.split(":")[-1][:24]
            process_metadata = {
                "snapshot_hash": snapshot.get("snapshot_hash"),
                "process_version_provenance": provenance,
                "comparison_claim_count": len(
                    (reviewed_analysis.get("comparison") or {}).get("claims") or []
                ),
            }
            cursor = conn.execute(
                "INSERT OR IGNORE INTO process_judge_records("
                "id,run_id,attempt_id,env_name,process_rubric_version,analysis_hash,"
                "valid_execution,claims_json,accepted_claim_ids_json,rejected_claims_json,"
                "metadata_json,created_at) VALUES(?,?,?,?,?,?,1,?,?,?,?,?)",
                (
                    record_id,
                    str(snapshot["run"]["id"]),
                    attempt_id,
                    str(snapshot["run"]["env_name"]),
                    process_version,
                    analysis_hash,
                    json.dumps(claims, ensure_ascii=False, sort_keys=True),
                    json.dumps(sorted(claim_ids & accepted), ensure_ascii=False),
                    json.dumps(attempt_rejections, ensure_ascii=False, sort_keys=True),
                    json.dumps(process_metadata, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            if cursor.rowcount == 0:
                existing = conn.execute(
                    "SELECT id,valid_execution,metadata_json FROM process_judge_records "
                    "WHERE attempt_id=? AND analysis_hash=?",
                    (attempt_id, analysis_hash),
                ).fetchone()
                existing_metadata = json.loads(existing[2] or "{}") if existing else {}
                if (
                    existing is not None and not bool(existing[1])
                    and existing_metadata.get("process_version_provenance")
                    == "quarantined_missing_provenance"
                ):
                    conn.execute(
                        "UPDATE process_judge_records SET process_rubric_version=?,"
                        "valid_execution=1,claims_json=?,accepted_claim_ids_json=?,"
                        "rejected_claims_json=?,metadata_json=? WHERE id=?",
                        (
                            process_version,
                            json.dumps(claims, ensure_ascii=False, sort_keys=True),
                            json.dumps(sorted(claim_ids & accepted), ensure_ascii=False),
                            json.dumps(attempt_rejections, ensure_ascii=False, sort_keys=True),
                            json.dumps(process_metadata, ensure_ascii=False, sort_keys=True),
                            existing[0],
                        ),
                    )
                    process_inserted += 1
            else:
                process_inserted += 1

            product_outcome = {
                "score_total": metadata.get("score_total"),
                "scores": snapshot_attempt.get("scores") or [],
                "status": metadata.get("status"),
                "execution_status": metadata.get("execution_status"),
                "scoring_status": metadata.get("scoring_status"),
            }
            process_findings = {
                "analysis_hash": analysis_hash,
                "claims": claims,
                "accepted_claim_ids": sorted(claim_ids & accepted),
                "rejected_claims": attempt_rejections,
            }
            controls = {
                "task_id": snapshot.get("task", {}).get("id"),
                "agent_name": metadata.get("agent_name"),
                "model": metadata.get("model"),
                "duration_ms": metadata.get("duration_ms"),
                "thinking_count": metadata.get("thinking_count"),
                "tool_call_count": metadata.get("tool_call_count"),
                "error_code": metadata.get("error_code"),
                "failure_kind": metadata.get("failure_kind"),
                "retryable": metadata.get("retryable"),
            }
            product_version_row = conn.execute(
                "SELECT rubric_version FROM attempts WHERE id=?", (attempt_id,)
            ).fetchone()
            product_version = product_version_row[0] if product_version_row else None
            case_payload = {
                "attempt_id": attempt_id,
                "product_rubric_version": product_version,
                "process_rubric_version": process_version,
                "product_outcome": product_outcome,
                "process_findings": process_findings,
                "controls": controls,
            }
            case_hash = canonical_hash(case_payload)
            case_id = "acase_" + case_hash.split(":")[-1][:24]
            cursor = conn.execute(
                "INSERT OR IGNORE INTO association_cases("
                "id,run_id,attempt_id,env_name,product_rubric_version,"
                "process_rubric_version,product_outcome_json,process_findings_json,"
                "controls_json,case_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    case_id,
                    str(snapshot["run"]["id"]),
                    attempt_id,
                    str(snapshot["run"]["env_name"]),
                    product_version,
                    process_version,
                    json.dumps(product_outcome, ensure_ascii=False, sort_keys=True),
                    json.dumps(process_findings, ensure_ascii=False, sort_keys=True),
                    json.dumps(controls, ensure_ascii=False, sort_keys=True),
                    case_hash,
                    now,
                ),
            )
            association_inserted += max(cursor.rowcount, 0)
        conn.commit()
    return process_inserted, association_inserted
