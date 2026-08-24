"""Metadata-only Auto Profile feature extraction."""

from __future__ import annotations

import statistics
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from backend.db import _open_sync
from backend.experiments.hashing import canonical_hash


FEATURE_VERSION = "octagon-auto-profile-features-v1"


@dataclass(frozen=True)
class ProfileFeatures:
    env_name: str
    task_id: str | None
    category: str
    modalities: tuple[str, ...]
    material_total_bytes: int
    multi_turn: bool
    danger_tool_count: int
    prerequisite_warning_count: int
    historical_count: int
    duration_p50_ms: int | None
    duration_p90_ms: int | None
    deterministic_task: bool
    feature_hash: str

    def projection(self) -> dict[str, Any]:
        return asdict(self)


def _material_bytes(task: Any) -> int:
    raw = getattr(task, "raw", {}) or {}
    materials = raw.get("materials") or raw.get("attachments") or []
    if not isinstance(materials, list):
        return 0
    total = 0
    for item in materials:
        if not isinstance(item, dict):
            continue
        for key in ("size", "size_bytes", "bytes"):
            value = item.get(key)
            if isinstance(value, int) and value >= 0:
                total += value
                break
    return total


def _percentile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = round((len(ordered) - 1) * fraction)
    return ordered[index]


def extract_features(
    *,
    env: Any,
    task: Any | None,
    db_path: Path,
) -> ProfileFeatures:
    meta = getattr(env, "meta", {}) or {}
    prerequisites = meta.get("prerequisites") or {}
    modalities = tuple(sorted(set(prerequisites.get("agent_modalities") or ["text"])))
    context = getattr(task, "context", {}) if task is not None else {}
    conversation = context.get("_conversation") if isinstance(context, dict) else None
    multi_turn = isinstance(conversation, list) and len(conversation) > 1
    task_id = getattr(task, "id", None)
    with _open_sync(Path(db_path)) as conn:
        if task_id:
            rows = conn.execute(
                "SELECT a.duration_ms FROM attempts a WHERE a.task_id=? "
                "AND a.status IN ('completed','gave_up','timeout') AND a.duration_ms>0",
                (task_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT a.duration_ms FROM attempts a WHERE a.env_name=? "
                "AND a.status IN ('completed','gave_up','timeout') AND a.duration_ms>0",
                (getattr(env, "name", ""),),
            ).fetchall()
    durations = [int(row[0]) for row in rows]
    base = {
        "env_name": str(getattr(env, "name", "")),
        "task_id": task_id,
        "category": str(meta.get("category") or "unknown"),
        "modalities": modalities,
        "material_total_bytes": _material_bytes(task) if task is not None else 0,
        "multi_turn": multi_turn,
        "danger_tool_count": len(meta.get("danger_tools") or {}),
        "prerequisite_warning_count": len(
            getattr(env, "prerequisite_warnings", []) or []
        ),
        "historical_count": len(durations),
        "duration_p50_ms": int(statistics.median(durations)) if durations else None,
        "duration_p90_ms": _percentile(durations, 0.9),
        "deterministic_task": bool(
            meta.get("deterministic")
            or meta.get("type") in {"coding", "benchmark-sample"}
        ),
    }
    return ProfileFeatures(**base, feature_hash=canonical_hash({"version": FEATURE_VERSION, **base}))
