"""Transactional append-only feedback repository."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.db import (
    IdempotencyConflict,
    IdempotencyInProgress,
    _now_iso,
    _open_sync,
    claim_idempotency,
    complete_idempotency,
)
from backend.experiments.hashing import canonical_hash, new_id

from .models import FeedbackCreate, ResearchFeedback


class FeedbackConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class FeedbackRepository:
    db_path: Path

    def append(self, request: FeedbackCreate, *, idempotency_key: str) -> ResearchFeedback:
        request_hash = canonical_hash(request)
        now = _now_iso()
        feedback_id = new_id("feedback")
        try:
            with _open_sync(Path(self.db_path)) as conn:
                conn.row_factory = sqlite3.Row
                claim = claim_idempotency(
                    conn,
                    operation="research-feedback:append",
                    key=idempotency_key,
                    request_hash=request_hash,
                )
                if claim.replayed:
                    return self._from_row(
                        conn.execute(
                            "SELECT * FROM research_feedback WHERE id=?",
                            (claim.result_id,),
                        ).fetchone()
                    )
                table_by_target = {
                    "profile-recommendation": "profile_recommendations",
                    "insight": "insight_reports",
                    "experiment-annotation": "experiments",
                }
                table = table_by_target.get(request.target_type)
                if table is not None and conn.execute(
                    f"SELECT 1 FROM {table} WHERE id=?", (request.target_id,)
                ).fetchone() is None:
                    raise FeedbackConflict("feedback_target_not_found")
                if request.target_type == "profile" and not request.scope.get("profile_hash"):
                    raise FeedbackConflict("profile_feedback_requires_frozen_hash")
                if request.supersedes:
                    previous = conn.execute(
                        "SELECT target_type,target_id,scope_json FROM research_feedback WHERE id=?",
                        (request.supersedes,),
                    ).fetchone()
                    if previous is None:
                        raise FeedbackConflict("superseded_feedback_not_found")
                    if (
                        previous["target_type"] != request.target_type
                        or previous["target_id"] != request.target_id
                        or json.loads(previous["scope_json"]) != request.scope
                    ):
                        raise FeedbackConflict("supersedes_target_mismatch")
                conn.execute(
                    "INSERT INTO research_feedback(id,target_type,target_id,scope_json,signal,"
                    "reason,rationale,actor,supersedes,schema_version,producer_version,"
                    "input_refs_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,"
                    "'octagon-feedback-v1','octagon-feedback-repository-v1','{}',?)",
                    (
                        feedback_id,
                        request.target_type,
                        request.target_id,
                        json.dumps(request.scope, sort_keys=True),
                        request.signal,
                        request.reason,
                        request.rationale,
                        request.actor,
                        request.supersedes,
                        now,
                    ),
                )
                response = {"id": feedback_id}
                complete_idempotency(
                    conn,
                    operation="research-feedback:append",
                    key=idempotency_key,
                    result_id=feedback_id,
                    response=response,
                )
                row = conn.execute(
                    "SELECT * FROM research_feedback WHERE id=?", (feedback_id,)
                ).fetchone()
                conn.commit()
        except IdempotencyConflict as exc:
            raise FeedbackConflict("idempotency_conflict") from exc
        except IdempotencyInProgress as exc:
            raise FeedbackConflict("idempotency_in_progress") from exc
        return self._from_row(row)

    def export(
        self,
        *,
        target_type: str | None = None,
        target_id: str | None = None,
        include_rationale: bool = False,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if target_type:
            clauses.append("target_type=?")
            params.append(target_type)
        if target_id:
            clauses.append("target_id=?")
            params.append(target_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with _open_sync(Path(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT * FROM research_feedback{where} ORDER BY created_at,id",
                params,
            ).fetchall()
        result = []
        for row in rows:
            item = self._from_row(row).model_dump(mode="json")
            if not include_rationale:
                item["rationale"] = None
            result.append(item)
        return result

    @staticmethod
    def _from_row(row: sqlite3.Row | None) -> ResearchFeedback:
        if row is None:
            raise FeedbackConflict("feedback_not_found")
        return ResearchFeedback(
            schema_version="octagon-feedback-v1",
            id=row["id"],
            target_type=row["target_type"],
            target_id=row["target_id"],
            scope=json.loads(row["scope_json"]),
            signal=row["signal"],
            reason=row["reason"],
            rationale=row["rationale"],
            actor=row["actor"],
            supersedes=row["supersedes"],
            created_at=row["created_at"],
        )
