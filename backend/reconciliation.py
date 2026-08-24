"""Ordered, auditable research-state reconciliation that never executes agents."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from backend.db import _now_iso, _open_sync
from backend.experiments.leader import consume_score_outbox
from backend.experiments.recovery import rebuild_group_projection
from backend.experiments.robustness import build_robustness_snapshot


RECONCILIATION_VERSION = "octagon-reconciliation-v1"
TERMINAL_GROUPS = {"completed", "partial", "failed", "cancelled"}


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        "nonterminal_attempts": conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE status IN "
            "('queued','running','starting_blade_session')"
        ).fetchone()[0],
        "nonterminal_groups": conn.execute(
            "SELECT COUNT(*) FROM run_groups WHERE status IN ('queued','running')"
        ).fetchone()[0],
        "pending_score_transitions": conn.execute(
            "SELECT COUNT(*) FROM score_transition_outbox WHERE consumed_at IS NULL"
        ).fetchone()[0],
        "active_insights": conn.execute(
            "SELECT COUNT(*) FROM insight_reports WHERE status IN ('processing','running')"
        ).fetchone()[0],
        "active_normalizations": conn.execute(
            "SELECT COUNT(*) FROM normalized_outputs WHERE status IN ('processing','running')"
        ).fetchone()[0],
    }


def reconcile_research_state(
    *, db_path: Path, data_path: Path, apply: bool = False
) -> dict[str, Any]:
    """Inspect or repair derived state in dependency order without dispatching work."""

    db_path = Path(db_path)
    data_path = Path(data_path)
    with _open_sync(db_path) as conn:
        conn.row_factory = sqlite3.Row
        before = _counts(conn)
        group_ids = [
            row[0] for row in conn.execute(
                "SELECT id FROM run_groups ORDER BY created_at,id"
            ).fetchall()
        ]
        existing_hashes = {
            row["run_group_id"]: row["input_hash"]
            for row in conn.execute(
                "SELECT r.run_group_id,r.input_hash FROM robustness_snapshots r "
                "JOIN (SELECT run_group_id,MAX(created_at) created_at "
                "FROM robustness_snapshots GROUP BY run_group_id) latest "
                "ON latest.run_group_id=r.run_group_id AND latest.created_at=r.created_at"
            ).fetchall()
        }

    report: dict[str, Any] = {
        "schema_version": RECONCILIATION_VERSION,
        "mode": "apply" if apply else "dry-run",
        "started_at": _now_iso(),
        "before": before,
        "actions": [],
        "diagnostics": [],
        "agent_dispatches": 0,
    }
    if not apply:
        report["after"] = before
        return report

    # Stage 1 is deliberately diagnostic. The established attempt recovery owns
    # reattachment/interruption; reconciliation must never replay an agent.
    if before["nonterminal_attempts"]:
        report["diagnostics"].append({
            "code": "attempt_recovery_pending",
            "count": before["nonterminal_attempts"],
        })

    # Stage 2: relational projections only.
    for group_id in group_ids:
        status = rebuild_group_projection(db_path, group_id)
        report["actions"].append({
            "stage": "groups", "group_id": group_id, "status": status
        })

    with _open_sync(db_path) as conn:
        terminal_groups = [
            row[0] for row in conn.execute(
                "SELECT id FROM run_groups WHERE status IN "
                "('completed','partial','failed','cancelled') ORDER BY created_at,id"
            ).fetchall()
        ]

    # Stage 3: immutable score facts -> leader timeline, then terminal aggregates.
    consumed = consume_score_outbox(db_path)
    report["actions"].append({"stage": "leader", "consumed": consumed})
    for group_id in terminal_groups:
        try:
            snapshot = build_robustness_snapshot(db_path, group_id)
        except ValueError as exc:
            report["diagnostics"].append({
                "code": "aggregate_unavailable",
                "group_id": group_id,
                "detail": str(exc),
            })
            continue
        previous = existing_hashes.get(group_id)
        current = snapshot["input_hash"]
        if previous is not None and previous != current:
            report["diagnostics"].append({
                "code": "aggregate_input_hash_changed",
                "group_id": group_id,
                "previous_input_hash": previous,
                "current_input_hash": current,
                "resolution": "preserved old snapshot and appended derived snapshot",
            })
        report["actions"].append({
            "stage": "aggregate",
            "group_id": group_id,
            "input_hash": current,
        })

    # Stage 4: interrupted derived jobs become explicit failures. Their source
    # payloads remain untouched and may be regenerated through normal APIs.
    with _open_sync(db_path) as conn:
        insight_rows = conn.execute(
            "SELECT id FROM insight_reports WHERE status IN ('processing','running')"
        ).fetchall()
        normalized_rows = conn.execute(
            "SELECT id FROM normalized_outputs WHERE status IN ('processing','running')"
        ).fetchall()
        conn.execute(
            "UPDATE insight_reports SET status='failed' "
            "WHERE status IN ('processing','running')"
        )
        conn.execute(
            "UPDATE normalized_outputs SET status='failed',"
            "warnings_json='[\"interrupted during restart; regenerate explicitly\"]' "
            "WHERE status IN ('processing','running')"
        )
        conn.commit()
        after = _counts(conn)
    report["actions"].append({
        "stage": "derived_jobs",
        "insights_failed": len(insight_rows),
        "normalizations_failed": len(normalized_rows),
    })
    report["after"] = after
    report["completed_at"] = _now_iso()

    audit_dir = data_path / "reconciliation"
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_path = audit_dir / f"rec_{uuid.uuid4().hex}.json"
    audit_path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    report["audit_ref"] = audit_path.relative_to(data_path).as_posix()
    return report


def schedule_research_reconciliation(
    *, db_path: Path, data_path: Path
) -> asyncio.Task[Any] | None:
    """Wait for startup attempt/group tasks, then reconcile derived state once."""

    from backend import runtime_state

    # Capture the startup boundary. An empty deployment must not leave a task
    # that can race with a newly created Experiment after the app is ready.
    with _open_sync(Path(db_path)) as conn:
        has_state = conn.execute(
            "SELECT EXISTS(SELECT 1 FROM experiments) OR "
            "EXISTS(SELECT 1 FROM insight_reports WHERE status IN ('processing','running')) OR "
            "EXISTS(SELECT 1 FROM normalized_outputs WHERE status IN ('processing','running')) OR "
            "EXISTS(SELECT 1 FROM score_transition_outbox WHERE consumed_at IS NULL)"
        ).fetchone()[0]
    if not has_state:
        return None

    async def ordered() -> dict[str, Any]:
        state = runtime_state.get()
        current = asyncio.current_task()
        pending = [
            task
            for tasks in list(state.active_tasks.values())
            for task in list(tasks)
            if task is not current and not task.done()
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        return reconcile_research_state(
            db_path=db_path, data_path=data_path, apply=True
        )

    return asyncio.create_task(ordered())
