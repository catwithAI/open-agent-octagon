"""Adapters from common repository Rubric JSON shapes to the executable contract."""

from __future__ import annotations

from typing import Any

from .models import CandidateRubric


def _criteria(criterion: str, evidence: str = "") -> dict[str, str]:
    basis = criterion.strip()
    evidence_note = evidence.strip()
    return {
        "pass": f"证据明确证明完整满足：{basis}",
        "partial": f"证据证明仅部分满足：{basis}",
        "fail": f"证据明确证明不满足：{basis}",
        "not_applicable": "该检查与当前候选交付物或任务范围无关。",
        "unknown": (
            "现有证据不足、冲突或评分标准边界不足，无法可靠判断。"
            + (f" 预期证据：{evidence_note}" if evidence_note else "")
        ),
    }


def _flat_items(document: dict[str, Any]) -> list[dict[str, Any]] | None:
    items = document.get("items")
    return items if isinstance(items, list) else None


def _official_items(document: dict[str, Any]) -> list[dict[str, Any]] | None:
    items = document.get("rubric_json")
    return items if isinstance(items, list) else None


def _judge_profile_items(document: dict[str, Any]) -> list[dict[str, Any]] | None:
    items = document.get("rubric")
    return items if isinstance(items, list) else None


def _group_items(document: dict[str, Any]) -> list[dict[str, Any]] | None:
    groups = document.get("groups")
    if not isinstance(groups, list):
        return None
    result: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("items"), list):
            continue
        members = [item for item in group["items"] if isinstance(item, dict)]
        if not members:
            continue
        group_weight = float(group.get("weight", 0) or 0)
        declared_points = [float(item.get("max_points", 0) or 0) for item in members]
        point_total = sum(declared_points)
        for index, item in enumerate(members):
            item_weight = (
                group_weight * declared_points[index] / point_total
                if point_total > 0
                else group_weight / len(members)
            )
            result.append({
                **item,
                "_group_id": group.get("id"),
                "_weight": item_weight,
            })
    return result


def normalize_existing_rubric(
    *, env_name: str, version: str, document: dict[str, Any]
) -> CandidateRubric:
    """Normalize supported existing formats without changing their semantics.

    Supported source shapes:
    - ``items`` with id/max_points/criterion;
    - GDPval ``rubric_json`` with rubric_item_id/score/criterion;
    - Judge profile ``rubric`` with criterion/weight/description;
    - grouped ``groups[].items`` with group weights;
    - the canonical ``octagon-evolved-rubric-v1`` contract.
    """
    if document.get("schema_version") == "octagon-evolved-rubric-v1":
        candidate = CandidateRubric.model_validate(document)
        if candidate.env_name != env_name:
            raise ValueError("canonical rubric env_name does not match registration target")
        return candidate

    source_items = _flat_items(document)
    source_kind = "items"
    if source_items is None:
        source_items = _official_items(document)
        source_kind = "rubric_json"
    if source_items is None:
        source_items = _judge_profile_items(document)
        source_kind = "judge_profile"
    if source_items is None:
        source_items = _group_items(document)
        source_kind = "groups"
    if source_items is None or not source_items:
        raise ValueError("unsupported rubric JSON shape")

    checks: list[dict[str, Any]] = []
    for index, item in enumerate(source_items):
        if not isinstance(item, dict):
            raise ValueError(f"rubric item {index} must be an object")
        check_id = item.get("id") or item.get("rubric_item_id")
        if source_kind == "judge_profile" and not check_id:
            check_id = item.get("criterion")
        criterion = str(
            item.get("description")
            if source_kind == "judge_profile" and item.get("description")
            else item.get("criterion") or ""
        ).strip()
        if not check_id or not criterion:
            raise ValueError(f"rubric item {index} lacks id or criterion")
        if source_kind == "rubric_json":
            weight = float(item.get("score", 0) or 0)
        elif source_kind == "judge_profile":
            weight = float(item.get("weight", 0) or 0)
        elif source_kind == "groups":
            weight = float(item.get("_weight", 0) or 0)
        else:
            weight = float(item.get("max_points", item.get("weight", 0)) or 0)
        if weight <= 0:
            raise ValueError(f"rubric item {check_id} has no positive weight")
        evidence = str(item.get("evidence_standard") or "")
        title = str(item.get("title") or check_id)
        group = item.get("_group_id")
        description = criterion if group is None else f"[{group}] {criterion}"
        checks.append({
            "check_id": str(check_id),
            "title": title,
            "description": description,
            "weight": weight,
            "evidence_requirements": ([evidence] if evidence else []),
            "criteria": _criteria(criterion, evidence),
        })
    return CandidateRubric.model_validate({
        "schema_version": "octagon-evolved-rubric-v1",
        "rubric_id": str(document.get("schema_version") or env_name),
        "parent_version": "bootstrap",
        "proposed_version": version,
        "scope": "environment",
        "env_name": env_name,
        "checks": checks,
    })
