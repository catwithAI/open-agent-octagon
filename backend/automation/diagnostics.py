"""Deterministic first-pass failure diagnosis.

This is deliberately not an LLM judge.  It converts terminal attempt facts into a
versioned, replayable diagnosis with an owner, confidence, evidence references and
a bounded next action.  Unknown evidence remains unknown instead of being forced
into an agent failure.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from backend.db import _now_iso, _open_sync

DIAGNOSIS_VERSION = "octagon-diagnosis-v1"


def _base_evidence(attempt: sqlite3.Row, data_path: Path) -> list[str]:
    attempt_id = str(attempt["id"])
    refs = [
        f"db:attempts/{attempt_id}#status",
        f"db:attempts/{attempt_id}#error_code",
        f"db:attempts/{attempt_id}#execution_status",
        f"db:attempts/{attempt_id}#scoring_status",
    ]
    attempt_dir = data_path / "attempts" / attempt_id
    for name in ("trace.jsonl", "events.jsonl", "blade_events.jsonl", "wire-manifest.json"):
        if (attempt_dir / name).is_file():
            refs.append(f"file:attempts/{attempt_id}/{name}")
    return refs


def _contains(value: str | None, *needles: str) -> bool:
    text = str(value or "").lower()
    return any(needle.lower() in text for needle in needles)


def diagnose_attempt(
    *, db_path: Path, data_path: Path, attempt_id: str
) -> dict[str, Any] | None:
    with _open_sync(db_path) as conn:
        conn.row_factory = sqlite3.Row
        attempt = conn.execute(
            "SELECT id,run_id,status,error_code,error_message,failure_kind,retryable,"
            "execution_status,scoring_status,event_count,tool_call_count FROM attempts "
            "WHERE id=?",
            (attempt_id,),
        ).fetchone()
    if attempt is None:
        return None

    status = str(attempt["status"] or "")
    error_code = str(attempt["error_code"] or "")
    error_message = str(attempt["error_message"] or "")
    evidence = _base_evidence(attempt, data_path)
    symptoms: list[str] = []
    if status:
        symptoms.append(f"terminal_status:{status}")
    if error_code:
        symptoms.append(f"error_code:{error_code}")
    if attempt["event_count"] == 0:
        symptoms.append("no_adapter_events")
    if attempt["tool_call_count"] == 0:
        symptoms.append("no_tool_calls")

    owner = "unknown"
    confidence = 0.55
    action = "manual_evidence_review"
    reason = "no deterministic rule matched the available evidence"

    if status == "cancelled":
        owner, confidence = "protocol", 1.0
        action = "do_not_retry_without_new_user_intent"
        reason = "the run was explicitly cancelled"
    elif status == "scoring_failed":
        owner, confidence = "evaluator", 0.99
        action = "replay_scorer_from_frozen_input"
        reason = "candidate execution ended, but the scoring stage failed"
    elif status in {"input_snapshot_missing", "model_integrity_failed"}:
        owner, confidence = "protocol", 0.99
        action = "repair_protocol_or_snapshot_then_replay"
        reason = "the frozen comparison contract was unavailable or violated"
    elif status == "session_create_failed" and _contains(
        error_message, "skill", "registry", "not found"
    ):
        owner, confidence = "protocol", 0.98
        action = "validate_agent_skill_registry_before_dispatch"
        reason = "the requested primary skill was not available in the remote registry"
    elif status in {
        "auth_failed", "cli_not_found", "server_unreachable",
        "provider_quota_exhausted", "blade_service_unavailable",
        "capture_infrastructure_failed", "session_socket_overflow",
        "sandbox_unavailable",
    } or error_code in {
        "adapter_crashed", "cli_launch_error", "network_error", "quota_exhausted",
        "upstream_error", "upstream_rate_limited", "upstream_unavailable",
    }:
        owner, confidence = "infrastructure", 0.97
        action = (
            "retry_with_backoff_after_health_check"
            if bool(attempt["retryable"])
            else "repair_infrastructure_before_retry"
        )
        reason = "the failure occurred in an adapter, provider, transport or capture dependency"
    elif status in {"timeout", "gave_up"}:
        owner, confidence = "candidate", 0.9
        action = "inspect_trajectory_and_create_regression_case"
        reason = "the candidate failed to complete within the frozen execution contract"
    elif status in {"cli_error", "chat_failed", "interrupted"}:
        # A generic CLI/chat terminal can be caused by either the candidate loop or
        # its provider.  Without a stable structured error code we must not force it
        # into candidate/infrastructure.
        owner, confidence = "unknown", 0.65
        action = "inspect_adapter_events_before_retry"
        reason = "the terminal status is ambiguous without a structured upstream cause"
    elif status == "completed":
        owner, confidence = "none", 1.0
        action = "no_failure_action"
        reason = "the attempt completed without a terminal failure"

    return {
        "schema_version": DIAGNOSIS_VERSION,
        "attempt_id": attempt_id,
        "run_id": attempt["run_id"],
        "owner": owner,
        "confidence": confidence,
        "reason": reason,
        "symptoms": symptoms,
        "retryable": bool(attempt["retryable"]),
        "recommended_action": action,
        "evidence_refs": evidence,
        "diagnosed_at": _now_iso(),
    }


def diagnosis_path(data_path: Path, attempt_id: str) -> Path:
    return data_path / "attempts" / attempt_id / "automation" / "diagnosis.json"


def diagnose_and_persist(
    *, db_path: Path, data_path: Path, attempt_id: str
) -> dict[str, Any] | None:
    """Persist derived diagnosis as an artifact, not another DB status axis."""
    diagnosis = diagnose_attempt(
        db_path=db_path, data_path=data_path, attempt_id=attempt_id
    )
    if diagnosis is None:
        return None
    path = diagnosis_path(data_path, attempt_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(diagnosis, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return diagnosis


def read_diagnosis(data_path: Path, attempt_id: str) -> dict[str, Any] | None:
    path = diagnosis_path(data_path, attempt_id)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None
