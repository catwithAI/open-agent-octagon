"""RunGroup state projection and idempotent startup recovery."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

from backend import runtime_state
from backend.db import _now_iso, _open_sync
from backend.models import NON_TERMINAL_ATTEMPT_STATUSES

from .coordinator import run_group
from .repository import ExperimentRepository


def _attempt_projection(rows: list[sqlite3.Row]) -> tuple[str, str | None]:
    if not rows:
        return "failed", "attempts_missing"
    if any(row["status"] in NON_TERMINAL_ATTEMPT_STATUSES for row in rows):
        return "running", None
    if any(row["status"] == "scoring" for row in rows):
        return "scoring", None
    # 用户主动停止是独立终态：所有 attempt 都被取消时 cell 投影
    # cancelled，不能落到下面的 failed 分支——那会把一次操作决定报成执行失败。
    # 只要还有**真实**失败或产物，仍按原规则走 partial/failed，取消不掩盖事实。
    if all(row["status"] == "cancelled" for row in rows):
        return "cancelled", "user_stopped"
    score_available = [
        row
        for row in rows
        if row["status"] in {"completed", "gave_up"}
        and row["score_total"] is not None
    ]
    if len(score_available) == len(rows):
        return "completed", None
    output_available = [
        row
        for row in rows
        if row["status"] in {"completed", "gave_up", "scoring_failed"}
    ]
    if score_available or output_available:
        failed = next((row for row in rows if row not in score_available), None)
        code = None if failed is None else failed["error_code"] or failed["status"]
        return "partial", code
    failed = rows[0]
    return "failed", failed["error_code"] or failed["status"]


def project_cell_for_run(db_path: Path, run_id: str) -> str | None:
    """Project one relational cell; terminal cancellation is never overwritten."""
    with _open_sync(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cell = conn.execute(
            "SELECT id,status,run_group_id FROM run_group_cells WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if cell is None:
            return None
        if cell["status"] == "cancelled":
            return "cancelled"
        attempts = conn.execute(
            "SELECT id,status,score_total,error_code,failure_kind FROM attempts "
            "WHERE run_id=? ORDER BY created_at,id",
            (run_id,),
        ).fetchall()
        status, error_code = _attempt_projection(attempts)
        conn.execute(
            "UPDATE run_group_cells SET status=?,error_code=?,updated_at=? WHERE id=?",
            (status, error_code, _now_iso(), cell["id"]),
        )
        if status != cell["status"]:
            ExperimentRepository.append_group_event(
                conn,
                cell["run_group_id"],
                "cell.projected",
                {
                    "cell_id": cell["id"],
                    "status": status,
                    "run_id": run_id,
                    "error_code": error_code,
                },
            )
        conn.commit()
    rebuild_group_projection(db_path, cell["run_group_id"])
    return status


def _group_status(statuses: list[str], previous: str) -> str:
    if not statuses:
        return "queued"
    if any(status in {"provisioning", "running"} for status in statuses):
        return "running"
    if any(status == "scoring" for status in statuses):
        return "scoring"
    if any(status == "queued" for status in statuses):
        return "queued" if all(status == "queued" for status in statuses) else "running"
    if all(status == "cancelled" for status in statuses):
        return "cancelled"
    if all(status == "completed" for status in statuses):
        return "completed"
    if any(status in {"completed", "partial"} for status in statuses):
        return "partial"
    if previous == "cancelled" and all(
        status in {"failed", "cancelled"} for status in statuses
    ):
        return "cancelled"
    return "failed"


def _project_experiment(conn: sqlite3.Connection, experiment_id: str) -> None:
    row = conn.execute(
        "SELECT status FROM run_groups WHERE experiment_id=? "
        "ORDER BY CASE WHEN status IN ('queued','running') THEN 0 ELSE 1 END,"
        "created_at DESC,id DESC LIMIT 1",
        (experiment_id,),
    ).fetchone()
    if row is not None:
        conn.execute(
            "UPDATE experiments SET status=?,updated_at=? WHERE id=?",
            (row[0], _now_iso(), experiment_id),
        )


def rebuild_group_projection(db_path: Path, group_id: str) -> str | None:
    with _open_sync(db_path) as conn:
        conn.row_factory = sqlite3.Row
        group = conn.execute(
            "SELECT experiment_id,status FROM run_groups WHERE id=?", (group_id,)
        ).fetchone()
        if group is None:
            return None
        statuses = [
            row[0]
            for row in conn.execute(
                "SELECT status FROM run_group_cells WHERE run_group_id=?",
                (group_id,),
            ).fetchall()
        ]
        status = _group_status(statuses, group["status"])
        counts = {name: statuses.count(name) for name in (
            "completed", "partial", "failed", "cancelled"
        )}
        ended_at = _now_iso() if status in {
            "completed", "partial", "failed", "cancelled"
        } else None
        conn.execute(
            "UPDATE run_groups SET status=?,completed_cells=?,partial_cells=?,"
            "failed_cells=?,cancelled_cells=?,ended_at=CASE WHEN ? IS NULL THEN "
            "ended_at ELSE COALESCE(ended_at,?) END WHERE id=?",
            (
                status,
                counts["completed"],
                counts["partial"],
                counts["failed"],
                counts["cancelled"],
                ended_at,
                ended_at,
                group_id,
            ),
        )
        if status != group["status"]:
            ExperimentRepository.append_group_event(
                conn,
                group_id,
                "group.projected",
                {"status": status, "counts": counts},
            )
        _project_experiment(conn, group["experiment_id"])
        conn.commit()
    return status


def recover_groups_sync(db_path: Path) -> tuple[str, ...]:
    """Repair orphan provisioning and rebuild projections; safe to repeat."""
    with _open_sync(db_path) as conn:
        orphans = conn.execute(
            "SELECT id,run_group_id FROM run_group_cells "
            "WHERE status='provisioning' AND run_id IS NULL"
        ).fetchall()
        conn.execute(
            "UPDATE run_group_cells SET status='queued',error_code=NULL,updated_at=? "
            "WHERE status='provisioning' AND run_id IS NULL",
            (_now_iso(),),
        )
        for cell_id, group_id in orphans:
            ExperimentRepository.append_group_event(
                conn,
                group_id,
                "cell.recovered",
                {"cell_id": cell_id, "status": "queued"},
            )
        run_ids = [
            row[0]
            for row in conn.execute(
                "SELECT run_id FROM run_group_cells WHERE run_id IS NOT NULL "
                "AND status IN ('provisioning','running')"
            ).fetchall()
        ]
        group_ids = [
            row[0]
            for row in conn.execute(
                "SELECT id FROM run_groups ORDER BY created_at,id"
            ).fetchall()
        ]
        conn.commit()
    for run_id in run_ids:
        project_cell_for_run(db_path, run_id)
    resumable: list[str] = []
    for group_id in group_ids:
        status = rebuild_group_projection(db_path, group_id)
        if status in {"queued", "running"}:
            with _open_sync(db_path) as conn:
                queued = conn.execute(
                    "SELECT 1 FROM run_group_cells WHERE run_group_id=? "
                    "AND status='queued' LIMIT 1",
                    (group_id,),
                ).fetchone()
            if queued is not None:
                resumable.append(group_id)
    return tuple(resumable)


def schedule_run_group_recovery(settings: Any) -> int:
    state = runtime_state.get()
    group_ids = recover_groups_sync(state.db_path)
    for group_id in group_ids:
        async def resume(recovery_group_id: str = group_id) -> None:
            try:
                with _open_sync(state.db_path) as conn:
                    run_ids = [
                        row[0]
                        for row in conn.execute(
                            "SELECT run_id FROM run_group_cells WHERE run_group_id=? "
                            "AND run_id IS NOT NULL",
                            (recovery_group_id,),
                        ).fetchall()
                    ]
                pending = [
                    task
                    for run_id in run_ids
                    for task in state.active_tasks.get(run_id, [])
                    if not task.done()
                ]
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                rebuild_group_projection(state.db_path, recovery_group_id)
                await run_group(settings, recovery_group_id)
            finally:
                state.active_tasks.pop(f"group:{recovery_group_id}", None)

        task = asyncio.create_task(resume())
        state.active_tasks.setdefault(f"group:{group_id}", []).append(task)
    return len(group_ids)
