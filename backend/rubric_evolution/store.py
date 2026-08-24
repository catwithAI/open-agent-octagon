from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Literal

from backend.db import _init_db_sync, _now_iso, _open_sync
from backend.experiments.hashing import canonical_hash

from .models import (
    CandidateRubric,
    EvolutionBatch,
    JudgeCheckRecord,
    JudgeRecord,
    RubricEvolutionResult,
    RubricScoreView,
)

ScoreKind = Literal["official", "replay", "shadow"]


def append_judge_record_sync(
    db_path: Path,
    record: JudgeRecord | dict[str, Any],
    *,
    operation_id: str | None = None,
) -> str:
    _init_db_sync(db_path)
    parsed = record if isinstance(record, JudgeRecord) else JudgeRecord.model_validate(record)
    with _open_sync(db_path) as conn:
        if operation_id is not None:
            existing = conn.execute(
                "SELECT id FROM rubric_judge_records WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if existing is not None:
                return str(existing[0])
        cursor = conn.execute(
            "INSERT OR IGNORE INTO rubric_judge_records(id,env_name,rubric_version,run_id,attempt_id,"
            "judge_execution_id,created_at,valid_execution,checks_json,execution_error,"
            "metadata_json,operation_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                parsed.record_id,
                parsed.env_name,
                parsed.rubric_version,
                parsed.run_id,
                parsed.attempt_id,
                parsed.judge_execution_id,
                parsed.created_at,
                int(parsed.valid_execution),
                json.dumps(
                    [item.model_dump(mode="json") for item in parsed.checks],
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                parsed.execution_error,
                json.dumps(parsed.metadata, ensure_ascii=False, sort_keys=True),
                operation_id,
            ),
        )
        if cursor.rowcount == 0:
            if operation_id is None:
                raise ValueError("Judge Record id already exists")
            existing = conn.execute(
                "SELECT id FROM rubric_judge_records WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if existing is None:
                raise ValueError("Judge Record operation collided without a stored row")
            conn.commit()
            return str(existing[0])
        conn.commit()
    return parsed.record_id


def list_unbatched_judge_records_sync(
    *, db_path: Path, scope_key: str, env_name: str | None = None,
    rubric_version: str | None = None, limit: int = 1000,
) -> list[JudgeRecord]:
    _init_db_sync(db_path)
    clauses = ["r.valid_execution=1"]
    params: list[Any] = []
    if env_name is not None:
        clauses.append("r.env_name=?")
        params.append(env_name)
    if rubric_version is not None:
        clauses.append("r.rubric_version=?")
        params.append(rubric_version)
    params.extend([scope_key, max(1, min(limit, 10000))])
    query = (
        "SELECT r.* FROM rubric_judge_records r WHERE " + " AND ".join(clauses) +
        " AND NOT EXISTS (SELECT 1 FROM rubric_evolution_batch_members m "
        "WHERE m.record_id=r.id AND m.scope_key=? AND m.role='trigger') "
        "ORDER BY r.created_at,r.id LIMIT ?"
    )
    with _open_sync(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, tuple(params)).fetchall()
    result: list[JudgeRecord] = []
    for row in rows:
        result.append(JudgeRecord(
            record_id=row["id"],
            env_name=row["env_name"],
            rubric_version=row["rubric_version"],
            run_id=row["run_id"],
            attempt_id=row["attempt_id"],
            judge_execution_id=row["judge_execution_id"],
            created_at=row["created_at"],
            valid_execution=bool(row["valid_execution"]),
            checks=json.loads(row["checks_json"] or "[]"),
            execution_error=row["execution_error"],
            metadata=json.loads(row["metadata_json"] or "{}"),
        ))
    return result


def persist_batch_membership_sync(
    db_path: Path, batch: EvolutionBatch, *, scope_key: str
) -> None:
    _init_db_sync(db_path)
    trigger_count = batch.trigger_record_count
    now = _now_iso()
    with _open_sync(db_path) as conn:
        for index, record in enumerate(batch.records):
            conn.execute(
                "INSERT OR IGNORE INTO rubric_evolution_batch_members("
                "record_id,batch_id,scope_key,role,created_at) VALUES(?,?,?,?,?)",
                (
                    record.record_id,
                    batch.batch_id,
                    scope_key,
                    "trigger" if index < trigger_count else "overlap",
                    now,
                ),
            )
        conn.commit()


def latest_batch_records_sync(
    *, db_path: Path, scope_key: str, limit: int = 1000
) -> list[JudgeRecord]:
    _init_db_sync(db_path)
    with _open_sync(db_path) as conn:
        latest = conn.execute(
            "SELECT batch_id FROM rubric_evolution_batch_members WHERE scope_key=? "
            "ORDER BY created_at DESC LIMIT 1",
            (scope_key,),
        ).fetchone()
        if latest is None:
            return []
        ids = [
            row[0] for row in conn.execute(
                "SELECT record_id FROM rubric_evolution_batch_members "
                "WHERE batch_id=? AND scope_key=? ORDER BY created_at,record_id LIMIT ?",
                (latest[0], scope_key, max(1, min(limit, 10000))),
            ).fetchall()
        ]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    with _open_sync(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT * FROM rubric_judge_records WHERE id IN ({placeholders}) "
            "ORDER BY created_at,id",
            tuple(ids),
        ).fetchall()
    return [JudgeRecord(
        record_id=row["id"], env_name=row["env_name"],
        rubric_version=row["rubric_version"], run_id=row["run_id"],
        attempt_id=row["attempt_id"], judge_execution_id=row["judge_execution_id"],
        created_at=row["created_at"], valid_execution=bool(row["valid_execution"]),
        checks=json.loads(row["checks_json"] or "[]"),
        execution_error=row["execution_error"],
        metadata=json.loads(row["metadata_json"] or "{}"),
    ) for row in rows]


def register_active_rubric_sync(
    *, db_path: Path, env_name: str, version: str,
    rubric: dict[str, Any], actor: str, executor_kind: str = "hybrid",
) -> str:
    """Human bootstrap for an existing production Rubric of any source shape."""
    if not env_name.strip() or not version.strip() or not actor.strip():
        raise ValueError("env_name, version and actor are required")
    _init_db_sync(db_path)
    rubric_hash = canonical_hash(rubric)
    rubric_id = f"rub_{rubric_hash.split(':')[-1][:24]}"
    now = _now_iso()
    with _open_sync(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT rubric_hash FROM rubric_versions WHERE env_name=? AND version=?",
            (env_name, version),
        ).fetchone()
        if existing is not None and existing[0] != rubric_hash:
            raise ValueError("rubric version already exists with different content")
        if existing is None:
            conn.execute(
                "INSERT INTO rubric_versions(id,env_name,version,parent_version,scope,status,"
                "rubric_json,rubric_hash,source_batch_id,created_at,published_at,published_by,"
                "evolution_domain,executor_kind) "
                "VALUES(?,?,?,'bootstrap','environment','published',?,?,NULL,?,?,?,'product',?)",
                (
                    rubric_id, env_name, version,
                    json.dumps(rubric, ensure_ascii=False, sort_keys=True),
                    rubric_hash, now, now, actor, executor_kind,
                ),
            )
        else:
            stored = conn.execute(
                "SELECT id FROM rubric_versions WHERE env_name=? AND version=?",
                (env_name, version),
            ).fetchone()
            rubric_id = str(stored[0])
        conn.execute(
            "INSERT INTO active_rubrics(env_name,rubric_version,rubric_id,updated_at) "
            "VALUES(?,?,?,?) ON CONFLICT(env_name) DO UPDATE SET "
            "rubric_version=excluded.rubric_version,rubric_id=excluded.rubric_id,"
            "updated_at=excluded.updated_at",
            (env_name, version, rubric_id, now),
        )
        conn.commit()
    return rubric_id


def list_rubric_registry_sync(db_path: Path) -> list[dict[str, Any]]:
    _init_db_sync(db_path)
    with _open_sync(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT rv.id,rv.env_name,rv.version,rv.parent_version,rv.scope,rv.status,"
            "rv.evolution_domain,rv.executor_kind,"
            "rv.rubric_hash,rv.source_batch_id,rv.created_at,rv.published_at,"
            "rv.published_by,CASE WHEN ar.rubric_id=rv.id THEN 1 ELSE 0 END AS active "
            "FROM rubric_versions rv LEFT JOIN active_rubrics ar ON ar.rubric_id=rv.id "
            "ORDER BY rv.created_at DESC,rv.id"
        ).fetchall()
    return [dict(row) for row in rows]


def list_batch_summaries_sync(db_path: Path, limit: int = 100) -> list[dict[str, Any]]:
    _init_db_sync(db_path)
    with _open_sync(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT batch_id,scope_key,"
            "SUM(CASE WHEN role='trigger' THEN 1 ELSE 0 END) AS trigger_count,"
            "SUM(CASE WHEN role='overlap' THEN 1 ELSE 0 END) AS overlap_count,"
            "MIN(created_at) AS created_at FROM rubric_evolution_batch_members "
            "GROUP BY batch_id,scope_key ORDER BY created_at DESC LIMIT ?",
            (max(1, min(limit, 1000)),),
        ).fetchall()
    return [dict(row) for row in rows]


def reconcile_unexecutable_active_rubrics_sync(db_path: Path) -> list[dict[str, Any]]:
    """Quarantine Product candidates activated by pre-executor releases.

    Versions produced by an Evolution Batch (``source_batch_id`` is non-null)
    are not executable by the environment-native scorer. Restore the newest
    native/bootstrap version, or remove the Active pointer so startup bootstrap
    can rediscover the environment contract.
    """
    _init_db_sync(db_path)
    repaired: list[dict[str, Any]] = []
    with _open_sync(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT ar.env_name,ar.rubric_version,ar.rubric_id "
            "FROM active_rubrics ar JOIN rubric_versions rv ON rv.id=ar.rubric_id "
            "WHERE rv.source_batch_id IS NOT NULL"
        ).fetchall()
        for row in rows:
            fallback = conn.execute(
                "SELECT id,version FROM rubric_versions WHERE env_name=? "
                "AND source_batch_id IS NULL AND status='published' "
                "ORDER BY COALESCE(published_at,created_at) DESC,created_at DESC LIMIT 1",
                (row["env_name"],),
            ).fetchone()
            conn.execute(
                "UPDATE rubric_versions SET status='quarantined_unexecutable' WHERE id=?",
                (row["rubric_id"],),
            )
            if fallback is None:
                conn.execute(
                    "DELETE FROM active_rubrics WHERE env_name=?", (row["env_name"],)
                )
                restored = None
            else:
                conn.execute(
                    "UPDATE active_rubrics SET rubric_version=?,rubric_id=?,updated_at=? "
                    "WHERE env_name=?",
                    (fallback["version"], fallback["id"], _now_iso(), row["env_name"]),
                )
                restored = str(fallback["version"])
            repaired.append({
                "env_name": str(row["env_name"]),
                "quarantined_version": str(row["rubric_version"]),
                "restored_version": restored,
            })
        conn.commit()
    return repaired


def get_active_rubric_version_sync(db_path: Path, env_name: str) -> str | None:
    _init_db_sync(db_path)
    with _open_sync(db_path) as conn:
        row = conn.execute(
            "SELECT ar.rubric_version FROM active_rubrics ar "
            "JOIN rubric_versions rv ON rv.id=ar.rubric_id "
            "WHERE ar.env_name=? AND rv.source_batch_id IS NULL "
            "AND rv.status='published'",
            (env_name,),
        ).fetchone()
    return str(row[0]) if row else None


def get_published_rubric_sync(
    db_path: Path, env_name: str, version: str | None = None
) -> dict[str, Any] | None:
    _init_db_sync(db_path)
    with _open_sync(db_path) as conn:
        if version is None:
            row = conn.execute(
                "SELECT rv.rubric_json FROM active_rubrics ar "
                "JOIN rubric_versions rv ON rv.id=ar.rubric_id WHERE ar.env_name=? "
                "AND rv.source_batch_id IS NULL AND rv.status='published'",
                (env_name,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT rubric_json FROM rubric_versions "
                "WHERE env_name=? AND version=? AND status='published'",
                (env_name, version),
            ).fetchone()
    return json.loads(row[0]) if row else None


def _load_candidate_artifact(artifact_dir: Path) -> tuple[RubricEvolutionResult, str]:
    result_path = artifact_dir / "evolution-result.json"
    batch_path = artifact_dir / "batch.json"
    if not result_path.is_file() or not batch_path.is_file():
        raise ValueError("rubric evolution artifact is incomplete")
    result = RubricEvolutionResult.model_validate_json(
        result_path.read_text(encoding="utf-8")
    )
    if result.result != "candidate_generated" or result.candidate_rubric is None:
        raise ValueError("artifact does not contain a candidate rubric")
    batch = json.loads(batch_path.read_text(encoding="utf-8"))
    batch_id = str(batch.get("batch_id") or "")
    if not batch_id:
        raise ValueError("batch artifact has no batch_id")
    return result, batch_id


def publish_candidate_sync(
    *,
    db_path: Path,
    artifact_dir: Path,
    actor: str,
) -> CandidateRubric:
    """Human-gated publication. This is the only active-version mutation path."""
    if not actor.strip():
        raise ValueError("publication actor is required")
    _init_db_sync(db_path)
    result, batch_id = _load_candidate_artifact(artifact_dir)
    candidate = result.candidate_rubric
    assert candidate is not None
    if candidate.scope == "environment":
        raise ValueError(
            "environment candidate cannot be activated: no official scoring executor "
            "is installed for this evolved Rubric version"
        )
    payload = candidate.model_dump(mode="json", by_alias=True)
    rubric_hash = canonical_hash(payload)
    rubric_id = f"rub_{rubric_hash.split(':')[-1][:24]}"
    now = _now_iso()
    env_name = candidate.env_name
    with _open_sync(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT rubric_hash,status FROM rubric_versions WHERE id=?", (rubric_id,)
        ).fetchone()
        if existing is not None:
            if existing["rubric_hash"] != rubric_hash:
                raise ValueError("rubric id collision")
            conn.commit()
            return candidate

        conn.execute(
            "INSERT INTO rubric_versions(id,env_name,version,parent_version,scope,status,"
            "rubric_json,rubric_hash,source_batch_id,created_at,published_at,published_by,"
            "evolution_domain,executor_kind) "
            "VALUES(?,?,?,?,?,'published',?,?,?,?,?,?,'product','llm_as_judge')",
            (
                rubric_id,
                env_name,
                candidate.proposed_version,
                candidate.parent_version,
                candidate.scope,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                rubric_hash,
                batch_id,
                now,
                now,
                actor.strip(),
            ),
        )
        # Cross-environment candidates are published templates. They never mutate
        # an environment's Active Product Rubric implicitly.
        conn.commit()
    return candidate


def append_score_revision_sync(
    *,
    db_path: Path,
    attempt_id: str,
    rubric_version: str,
    score_kind: ScoreKind,
    score: RubricScoreView,
    checks: list[JudgeCheckRecord | dict[str, Any]],
    evaluation_manifest_ref: str | None = None,
    source_batch_id: str | None = None,
    operation_id: str | None = None,
) -> str:
    """Append replay/shadow/official score without changing legacy score rows."""
    _init_db_sync(db_path)
    revision_id = f"rsr_{uuid.uuid4().hex}"
    parsed_checks = [
        item if isinstance(item, JudgeCheckRecord) else JudgeCheckRecord.model_validate(item)
        for item in checks
    ]
    with _open_sync(db_path) as conn:
        if operation_id:
            existing_revision = conn.execute(
                "SELECT id FROM rubric_score_revisions WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if existing_revision is not None:
                return str(existing_revision[0])
        exists = conn.execute(
            "SELECT 1 FROM attempts WHERE id=?", (attempt_id,)
        ).fetchone()
        if exists is None:
            raise ValueError(f"attempt not found: {attempt_id}")
        conn.execute(
            "INSERT INTO rubric_score_revisions(id,attempt_id,rubric_version,score_kind,"
            "score_with_unknown,normalized_score_without_unknown,unknown_count,unknown_weight,"
            "checks_json,evaluation_manifest_ref,source_batch_id,operation_id,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                revision_id,
                attempt_id,
                rubric_version,
                score_kind,
                score.score_with_unknown,
                score.normalized_score_without_unknown,
                score.unknown_count,
                score.unknown_weight,
                json.dumps(
                    [item.model_dump(mode="json") for item in parsed_checks],
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                evaluation_manifest_ref,
                source_batch_id,
                operation_id,
                _now_iso(),
            ),
        )
        conn.commit()
    return revision_id


def list_score_revisions_sync(db_path: Path, attempt_id: str) -> list[dict[str, Any]]:
    _init_db_sync(db_path)
    with _open_sync(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM rubric_score_revisions WHERE attempt_id=? "
            "ORDER BY created_at,id",
            (attempt_id,),
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["checks"] = json.loads(item.pop("checks_json"))
        result.append(item)
    return result
