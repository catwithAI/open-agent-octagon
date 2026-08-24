"""Immutable, retention-aware execution input snapshots (RC-F-03)."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import rfc8785

from .db import _now_iso, _open_sync
from .experiments.hashing import canonical_hash, hash_bytes, new_id


class InputSnapshotError(RuntimeError):
    pass


class InputSnapshotMissing(InputSnapshotError):
    pass


class InputSnapshotPurged(InputSnapshotError):
    pass


@dataclass(slots=True)
class SnapshotDraft:
    id: str
    attempt_id: str
    variant_id: str | None
    prompt_ref: str = field(repr=False)
    context_ref: str = field(repr=False)
    constraints_ref: str = field(repr=False)
    context_meta_json: str
    material_refs_json: str
    content_hash: str
    created_at: str

    def insert_values(self) -> tuple[Any, ...]:
        return (
            self.id,
            self.attempt_id,
            self.variant_id,
            self.prompt_ref,
            self.context_ref,
            self.constraints_ref,
            self.context_meta_json,
            self.material_refs_json,
            self.content_hash,
            self.created_at,
        )


@dataclass(slots=True)
class ResolvedAttemptInput:
    prompt: str = field(repr=False)
    context: dict[str, Any] = field(repr=False)
    constraints: dict[str, Any] = field(repr=False)
    timeout_seconds: int | None
    content_hash: str
    input_provenance: str
    variant_id: str | None = None


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.read_bytes() == payload:
        return
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _material_metadata(context: dict[str, Any]) -> dict[str, Any]:
    """Capture hashes and safe names, never host absolute paths."""
    result: list[dict[str, Any]] = []
    for key in ("uploaded_files", "materials"):
        values = context.get(key)
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                continue
            raw_path = item.get("path")
            path = Path(raw_path) if isinstance(raw_path, str) else None
            entry: dict[str, Any] = {
                "field": key,
                "name": str(item.get("name") or (path.name if path else "")),
            }
            if path is not None and path.is_file():
                entry["content_hash"] = hash_bytes(path.read_bytes())
                entry["size"] = path.stat().st_size
            elif isinstance(item.get("content_hash"), str):
                entry["content_hash"] = item["content_hash"]
            result.append(entry)
    return {"items": result}


def prepare_snapshot(
    *,
    data_path: Path,
    attempt_id: str,
    prompt: str,
    context: dict[str, Any],
    constraints: dict[str, Any],
    timeout_seconds: int | None,
    variant_id: str | None = None,
) -> SnapshotDraft:
    """Persist payload first; caller inserts the returned row with Attempt."""
    prompt_bytes = prompt.encode("utf-8")
    context_bytes = rfc8785.dumps(context)
    constraints_bytes = rfc8785.dumps(constraints)
    material_meta = _material_metadata(context)
    content_hash = canonical_hash(
        {
            "prompt_hash": hash_bytes(prompt_bytes),
            "context_hash": hash_bytes(context_bytes),
            "constraints_hash": hash_bytes(constraints_bytes),
            "material_refs": material_meta,
            "timeout_seconds": timeout_seconds,
        }
    )
    digest = content_hash.removeprefix("sha256:")
    relative_dir = Path("input-snapshots") / attempt_id / digest
    absolute_dir = Path(data_path) / relative_dir
    prompt_ref = relative_dir / "prompt.txt"
    context_ref = relative_dir / "context.json"
    constraints_ref = relative_dir / "constraints.json"
    _write_atomic(absolute_dir / "prompt.txt", prompt_bytes)
    _write_atomic(absolute_dir / "context.json", context_bytes)
    _write_atomic(absolute_dir / "constraints.json", constraints_bytes)
    context_meta = {
        "context_keys": sorted(context),
        "constraint_keys": sorted(constraints),
        "prompt_hash": hash_bytes(prompt_bytes),
        "context_hash": hash_bytes(context_bytes),
        "constraints_hash": hash_bytes(constraints_bytes),
        "timeout_seconds": timeout_seconds,
    }
    return SnapshotDraft(
        id=new_id("input_snapshot"),
        attempt_id=attempt_id,
        variant_id=variant_id,
        prompt_ref=prompt_ref.as_posix(),
        context_ref=context_ref.as_posix(),
        constraints_ref=constraints_ref.as_posix(),
        context_meta_json=json.dumps(context_meta, ensure_ascii=False, sort_keys=True),
        material_refs_json=json.dumps(material_meta, ensure_ascii=False, sort_keys=True),
        content_hash=content_hash,
        created_at=_now_iso(),
    )


def insert_snapshot(conn: sqlite3.Connection, draft: SnapshotDraft) -> None:
    conn.execute(
        "INSERT INTO attempt_input_snapshots("
        "id, attempt_id, variant_id, prompt_ref, context_ref, constraints_ref, "
        "context_meta_json, material_refs_json, content_hash, created_at"
        ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        draft.insert_values(),
    )


def _safe_ref(data_path: Path, ref: str) -> Path:
    root = Path(data_path).resolve()
    candidate = (root / ref).resolve()
    if not candidate.is_relative_to(root):
        raise InputSnapshotError("snapshot ref escapes data path")
    return candidate


def _is_experiment_attempt(conn: sqlite3.Connection, attempt_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM attempts a "
        "JOIN run_group_cells c ON c.run_id=a.run_id "
        "WHERE a.id=? LIMIT 1",
        (attempt_id,),
    ).fetchone()
    return row is not None


def resolve_attempt_input(
    *, data_path: Path, db_path: Path, attempt_id: str
) -> ResolvedAttemptInput:
    """Resolve snapshot; only pre-migration legacy attempts may use task rows."""
    with _open_sync(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM attempt_input_snapshots WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
        if row is None:
            if _is_experiment_attempt(conn, attempt_id):
                raise InputSnapshotMissing(
                    f"experiment attempt has no input snapshot: {attempt_id}"
                )
            task = conn.execute(
                "SELECT t.* FROM tasks t JOIN attempts a ON a.task_id=t.id WHERE a.id=?",
                (attempt_id,),
            ).fetchone()
            if task is None:
                raise InputSnapshotMissing(f"input and source task missing: {attempt_id}")
            context_json = task["context_json"] or "{}"
            constraints_json = task["constraints_json"] or "{}"
            return ResolvedAttemptInput(
                prompt=task["prompt"],
                context=json.loads(context_json),
                constraints=json.loads(constraints_json),
                timeout_seconds=task["timeout_seconds"],
                content_hash=canonical_hash(
                    {
                        "legacy_task_id": task["id"],
                        "prompt": task["prompt"],
                        "context": json.loads(context_json),
                        "constraints": json.loads(constraints_json),
                    }
                ),
                input_provenance="legacy_mutable_input",
            )
        snapshot = dict(row)

    if snapshot["payload_state"] == "purged":
        raise InputSnapshotPurged(f"input snapshot was purged: {attempt_id}")
    prompt_path = _safe_ref(data_path, snapshot["prompt_ref"])
    context_path = _safe_ref(data_path, snapshot["context_ref"])
    constraints_path = _safe_ref(data_path, snapshot["constraints_ref"])
    try:
        prompt = prompt_path.read_text(encoding="utf-8")
        context = json.loads(context_path.read_text(encoding="utf-8"))
        constraints = json.loads(constraints_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InputSnapshotMissing(f"snapshot payload unreadable: {attempt_id}") from exc
    meta = json.loads(snapshot["context_meta_json"] or "{}")
    return ResolvedAttemptInput(
        prompt=prompt,
        context=context,
        constraints=constraints,
        timeout_seconds=meta.get("timeout_seconds"),
        content_hash=snapshot["content_hash"],
        input_provenance="immutable_snapshot",
        variant_id=snapshot["variant_id"],
    )


def purge_attempt_input(*, data_path: Path, db_path: Path, attempt_id: str) -> None:
    with _open_sync(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT prompt_ref, context_ref, constraints_ref, payload_state "
            "FROM attempt_input_snapshots WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()
        if row is None:
            raise InputSnapshotMissing(attempt_id)
        if row["payload_state"] == "purged":
            return
        for key in ("prompt_ref", "context_ref", "constraints_ref"):
            _safe_ref(data_path, row[key]).unlink(missing_ok=True)
        conn.execute(
            "UPDATE attempt_input_snapshots SET payload_state='purged', purged_at=? "
            "WHERE attempt_id=?",
            (_now_iso(), attempt_id),
        )
        conn.commit()
