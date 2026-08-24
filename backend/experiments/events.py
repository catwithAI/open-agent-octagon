"""Redacted RunGroup snapshots and versioned event envelopes."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from backend.db import _open_sync

EVENT_SCHEMA_VERSION = "octagon-run-group-event-v1"


def group_snapshot(
    db_path: Path, group_id: str, *, experiment_id: str | None = None
) -> dict[str, Any] | None:
    with _open_sync(db_path) as conn:
        conn.row_factory = sqlite3.Row
        params: tuple[Any, ...] = (group_id,)
        predicate = "g.id=?"
        if experiment_id is not None:
            predicate += " AND g.experiment_id=?"
            params = (group_id, experiment_id)
        group = conn.execute(
            "SELECT g.id,g.experiment_id,g.strategy,g.status,g.total_cells,"
            "g.completed_cells,g.partial_cells,g.failed_cells,g.cancelled_cells,"
            "g.created_at,g.started_at,g.ended_at,g.error_code,g.error_message "
            f"FROM run_groups g WHERE {predicate}",
            params,
        ).fetchone()
        if group is None:
            return None
        cells = conn.execute(
            "SELECT c.id,c.variant_id,c.repeat_index,c.run_id,c.status,c.error_code,"
            "v.kind,v.mutator_id,v.mutator_version,v.seed,v.source_hash,v.content_hash "
            "FROM run_group_cells c JOIN task_variants v ON v.id=c.variant_id "
            "WHERE c.run_group_id=? ORDER BY c.created_at,c.id",
            (group_id,),
        ).fetchall()
        leaders = conn.execute(
            "SELECT l.id AS event_id,l.scope_key,l.sequence,l.metric,"
            "l.previous_attempt_id,l.current_attempt_id,l.previous_value,"
            "l.current_value,l.delta,l.reason,l.provisional,"
            "l.source_score_fingerprint,l.producer_version,l.source_outbox_id,"
            "l.created_at FROM leader_events l "
            "JOIN (SELECT scope_key,MAX(sequence) AS sequence FROM leader_events "
            "WHERE run_group_id=? GROUP BY scope_key) latest "
            "ON latest.scope_key=l.scope_key AND latest.sequence=l.sequence "
            "WHERE l.run_group_id=? ORDER BY l.scope_key",
            (group_id, group_id),
        ).fetchall()
        cursor = conn.execute(
            "SELECT COALESCE(MAX(sequence),0) FROM run_group_events WHERE run_group_id=?",
            (group_id,),
        ).fetchone()[0]
    return {
        "schema_version": "octagon-run-group-snapshot-v1",
        "cursor": int(cursor),
        "group": dict(group),
        # Deliberately excludes prompt_ref, context_delta and input payload refs.
        "cells": [dict(cell) for cell in cells],
        "leaders": [
            {
                **dict(leader),
                "schema_version": "octagon-leader-event-v1",
                "provisional": bool(leader["provisional"]),
            }
            for leader in leaders
        ],
    }


def group_events(
    db_path: Path, group_id: str, *, after: int = 0
) -> list[dict[str, Any]]:
    with _open_sync(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT sequence,event_type,payload_json,created_at FROM run_group_events "
            "WHERE run_group_id=? AND sequence>? ORDER BY sequence",
            (group_id, after),
        ).fetchall()
    return [
        {
            "schema_version": EVENT_SCHEMA_VERSION,
            "sequence": row["sequence"],
            "event_type": row["event_type"],
            "created_at": row["created_at"],
            "data": json.loads(row["payload_json"] or "{}"),
        }
        for row in rows
    ]
