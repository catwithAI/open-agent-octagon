"""Normalize ordinary scoring outcomes into Rubric Evolution Judge Records."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from backend.db import _now_iso, _open_sync
from backend.experiments.hashing import canonical_hash

from .models import JudgeCheckRecord, JudgeRecord
from .store import append_judge_record_sync

_ALLOWED_RESULTS = {"pass", "partial", "fail", "not_applicable", "unknown"}


def _normalize_check(raw: dict[str, Any]) -> JudgeCheckRecord:
    check_id = str(raw.get("check_id") or raw.get("dimension") or "").strip()
    if not check_id:
        raise ValueError("score row has no check_id/dimension")
    maximum = float(raw.get("maximum", 100) or 100)
    explicit = str(raw.get("result") or "").strip().lower()
    if explicit and explicit not in _ALLOWED_RESULTS:
        raise ValueError(f"unsupported judge result: {explicit}")
    value = raw.get("value", raw.get("awarded"))
    if explicit:
        result = explicit
    else:
        numeric = float(value or 0)
        result = "pass" if numeric >= 100 else "fail" if numeric <= 0 else "partial"
    if result in {"unknown", "not_applicable"}:
        awarded = None
    elif "awarded" in raw:
        awarded = float(raw["awarded"])
    else:
        # Existing Octagon scorer dimensions are percentages on [0,100].
        awarded = maximum * float(value or 0) / 100.0
    evidence_refs = raw.get("evidence_refs") or []
    if isinstance(evidence_refs, str):
        evidence_refs = [evidence_refs]
    return JudgeCheckRecord(
        check_id=check_id,
        result=result,  # type: ignore[arg-type]
        awarded=awarded,
        maximum=maximum,
        evidence_refs=[str(item) for item in evidence_refs],
        unknown_reason=(raw.get("unknown_reason") if result == "unknown" else None),
        detail=str(raw.get("detail") or ""),
    )


def record_evaluation_outcome_sync(
    *,
    db_path: Path,
    attempt_id: str,
    scores: list[dict[str, Any]],
    evaluation_manifest: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> str:
    """Append one valid Judge Record without changing the official score path."""
    with _open_sync(db_path) as conn:
        row = conn.execute(
            "SELECT run_id,env_name,rubric_version,score_total,scoring_status "
            "FROM attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()
        committed_scores = conn.execute(
            "SELECT dimension,value FROM scores WHERE attempt_id=? ORDER BY id",
            (attempt_id,),
        ).fetchall()
        committed_revision = conn.execute(
            "SELECT score_revision,scorer_fingerprint,manifest_ref "
            "FROM score_transition_outbox WHERE attempt_id=? ORDER BY score_revision DESC LIMIT 1",
            (attempt_id,),
        ).fetchone()
    if row is None:
        raise ValueError(f"attempt not found: {attempt_id}")
    if (
        row[3] is None or row[4] != "completed" or not committed_scores
        or committed_revision is None
    ):
        raise ValueError("official scoring result is not committed")
    expected = Counter((str(item[0]), int(item[1])) for item in committed_scores)
    supplied = Counter(
        (str(item.get("dimension") or item.get("check_id") or ""), int(item.get("value", 0) or 0))
        for item in scores
    )
    if supplied != expected:
        raise ValueError("Judge Record scores do not match the committed official result")
    manifest_hash = canonical_hash(evaluation_manifest)
    if str(committed_revision[1]) != manifest_hash:
        raise ValueError("evaluation manifest does not match the committed official result")
    rubric_version = row[2] or (
        "manifest:" + manifest_hash.split(":")[-1][:24]
    )
    operation_id = "official-score:" + canonical_hash({
        "attempt_id": attempt_id,
        "score_revision": int(committed_revision[0]),
        "scorer_fingerprint": str(committed_revision[1]),
        "manifest_ref": str(committed_revision[2]),
    }).split(":")[-1]
    operation_suffix = operation_id.split(":")[-1][:24]
    execution_id = f"judge_{operation_suffix}"
    record = JudgeRecord(
        record_id=f"jrec_{operation_suffix}",
        env_name=str(row[1]),
        rubric_version=str(rubric_version),
        run_id=str(row[0]),
        attempt_id=attempt_id,
        judge_execution_id=execution_id,
        created_at=_now_iso(),
        valid_execution=True,
        checks=[_normalize_check(item) for item in scores],
        metadata={
            "evaluation_manifest_hash": manifest_hash,
            "official_score_revision": int(committed_revision[0]),
            "operation_id": operation_id,
            **(metadata or {}),
        },
    )
    return append_judge_record_sync(db_path, record, operation_id=operation_id)


def project_historical_score_sync(
    *,
    db_path: Path,
    attempt_id: str,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Project one historical official score into a Judge Record.

    Reads ``attempts`` and ``scores`` only. It never updates those tables, never
    invents an outbox row, and never writes ``attempts.rubric_version``. Missing
    current-contract fields are recorded as projection provenance.
    """
    with _open_sync(db_path) as conn:
        existing = conn.execute(
            "SELECT id FROM rubric_judge_records WHERE attempt_id=? LIMIT 1",
            (attempt_id,),
        ).fetchone()
        if existing is not None:
            return str(existing[0])
        row = conn.execute(
            "SELECT run_id,env_name,rubric_version,score_total,scoring_status,"
            "scoring_ended_at,ended_at FROM attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()
        committed_scores = conn.execute(
            "SELECT dimension,value,detail FROM scores WHERE attempt_id=? ORDER BY id",
            (attempt_id,),
        ).fetchall()
        outbox = conn.execute(
            "SELECT score_revision,scorer_fingerprint,manifest_ref "
            "FROM score_transition_outbox WHERE attempt_id=? "
            "ORDER BY score_revision DESC LIMIT 1",
            (attempt_id,),
        ).fetchone()
    if row is None:
        raise ValueError(f"attempt not found: {attempt_id}")
    if row[3] is None or row[4] != "completed" or not committed_scores:
        raise ValueError("historical attempt has no committed official score")
    scores = [
        {"dimension": str(item[0]), "value": item[1], "detail": item[2] or ""}
        for item in committed_scores
    ]
    score_fingerprint = canonical_hash({
        "attempt_id": attempt_id,
        "scores": [(str(item[0]), int(item[1] or 0)) for item in committed_scores],
        "score_total": row[3],
    })
    missing = []
    if not row[2]:
        missing.append("attempts.rubric_version")
    if outbox is None:
        missing.append("score_transition_outbox")
    rubric_version = row[2] or ("historical-score:" + score_fingerprint.split(":")[-1][:24])
    operation_id = "historical-score:" + score_fingerprint.split(":")[-1]
    operation_suffix = operation_id.split(":")[-1][:24]
    record = JudgeRecord(
        record_id=f"jrec_{operation_suffix}",
        env_name=str(row[1]),
        rubric_version=str(rubric_version),
        run_id=str(row[0]),
        attempt_id=attempt_id,
        judge_execution_id=f"hist_{operation_suffix}",
        created_at=_now_iso(),
        valid_execution=True,
        checks=[_normalize_check(item) for item in scores],
        metadata={
            "projection": "historical_official_score_v1",
            "source_tables": ["attempts", "scores"],
            "missing_current_fields": missing,
            "historical_score_total": row[3],
            "historical_scoring_ended_at": row[5] or row[6],
            "outbox_revision": None if outbox is None else int(outbox[0]),
            "outbox_fingerprint": None if outbox is None else str(outbox[1]),
            "operation_id": operation_id,
            **(metadata or {}),
        },
    )
    return append_judge_record_sync(db_path, record, operation_id=operation_id)


def project_historical_scores_sync(
    *,
    db_path: Path,
    env_name: str | None = None,
    limit: int = 10000,
) -> dict[str, int]:
    """Project completed historical scores that still lack a Judge Record."""
    clauses = [
        "a.scoring_status='completed'",
        "a.score_total IS NOT NULL",
        "EXISTS (SELECT 1 FROM scores s WHERE s.attempt_id=a.id)",
        "NOT EXISTS (SELECT 1 FROM rubric_judge_records r WHERE r.attempt_id=a.id)",
    ]
    params: list[Any] = []
    if env_name:
        clauses.append("a.env_name=?")
        params.append(env_name)
    params.append(max(1, min(int(limit), 100000)))
    with _open_sync(db_path) as conn:
        attempt_ids = [
            str(row[0]) for row in conn.execute(
                "SELECT a.id FROM attempts a WHERE " + " AND ".join(clauses) +
                " ORDER BY a.created_at,a.id LIMIT ?",
                tuple(params),
            ).fetchall()
        ]
    inserted = 0
    reused = 0
    for attempt_id in attempt_ids:
        record_id = project_historical_score_sync(db_path=db_path, attempt_id=attempt_id)
        with _open_sync(db_path) as conn:
            meta = json.loads(conn.execute(
                "SELECT metadata_json FROM rubric_judge_records WHERE id=?",
                (record_id,),
            ).fetchone()[0] or "{}")
        if meta.get("projection") == "historical_official_score_v1":
            inserted += 1
        else:
            reused += 1
    return {
        "candidates": len(attempt_ids),
        "inserted": inserted,
        "reused": reused,
    }
