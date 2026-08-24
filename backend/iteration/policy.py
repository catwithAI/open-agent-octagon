"""环境 interaction 配置的严格解析与校验。"""

from __future__ import annotations

from typing import Any

from .models import (
    ITERATIVE_PRODUCT_REVIEW_MODE,
    IterativeReviewPolicy,
    ProductReviewerPolicy,
)


class IterationPolicyError(ValueError):
    """迭代环境声明非法。"""


_INTERACTION_FIELDS = frozenset(
    {
        "mode",
        "max_iterations",
        "submission_boundary",
        "stop_policy",
        "preserve_workspace",
        "preserve_agent_session",
        "judge_on_each_submission",
        "final_score_policy",
    }
)
_REVIEWER_FIELDS = frozenset(
    {
        "role",
        "expose_scores",
        "expose_rubric",
        "expose_check_ids",
        "max_required_changes",
        "max_feedback_chars",
    }
)


def _require_bool(value: Any, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise IterationPolicyError(f"{field} 必须是布尔值")
    return value


def _require_int(
    value: Any, *, field: str, minimum: int, maximum: int
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise IterationPolicyError(f"{field} 必须是整数")
    if not minimum <= value <= maximum:
        raise IterationPolicyError(
            f"{field} 必须位于 {minimum}-{maximum}，实际为 {value}"
        )
    return value


def _parse_reviewer(raw: Any) -> ProductReviewerPolicy:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise IterationPolicyError("reviewer 必须是对象")
    unknown = set(raw) - _REVIEWER_FIELDS
    if unknown:
        raise IterationPolicyError(f"reviewer 含未知字段 {sorted(unknown)}")

    role = raw.get("role", "product_reviewer")
    if not isinstance(role, str) or not role.strip():
        raise IterationPolicyError("reviewer.role 必须是非空字符串")

    expose_scores = _require_bool(
        raw.get("expose_scores", False), field="reviewer.expose_scores"
    )
    expose_rubric = _require_bool(
        raw.get("expose_rubric", False), field="reviewer.expose_rubric"
    )
    expose_check_ids = _require_bool(
        raw.get("expose_check_ids", False), field="reviewer.expose_check_ids"
    )
    if expose_scores or expose_rubric or expose_check_ids:
        raise IterationPolicyError(
            "Product Reviewer 不得暴露分数、Rubric 或内部检查 ID"
        )

    return ProductReviewerPolicy(
        role=role.strip(),
        expose_scores=expose_scores,
        expose_rubric=expose_rubric,
        expose_check_ids=expose_check_ids,
        max_required_changes=_require_int(
            raw.get("max_required_changes", 4),
            field="reviewer.max_required_changes",
            minimum=1,
            maximum=8,
        ),
        max_feedback_chars=_require_int(
            raw.get("max_feedback_chars", 2400),
            field="reviewer.max_feedback_chars",
            minimum=200,
            maximum=10_000,
        ),
    )


def parse_iterative_review_policy(
    meta: dict[str, Any] | None,
) -> IterativeReviewPolicy | None:
    """解析环境 meta；未声明迭代模式时返回 ``None``。"""
    if not meta:
        return None
    interaction = meta.get("interaction")
    if interaction is None:
        return None
    if not isinstance(interaction, dict):
        raise IterationPolicyError("interaction 必须是对象")

    mode = interaction.get("mode")
    if mode is None:
        return None
    if mode != ITERATIVE_PRODUCT_REVIEW_MODE:
        raise IterationPolicyError(f"未知 interaction.mode: {mode!r}")

    unknown = set(interaction) - _INTERACTION_FIELDS
    if unknown:
        raise IterationPolicyError(f"interaction 含未知字段 {sorted(unknown)}")

    max_iterations = _require_int(
        interaction.get("max_iterations", 3),
        field="interaction.max_iterations",
        minimum=1,
        maximum=10,
    )

    submission_boundary = interaction.get(
        "submission_boundary", "normal_turn_completion"
    )
    if submission_boundary != "normal_turn_completion":
        raise IterationPolicyError(
            "interaction.submission_boundary 当前只支持 'normal_turn_completion'"
        )
    stop_policy = interaction.get("stop_policy", "no_requested_changes")
    if stop_policy != "no_requested_changes":
        raise IterationPolicyError(
            "interaction.stop_policy 当前只支持 'no_requested_changes'"
        )
    final_score_policy = interaction.get(
        "final_score_policy", "last_successful_submission"
    )
    if final_score_policy != "last_successful_submission":
        raise IterationPolicyError(
            "interaction.final_score_policy 当前只支持 'last_successful_submission'"
        )

    preserve_workspace = _require_bool(
        interaction.get("preserve_workspace", True),
        field="interaction.preserve_workspace",
    )
    preserve_agent_session = _require_bool(
        interaction.get("preserve_agent_session", True),
        field="interaction.preserve_agent_session",
    )
    judge_on_each_submission = _require_bool(
        interaction.get("judge_on_each_submission", True),
        field="interaction.judge_on_each_submission",
    )
    if not preserve_workspace or not preserve_agent_session:
        raise IterationPolicyError(
            "迭代产品评审必须保持同一工作区和 Agent session"
        )
    if not judge_on_each_submission:
        raise IterationPolicyError("每个 Submission 都必须运行双 Judge")

    return IterativeReviewPolicy(
        mode=ITERATIVE_PRODUCT_REVIEW_MODE,
        max_iterations=max_iterations,
        submission_boundary=submission_boundary,
        stop_policy=stop_policy,
        preserve_workspace=preserve_workspace,
        preserve_agent_session=preserve_agent_session,
        judge_on_each_submission=judge_on_each_submission,
        final_score_policy=final_score_policy,
        reviewer=_parse_reviewer(meta.get("reviewer")),
    )
