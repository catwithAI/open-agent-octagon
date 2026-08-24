"""Idempotent Insight generation orchestration with a DB active-job lease."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from backend.db import (
    IdempotencyConflict,
    IdempotencyInProgress,
    _open_sync,
    claim_idempotency,
    complete_idempotency,
    fail_idempotency,
)
from backend.experiments.hashing import canonical_hash

from .generator import generate_and_store
from .providers import InsightProvider
from .repository import InsightRepository


class InsightConflict(RuntimeError):
    pass


class InsightInProgress(RuntimeError):
    pass


async def generate_idempotent(
    *,
    experiment_id: str,
    bundle: dict[str, Any],
    provider: InsightProvider,
    repository: InsightRepository,
    idempotency_key: str,
    source_version: int | None = None,
) -> dict[str, Any]:
    operation = "insight:generate"
    request_hash = canonical_hash(
        {
            "experiment_id": experiment_id,
            "bundle_hash": bundle["bundle_hash"],
            "provider": getattr(provider, "provider_name", provider.__class__.__name__),
            "model": getattr(provider, "model", None),
            "source_version": source_version,
        }
    )
    try:
        with _open_sync(repository.db_path) as conn:
            claim = claim_idempotency(
                conn,
                operation=operation,
                key=idempotency_key,
                request_hash=request_hash,
            )
            conn.commit()
        if claim.replayed:
            return {**(claim.response or {}), "replayed": True}
    except IdempotencyConflict as exc:
        raise InsightConflict("idempotency_conflict") from exc
    except IdempotencyInProgress as exc:
        raise InsightInProgress("idempotency_in_progress") from exc

    active_claimed = False
    try:
        with _open_sync(repository.db_path) as conn:
            claim_idempotency(
                conn,
                operation="insight:active",
                key=experiment_id,
                request_hash="octagon-insight-active-v1",
            )
            conn.commit()
            active_claimed = True
    except IdempotencyInProgress as exc:
        with _open_sync(repository.db_path) as conn:
            fail_idempotency(conn, operation=operation, key=idempotency_key)
            conn.commit()
        raise InsightInProgress("insight_generation_in_progress") from exc
    except IdempotencyConflict as exc:  # defensive: fixed active request hash
        raise InsightConflict("insight_active_lease_conflict") from exc

    try:
        outcome = await generate_and_store(
            experiment_id=experiment_id,
            bundle=bundle,
            provider=provider,
            repository=repository,
        )
        response = {
            key: value
            for key, value in asdict(outcome).items()
            if key != "report"
        }
        response["limitations"] = (
            outcome.report.get("limitations", []) if outcome.report else []
        )
        with _open_sync(repository.db_path) as conn:
            complete_idempotency(
                conn,
                operation=operation,
                key=idempotency_key,
                result_id=str(outcome.report_id),
                response=response,
            )
            fail_idempotency(conn, operation="insight:active", key=experiment_id)
            conn.commit()
        return {**response, "replayed": False}
    except Exception:
        with _open_sync(repository.db_path) as conn:
            fail_idempotency(conn, operation=operation, key=idempotency_key)
            if active_claimed:
                fail_idempotency(conn, operation="insight:active", key=experiment_id)
            conn.commit()
        raise
