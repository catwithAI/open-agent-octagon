from __future__ import annotations

from collections.abc import Iterable

from .models import JudgeCheckRecord, RubricScoreView


def compute_rubric_score_views(
    checks: Iterable[JudgeCheckRecord | dict],
) -> RubricScoreView:
    """Return the two production score views required by the design.

    ``score_with_unknown`` keeps unknown checks in the denominator while they
    contribute no awarded points. This is only a numeric view: the check result
    remains ``unknown`` and is never rewritten to ``fail``.

    ``normalized_score_without_unknown`` removes unknown and not-applicable
    checks from the denominator and normalizes the resolved checks to 100.
    """
    parsed = [
        item if isinstance(item, JudgeCheckRecord) else JudgeCheckRecord.model_validate(item)
        for item in checks
    ]
    resolved_points = sum(float(item.awarded or 0) for item in parsed)
    applicable = [item for item in parsed if item.result != "not_applicable"]
    resolved = [item for item in applicable if item.result != "unknown"]
    total_with_unknown = sum(float(item.maximum) for item in applicable)
    total_without_unknown = sum(float(item.maximum) for item in resolved)
    score_with_unknown = (
        100.0 * resolved_points / total_with_unknown if total_with_unknown else 0.0
    )
    normalized = (
        100.0 * resolved_points / total_without_unknown
        if total_without_unknown
        else None
    )
    return RubricScoreView(
        score_with_unknown=round(score_with_unknown, 4),
        normalized_score_without_unknown=(round(normalized, 4) if normalized is not None else None),
        resolved_points=round(resolved_points, 4),
        total_points_with_unknown=round(total_with_unknown, 4),
        total_points_without_unknown=round(total_without_unknown, 4),
        unknown_count=sum(1 for item in applicable if item.result == "unknown"),
        unknown_weight=round(
            sum(float(item.maximum) for item in applicable if item.result == "unknown"), 4
        ),
    )
