"""Durable leader evaluation driven exclusively by the score outbox."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from backend.db import _now_iso, _open_sync
from backend.models import TERMINAL_ATTEMPT_STATUSES

from .hashing import content_id
from .repository import ExperimentRepository

PRODUCER_VERSION = "rc-a02-v1"


def _rank(row: sqlite3.Row, tie_break: str) -> tuple[Any, ...]:
    if tie_break == "duration":
        return (row["score"], -row["duration_ms"], row["attempt_id"])
    return (row["score"], row["attempt_id"])


def _scope_terminal(conn: sqlite3.Connection, group_id: str, scope_key: str) -> bool:
    cell = conn.execute(
        "SELECT run_id FROM run_group_cells WHERE run_group_id=? AND "
        "('variant:' || variant_id || '/repeat:' || repeat_index)=?",
        (group_id, scope_key),
    ).fetchone()
    if cell is None or cell[0] is None:
        return False
    statuses = [
        row[0]
        for row in conn.execute(
            "SELECT status FROM attempts WHERE run_id=?", (cell[0],)
        ).fetchall()
    ]
    return bool(statuses) and all(
        status in TERMINAL_ATTEMPT_STATUSES for status in statuses
    )


def _tie_break(conn: sqlite3.Connection, group_id: str) -> str:
    row = conn.execute(
        "SELECT e.protocol_json FROM experiments e JOIN run_groups g "
        "ON g.experiment_id=e.id WHERE g.id=?",
        (group_id,),
    ).fetchone()
    if row is None:
        return "candidate_id"
    try:
        return json.loads(row[0])["leader"]["tie_break"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return "candidate_id"


def _candidates_through(
    conn: sqlite3.Connection, group_id: str, scope_key: str, seq: int | None = None
) -> list[sqlite3.Row]:
    sequence_clause = "" if seq is None else " AND o.seq<=?"
    params: tuple[Any, ...] = (group_id, scope_key)
    if seq is not None:
        params += (seq,)
    return conn.execute(
        "SELECT o.id AS outbox_id,o.attempt_id,o.score,o.scorer_fingerprint,o.seq,"
        "o.created_at,a.duration_ms FROM score_transition_outbox o "
        "JOIN attempts a ON a.id=o.attempt_id "
        "JOIN run_group_cells c ON c.run_id=a.run_id "
        "WHERE c.run_group_id=? AND o.scope_key=? "
        "AND a.status IN ('completed','gave_up')"
        + sequence_clause
        + " ORDER BY o.seq,o.id",
        params,
    ).fetchall()


def consume_score_outbox(db_path: Path, *, group_id: str | None = None) -> int:
    """Consume committed score transitions in deterministic global sequence order."""
    consumed = 0
    while True:
        with _open_sync(db_path) as conn:
            conn.row_factory = sqlite3.Row
            params: tuple[Any, ...] = ()
            group_clause = ""
            if group_id is not None:
                group_clause = " AND c.run_group_id=?"
                params = (group_id,)
            source = conn.execute(
                "SELECT o.*,c.run_group_id FROM score_transition_outbox o "
                "JOIN attempts a ON a.id=o.attempt_id "
                "JOIN run_group_cells c ON c.run_id=a.run_id "
                "WHERE o.consumed_at IS NULL AND o.scope_key IS NOT NULL"
                + group_clause
                + " ORDER BY o.seq,o.id LIMIT 1",
                params,
            ).fetchone()
            if source is None:
                return consumed
            existing = conn.execute(
                "SELECT 1 FROM leader_events WHERE source_outbox_id=?", (source["id"],)
            ).fetchone()
            if existing is not None:
                conn.execute(
                    "UPDATE score_transition_outbox SET consumed_at=COALESCE(consumed_at,?) "
                    "WHERE id=?",
                    (_now_iso(), source["id"]),
                )
                conn.commit()
                consumed += 1
                continue

            candidates = _candidates_through(
                conn, source["run_group_id"], source["scope_key"], source["seq"]
            )
            tie_break = _tie_break(conn, source["run_group_id"])
            best = max(candidates, key=lambda row: _rank(row, tie_break))
            previous = conn.execute(
                "SELECT * FROM leader_events WHERE run_group_id=? AND scope_key=? "
                "ORDER BY sequence DESC LIMIT 1",
                (source["run_group_id"], source["scope_key"]),
            ).fetchone()
            terminal = (
                bool(source["scope_terminal"])
                if source["scope_terminal"] is not None
                else _scope_terminal(
                    conn, source["run_group_id"], source["scope_key"]
                )
            )
            fingerprints = {row["scorer_fingerprint"] for row in candidates}
            reason = None
            if len(fingerprints) > 1:
                reason = "incomparable"
            elif terminal:
                reason = "scope-finalized"
            elif previous is None:
                reason = "first"
            elif previous["current_attempt_id"] != best["attempt_id"]:
                reason = "upgrade"

            if reason is not None:
                sequence = conn.execute(
                    "SELECT COALESCE(MAX(sequence),0)+1 FROM leader_events "
                    "WHERE run_group_id=? AND scope_key=?",
                    (source["run_group_id"], source["scope_key"]),
                ).fetchone()[0]
                previous_attempt = previous["current_attempt_id"] if previous else None
                previous_value = previous["current_value"] if previous else None
                event_id = content_id(
                    "leader_event",
                    {"source_outbox_id": source["id"], "reason": reason},
                )
                conn.execute(
                    "INSERT INTO leader_events(id,run_group_id,scope_key,sequence,metric,"
                    "previous_attempt_id,current_attempt_id,previous_value,current_value,"
                    "delta,reason,provisional,source_score_fingerprint,schema_version,"
                    "producer_version,input_refs_json,source_outbox_id,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        event_id,
                        source["run_group_id"],
                        source["scope_key"],
                        sequence,
                        "task_score",
                        previous_attempt,
                        best["attempt_id"],
                        previous_value,
                        best["score"],
                        None if previous_value is None else best["score"] - previous_value,
                        reason,
                        int(not terminal),
                        source["scorer_fingerprint"],
                        "octagon-leader-event-v1",
                        PRODUCER_VERSION,
                        json.dumps({"source_outbox_id": source["id"]}, sort_keys=True),
                        source["id"],
                        source["created_at"],
                    ),
                )
                leader_payload = {
                    "schema_version": "octagon-leader-event-v1",
                    "event_id": event_id,
                    "scope_key": source["scope_key"],
                    "sequence": int(sequence),
                    "metric": "task_score",
                    "previous_attempt_id": previous_attempt,
                    "current_attempt_id": best["attempt_id"],
                    "previous_value": previous_value,
                    "current_value": best["score"],
                    "delta": (
                        None
                        if previous_value is None
                        else best["score"] - previous_value
                    ),
                    "reason": reason,
                    "provisional": not terminal,
                    "source_score_fingerprint": source["scorer_fingerprint"],
                    "producer_version": PRODUCER_VERSION,
                    "source_outbox_id": source["id"],
                }
                already_projected = any(
                    json.loads(row[0] or "{}").get("source_outbox_id")
                    == source["id"]
                    for row in conn.execute(
                        "SELECT payload_json FROM run_group_events "
                        "WHERE run_group_id=? AND event_type='leader_event'",
                        (source["run_group_id"],),
                    ).fetchall()
                )
                if not already_projected:
                    ExperimentRepository.append_group_event(
                        conn,
                        source["run_group_id"],
                        "leader_event",
                        leader_payload,
                        now=source["created_at"],
                    )
            conn.execute(
                "UPDATE score_transition_outbox SET consumed_at=? WHERE id=?",
                (_now_iso(), source["id"]),
            )
            conn.commit()
            consumed += 1


def final_leader_state(db_path: Path, group_id: str, scope_key: str) -> dict[str, Any]:
    """Rebuild final state from score facts, not the historical event timeline."""
    with _open_sync(db_path) as conn:
        conn.row_factory = sqlite3.Row
        candidates = _candidates_through(conn, group_id, scope_key)
        terminal = _scope_terminal(conn, group_id, scope_key)
        if not candidates:
            return {
                "status": "unavailable" if terminal else "provisional",
                "leader": None,
            }
        fingerprints = sorted({row["scorer_fingerprint"] for row in candidates})
        if len(fingerprints) > 1:
            return {
                "status": "incomparable",
                "leader": None,
                "fingerprints": fingerprints,
            }
        best = max(candidates, key=lambda row: _rank(row, _tie_break(conn, group_id)))
        return {
            "status": "final" if terminal else "provisional",
            "leader": {
                "attempt_id": best["attempt_id"],
                "score": best["score"],
                "scorer_fingerprint": best["scorer_fingerprint"],
            },
        }


def replay_leader_events(db_path: Path, group_id: str) -> int:
    """Rebuild the exact timeline from immutable outbox ordering."""
    with _open_sync(db_path) as conn:
        conn.execute("DELETE FROM leader_events WHERE run_group_id=?", (group_id,))
        conn.execute(
            "UPDATE score_transition_outbox SET consumed_at=NULL WHERE attempt_id IN ("
            "SELECT a.id FROM attempts a JOIN run_group_cells c ON c.run_id=a.run_id "
            "WHERE c.run_group_id=?)",
            (group_id,),
        )
        conn.commit()
    return consume_score_outbox(db_path, group_id=group_id)
