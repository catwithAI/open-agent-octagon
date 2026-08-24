"""Robustness snapshot persistence, filtering and rebuild diagnostics."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any

from backend.db import _now_iso, _open_sync

from .aggregate import AttemptFact, aggregate_attempts, rank_variant_labels
from .hashing import canonical_hash, canonical_json_bytes, new_id
from .models import ExperimentProtocol

ALGORITHM_VERSION = "octagon-robustness-v2"


def _snapshot_input(conn: sqlite3.Connection, group_id: str) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    group = conn.execute(
        "SELECT g.*,e.protocol_json,e.protocol_hash FROM run_groups g "
        "JOIN experiments e ON e.id=g.experiment_id WHERE g.id=?",
        (group_id,),
    ).fetchone()
    if group is None:
        raise ValueError(f"run group not found: {group_id}")
    if group["status"] not in {"completed", "partial", "failed", "cancelled"}:
        raise ValueError("robustness snapshot requires terminal run group")
    protocol = ExperimentProtocol.model_validate_json(group["protocol_json"])
    variants = conn.execute(
        "SELECT * FROM task_variants WHERE experiment_id=? ORDER BY created_at,id",
        (group["experiment_id"],),
    ).fetchall()
    cells = conn.execute(
        "SELECT * FROM run_group_cells WHERE run_group_id=? ORDER BY repeat_index,id",
        (group_id,),
    ).fetchall()
    attempts = conn.execute(
        "SELECT a.*,o.scorer_fingerprint FROM attempts a "
        "JOIN run_group_cells c ON c.run_id=a.run_id "
        "LEFT JOIN score_transition_outbox o ON o.attempt_id=a.id "
        "WHERE c.run_group_id=? ORDER BY a.created_at,a.id",
        (group_id,),
    ).fetchall()
    return {
        "group": dict(group),
        "protocol": protocol,
        "variants": [dict(row) for row in variants],
        "cells": [dict(row) for row in cells],
        "attempts": [dict(row) for row in attempts],
    }


def _build_payload(source: dict[str, Any]) -> dict[str, Any]:
    group = source["group"]
    protocol: ExperimentProtocol = source["protocol"]
    cells_by_variant: dict[str, list[dict[str, Any]]] = {}
    for cell in source["cells"]:
        cells_by_variant.setdefault(cell["variant_id"], []).append(cell)
    attempts_by_run: dict[str, list[dict[str, Any]]] = {}
    for attempt in source["attempts"]:
        attempts_by_run.setdefault(attempt["run_id"], []).append(attempt)

    slices: list[dict[str, Any]] = []
    aggregates: dict[tuple[str, str, str | None], Any] = {}
    for variant in source["variants"]:
        variant_cells = cells_by_variant.get(variant["id"], [])
        candidate_keys = sorted(
            {
                (attempt["agent_name"], attempt.get("model"))
                for cell in variant_cells
                for attempt in attempts_by_run.get(cell.get("run_id") or "", [])
            },
            key=lambda item: (item[0], item[1] or ""),
        )
        if not candidate_keys:
            candidate_keys = sorted(
                {(candidate.agent, candidate.model) for candidate in protocol.agents},
                key=lambda item: (item[0], item[1] or ""),
            )
        for agent, model in candidate_keys:
            facts: list[AttemptFact] = []
            fingerprints: set[str] = set()
            for cell in variant_cells:
                matches = [
                    attempt
                    for attempt in attempts_by_run.get(cell.get("run_id") or "", [])
                    if attempt["agent_name"] == agent and attempt.get("model") == model
                ]
                if matches:
                    attempt = matches[0]
                    facts.append(
                        AttemptFact(
                            attempt_id=attempt["id"],
                            status=attempt["status"],
                            score_total=attempt["score_total"],
                            failure_kind=attempt["failure_kind"],
                            security_event_count=attempt["security_event_count"],
                        )
                    )
                    if attempt.get("scorer_fingerprint"):
                        fingerprints.add(attempt["scorer_fingerprint"])
                else:
                    status = "cancelled" if cell["status"] == "cancelled" else "missing"
                    facts.append(
                        AttemptFact(
                            attempt_id=f"cell:{cell['id']}:{agent}:{model or 'default'}",
                            status=status,
                        )
                    )
            aggregate = aggregate_attempts(facts, expected_count=len(variant_cells))
            aggregates[(variant["id"], agent, model)] = aggregate
            slices.append(
                {
                    "variant_id": variant["id"],
                    "kind": variant["kind"],
                    "mutator_id": variant["mutator_id"],
                    "mutator_version": variant["mutator_version"],
                    "agent": agent,
                    "model": model,
                    "source_hash": variant["source_hash"],
                    "content_hash": variant["content_hash"],
                    "scorer_fingerprints": sorted(fingerprints),
                    "aggregate": asdict(aggregate),
                }
            )
    candidate_keys = {(item["agent"], item["model"]) for item in slices}
    for agent, model in candidate_keys:
        labels = rank_variant_labels(
            {
                variant_id: aggregate
                for (variant_id, candidate_agent, candidate_model), aggregate in aggregates.items()
                if candidate_agent == agent and candidate_model == model
            }
        )
        for item in slices:
            if item["agent"] == agent and item["model"] == model:
                item["labels"] = labels[item["variant_id"]]
    return {
        "schema_version": "octagon-robustness-snapshot-v1",
        "algorithm_version": ALGORITHM_VERSION,
        "run_group_id": group["id"],
        "experiment_id": group["experiment_id"],
        "protocol_hash": group["protocol_hash"],
        "slices": slices,
    }


def build_robustness_snapshot(db_path: Path, group_id: str) -> dict[str, Any]:
    with _open_sync(db_path) as conn:
        source = _snapshot_input(conn, group_id)
    payload = _build_payload(source)
    input_hash = canonical_hash(
        {
            "protocol_hash": source["group"]["protocol_hash"],
            "cells": source["cells"],
            "attempts": source["attempts"],
            "algorithm_version": ALGORITHM_VERSION,
        }
    )
    payload["input_hash"] = input_hash
    # Return the exact JSON shape that is persisted (tuples become arrays), so
    # first build and idempotent replay are structurally identical.
    payload = json.loads(canonical_json_bytes(payload))
    with _open_sync(db_path) as conn:
        existing = conn.execute(
            "SELECT id,data_json FROM robustness_snapshots WHERE run_group_id=? "
            "AND input_hash=? ORDER BY created_at DESC LIMIT 1",
            (group_id, input_hash),
        ).fetchone()
        if existing is not None:
            return json.loads(existing[1])
        conn.execute(
            "INSERT INTO robustness_snapshots(id,run_group_id,algorithm_version,input_hash,"
            "data_json,status,schema_version,producer_version,input_refs_json,created_at) "
            "VALUES(?,?,?,?,?,'ready','octagon-robustness-snapshot-v1',?,?,?)",
            (
                new_id("robustness_snapshot"),
                group_id,
                ALGORITHM_VERSION,
                input_hash,
                canonical_json_bytes(payload).decode("utf-8"),
                ALGORITHM_VERSION,
                json.dumps({"group_id": group_id}, sort_keys=True),
                _now_iso(),
            ),
        )
        conn.commit()
    return payload


def rebuild_diagnostic(db_path: Path, group_id: str) -> dict[str, Any]:
    with _open_sync(db_path) as conn:
        latest = conn.execute(
            "SELECT input_hash FROM robustness_snapshots WHERE run_group_id=? "
            "ORDER BY created_at DESC,id DESC LIMIT 1",
            (group_id,),
        ).fetchone()
    rebuilt = build_robustness_snapshot(db_path, group_id)
    return {
        "status": (
            "match" if latest is not None and latest[0] == rebuilt["input_hash"] else "rebuilt"
        ),
        "stored_input_hash": latest[0] if latest else None,
        "rebuilt_input_hash": rebuilt["input_hash"],
    }


def filter_snapshot(
    snapshot: dict[str, Any],
    *,
    mutator: str | None = None,
    agent: str | None = None,
    model: str | None = None,
    metric: str | None = None,
    offset: int = 0,
    limit: int = 100,
) -> dict[str, Any]:
    items = [
        item
        for item in snapshot["slices"]
        if (mutator is None or item["mutator_id"] == mutator)
        and (agent is None or item["agent"] == agent)
        and (model is None or item["model"] == model)
    ]
    page = items[offset : offset + limit]
    if metric is not None:
        for item in page:
            aggregate = item["aggregate"]
            if metric in aggregate:
                item["metric_value"] = aggregate[metric]
            elif metric in aggregate["rates"]:
                item["metric_value"] = aggregate["rates"][metric]["value"]
            else:
                raise ValueError(f"unknown robustness metric: {metric}")
    return {
        **{key: value for key, value in snapshot.items() if key != "slices"},
        "total": len(items),
        "offset": offset,
        "limit": limit,
        "metric": metric,
        "slices": page,
    }


def cell_drilldown(db_path: Path, group_id: str, cell_id: str) -> dict[str, Any] | None:
    with _open_sync(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cell = conn.execute(
            "SELECT id,variant_id,repeat_index,run_id,status,error_code FROM "
            "run_group_cells WHERE id=? AND run_group_id=?",
            (cell_id, group_id),
        ).fetchone()
        if cell is None:
            return None
        attempts = conn.execute(
            "SELECT id,agent_name,model,status,score_total,failure_kind,retryable,"
            "duration_ms,error_code FROM attempts WHERE run_id=? ORDER BY created_at,id",
            (cell["run_id"],),
        ).fetchall() if cell["run_id"] else []
    scored = [row for row in attempts if row["score_total"] is not None]
    worst = min(scored, key=lambda row: (row["score_total"], row["id"])) if scored else None
    return {
        "cell": dict(cell),
        "attempts": [dict(row) for row in attempts],
        "worst_attempt_id": worst["id"] if worst else None,
    }
