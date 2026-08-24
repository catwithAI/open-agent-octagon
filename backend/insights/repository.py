"""Repository skeleton for versioned insight reports."""

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
class InsightRepository:
    db_path: Path
    payloads: DerivedPayloadStore

    def next_version(self, experiment_id: str) -> int:
        with _open_sync(self.db_path) as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM insight_reports WHERE experiment_id=?",
                (experiment_id,),
            ).fetchone()
        return int(row[0])

    def list(
        self,
        experiment_id: str,
        *,
        before_version: int | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("insight list limit must be between 1 and 100")
        clause = " AND version<?" if before_version is not None else ""
        params: tuple[Any, ...] = (
            (experiment_id, before_version, limit)
            if before_version is not None
            else (experiment_id, limit)
        )
        with _open_sync(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id,experiment_id,version,bundle_hash,generator_json,status,"
                "schema_version,producer_version,input_refs_json,created_at "
                f"FROM insight_reports WHERE experiment_id=?{clause} "
                "ORDER BY version DESC LIMIT ?",
                params,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["generator"] = json.loads(item.pop("generator_json"))
            item["input_refs"] = json.loads(item.pop("input_refs_json"))
            result.append(item)
        return result

    def get(self, experiment_id: str, version: int) -> dict[str, Any] | None:
        with _open_sync(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM insight_reports WHERE experiment_id=? AND version=?",
                (experiment_id, version),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["generator"] = json.loads(item.pop("generator_json"))
        item["input_refs"] = json.loads(item.pop("input_refs_json"))
        item["report"] = self.payloads.get(json.loads(item.pop("report_json")))
        return item

    def add(
        self,
        *,
        experiment_id: str,
        version: int,
        bundle_hash: str,
        generator: dict[str, Any],
        report: dict[str, Any],
        status: str,
        schema_version: str,
        producer_version: str,
        input_refs: dict[str, Any],
    ) -> str:
        report_id = new_id("insight_report")
        stored = self.payloads.put("insights", report)
        report_index = {
            "content_hash": stored.content_hash,
            "inline": stored.inline_json,
            "object_ref": stored.object_ref,
        }
        with _open_sync(self.db_path) as conn:
            conn.execute(
                "INSERT INTO insight_reports(id,experiment_id,version,bundle_hash,generator_json,"
                "report_json,status,schema_version,producer_version,input_refs_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    report_id,
                    experiment_id,
                    version,
                    bundle_hash,
                    json.dumps(generator, sort_keys=True),
                    json.dumps(report_index, sort_keys=True),
                    status,
                    schema_version,
                    producer_version,
                    json.dumps(input_refs, sort_keys=True),
                    _now_iso(),
                ),
            )
            conn.commit()
        return report_id
