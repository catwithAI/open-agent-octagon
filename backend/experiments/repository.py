"""Typed transactional persistence for Experiment aggregate roots."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.db import (
    IdempotencyConflict,
    IdempotencyInProgress,
    _init_db_sync,
    _now_iso,
    _open_sync,
    claim_idempotency,
    complete_idempotency,
)

from .hashing import canonical_hash, canonical_json_bytes, new_id
from .models import (
    CellStatus,
    ExperimentProtocol,
    MatrixCell,
    RunGroupPlan,
    VariantSpec,
)


class RepositoryConflict(RuntimeError):
    pass


class RepositoryInProgress(RuntimeError):
    pass


class FrozenVariant(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^var_[0-9a-f]{20}$")
    kind: str
    spec: VariantSpec
    source_hash: str
    content_hash: str
    prompt_ref: str | None = Field(default=None, repr=False)
    context_delta: dict[str, Any] = Field(default_factory=dict, repr=False)
    summary: dict[str, Any] = Field(default_factory=dict)
    status: str = "ready"
    error_code: str | None = None


class CreateExperimentBundle(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(min_length=1)
    parent_experiment_id: str | None = Field(
        default=None, pattern=r"^exp_[0-9a-f]{20}$"
    )
    env_name: str = Field(min_length=1)
    source_task_id: str | None = None
    question: str = Field(min_length=1, repr=False)
    protocol: ExperimentProtocol
    variants: tuple[FrozenVariant, ...] = Field(min_length=1)
    group_id: str = Field(pattern=r"^grp_[0-9a-f]{20}$")
    group_plan: RunGroupPlan

    @model_validator(mode="after")
    def _cells_reference_frozen_variants(self) -> "CreateExperimentBundle":
        variants = {item.id for item in self.variants}
        missing = sorted(
            {cell.variant_id for cell in self.group_plan.cells} - variants
        )
        if missing:
            raise ValueError(f"cells reference unknown variants: {missing}")
        if len(self.protocol.variant_specs) != len(self.variants):
            raise ValueError("protocol and frozen variant cardinality differ")
        return self


@dataclass(frozen=True)
class CreatedExperimentBundle:
    experiment_id: str
    run_group_id: str
    variant_ids: tuple[str, ...]
    cell_ids: tuple[str, ...]
    replayed: bool = False

    def response(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "run_group_id": self.run_group_id,
            "variant_ids": list(self.variant_ids),
            "cell_ids": list(self.cell_ids),
        }


@dataclass
class ExperimentRepository:
    db_path: Path
    data_path: Path

    def __post_init__(self) -> None:
        self.db_path = Path(self.db_path)
        self.data_path = Path(self.data_path)
        _init_db_sync(self.db_path)

    def create_bundle(
        self,
        request: CreateExperimentBundle,
        *,
        idempotency_key: str,
        experiment_id: str | None = None,
    ) -> CreatedExperimentBundle:
        """Atomically create Experiment, variants, group, cells and key."""
        experiment_id = experiment_id or new_id("experiment")
        request_hash = canonical_hash(request)
        now = _now_iso()
        protocol_json = canonical_json_bytes(request.protocol).decode("utf-8")
        protocol_hash = canonical_hash(request.protocol)
        plan_json = canonical_json_bytes(request.group_plan).decode("utf-8")
        plan_hash = canonical_hash(request.group_plan)
        response = CreatedExperimentBundle(
            experiment_id=experiment_id,
            run_group_id=request.group_id,
            variant_ids=tuple(item.id for item in request.variants),
            cell_ids=tuple(item.id for item in request.group_plan.cells),
        )
        try:
            with _open_sync(self.db_path) as conn:
                claim = claim_idempotency(
                    conn,
                    operation="experiment:create",
                    key=idempotency_key,
                    request_hash=request_hash,
                )
                if claim.replayed:
                    stored = claim.response or {}
                    return CreatedExperimentBundle(
                        experiment_id=str(stored["experiment_id"]),
                        run_group_id=str(stored["run_group_id"]),
                        variant_ids=tuple(stored.get("variant_ids") or ()),
                        cell_ids=tuple(stored.get("cell_ids") or ()),
                        replayed=True,
                    )
                conn.execute(
                    "INSERT INTO experiments(id,parent_experiment_id,title,env_name,"
                    "source_task_id,question,"
                    "protocol_json,protocol_hash,schema_version,status,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?, 'queued', ?,?)",
                    (
                        experiment_id,
                        request.parent_experiment_id,
                        request.title,
                        request.env_name,
                        request.source_task_id,
                        request.question,
                        protocol_json,
                        protocol_hash,
                        request.protocol.schema_version,
                        now,
                        now,
                    ),
                )
                self._insert_variants(
                    conn,
                    experiment_id,
                    request.source_task_id,
                    request.variants,
                    now,
                )
                conn.execute(
                    "INSERT INTO run_groups(id,experiment_id,strategy,status,plan_json,"
                    "plan_hash,stop_policy_json,total_cells,created_at) "
                    "VALUES(?,?,?,'queued',?,?,?,?,?)",
                    (
                        request.group_id,
                        experiment_id,
                        request.group_plan.strategy,
                        plan_json,
                        plan_hash,
                        json.dumps(
                            request.group_plan.stop_policy,
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        len(request.group_plan.cells),
                        now,
                    ),
                )
                self._insert_cells(conn, request.group_id, request.group_plan.cells, now)
                self.append_group_event(
                    conn,
                    request.group_id,
                    "group.created",
                    {
                        "status": "queued",
                        "total_cells": len(request.group_plan.cells),
                    },
                    now=now,
                )
                complete_idempotency(
                    conn,
                    operation="experiment:create",
                    key=idempotency_key,
                    result_id=experiment_id,
                    response=response.response(),
                )
                conn.commit()
        except IdempotencyConflict as exc:
            raise RepositoryConflict("idempotency_conflict") from exc
        except IdempotencyInProgress as exc:
            raise RepositoryInProgress("idempotency_in_progress") from exc
        return response

    @staticmethod
    def append_group_event(
        conn: sqlite3.Connection,
        group_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        now: str | None = None,
    ) -> int:
        sequence = conn.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM run_group_events "
            "WHERE run_group_id=?",
            (group_id,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO run_group_events(run_group_id,sequence,event_type,payload_json,"
            "created_at) VALUES(?,?,?,?,?)",
            (
                group_id,
                sequence,
                event_type,
                canonical_json_bytes(payload).decode("utf-8"),
                now or _now_iso(),
            ),
        )
        return int(sequence)

    @staticmethod
    def _insert_variants(
        conn: sqlite3.Connection,
        experiment_id: str,
        source_task_id: str | None,
        variants: Iterable[FrozenVariant],
        now: str,
    ) -> None:
        conn.executemany(
            "INSERT INTO task_variants(id,experiment_id,source_task_id,kind,mutator_id,mutator_version,"
            "seed,params_json,source_hash,content_hash,prompt_ref,context_delta_json,"
            "summary_json,status,error_code,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    variant.id,
                    experiment_id,
                    source_task_id,
                    variant.kind,
                    variant.spec.mutator_id,
                    variant.spec.mutator_version,
                    variant.spec.seed,
                    canonical_json_bytes(variant.spec.params).decode("utf-8"),
                    variant.source_hash,
                    variant.content_hash,
                    variant.prompt_ref,
                    canonical_json_bytes(variant.context_delta).decode("utf-8"),
                    canonical_json_bytes(variant.summary).decode("utf-8"),
                    variant.status,
                    variant.error_code,
                    now,
                )
                for variant in variants
            ],
        )

    @staticmethod
    def _insert_cells(
        conn: sqlite3.Connection,
        group_id: str,
        cells: Iterable[MatrixCell],
        now: str,
    ) -> None:
        conn.executemany(
            "INSERT INTO run_group_cells(id,run_group_id,variant_id,repeat_index,run_id,"
            "status,error_code,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            [
                (
                    cell.id,
                    group_id,
                    cell.variant_id,
                    cell.repeat_index,
                    cell.run_id,
                    cell.status,
                    cell.error_code,
                    now,
                    now,
                )
                for cell in cells
            ],
        )

    def transition_cell(
        self,
        cell_id: str,
        *,
        expected: set[CellStatus],
        target: CellStatus,
        run_id: str | None = None,
        error_code: str | None = None,
    ) -> bool:
        if not expected:
            raise ValueError("expected states must not be empty")
        placeholders = ",".join("?" for _ in expected)
        with _open_sync(self.db_path) as conn:
            cursor = conn.execute(
                f"UPDATE run_group_cells SET status=?, run_id=COALESCE(?,run_id), "
                f"error_code=?, updated_at=? WHERE id=? AND status IN ({placeholders})",
                (
                    target,
                    run_id,
                    error_code,
                    _now_iso(),
                    cell_id,
                    *sorted(expected),
                ),
            )
            if cursor.rowcount == 1:
                self.append_group_event(
                    conn,
                    conn.execute(
                        "SELECT run_group_id FROM run_group_cells WHERE id=?",
                        (cell_id,),
                    ).fetchone()[0],
                    "cell.transition",
                    {
                        "cell_id": cell_id,
                        "status": target,
                        "run_id": run_id,
                        "error_code": error_code,
                    },
                )
            conn.commit()
            return cursor.rowcount == 1

    def transition_group(
        self, group_id: str, *, expected: set[str], target: str
    ) -> bool:
        if not expected:
            raise ValueError("expected states must not be empty")
        placeholders = ",".join("?" for _ in expected)
        now = _now_iso()
        started_at = now if target == "running" else None
        ended_at = now if target in {"partial", "completed", "failed", "cancelled"} else None
        with _open_sync(self.db_path) as conn:
            cursor = conn.execute(
                f"UPDATE run_groups SET status=?, started_at=COALESCE(started_at,?), "
                f"ended_at=COALESCE(?,ended_at) WHERE id=? AND status IN ({placeholders})",
                (target, started_at, ended_at, group_id, *sorted(expected)),
            )
            if cursor.rowcount == 1:
                self.append_group_event(
                    conn,
                    group_id,
                    "group.transition",
                    {"status": target},
                    now=now,
                )
            conn.commit()
            return cursor.rowcount == 1

    def get_bundle(self, experiment_id: str) -> dict[str, Any] | None:
        with _open_sync(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            experiment = conn.execute(
                "SELECT * FROM experiments WHERE id=?", (experiment_id,)
            ).fetchone()
            if experiment is None:
                return None
            variants = conn.execute(
                "SELECT * FROM task_variants WHERE experiment_id=? ORDER BY created_at,id",
                (experiment_id,),
            ).fetchall()
            groups = conn.execute(
                "SELECT * FROM run_groups WHERE experiment_id=? ORDER BY created_at,id",
                (experiment_id,),
            ).fetchall()
            group_ids = [row["id"] for row in groups]
            cells: list[sqlite3.Row] = []
            if group_ids:
                placeholders = ",".join("?" for _ in group_ids)
                cells = conn.execute(
                    f"SELECT * FROM run_group_cells WHERE run_group_id IN ({placeholders}) "
                    "ORDER BY created_at,id",
                    group_ids,
                ).fetchall()
        return {
            "experiment": dict(experiment),
            "variants": [dict(row) for row in variants],
            "groups": [dict(row) for row in groups],
            "cells": [dict(row) for row in cells],
        }
