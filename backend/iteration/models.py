"""多轮产品评审的纯数据模型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


ITERATIVE_PRODUCT_REVIEW_MODE = "iterative_product_review"

SubmissionSignalSource = Literal["implicit_turn_completion"]
SubmissionBoundary = Literal["normal_turn_completion"]
StopPolicy = Literal["no_requested_changes"]
FinalScorePolicy = Literal["last_successful_submission"]
FeedbackPriority = Literal["high", "medium", "low"]


@dataclass(frozen=True, slots=True)
class ProductReviewerPolicy:
    role: str = "product_reviewer"
    expose_scores: bool = False
    expose_rubric: bool = False
    expose_check_ids: bool = False
    max_required_changes: int = 4
    max_feedback_chars: int = 2400


@dataclass(frozen=True, slots=True)
class IterativeReviewPolicy:
    mode: str
    max_iterations: int
    submission_boundary: SubmissionBoundary
    stop_policy: StopPolicy
    preserve_workspace: bool
    preserve_agent_session: bool
    judge_on_each_submission: bool
    final_score_policy: FinalScorePolicy
    reviewer: ProductReviewerPolicy

    @property
    def submission_count(self) -> int:
        """兼容基础 Repository 的既有字段名；语义是迭代硬上限。"""
        return self.max_iterations

    @property
    def final_round_index(self) -> int:
        return self.max_iterations - 1

    def should_generate_feedback(self, round_index: int) -> bool:
        return 0 <= round_index < self.final_round_index


@dataclass(frozen=True, slots=True)
class SubmissionSignal:
    summary: str
    verification: str
    source: SubmissionSignalSource
    emitted_at: str


@dataclass(frozen=True, slots=True)
class SubmissionRecord:
    submission_id: str
    attempt_id: str
    round_index: int
    producer_session_id: str | None
    summary: str
    verification: str
    signal_source: SubmissionSignalSource
    created_at: str
    snapshot_status: str = "pending"
    evaluation_status: str = "pending"
    feedback_status: str = "not_required"
    snapshot_manifest_hash: str | None = None
    selected_for_final_score: bool = False


@dataclass(frozen=True, slots=True)
class RequestedChange:
    priority: FeedbackPriority
    area: str
    problem: str
    request: str
    acceptance_signal: str


@dataclass(frozen=True, slots=True)
class PublicProductFeedback:
    overall_assessment: str
    working_well: tuple[str, ...]
    requested_changes: tuple[RequestedChange, ...]
    instruction: str


@dataclass(frozen=True, slots=True)
class SubmissionEvaluation:
    attempt_id: str
    submission_id: str
    round_index: int
    snapshot_manifest_hash: str
    score_total: int
    pass_threshold: int
    passed: bool
    scores: tuple[dict[str, Any], ...]
    evaluation_manifest: dict[str, Any]
    artifact_dir: str
    judge_review: dict[str, Any] | None = None
