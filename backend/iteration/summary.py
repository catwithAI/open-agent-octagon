"""iteration 事件读取、尾行容错和 Attempt 公开摘要投影。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .writer import ITERATIONS_FILENAME


def read_iteration_events(path: Path) -> tuple[list[dict[str, Any]], bool]:
    path = Path(path)
    if not path.is_file():
        return [], False

    events: list[dict[str, Any]] = []
    seen_operations: set[str] = set()
    partial = False
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                partial = True
                break
            if not isinstance(record, dict):
                partial = True
                break
            operation = record.get("operation_id")
            if isinstance(operation, str) and operation:
                if operation in seen_operations:
                    continue
                seen_operations.add(operation)
            events.append(record)
    return events, partial


def _submission_projection(record: dict[str, Any]) -> dict[str, Any]:
    round_index = int(record["round_index"])
    feedback_required = bool(record.get("feedback_required", False))
    return {
        "submission_id": str(record["submission_id"]),
        "round_index": round_index,
        "producer_session_id": record.get("producer_session_id"),
        "created_at": record.get("timestamp"),
        "snapshot_status": "pending",
        "evaluation_status": "pending",
        "score_total": None,
        "feedback_status": "pending" if feedback_required else "not_required",
        "requested_change_count": 0,
        "resolved_problem_count": 0,
        "regression_count": 0,
        "snapshot_manifest_hash": None,
        "selected_for_final_score": False,
    }


def summarize_iteration(attempt_dir: Path) -> dict[str, Any] | None:
    events, partial = read_iteration_events(
        Path(attempt_dir) / ITERATIONS_FILENAME
    )
    if not events:
        return None

    phase = "preparing"
    submission_count: int | None = None
    max_iterations: int | None = None
    session_continuity = "unknown"
    submissions: dict[str, dict[str, Any]] = {}
    failed: dict[str, Any] | None = None
    completed = False

    for event in events:
        event_name = str(event.get("event") or "")
        submission_id = event.get("submission_id")

        if event_name == "iteration.started":
            submission_count = int(event.get("submission_count") or 0) or None
            max_iterations = int(event.get("max_iterations") or 0) or submission_count
            phase = "agent_running"
            session_continuity = str(
                event.get("session_continuity") or session_continuity
            )
        elif event_name == "submission.created":
            if isinstance(submission_id, str) and submission_id:
                submissions[submission_id] = _submission_projection(event)
            phase = "submission_snapshotting"
        elif event_name == "submission.snapshot_completed":
            if isinstance(submission_id, str) and submission_id in submissions:
                submissions[submission_id]["snapshot_status"] = "completed"
                submissions[submission_id]["snapshot_manifest_hash"] = event.get(
                    "snapshot_manifest_hash"
                )
            phase = "submission_scoring"
        elif event_name == "submission.snapshot_failed":
            if isinstance(submission_id, str) and submission_id in submissions:
                submissions[submission_id]["snapshot_status"] = "failed"
            phase = "failed"
            failed = event
        elif event_name == "submission.evaluation_started":
            if isinstance(submission_id, str) and submission_id in submissions:
                submissions[submission_id]["evaluation_status"] = "running"
            phase = "submission_scoring"
        elif event_name == "submission.evaluation_completed":
            if isinstance(submission_id, str) and submission_id in submissions:
                submissions[submission_id]["evaluation_status"] = "completed"
                submissions[submission_id]["score_total"] = event.get("score_total")
            phase = (
                "feedback_generating"
                if bool(event.get("feedback_required", False))
                else "finalizing"
            )
        elif event_name == "submission.evaluation_failed":
            if isinstance(submission_id, str) and submission_id in submissions:
                submissions[submission_id]["evaluation_status"] = "failed"
            phase = "failed"
            failed = event
        elif event_name == "submission.feedback_ready":
            if isinstance(submission_id, str) and submission_id in submissions:
                submissions[submission_id]["feedback_status"] = "ready"
                submissions[submission_id]["requested_change_count"] = int(
                    event.get("requested_change_count") or 0
                )
                submissions[submission_id]["resolved_problem_count"] = int(
                    event.get("resolved_problem_count") or 0
                )
                submissions[submission_id]["regression_count"] = int(
                    event.get("regression_count") or 0
                )
            phase = "feedback_generating"
        elif event_name == "submission.feedback_sending":
            if isinstance(submission_id, str) and submission_id in submissions:
                submissions[submission_id]["feedback_status"] = "sending"
            phase = "feedback_sending"
        elif event_name == "submission.feedback_delivered":
            if isinstance(submission_id, str) and submission_id in submissions:
                submissions[submission_id]["feedback_status"] = "delivered"
            phase = "agent_running"
        elif event_name == "submission.selected_for_final_score":
            if isinstance(submission_id, str) and submission_id in submissions:
                submissions[submission_id]["selected_for_final_score"] = True
            phase = "finalizing"
        elif event_name == "iteration.completed":
            phase = "completed"
            completed = True
        elif event_name == "iteration.failed":
            phase = "failed"
            failed = event

    ordered = sorted(submissions.values(), key=lambda row: row["round_index"])
    completed_evaluations = sum(
        row["evaluation_status"] == "completed" for row in ordered
    )
    scored = [row for row in ordered if isinstance(row.get("score_total"), int)]
    first_score = scored[0]["score_total"] if scored else None
    final_rows = [row for row in ordered if row["selected_for_final_score"]]
    final_score = final_rows[0].get("score_total") if len(final_rows) == 1 else None
    requested_changes = sum(row["requested_change_count"] for row in ordered)
    resolved_problems = sum(row["resolved_problem_count"] for row in ordered)
    regressions = sum(row["regression_count"] for row in ordered)
    return {
        "mode": "iterative_product_review",
        "phase": phase,
        "submission_count": submission_count,
        "max_iterations": max_iterations,
        "current_round": ordered[-1]["round_index"] if ordered else None,
        "completed_submission_count": completed_evaluations,
        "session_continuity": session_continuity,
        "submissions": ordered,
        "completed": completed,
        "failed": failed,
        "partial": partial,
        "metrics": {
            "first_score": first_score,
            "final_score": final_score,
            "absolute_improvement": (
                final_score - first_score
                if isinstance(first_score, int) and isinstance(final_score, int)
                else None
            ),
            "resolved_problem_count": resolved_problems,
            "regression_count": regressions,
            "feedback_adoption_rate": (
                resolved_problems / requested_changes if requested_changes else None
            ),
        },
    }


def public_iteration_summary(attempt_dir: Path) -> dict[str, Any] | None:
    summary = summarize_iteration(attempt_dir)
    if summary is None:
        return None
    public = dict(summary)
    hidden = {
        "score_total",
        "requested_change_count",
        "resolved_problem_count",
        "regression_count",
    }
    public["submissions"] = [
        {key: value for key, value in item.items() if key not in hidden}
        for item in summary["submissions"]
    ]
    return public
