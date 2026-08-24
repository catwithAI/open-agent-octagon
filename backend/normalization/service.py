"""Idempotent Normalized Output generation service."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.db import (
    IdempotencyConflict,
    IdempotencyInProgress,
    _open_sync,
    claim_idempotency,
    complete_idempotency,
    fail_idempotency,
)
from backend.experiments.hashing import canonical_hash, hash_bytes

from .pipeline import PIPELINE_HASH, normalize_attempt
from .repository import NormalizationRepository


class NormalizationConflict(RuntimeError):
    pass


def current_source_hash(data_path: Path, attempt_id: str) -> str | None:
    path = Path(data_path) / "attempts" / attempt_id / "final_state.json"
    if not path.is_file() or path.is_symlink():
        return None
    return hash_bytes(path.read_bytes())


def generate_idempotent(
    *,
    db_path: Path,
    data_path: Path,
    attempt_id: str,
    repository: NormalizationRepository,
    idempotency_key: str,
) -> dict[str, Any]:
    source_hash = current_source_hash(data_path, attempt_id)
    request_hash = canonical_hash(
        {
            "attempt_id": attempt_id,
            "source_hash": source_hash,
            "pipeline_hash": PIPELINE_HASH,
        }
    )
    try:
        with _open_sync(Path(db_path)) as conn:
            claim = claim_idempotency(
                conn,
                operation="normalized-output:generate",
                key=idempotency_key,
                request_hash=request_hash,
            )
            conn.commit()
        if claim.replayed:
            return {**(claim.response or {}), "replayed": True}
    except IdempotencyConflict as exc:
        raise NormalizationConflict("idempotency_conflict") from exc
    except IdempotencyInProgress as exc:
        raise NormalizationConflict("idempotency_in_progress") from exc
    try:
        document = normalize_attempt(
            db_path=Path(db_path), data_path=Path(data_path), attempt_id=attempt_id
        )
        output_id = repository.add(
            attempt_id=attempt_id,
            source_hash=document.source_hash,
            pipeline_hash=document.pipeline_hash,
            output=document.model_dump(mode="json"),
            status="current",
            warnings=[],
            schema_version=document.schema_version,
            producer_version=str(document.transform_manifest["pipeline_version"]),
            input_refs={"raw_ref": f"attempts/{attempt_id}/final_state.json"},
        )
        response = {
            "id": output_id,
            "attempt_id": attempt_id,
            "status": "current",
            "source_hash": document.source_hash,
            "pipeline_hash": document.pipeline_hash,
        }
        with _open_sync(Path(db_path)) as conn:
            complete_idempotency(
                conn,
                operation="normalized-output:generate",
                key=idempotency_key,
                result_id=output_id,
                response=response,
            )
            conn.commit()
        return {**response, "replayed": False}
    except Exception:
        with _open_sync(Path(db_path)) as conn:
            fail_idempotency(
                conn, operation="normalized-output:generate", key=idempotency_key
            )
            conn.commit()
        raise
