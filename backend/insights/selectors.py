"""Deterministic, budgeted selection for Evidence Bundles."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from backend.experiments.hashing import canonical_json_bytes


CATEGORY_PRIORITY = {
    "aggregate": 0,
    "result": 1,
    "score": 2,
    "error": 3,
    "security": 4,
    "completeness": 5,
    "trajectory": 6,
    "artifact": 7,
}


@dataclass(frozen=True)
class EvidenceBudget:
    max_records: int = 200
    max_tokens: int = 8_000

    def __post_init__(self) -> None:
        if self.max_records < 1 or self.max_tokens < 128:
            raise ValueError("evidence budget is too small")


@dataclass(frozen=True)
class SelectionCandidate:
    category: str
    stable_key: str
    record: dict[str, Any]


def estimate_tokens(value: Any) -> int:
    """Stable conservative approximation; no tokenizer/provider dependency."""

    size = len(canonical_json_bytes(value))
    return max(1, (size + 2) // 3)


def select_records(
    candidates: Iterable[SelectionCandidate],
    *,
    budget: EvidenceBudget,
    reserved_tokens: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    ordered = sorted(
        candidates,
        key=lambda item: (
            CATEGORY_PRIORITY.get(item.category, 100),
            item.category,
            item.stable_key,
        ),
    )
    selected: list[dict[str, Any]] = []
    used_tokens = reserved_tokens
    omitted: dict[str, int] = {}
    selected_by_category: dict[str, int] = {}
    for item in ordered:
        cost = estimate_tokens(item.record)
        if len(selected) >= budget.max_records or used_tokens + cost > budget.max_tokens:
            omitted[item.category] = omitted.get(item.category, 0) + 1
            continue
        selected.append(item.record)
        used_tokens += cost
        selected_by_category[item.category] = (
            selected_by_category.get(item.category, 0) + 1
        )
    return selected, {
        "algorithm_version": "octagon-evidence-selection-v1",
        "max_records": budget.max_records,
        "max_tokens": budget.max_tokens,
        "reserved_tokens": reserved_tokens,
        "selected_records": len(selected),
        "selected_by_category": selected_by_category,
        "omitted_by_category": omitted,
        "truncated": bool(omitted),
        "estimated_tokens": used_tokens,
    }
