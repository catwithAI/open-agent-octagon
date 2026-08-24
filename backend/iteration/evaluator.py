"""冻结 Submission 的中间评测契约；不写 Attempt 正式成绩。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from ..evaluator import _aggregate_total, _extract_meta, _scorer_manifest
from .models import SubmissionEvaluation
from .writer import atomic_write_json


class SubmissionEvaluationError(RuntimeError):
    pass


def load_submission_evaluation(
    artifact_dir: Path,
    *,
    attempt_id: str,
    submission_id: str,
    round_index: int,
    snapshot_manifest_hash: str,
) -> SubmissionEvaluation:
    path = Path(artifact_dir) / "submission-evaluation.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise SubmissionEvaluationError("Submission 评测产物缺失或损坏") from exc
    if not isinstance(raw, dict):
        raise SubmissionEvaluationError("Submission 评测产物必须是 object")
    expected = {
        "attempt_id": attempt_id,
        "submission_id": submission_id,
        "round_index": round_index,
        "snapshot_manifest_hash": snapshot_manifest_hash,
    }
    for key, value in expected.items():
        if raw.get(key) != value:
            raise SubmissionEvaluationError(f"Submission 评测身份不一致: {key}")
    scores = raw.get("scores")
    if not isinstance(scores, list) or not all(isinstance(item, dict) for item in scores):
        raise SubmissionEvaluationError("Submission 评测维度分非法")
    judge_review = raw.get("judge_review")
    if judge_review is not None and not isinstance(judge_review, dict):
        raise SubmissionEvaluationError("Submission Judge Review 非法")
    return SubmissionEvaluation(
        attempt_id=attempt_id,
        submission_id=submission_id,
        round_index=round_index,
        snapshot_manifest_hash=snapshot_manifest_hash,
        score_total=int(raw["score_total"]),
        pass_threshold=int(raw["pass_threshold"]),
        passed=bool(raw["passed"]),
        scores=tuple(scores),
        evaluation_manifest=dict(raw.get("evaluation_manifest") or {}),
        artifact_dir=str(artifact_dir),
        judge_review=judge_review,
    )


def evaluate_submission(
    *,
    attempt_id: str,
    submission_id: str,
    round_index: int,
    snapshot_workspace: Path,
    snapshot_manifest_hash: str,
    task: dict[str, Any],
    env: Any,
    env_db: Path | None,
    scorer: Callable[..., Any],
    artifact_dir: Path,
    trace: list[dict[str, Any]] | None = None,
    final_state: dict[str, Any] | None = None,
) -> SubmissionEvaluation:
    """运行一轮 scorer 并持久化私有结果，不触碰正式 scores 表。"""
    snapshot_workspace = Path(snapshot_workspace)
    artifact_dir = Path(artifact_dir)
    if not snapshot_workspace.is_dir():
        raise SubmissionEvaluationError(
            f"Submission 快照不存在: {snapshot_workspace}"
        )
    if not snapshot_manifest_hash.startswith("sha256:"):
        raise SubmissionEvaluationError("snapshot_manifest_hash 必须是 sha256 标识")

    raw = scorer(
        attempt_id=attempt_id,
        submission_id=submission_id,
        round_index=round_index,
        task=task,
        env_db=env_db,
        trace=list(trace or []),
        final_state=dict(final_state or {}),
        workspace=snapshot_workspace,
        snapshot_workspace=snapshot_workspace,
        snapshot_manifest_hash=snapshot_manifest_hash,
        artifact_dir=artifact_dir,
    )
    judge_review: dict[str, Any] | None = None
    if isinstance(raw, dict):
        raw_scores = raw.get("scores")
        candidate_review = raw.get("judge_review")
        if candidate_review is not None and not isinstance(candidate_review, dict):
            raise TypeError("submission scorer judge_review must be a mapping")
        judge_review = candidate_review
    else:
        raw_scores = raw
    if not isinstance(raw_scores, list):
        raise TypeError(
            "submission scorer must return a score list or {'scores': [...]}"
        )
    for index, score in enumerate(raw_scores):
        if not isinstance(score, dict):
            raise TypeError(f"submission score[{index}] must be a mapping")

    pass_threshold, weights = _extract_meta(env)
    score_total = _aggregate_total(raw_scores, weights)
    evaluation_manifest = _scorer_manifest(env, scorer)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        artifact_dir / "submission-evaluation.json",
        {
            "schema_version": "octagon-submission-evaluation-v1",
            "attempt_id": attempt_id,
            "submission_id": submission_id,
            "round_index": round_index,
            "snapshot_manifest_hash": snapshot_manifest_hash,
            "score_total": score_total,
            "pass_threshold": pass_threshold,
            "passed": score_total >= pass_threshold,
            "scores": raw_scores,
            "evaluation_manifest": evaluation_manifest,
            "judge_review": judge_review,
        },
    )
    return SubmissionEvaluation(
        attempt_id=attempt_id,
        submission_id=submission_id,
        round_index=round_index,
        snapshot_manifest_hash=snapshot_manifest_hash,
        score_total=score_total,
        pass_threshold=pass_threshold,
        passed=score_total >= pass_threshold,
        scores=tuple(raw_scores),
        evaluation_manifest=evaluation_manifest,
        artifact_dir=str(artifact_dir),
        judge_review=judge_review,
    )
