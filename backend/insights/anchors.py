"""Stable, hierarchy-checked Evidence Anchors with fail-closed payload policy."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from backend.db import _open_sync
from backend.experiments.hashing import hash_bytes

SOURCES = frozenset({"trace", "events", "conversation", "scores", "artifacts", "wire"})
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,240}$")


@dataclass(frozen=True)
class EvidenceAnchor:
    experiment_id: str
    group_id: str
    run_id: str
    attempt_id: str
    source: str
    record_id: str

    def __post_init__(self) -> None:
        for value in (
            self.experiment_id,
            self.group_id,
            self.run_id,
            self.attempt_id,
            self.record_id,
        ):
            if not _ID.fullmatch(value) or ".." in value:
                raise ValueError("invalid evidence anchor identifier")
        if self.source not in SOURCES:
            raise ValueError(f"unsupported evidence source: {self.source}")

    def uri(self) -> str:
        path = (
            f"/experiments/{self.experiment_id}/groups/{self.group_id}/runs/"
            f"{self.run_id}/attempts/{self.attempt_id}"
        )
        return f"octagon://{path.lstrip('/')}?{urlencode({'source': self.source, 'record': self.record_id})}"

    @classmethod
    def parse(cls, uri: str) -> "EvidenceAnchor":
        parsed = urlparse(uri)
        if parsed.scheme != "octagon" or parsed.fragment:
            raise ValueError("invalid evidence anchor URI")
        parts = [parsed.netloc, *parsed.path.strip("/").split("/")]
        if len(parts) != 8 or parts[0::2] != [
            "experiments", "groups", "runs", "attempts"
        ]:
            raise ValueError("invalid evidence anchor hierarchy")
        query = parse_qs(parsed.query, strict_parsing=True)
        if set(query) != {"source", "record"} or any(len(value) != 1 for value in query.values()):
            raise ValueError("invalid evidence anchor query")
        return cls(
            experiment_id=parts[1],
            group_id=parts[3],
            run_id=parts[5],
            attempt_id=parts[7],
            source=query["source"][0],
            record_id=query["record"][0],
        )


@dataclass(frozen=True)
class EvidencePolicy:
    allow_payload: bool = False
    allowed_sources: frozenset[str] = SOURCES


def _legacy_id(path: Path, line_index: int) -> str:
    return f"legacy:{hashlib.sha256(path.read_bytes()).hexdigest()[:20]}:{line_index}"


def _jsonl_records(path: Path) -> list[tuple[str, dict[str, Any], int]]:
    if not path.is_file():
        return []
    result: list[tuple[str, dict[str, Any], int]] = []
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        stable = record.get("record_id") or record.get("id") or record.get("canonical_id")
        record_id = str(stable) if stable else _legacy_id(path, index)
        result.append((record_id, record, index))
    return result


def source_record_ids(data_path: Path, attempt_id: str, source: str) -> list[str]:
    """Project resolver-compatible record IDs without exposing record payloads.

    Canonical wire rows already carry ``record_id``. New trace/event writers may
    use ``record_id`` (or the older ``id``/``canonical_id`` aliases); historical
    rows receive the documented file-hash + zero-based line projection.
    """

    if source not in {"trace", "events", "conversation", "wire"}:
        raise ValueError("source does not use JSONL record IDs")
    filenames = {
        "trace": ("trace.jsonl",),
        "events": ("events.jsonl", "blade_events.jsonl"),
        "conversation": ("conversation.jsonl",),
        "wire": ("wire.jsonl",),
    }[source]
    attempt_dir = Path(data_path) / "attempts" / attempt_id
    for filename in filenames:
        path = attempt_dir / filename
        if path.is_file():
            return [record_id for record_id, _record, _line in _jsonl_records(path)]
    return []


class EvidenceResolver:
    def __init__(self, *, db_path: Path, data_path: Path) -> None:
        self.db_path = Path(db_path)
        self.data_path = Path(data_path)

    def _hierarchy(self, anchor: EvidenceAnchor) -> bool:
        with _open_sync(self.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM run_group_cells c JOIN run_groups g ON g.id=c.run_group_id "
                "JOIN attempts a ON a.run_id=c.run_id WHERE g.experiment_id=? AND g.id=? "
                "AND c.run_id=? AND a.id=?",
                (
                    anchor.experiment_id,
                    anchor.group_id,
                    anchor.run_id,
                    anchor.attempt_id,
                ),
            ).fetchone()
        return row is not None

    def resolve(
        self, anchor: EvidenceAnchor | str, policy: EvidencePolicy | None = None
    ) -> dict[str, Any]:
        if isinstance(anchor, str):
            anchor = EvidenceAnchor.parse(anchor)
        policy = policy or EvidencePolicy()
        if not self._hierarchy(anchor):
            return {"status": "rejected", "reason": "foreign_or_invalid_hierarchy"}
        if anchor.source not in policy.allowed_sources:
            return {"status": "unsupported", "reason": "source_not_allowed"}
        if anchor.source == "scores":
            return self._score(anchor, policy)
        if anchor.source == "artifacts":
            return self._artifact(anchor)
        filenames = {
            "trace": ("trace.jsonl",),
            "events": ("events.jsonl", "blade_events.jsonl"),
            "conversation": ("conversation.jsonl",),
            "wire": ("wire.jsonl",),
        }[anchor.source]
        attempt_dir = self.data_path / "attempts" / anchor.attempt_id
        for filename in filenames:
            path = attempt_dir / filename
            for record_id, record, line_index in _jsonl_records(path):
                if record_id == anchor.record_id:
                    result = {
                        "status": "resolved" if policy.allow_payload else "redacted",
                        "source": anchor.source,
                        "record_id": record_id,
                        "metadata": {
                            "file_hash": hash_bytes(path.read_bytes()),
                            "line_index": line_index,
                            "record_keys": sorted(record),
                        },
                    }
                    if policy.allow_payload:
                        result["payload"] = record
                    return result
        if anchor.record_id.startswith("legacy:") and any(
            (attempt_dir / filename).is_file() for filename in filenames
        ):
            return {"status": "rotated", "reason": "legacy_file_hash_changed"}
        return {"status": "missing", "reason": "record_not_found"}

    def resolve_jsonl_source(
        self,
        *,
        experiment_id: str,
        group_id: str,
        run_id: str,
        attempt_id: str,
        source: str,
        policy: EvidencePolicy | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Resolve one JSONL source in one pass for large evidence collections."""

        if source not in {"trace", "events", "conversation", "wire"}:
            raise ValueError("source does not use JSONL records")
        policy = policy or EvidencePolicy()
        probe = EvidenceAnchor(
            experiment_id=experiment_id,
            group_id=group_id,
            run_id=run_id,
            attempt_id=attempt_id,
            source=source,
            record_id="batch-probe",
        )
        if not self._hierarchy(probe):
            return {}
        filenames = {
            "trace": ("trace.jsonl",),
            "events": ("events.jsonl", "blade_events.jsonl"),
            "conversation": ("conversation.jsonl",),
            "wire": ("wire.jsonl",),
        }[source]
        attempt_dir = self.data_path / "attempts" / attempt_id
        for filename in filenames:
            path = attempt_dir / filename
            if not path.is_file():
                continue
            records = _jsonl_records(path)
            if source not in policy.allowed_sources:
                return {
                    record_id: {"status": "unsupported", "reason": "source_not_allowed"}
                    for record_id, _record, _line in records
                }
            file_hash = hash_bytes(path.read_bytes())
            result: dict[str, dict[str, Any]] = {}
            for record_id, record, line_index in records:
                resolution: dict[str, Any] = {
                    "status": "resolved" if policy.allow_payload else "redacted",
                    "source": source,
                    "record_id": record_id,
                    "metadata": {
                        "file_hash": file_hash,
                        "line_index": line_index,
                        "record_keys": sorted(record),
                    },
                }
                if policy.allow_payload:
                    resolution["payload"] = record
                result[record_id] = resolution
            return result
        return {}

    def _score(self, anchor: EvidenceAnchor, policy: EvidencePolicy) -> dict[str, Any]:
        if not anchor.record_id.startswith("score:"):
            return {"status": "missing", "reason": "invalid_score_record"}
        try:
            score_id = int(anchor.record_id.split(":", 1)[1])
        except ValueError:
            return {"status": "missing", "reason": "invalid_score_record"}
        with _open_sync(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT id,attempt_id,dimension,value,detail,scored_at,"
                "evaluation_manifest_ref FROM scores WHERE id=? AND attempt_id=?",
                (score_id, anchor.attempt_id),
            ).fetchone()
        if row is None:
            return {"status": "missing", "reason": "score_not_found"}
        data = dict(row)
        detail = data.pop("detail")
        result = {
            "status": "resolved" if policy.allow_payload else "redacted",
            "source": "scores",
            "record_id": anchor.record_id,
            "metadata": data,
        }
        if policy.allow_payload:
            result["payload"] = {"detail": detail}
        return result

    def _artifact(self, anchor: EvidenceAnchor) -> dict[str, Any]:
        root = self.data_path / "attempts" / anchor.attempt_id / "skill_workspace"
        if not root.is_dir() or root.is_symlink():
            return {"status": "missing", "reason": "artifact_root_missing"}
        root_resolved = root.resolve()
        for path in root.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            try:
                relative = path.resolve().relative_to(root_resolved).as_posix()
            except (OSError, ValueError):
                continue
            record_id = f"artifact:{hashlib.sha256(relative.encode()).hexdigest()[:20]}"
            if record_id == anchor.record_id:
                stat = path.stat()
                return {
                    "status": "resolved",
                    "source": "artifacts",
                    "record_id": record_id,
                    "metadata": {
                        "name": path.name,
                        "relative_ref": relative,
                        "size": stat.st_size,
                        "content_hash": hash_bytes(path.read_bytes()),
                    },
                }
        return {"status": "missing", "reason": "artifact_not_found"}
