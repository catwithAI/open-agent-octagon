"""Repository skeleton for normalized attempt outputs."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.db import _now_iso, _open_sync
from backend.derived_payloads import DerivedPayloadStore
from backend.experiments.hashing import new_id


@dataclass
class NormalizationRepository:
    db_path: Path
    payloads: DerivedPayloadStore

    def add(
        self,
        *,
        attempt_id: str,
        source_hash: str,
        pipeline_hash: str,
        output: Any,
        status: str,
        warnings: list[str],
        schema_version: str,
        producer_version: str,
        input_refs: dict[str, Any],
    ) -> str:
        stored = self.payloads.put("normalized", output)
        output_ref = json.dumps(
            {
                "content_hash": stored.content_hash,
                "inline": stored.inline_json,
                "object_ref": stored.object_ref,
            },
            sort_keys=True,
        )
        with _open_sync(self.db_path) as conn:
            existing = conn.execute(
                "SELECT id FROM normalized_outputs WHERE attempt_id=? AND pipeline_hash=?",
                (attempt_id, pipeline_hash),
            ).fetchone()
            if existing is not None:
                return str(existing[0])
            output_id = new_id("normalized_output")
            conn.execute(
                "INSERT INTO normalized_outputs(id,attempt_id,source_hash,pipeline_hash,"
                "output_ref,status,warnings_json,schema_version,producer_version,input_refs_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    output_id,
                    attempt_id,
                    source_hash,
                    pipeline_hash,
                    output_ref,
                    status,
                    json.dumps(warnings, ensure_ascii=False),
                    schema_version,
                    producer_version,
                    json.dumps(input_refs, ensure_ascii=False, sort_keys=True),
                    _now_iso(),
                ),
            )
            conn.commit()
        return output_id

    def get(
        self,
        attempt_id: str,
        *,
        pipeline_hash: str,
        current_source_hash: str | None,
    ) -> dict[str, Any] | None:
        with _open_sync(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM normalized_outputs WHERE attempt_id=? AND pipeline_hash=?",
                (attempt_id, pipeline_hash),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["warnings"] = json.loads(item.pop("warnings_json"))
        item["input_refs"] = json.loads(item.pop("input_refs_json"))
        stale = current_source_hash is not None and current_source_hash != item["source_hash"]
        item["status"] = "stale" if stale else item["status"]
        item["output"] = None if stale else self.payloads.get(json.loads(item.pop("output_ref")))
        if stale:
            item.pop("output_ref")
        return item
