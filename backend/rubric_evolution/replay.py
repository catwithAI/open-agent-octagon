"""Fixed-candidate replay and shadow score append path."""

from __future__ import annotations

import inspect
import json
import os
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.db import _now_iso
from backend.experiments.hashing import canonical_hash

from .models import CandidateRubric, JudgeCheckRecord, RubricScoreView
from .scoring import compute_rubric_score_views
from .store import append_score_revision_sync

ReplayExecutor = Callable[
    ["RubricReplayCase", CandidateRubric],
    Awaitable[list[JudgeCheckRecord | dict[str, Any]]] | list[JudgeCheckRecord | dict[str, Any]],
]


class RubricReplayCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1, max_length=200)
    attempt_id: str = Field(min_length=1, max_length=200)
    env_name: str = Field(min_length=1, max_length=300)
    snapshot_hash: str = Field(min_length=1, max_length=300)
    evidence_snapshot: dict[str, Any]
    baseline_rubric_version: str = Field(min_length=1, max_length=300)
    baseline_checks: list[JudgeCheckRecord] = Field(min_length=1, max_length=1000)


class RubricReplayCaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    attempt_id: str
    baseline_rubric_version: str
    candidate_rubric_version: str
    baseline_score: RubricScoreView
    candidate_score: RubricScoreView
    score_with_unknown_delta: float
    normalized_score_delta: float | None
    revision_id: str


class RubricReplayOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["octagon-rubric-replay-v1"]
    replay_id: str
    candidate_rubric_version: str
    score_kind: Literal["replay", "shadow"]
    case_results: list[RubricReplayCaseResult]
    artifact_dir: str
    input_hash: str


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, default=str)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


async def run_rubric_replay(
    *,
    db_path: Path,
    data_path: Path,
    candidate_rubric: CandidateRubric,
    cases: list[RubricReplayCase | dict[str, Any]],
    executor: ReplayExecutor,
    score_kind: Literal["replay", "shadow"] = "replay",
    source_batch_id: str | None = None,
) -> RubricReplayOutcome:
    """Execute a candidate rubric against immutable cases and append scores.

    The executor is environment-specific; this shared layer owns freezing,
    versioned score storage and old/new comparison. It never updates legacy
    ``scores`` or ``attempts.score_total``.
    """
    parsed_cases = [
        item if isinstance(item, RubricReplayCase) else RubricReplayCase.model_validate(item)
        for item in cases
    ]
    if not parsed_cases:
        raise ValueError("rubric replay requires at least one case")
    if candidate_rubric.scope == "environment":
        mismatched = [
            item.case_id for item in parsed_cases
            if item.env_name != candidate_rubric.env_name
        ]
        if mismatched:
            raise ValueError(f"replay cases do not match candidate environment: {mismatched[:10]}")
    input_payload = {
        "candidate": candidate_rubric.model_dump(mode="json", by_alias=True),
        "cases": [item.model_dump(mode="json") for item in parsed_cases],
        "score_kind": score_kind,
        "source_batch_id": source_batch_id,
    }
    input_hash = canonical_hash(input_payload)
    replay_id = f"rrp_{input_hash.split(':')[-1][:20]}"
    root = data_path / "rubric-evolution" / "replays" / replay_id
    existing = root / "replay-result.json"
    if existing.is_file():
        return RubricReplayOutcome.model_validate_json(existing.read_text(encoding="utf-8"))

    _atomic_json(root / "replay-input.json", {
        "schema_version": "octagon-rubric-replay-input-v1",
        "input_hash": input_hash,
        **input_payload,
        "created_at": _now_iso(),
    })
    results: list[RubricReplayCaseResult] = []
    for case in parsed_cases:
        produced = executor(case, candidate_rubric)
        if inspect.isawaitable(produced):
            produced = await produced
        candidate_checks = [
            item if isinstance(item, JudgeCheckRecord) else JudgeCheckRecord.model_validate(item)
            for item in produced
        ]
        baseline_score = compute_rubric_score_views(case.baseline_checks)
        candidate_score = compute_rubric_score_views(candidate_checks)
        revision_id = append_score_revision_sync(
            db_path=db_path,
            attempt_id=case.attempt_id,
            rubric_version=candidate_rubric.proposed_version,
            score_kind=score_kind,
            score=candidate_score,
            checks=candidate_checks,
            source_batch_id=source_batch_id,
            operation_id=f"{replay_id}:{case.case_id}",
        )
        normalized_delta = None
        if (
            baseline_score.normalized_score_without_unknown is not None
            and candidate_score.normalized_score_without_unknown is not None
        ):
            normalized_delta = round(
                candidate_score.normalized_score_without_unknown
                - baseline_score.normalized_score_without_unknown,
                4,
            )
        results.append(RubricReplayCaseResult(
            case_id=case.case_id,
            attempt_id=case.attempt_id,
            baseline_rubric_version=case.baseline_rubric_version,
            candidate_rubric_version=candidate_rubric.proposed_version,
            baseline_score=baseline_score,
            candidate_score=candidate_score,
            score_with_unknown_delta=round(
                candidate_score.score_with_unknown - baseline_score.score_with_unknown,
                4,
            ),
            normalized_score_delta=normalized_delta,
            revision_id=revision_id,
        ))
    outcome = RubricReplayOutcome(
        schema_version="octagon-rubric-replay-v1",
        replay_id=replay_id,
        candidate_rubric_version=candidate_rubric.proposed_version,
        score_kind=score_kind,
        case_results=results,
        artifact_dir=str(root),
        input_hash=input_hash,
    )
    _atomic_json(existing, outcome.model_dump(mode="json"))
    return outcome
