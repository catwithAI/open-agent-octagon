from __future__ import annotations

import math
from collections import defaultdict
from typing import Iterable

from backend.db import _now_iso
from backend.experiments.hashing import canonical_hash

from .models import (
    EvolutionBatch,
    EvolutionBatchPreparation,
    EvolutionScope,
    JudgeRecord,
)

ENVIRONMENT_THRESHOLD = 10
CROSS_ENVIRONMENT_THRESHOLD = 100
DEFAULT_OVERLAP_RATIO = 0.25


def _record_stratum(record: JudgeRecord) -> str:
    results = {check.result for check in record.checks}
    if "unknown" in results:
        return "unknown"
    if "fail" in results:
        return "fail"
    if "partial" in results:
        return "partial"
    if results == {"not_applicable"}:
        return "not_applicable"
    return "pass"


def _select_overlap(records: list[JudgeRecord], count: int) -> list[JudgeRecord]:
    """Deterministically retain a stratified slice from the previous batch."""
    if count <= 0 or not records:
        return []
    groups: dict[str, list[JudgeRecord]] = defaultdict(list)
    for record in sorted(records, key=lambda item: (item.created_at, item.record_id)):
        if record.valid_execution:
            groups[_record_stratum(record)].append(record)
    order = ("pass", "partial", "fail", "unknown", "not_applicable")
    selected: list[JudgeRecord] = []
    offsets = {key: 0 for key in order}
    while len(selected) < count:
        progressed = False
        for key in order:
            bucket = groups.get(key, [])
            index = offsets[key]
            if index < len(bucket):
                selected.append(bucket[index])
                offsets[key] += 1
                progressed = True
                if len(selected) >= count:
                    break
        if not progressed:
            break
    return selected


def prepare_evolution_batch(
    *,
    new_records: Iterable[JudgeRecord | dict],
    scope: EvolutionScope,
    env_name: str | None = None,
    previous_records: Iterable[JudgeRecord | dict] = (),
    overlap_ratio: float = DEFAULT_OVERLAP_RATIO,
    threshold: int | None = None,
) -> EvolutionBatchPreparation:
    """Freeze a production evolution batch when its scope threshold is met.

    All valid judge retries count independently. Invalid judge executions remain
    observable but do not count toward the 10/100 trigger threshold.
    """
    if not 0 <= overlap_ratio <= 0.5:
        raise ValueError("overlap_ratio must be between 0 and 0.5")
    if scope == "environment" and not env_name:
        raise ValueError("environment scope requires env_name")
    expected = threshold or (
        ENVIRONMENT_THRESHOLD if scope == "environment" else CROSS_ENVIRONMENT_THRESHOLD
    )
    parsed_new = [
        item if isinstance(item, JudgeRecord) else JudgeRecord.model_validate(item)
        for item in new_records
    ]
    parsed_previous = [
        item if isinstance(item, JudgeRecord) else JudgeRecord.model_validate(item)
        for item in previous_records
    ]
    if scope == "environment":
        parsed_new = [item for item in parsed_new if item.env_name == env_name]
        parsed_previous = [item for item in parsed_previous if item.env_name == env_name]
    valid_new = [item for item in parsed_new if item.valid_execution]
    invalid_count = len(parsed_new) - len(valid_new)
    # Overall-rubric work is not production yet. Keep the helper from treating a
    # single environment's records as a cross-environment batch.
    if scope == "cross_environment" and len({item.env_name for item in valid_new}) < 2:
        return EvolutionBatchPreparation(
            status="waiting",
            scope=scope,
            threshold=expected,
            valid_new_record_count=len(valid_new),
            excluded_invalid_record_count=invalid_count,
        )
    if len(valid_new) < expected:
        return EvolutionBatchPreparation(
            status="waiting",
            scope=scope,
            threshold=expected,
            valid_new_record_count=len(valid_new),
            excluded_invalid_record_count=invalid_count,
        )

    # Freeze exactly the threshold-triggering window. Extra records belong to
    # the next batch rather than silently changing an already-ready input.
    trigger = sorted(valid_new, key=lambda item: (item.created_at, item.record_id))[:expected]
    overlap_count = math.ceil(len(trigger) * overlap_ratio)
    overlap = _select_overlap(parsed_previous, overlap_count)
    records = trigger + overlap
    payload = {
        "scope": scope,
        "env_name": env_name,
        "threshold": expected,
        "trigger_record_ids": [item.record_id for item in trigger],
        "overlap_record_ids": [item.record_id for item in overlap],
        "records": [item.model_dump(mode="json") for item in records],
    }
    input_hash = canonical_hash(payload)
    suffix = input_hash.split(":")[-1][:20]
    versions = sorted({item.rubric_version for item in records})
    batch = EvolutionBatch(
        schema_version="octagon-rubric-evolution-batch-v1",
        batch_id=f"reb_{suffix}",
        scope=scope,
        env_name=env_name,
        rubric_versions=versions,
        threshold=expected,
        trigger_record_count=len(trigger),
        overlap_record_count=len(overlap),
        unique_attempt_count=len({item.attempt_id for item in records}),
        unknown_record_count=sum(
            1 for item in records if any(check.result == "unknown" for check in item.checks)
        ),
        records=records,
        input_hash=input_hash,
        frozen_at=_now_iso(),
    )
    return EvolutionBatchPreparation(
        status="ready",
        scope=scope,
        threshold=expected,
        valid_new_record_count=len(valid_new),
        excluded_invalid_record_count=invalid_count,
        batch=batch,
    )
