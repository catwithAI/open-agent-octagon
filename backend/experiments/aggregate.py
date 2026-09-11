"""Pure robustness aggregation with explicit orthogonal rate semantics."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any

from .hashing import canonical_hash

RULES_VERSION = "octagon-robustness-rules-v1"

INFRASTRUCTURE_STATUSES = frozenset(
    {
        "session_create_failed",
        "session_socket_overflow",
        "server_unreachable",
        "provider_quota_exhausted",
        "capture_infrastructure_failed",
        "model_integrity_failed",
        "blade_service_unavailable",
        "sandbox_unavailable",
    }
)
SCORED_STATUSES = frozenset({"completed", "gave_up"})
EXECUTION_OUTPUT_STATUSES = SCORED_STATUSES | {"scoring_failed"}


@dataclass(frozen=True)
class AttemptFact:
    attempt_id: str
    status: str
    score_total: float | None = None
    failure_kind: str | None = None
    security_event_count: int = 0


@dataclass(frozen=True)
class Ratio:
    numerator: int
    denominator: int
    value: float | None
    numerator_statuses: tuple[str, ...]


@dataclass(frozen=True)
class RobustnessAggregate:
    sample_count: int
    expected_count: int
    mean: float | None
    minimum: float | None
    maximum: float | None
    sample_variance: float | None
    variance_status: str
    security_event_mean: float | None
    rates: dict[str, Ratio]
    status_counts: dict[str, int]
    included_attempt_ids: tuple[str, ...]
    excluded_attempts: dict[str, str]
    labels: tuple[str, ...]
    rules_version: str
    input_hash: str


def _ratio(
    facts: list[AttemptFact], statuses: set[str] | frozenset[str], denominator: int
) -> Ratio:
    numerator = sum(fact.status in statuses for fact in facts)
    return Ratio(
        numerator=numerator,
        denominator=denominator,
        value=(numerator / denominator if denominator else None),
        numerator_statuses=tuple(sorted(statuses)),
    )


def aggregate_attempts(
    facts: list[AttemptFact],
    *,
    expected_count: int | None = None,
    pass_threshold: float = 60,
) -> RobustnessAggregate:
    expected = expected_count if expected_count is not None else len(facts)
    if expected < len(facts):
        raise ValueError("expected_count cannot be smaller than observed facts")
    scored = [
        fact
        for fact in facts
        if fact.status in SCORED_STATUSES and fact.score_total is not None
    ]
    scores = [float(fact.score_total) for fact in scored if fact.score_total is not None]
    passing = [fact for fact in scored if float(fact.score_total or 0) >= pass_threshold]
    execution = [fact for fact in facts if fact.status in EXECUTION_OUTPUT_STATUSES]
    infrastructure = [
        fact
        for fact in facts
        if fact.failure_kind == "infrastructure"
        or fact.status in INFRASTRUCTURE_STATUSES
    ]
    scoring_failures = [
        fact
        for fact in facts
        if fact.failure_kind == "scoring" or fact.status == "scoring_failed"
    ]
    agent_failures = [
        fact
        for fact in facts
        if fact.failure_kind == "agent"
        and fact not in infrastructure
        and fact not in scoring_failures
    ]

    def explicit_ratio(items: list[AttemptFact], statuses: tuple[str, ...]) -> Ratio:
        return Ratio(
            len(items),
            expected,
            len(items) / expected if expected else None,
            statuses,
        )

    scored_pass = Ratio(
        len(passing),
        len(scored),
        len(passing) / len(scored) if scored else None,
        ("completed(score>=threshold)",),
    )
    rates = {
        "execution_success_rate": explicit_ratio(
            execution, tuple(sorted(EXECUTION_OUTPUT_STATUSES))
        ),
        "scored_rate": explicit_ratio(scored, tuple(sorted(SCORED_STATUSES))),
        "scored_pass_rate": scored_pass,
        "end_to_end_pass_rate": Ratio(
            len(passing),
            expected,
            len(passing) / expected if expected else None,
            ("completed(score>=threshold)",),
        ),
        "infrastructure_failure_rate": explicit_ratio(
            infrastructure, tuple(sorted(INFRASTRUCTURE_STATUSES))
        ),
        "agent_failure_rate": explicit_ratio(agent_failures, ("failure_kind=agent",)),
        "scoring_failure_rate": explicit_ratio(
            scoring_failures, ("scoring_failed", "failure_kind=scoring")
        ),
    }
    status_counts: dict[str, int] = {}
    for fact in facts:
        status_counts[fact.status] = status_counts.get(fact.status, 0) + 1
    if expected > len(facts):
        status_counts["missing"] = expected - len(facts)

    included_ids = tuple(sorted(fact.attempt_id for fact in scored))
    excluded: dict[str, str] = {
        fact.attempt_id: (
            "score_missing" if fact.status in SCORED_STATUSES else fact.status
        )
        for fact in facts
        if fact not in scored
    }
    labels: list[str] = []
    variance = statistics.variance(scores) if len(scores) >= 2 else None
    if (
        len(scores) >= 2
        and min(scores) >= pass_threshold
        and variance is not None
        and variance <= 25
    ):
        labels.append("stable")
    if len(scores) >= 2 and (
        max(scores) - min(scores) >= 30
        or any(score >= pass_threshold for score in scores)
        and any(score < pass_threshold for score in scores)
    ):
        labels.append("brittle")
    input_payload = {
        "facts": [fact.__dict__ for fact in facts],
        "expected_count": expected,
        "pass_threshold": pass_threshold,
        "rules_version": RULES_VERSION,
    }
    return RobustnessAggregate(
        sample_count=len(scores),
        expected_count=expected,
        mean=statistics.mean(scores) if scores else None,
        minimum=min(scores) if scores else None,
        maximum=max(scores) if scores else None,
        sample_variance=variance,
        variance_status="available" if variance is not None else "unavailable",
        security_event_mean=(
            statistics.mean(fact.security_event_count for fact in facts)
            if facts
            else None
        ),
        rates=rates,
        status_counts=status_counts,
        included_attempt_ids=included_ids,
        excluded_attempts=excluded,
        labels=tuple(labels),
        rules_version=RULES_VERSION,
        input_hash=canonical_hash(input_payload),
    )


def baseline_delta(
    variant: RobustnessAggregate,
    baseline: RobustnessAggregate,
    *,
    variant_compatibility: dict[str, Any],
    baseline_compatibility: dict[str, Any],
) -> dict[str, Any]:
    if canonical_hash(variant_compatibility) != canonical_hash(baseline_compatibility):
        return {
            "status": "incompatible",
            "delta": None,
            "variant_sample_count": variant.sample_count,
            "baseline_sample_count": baseline.sample_count,
        }
    if variant.mean is None or baseline.mean is None:
        return {
            "status": "unavailable",
            "delta": None,
            "variant_sample_count": variant.sample_count,
            "baseline_sample_count": baseline.sample_count,
        }
    return {
        "status": "available",
        "delta": variant.mean - baseline.mean,
        "variant_sample_count": variant.sample_count,
        "baseline_sample_count": baseline.sample_count,
    }


def rank_variant_labels(
    aggregates: dict[str, RobustnessAggregate]
) -> dict[str, tuple[str, ...]]:
    available = {
        name: aggregate for name, aggregate in aggregates.items() if aggregate.mean is not None
    }
    result = {name: list(aggregate.labels) for name, aggregate in aggregates.items()}
    if available:
        best = max(available, key=lambda name: (available[name].mean, name))
        worst = min(available, key=lambda name: (available[name].mean, name))
        result[best].append("best")
        result[worst].append("worst")
    return {name: tuple(labels) for name, labels in result.items()}
