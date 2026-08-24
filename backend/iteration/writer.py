"""iterations.jsonl append-only writer 与原子恢复 checkpoint。"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO


ITERATIONS_FILENAME = "iterations.jsonl"
ITERATION_STATE_FILENAME = "iteration_state.json"
SCHEMA_VERSION = "octagon-iteration-v1"


def now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def operation_id(
    attempt_id: str, operation: str, submission_id: str | None = None
) -> str:
    scope = submission_id or "attempt"
    return f"{attempt_id}:{scope}:{operation}"


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class IterationEventWriter:
    def __init__(self, path: Path, *, attempt_id: str) -> None:
        self._path = Path(path)
        self._attempt_id = attempt_id
        self._fp: TextIO | None = None

    def emit(
        self,
        event: str,
        *,
        operation: str,
        submission_id: str | None = None,
        round_index: int | None = None,
        timestamp: str | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        record = {
            "schema_version": SCHEMA_VERSION,
            "event": event,
            "attempt_id": self._attempt_id,
            "timestamp": timestamp or now_iso(),
            "operation_id": operation_id(
                self._attempt_id, operation, submission_id
            ),
        }
        if submission_id is not None:
            record["submission_id"] = submission_id
        if round_index is not None:
            record["round_index"] = round_index
        record.update(fields)

        if self._fp is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._fp = self._path.open("a", encoding="utf-8")
        self._fp.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._fp.flush()
        return record

    def close(self) -> None:
        if self._fp is not None:
            self._fp.close()
            self._fp = None

    def __enter__(self) -> "IterationEventWriter":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()
