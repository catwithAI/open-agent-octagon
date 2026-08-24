"""Batch scanner for the production Rubric Evolution Loop."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.db import _open_sync
from backend.insights.providers import InsightProvider

from .batching import prepare_evolution_batch
from .directional_loops import run_directional_cycle
from .pipeline import RubricEvolutionOutcome, evolve_rubric
from .store import (
    latest_batch_records_sync,
    list_unbatched_judge_records_sync,
    persist_batch_membership_sync,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SchedulerCycle:
    environment_batches: int
    cross_environment_batches: int
    outcomes: tuple[RubricEvolutionOutcome, ...]


def _latest_analysis_artifacts(
    data_path: Path, run_ids: set[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    reviewed: list[dict[str, Any]] = []
    raw: list[dict[str, Any]] = []
    for run_id in sorted(run_ids):
        root = data_path / "analysis" / "runs" / run_id
        if not root.is_dir():
            continue
        candidates = sorted(
            root.glob("*/reviewed-analysis.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        snapshots = sorted(
            root.glob("*/analysis-snapshot.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for collection, paths in ((reviewed, candidates), (raw, snapshots)):
            if not paths:
                continue
            try:
                value = json.loads(paths[0].read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                collection.append(value)
    return reviewed, raw


def _active_rubrics(db_path: Path) -> list[tuple[str, str, dict[str, Any]]]:
    with _open_sync(db_path) as conn:
        rows = conn.execute(
            "SELECT ar.env_name,ar.rubric_version,rv.rubric_json "
            "FROM active_rubrics ar JOIN rubric_versions rv ON rv.id=ar.rubric_id "
            "WHERE rv.source_batch_id IS NULL AND rv.status='published' "
            "ORDER BY ar.env_name"
        ).fetchall()
    return [
        (str(row[0]), str(row[1]), json.loads(row[2]))
        for row in rows
    ]


async def run_scheduler_cycle(
    *,
    db_path: Path,
    data_path: Path,
    provider: InsightProvider,
    environment_threshold: int = 10,
    cross_environment_threshold: int | None = None,
    overlap_ratio: float = 0.25,
) -> SchedulerCycle:
    """Scan frozen Judge Records and run every currently ready environment batch.

    Product Rubric Evolution is counted per environment. A later overall-rubric
    loop may reuse unused records, but this scheduler must not treat one
    environment's records as a global batch or load every Active Rubric.
    ``cross_environment_threshold`` is accepted for old callers and ignored.
    """
    del cross_environment_threshold
    outcomes: list[RubricEvolutionOutcome] = []
    active = _active_rubrics(db_path)
    environment_batches = 0
    for env_name, rubric_version, rubric in active:
        scope_key = f"environment:{env_name}:{rubric_version}"
        records = list_unbatched_judge_records_sync(
            db_path=db_path,
            scope_key=scope_key,
            env_name=env_name,
            rubric_version=rubric_version,
            limit=environment_threshold,
        )
        previous = latest_batch_records_sync(db_path=db_path, scope_key=scope_key)
        prepared = prepare_evolution_batch(
            new_records=records,
            previous_records=previous,
            scope="environment",
            env_name=env_name,
            threshold=environment_threshold,
            overlap_ratio=overlap_ratio,
        )
        if prepared.batch is None:
            continue
        reviewed, raw = _latest_analysis_artifacts(
            data_path, {item.run_id for item in prepared.batch.records}
        )
        outcome = await evolve_rubric(
            data_path=data_path,
            batch=prepared.batch,
            current_rubric=rubric,
            current_rubric_version=rubric_version,
            provider=provider,
            reviewed_analyses=reviewed,
            raw_runtrace_evidence=raw,
        )
        if outcome.status == "ready":
            persist_batch_membership_sync(
                db_path, prepared.batch, scope_key=scope_key
            )
            environment_batches += 1
        else:
            logger.warning(
                "Rubric Evolution batch remains retryable batch_id=%s error=%s",
                outcome.batch_id,
                outcome.error_code,
            )
        outcomes.append(outcome)
    return SchedulerCycle(
        environment_batches=environment_batches,
        cross_environment_batches=0,
        outcomes=tuple(outcomes),
    )


async def run_scheduler_loop(
    *,
    db_path: Path,
    data_path: Path,
    provider: InsightProvider,
    stop_event: asyncio.Event,
    interval_seconds: float,
    environment_threshold: int = 10,
    cross_environment_threshold: int = 100,
    process_threshold: int = 10,
    association_threshold: int = 100,
    overlap_ratio: float = 0.25,
) -> None:
    logger.info("rubric evolution scheduler started interval=%ss", interval_seconds)
    consecutive_failures = 0
    while not stop_event.is_set():
        try:
            cycle = await run_scheduler_cycle(
                db_path=db_path,
                data_path=data_path,
                provider=provider,
                environment_threshold=environment_threshold,
                cross_environment_threshold=cross_environment_threshold,
                overlap_ratio=overlap_ratio,
            )
            directional = await run_directional_cycle(
                db_path=db_path,
                data_path=data_path,
                provider=provider,
                process_threshold=process_threshold,
                association_threshold=association_threshold,
                overlap_ratio=overlap_ratio,
            )
            if cycle.outcomes or directional.process_batches or directional.association_batches:
                logger.info(
                    "evolution cycle completed product_environment=%d product_cross=%d "
                    "process=%d association=%d process_result=%s association_result=%s",
                    cycle.environment_batches,
                    cycle.cross_environment_batches,
                    directional.process_batches,
                    directional.association_batches,
                    directional.process_result,
                    directional.association_result,
                )
            consecutive_failures = 0
        except asyncio.CancelledError:
            raise
        except Exception:
            consecutive_failures += 1
            logger.exception(
                "rubric evolution scheduler cycle failed consecutive=%s",
                consecutive_failures,
            )
        delay = interval_seconds
        if consecutive_failures:
            delay = min(interval_seconds * (2 ** min(consecutive_failures, 4)), 3600)
            logger.warning("rubric evolution scheduler backing off for %.0fs", delay)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
    logger.info("rubric evolution scheduler stopped")
