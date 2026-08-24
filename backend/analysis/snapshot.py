"""Minimal evidence normalization for black-box analysis.

Normalize evidence location and safety, not behavioral meaning.  Tool names and
provider event shapes remain untouched so an analysis agent can interpret them in
context without a growing hand-written action taxonomy.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from backend.db import _open_sync
from backend.experiments.hashing import canonical_hash

SNAPSHOT_VERSION = "octagon-run-analysis-snapshot-v2"
# Process Judge reads how the agent thought, what it did, and the conversation
# envelope (turn boundaries / user-visible completion). Socket stream fragments
# (llm:thinking:delta / content:delta) stay in events.jsonl and are not evidence.
_SOURCES = ("thinking", "trace", "conversation")
_SECRET_KEYS = ("api_key", "apikey", "authorization", "cookie", "password", "secret")


def _sensitive_key(name: str) -> bool:
    lowered = name.lower()
    if any(part in lowered for part in _SECRET_KEYS):
        return True
    # Preserve usage counters such as input_tokens/output_tokens; redact actual
    # credential-shaped fields such as token/access_token/env_token.
    return lowered == "token" or (lowered.endswith("_token") and not lowered.endswith("_tokens"))


def _safe_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        return "<depth-limited>"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if _sensitive_key(name):
                result[name] = "<redacted>"
            else:
                result[name] = _safe_value(item, depth=depth + 1)
        return result
    if isinstance(value, list):
        return [_safe_value(item, depth=depth + 1) for item in value[:200]]
    if isinstance(value, str):
        return value if len(value) <= 8000 else value[:8000] + "<truncated>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:8000]


def _selected_positions(total: int, limit: int) -> list[int]:
    """Deterministically cover head, middle and tail without inferring semantics."""
    if total <= limit:
        return list(range(total))
    head = max(1, limit // 4)
    tail = max(1, limit // 4)
    middle_slots = max(0, limit - head - tail)
    selected = set(range(head))
    selected.update(range(total - tail, total))
    if middle_slots:
        start = head
        span = total - head - tail
        for slot in range(middle_slots):
            # Midpoint of each equal-width bucket. This remains stable for the
            # same file and covers long trajectories without tool-specific rules.
            position = start + int((slot + 0.5) * span / middle_slots)
            selected.add(min(total - tail - 1, max(head, position)))
    # Rounding may collide for very small spans; fill deterministically.
    if len(selected) < limit:
        for position in range(head, total - tail):
            selected.add(position)
            if len(selected) >= limit:
                break
    return sorted(selected)[:limit]


def _read_records(path: Path, *, limit: int) -> list[tuple[int, Any]]:
    if not path.is_file() or path.is_symlink():
        return []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        lines = [line.strip() for line in handle if line.strip()]
    records: list[tuple[int, Any]] = []
    for position in _selected_positions(len(lines), limit):
        line = lines[position]
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            value = {"raw_text": line[:8000], "parse_error": True}
        # Anchor indices refer to original non-empty record positions and remain
        # valid when the evidence budget changes.
        records.append((position + 1, _safe_value(value)))
    return records


def _artifact_manifest(attempt_dir: Path, attempt_id: str) -> list[dict[str, Any]]:
    root = attempt_dir / "skill_workspace"
    if not root.is_dir() or root.is_symlink():
        return []
    resolved = root.resolve()
    result: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if len(result) >= 500 or not path.is_file() or path.is_symlink():
            continue
        try:
            relative = path.resolve().relative_to(resolved).as_posix()
        except (OSError, ValueError):
            continue
        raw = path.read_bytes()
        result.append({
            "anchor": f"octagon://attempt/{attempt_id}/artifact/{len(result) + 1}",
            "path": relative,
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        })
    return result


def build_run_analysis_snapshot(
    *, db_path: Path, data_path: Path, run_id: str, per_source_limit: int = 500,
    attempt_ids: set[str] | None = None,
) -> dict[str, Any]:
    with _open_sync(db_path) as conn:
        conn.row_factory = sqlite3.Row
        run = conn.execute(
            "SELECT id,task_id,env_name,status,compare_mode,model,execution,created_at,"
            "started_at,ended_at FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        if run is None:
            raise ValueError(f"run not found: {run_id}")
        task = conn.execute(
            "SELECT id,prompt,context_json,constraints_json,timeout_seconds FROM tasks WHERE id=?",
            (run["task_id"],),
        ).fetchone()
        attempts = conn.execute(
            "SELECT id,agent_name,model,status,execution_status,scoring_status,error_code,"
            "error_message,failure_kind,retryable,score_total,duration_ms,event_count,"
            "thinking_count,tool_call_count,started_at,ended_at FROM attempts "
            "WHERE run_id=? ORDER BY created_at,id", (run_id,)
        ).fetchall()
        if attempt_ids is not None:
            wanted = {str(item) for item in attempt_ids}
            attempts = [row for row in attempts if str(row["id"]) in wanted]
            if not attempts:
                raise ValueError(f"no matching attempts for run {run_id}")
        scores = conn.execute(
            "SELECT attempt_id,dimension,value,detail FROM scores WHERE attempt_id IN "
            "(SELECT id FROM attempts WHERE run_id=?) ORDER BY attempt_id,id", (run_id,)
        ).fetchall()

    score_map: dict[str, list[dict[str, Any]]] = {}
    for score in scores:
        score_map.setdefault(str(score["attempt_id"]), []).append(dict(score))
    attempt_items: list[dict[str, Any]] = []
    anchors: dict[str, dict[str, Any]] = {}
    for attempt in attempts:
        attempt_id = str(attempt["id"])
        attempt_dir = data_path / "attempts" / attempt_id
        evidence: dict[str, list[dict[str, Any]]] = {}
        for source in _SOURCES:
            filename = f"{source}.jsonl"
            path = attempt_dir / filename
            items = []
            for index, record in _read_records(
                path, limit=max(1, min(per_source_limit, 2000))
            ):
                anchor = f"octagon://attempt/{attempt_id}/{source}/{index}"
                anchors[anchor] = {
                    "attempt_id": attempt_id,
                    "source": source,
                    "record_index": index,
                }
                items.append({"anchor": anchor, "record": record})
            evidence[source] = items
        artifacts = _artifact_manifest(attempt_dir, attempt_id)
        for artifact in artifacts:
            anchors[artifact["anchor"]] = {
                "attempt_id": attempt_id,
                "source": "artifact",
                "path": artifact["path"],
            }
        attempt_items.append({
            "metadata": dict(attempt),
            "scores": score_map.get(attempt_id, []),
            "evidence": evidence,
            "artifacts": artifacts,
        })

    snapshot = {
        "schema_version": SNAPSHOT_VERSION,
        "run": dict(run),
        "task": {
            "id": task["id"] if task else run["task_id"],
            "prompt": task["prompt"] if task else "",
            "context": _safe_value(json.loads(task["context_json"] or "{}")) if task else {},
            "constraints": _safe_value(json.loads(task["constraints_json"] or "{}")) if task else {},
            "timeout_seconds": task["timeout_seconds"] if task else None,
        },
        "attempts": attempt_items,
        "anchors": anchors,
        "normalization_policy": {
            "semantic_mapping": False,
            "tool_names_preserved": True,
            "secret_keys_redacted": True,
            "per_source_limit": per_source_limit,
            "selection_strategy": "stratified-head-middle-tail-v1",
        },
    }
    snapshot["snapshot_hash"] = canonical_hash(snapshot)
    return snapshot
