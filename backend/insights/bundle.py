"""Versioned, deterministic and metadata-only Evidence Bundle builder."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from backend.db import _open_sync
from backend.experiments.hashing import canonical_hash, hash_bytes

from .anchors import EvidenceAnchor, EvidencePolicy, EvidenceResolver, source_record_ids
from .selectors import EvidenceBudget, SelectionCandidate, estimate_tokens, select_records


BUNDLE_SCHEMA_VERSION = "octagon-evidence-bundle-v1"
BUILDER_VERSION = "octagon-evidence-builder-v1"
_JSONL_SOURCES = ("trace", "events", "conversation", "wire")


def _rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _artifact_ids(data_path: Path, attempt_id: str) -> list[str]:
    root = data_path / "attempts" / attempt_id / "skill_workspace"
    if not root.is_dir() or root.is_symlink():
        return []
    resolved = root.resolve()
    ids: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            relative = path.resolve().relative_to(resolved).as_posix()
        except (OSError, ValueError):
            continue
        import hashlib

        ids.append(f"artifact:{hashlib.sha256(relative.encode()).hexdigest()[:20]}")
    return ids


def _final_result_metadata(data_path: Path, attempt_id: str) -> dict[str, Any]:
    path = data_path / "attempts" / attempt_id / "final_state.json"
    if not path.is_file() or path.is_symlink():
        return {"state": "missing"}
    raw = path.read_bytes()
    keys: list[str] = []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            keys = sorted(str(key) for key in parsed)
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    return {
        "state": "captured",
        "content_hash": hash_bytes(raw),
        "size": len(raw),
        "top_level_keys": keys,
    }


def build_result_evidence_records(
    *,
    db_path: Path,
    data_path: Path,
    experiment_id: str,
) -> list[dict[str, Any]]:
    """Build the result-only index without walking trajectory or artifact files."""

    with _open_sync(Path(db_path)) as conn:
        exists = conn.execute(
            "SELECT 1 FROM experiments WHERE id=?",
            (experiment_id,),
        ).fetchone()
        if exists is None:
            raise ValueError(f"experiment not found: {experiment_id}")
        attempts = _rows(
            conn,
            "SELECT a.id,a.run_id,a.agent_name,a.model,a.status,a.score_total,"
            "a.duration_ms,c.run_group_id,c.id AS cell_id,c.variant_id,c.repeat_index,"
            "v.mutator_id FROM attempts a "
            "JOIN run_group_cells c ON c.run_id=a.run_id "
            "JOIN run_groups g ON g.id=c.run_group_id "
            "LEFT JOIN task_variants v ON v.id=c.variant_id "
            "WHERE g.experiment_id=? ORDER BY c.run_group_id,a.run_id,a.id",
            (experiment_id,),
        )

    return [
        {
            "category": "result",
            "experiment_id": experiment_id,
            "group_id": attempt["run_group_id"],
            "run_id": attempt["run_id"],
            "attempt_id": attempt["id"],
            "cell_id": attempt["cell_id"],
            "variant_id": attempt["variant_id"],
            "mutator_id": attempt["mutator_id"],
            "repeat_index": attempt["repeat_index"],
            "agent": attempt["agent_name"],
            "model": attempt["model"],
            "status": attempt["status"],
            "score_total": attempt["score_total"],
            "duration_ms": attempt["duration_ms"],
            "final_result": _final_result_metadata(Path(data_path), attempt["id"]),
        }
        for attempt in attempts
    ]


def build_evidence_bundle(
    *,
    db_path: Path,
    data_path: Path,
    experiment_id: str,
    budget: EvidenceBudget | None = None,
    policy: EvidencePolicy | None = None,
) -> dict[str, Any]:
    """Build a replayable bundle without including raw/thinking payloads.

    ``allow_payload`` is intentionally ignored: a Bundle is a lower-privilege
    derived input and can never widen the caller's capture policy.
    """

    budget = budget or EvidenceBudget()
    requested_policy = policy or EvidencePolicy()
    safe_policy = EvidencePolicy(
        allow_payload=False, allowed_sources=requested_policy.allowed_sources
    )
    with _open_sync(Path(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        experiment_row = conn.execute(
            "SELECT id,status,protocol_json,protocol_hash,schema_version FROM experiments "
            "WHERE id=?",
            (experiment_id,),
        ).fetchone()
        if experiment_row is None:
            raise ValueError(f"experiment not found: {experiment_id}")
        groups = _rows(
            conn,
            "SELECT id,status,total_cells,completed_cells,partial_cells,failed_cells,"
            "cancelled_cells,plan_hash FROM run_groups WHERE experiment_id=? ORDER BY id",
            (experiment_id,),
        )
        cells = _rows(
            conn,
            "SELECT c.id,c.run_group_id,c.variant_id,c.repeat_index,c.run_id,c.status,"
            "c.error_code,v.mutator_id FROM run_group_cells c "
            "JOIN run_groups g ON g.id=c.run_group_id "
            "LEFT JOIN task_variants v ON v.id=c.variant_id "
            "WHERE g.experiment_id=? ORDER BY c.run_group_id,c.repeat_index,c.id",
            (experiment_id,),
        )
        attempts = _rows(
            conn,
            "SELECT a.id,a.run_id,a.agent_name,a.model,a.status,a.transport_status,"
            "a.score_total,a.failure_kind,a.retryable,a.error_code,a.event_count,"
            "a.security_event_count,a.security_max_severity,a.duration_ms,c.run_group_id "
            "FROM attempts a JOIN run_group_cells c ON c.run_id=a.run_id "
            "JOIN run_groups g ON g.id=c.run_group_id WHERE g.experiment_id=? "
            "ORDER BY c.run_group_id,a.run_id,a.id",
            (experiment_id,),
        )
        scores = _rows(
            conn,
            "SELECT s.id,s.attempt_id,s.dimension,s.value,s.scored_at,"
            "s.evaluation_manifest_ref FROM scores s JOIN attempts a ON a.id=s.attempt_id "
            "JOIN run_group_cells c ON c.run_id=a.run_id JOIN run_groups g ON g.id=c.run_group_id "
            "WHERE g.experiment_id=? ORDER BY s.attempt_id,s.dimension,s.id",
            (experiment_id,),
        )

    experiment = dict(experiment_row)
    try:
        protocol = json.loads(experiment.pop("protocol_json"))
    except json.JSONDecodeError:
        protocol = {"state": "invalid", "protocol_hash": experiment["protocol_hash"]}
    cell_by_run = {cell["run_id"]: cell for cell in cells if cell["run_id"]}
    attempt_by_id = {attempt["id"]: attempt for attempt in attempts}
    resolver = EvidenceResolver(db_path=Path(db_path), data_path=Path(data_path))
    candidates: list[SelectionCandidate] = []
    anchors_map: dict[str, dict[str, Any]] = {}

    for group in groups:
        record = {"category": "aggregate", **group}
        candidates.append(SelectionCandidate("aggregate", group["id"], record))

    for attempt in attempts:
        cell = cell_by_run.get(attempt["run_id"])
        base = {
            "experiment_id": experiment_id,
            "group_id": attempt["run_group_id"],
            "run_id": attempt["run_id"],
            "attempt_id": attempt["id"],
            "cell_id": cell["id"] if cell else None,
            "variant_id": cell["variant_id"] if cell else None,
            "mutator_id": cell["mutator_id"] if cell else None,
            "repeat_index": cell["repeat_index"] if cell else None,
            "agent": attempt["agent_name"],
            "model": attempt["model"],
        }
        result = {
            "category": "result",
            **base,
            "status": attempt["status"],
            "score_total": attempt["score_total"],
            "duration_ms": attempt["duration_ms"],
            "final_result": _final_result_metadata(Path(data_path), attempt["id"]),
        }
        candidates.append(SelectionCandidate("result", attempt["id"], result))
        if attempt["failure_kind"] or attempt["error_code"]:
            candidates.append(
                SelectionCandidate(
                    "error",
                    attempt["id"],
                    {
                        "category": "error",
                        **base,
                        "failure_kind": attempt["failure_kind"],
                        "error_code": attempt["error_code"],
                        "retryable": bool(attempt["retryable"]),
                    },
                )
            )
        candidates.append(
            SelectionCandidate(
                "security",
                attempt["id"],
                {
                    "category": "security",
                    **base,
                    "event_count": attempt["security_event_count"],
                    "max_severity": attempt["security_max_severity"],
                },
            )
        )
        present_sources = []
        for source in _JSONL_SOURCES:
            ids = source_record_ids(Path(data_path), attempt["id"], source)
            if ids:
                present_sources.append(source)
            batch_resolutions = resolver.resolve_jsonl_source(
                experiment_id=experiment_id,
                group_id=attempt["run_group_id"],
                run_id=attempt["run_id"],
                attempt_id=attempt["id"],
                source=source,
                policy=safe_policy,
            )
            for record_id in ids:
                anchor = EvidenceAnchor(
                    experiment_id=experiment_id,
                    group_id=attempt["run_group_id"],
                    run_id=attempt["run_id"],
                    attempt_id=attempt["id"],
                    source=source,
                    record_id=record_id,
                )
                uri = anchor.uri()
                resolution = batch_resolutions.get(
                    record_id,
                    {"status": "missing", "reason": "record_not_found"},
                )
                anchors_map[uri] = resolution
                candidates.append(
                    SelectionCandidate(
                        "trajectory",
                        f"{attempt['id']}:{source}:{record_id}",
                        {
                            "category": "trajectory",
                            **base,
                            "source": source,
                            "anchor": uri,
                            "resolution": resolution,
                        },
                    )
                )
        artifact_ids = _artifact_ids(Path(data_path), attempt["id"])
        for record_id in artifact_ids:
            anchor = EvidenceAnchor(
                experiment_id=experiment_id,
                group_id=attempt["run_group_id"],
                run_id=attempt["run_id"],
                attempt_id=attempt["id"],
                source="artifacts",
                record_id=record_id,
            )
            uri = anchor.uri()
            resolution = resolver.resolve(anchor, safe_policy)
            anchors_map[uri] = resolution
            candidates.append(
                SelectionCandidate(
                    "artifact",
                    f"{attempt['id']}:{record_id}",
                    {"category": "artifact", **base, "anchor": uri, "resolution": resolution},
                )
            )
        candidates.append(
            SelectionCandidate(
                "completeness",
                attempt["id"],
                {
                    "category": "completeness",
                    **base,
                    "transport_status": attempt["transport_status"],
                    "declared_event_count": attempt["event_count"],
                    "present_sources": sorted(present_sources),
                    "missing_sources": sorted(set(_JSONL_SOURCES) - set(present_sources)),
                },
            )
        )

    for score in scores:
        attempt = attempt_by_id[score["attempt_id"]]
        anchor = EvidenceAnchor(
            experiment_id=experiment_id,
            group_id=attempt["run_group_id"],
            run_id=attempt["run_id"],
            attempt_id=attempt["id"],
            source="scores",
            record_id=f"score:{score['id']}",
        )
        uri = anchor.uri()
        resolution = resolver.resolve(anchor, safe_policy)
        anchors_map[uri] = resolution
        candidates.append(
            SelectionCandidate(
                "score",
                f"{attempt['id']}:{score['dimension']}:{score['id']}",
                {
                    "category": "score",
                    "attempt_id": attempt["id"],
                    "dimension": score["dimension"],
                    "value": score["value"],
                    "scored_at": score["scored_at"],
                    "evaluation_manifest_ref": score["evaluation_manifest_ref"],
                    "anchor": uri,
                },
            )
        )

    header = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "builder_version": BUILDER_VERSION,
        "experiment": experiment,
        "protocol": protocol,
        "capture_policy": {
            "payload_included": False,
            "allowed_sources": sorted(safe_policy.allowed_sources),
        },
    }
    reserved = estimate_tokens({**header, "records": [], "anchors": {}, "manifest": {}})
    selected, manifest = select_records(candidates, budget=budget, reserved_tokens=reserved)
    def assemble(records: list[dict[str, Any]]) -> dict[str, Any]:
        selected_uris = {
            record["anchor"]
            for record in records
            if isinstance(record.get("anchor"), str)
        }
        return {
            **header,
            "records": records,
            "anchors": {uri: anchors_map[uri] for uri in sorted(selected_uris)},
            "manifest": manifest,
        }

    bundle = assemble(selected)
    # Account for the anchors map and final envelope, not just record bodies.
    # Lower-priority records are at the tail by selector contract.
    while selected and estimate_tokens({**bundle, "bundle_hash": "sha256:" + "0" * 64}) > budget.max_tokens:
        removed = selected.pop()
        category = str(removed.get("category", "unknown"))
        manifest["omitted_by_category"][category] = (
            manifest["omitted_by_category"].get(category, 0) + 1
        )
        manifest["selected_records"] -= 1
        manifest["selected_by_category"][category] -= 1
        if manifest["selected_by_category"][category] == 0:
            del manifest["selected_by_category"][category]
        manifest["truncated"] = True
        bundle = assemble(selected)
    manifest["estimated_tokens"] = estimate_tokens(
        {**bundle, "bundle_hash": "sha256:" + "0" * 64}
    )
    bundle["bundle_hash"] = canonical_hash(bundle)
    return bundle
