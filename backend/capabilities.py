"""Research capability projection from schema and dependencies."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any

from .config import Settings
from .db import _open_sync

CAPABILITY_SCHEMA_VERSION = "octagon-capabilities-v1"

FEATURE_REQUIREMENTS: dict[str, tuple[set[str], tuple[str, ...]]] = {
    "experiments": (
        {"experiments", "idempotency_keys"},
        ("backend.experiments.repository",),
    ),
    "task_variants": ({"task_variants"}, ("backend.mutations.registry",)),
    "run_groups": (
        {
            "run_groups",
            "run_group_cells",
            "run_group_events",
            "attempt_input_snapshots",
        },
        ("backend.experiments.repository",),
    ),
    "leader_events": (
        {"leader_events", "score_transition_outbox"},
        ("backend.experiments.leader",),
    ),
    "robustness": (
        {"robustness_snapshots"},
        ("backend.experiments.aggregate",),
    ),
    "profiles": (set(), ("backend.profiles.loader",)),
    "auto_profile": (
        {"recommendation_states", "profile_recommendations"},
        ("backend.profiles.recommend",),
    ),
    "insights": ({"insight_reports"}, ("backend.insights.repository",)),
    "research_feedback": ({"research_feedback"}, ("backend.feedback",)),
    "normalized_output": (
        {"normalized_outputs"},
        ("backend.normalization.repository",),
    ),
    "attack_coverage": (set(), ("backend.attack_coverage",)),
}


def _tables(db_path: Path) -> set[str]:
    if not Path(db_path).is_file():
        return set()
    with _open_sync(db_path) as conn:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }


def _module_available(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ModuleNotFoundError, AttributeError):
        return False


def detect_capabilities(settings: Settings, db_path: Path) -> dict[str, Any]:
    tables = _tables(db_path)
    features: dict[str, bool] = {}
    details: dict[str, dict[str, Any]] = {}
    for name, (required_tables, modules) in FEATURE_REQUIREMENTS.items():
        missing_tables = sorted(required_tables - tables)
        missing_modules = sorted(
            module for module in modules if not _module_available(module)
        )
        reasons: list[str] = []
        if missing_tables:
            reasons.append("schema_missing")
        if missing_modules:
            reasons.append("dependency_missing")
        if name == "insights":
            config = settings.insights
            configured = bool(
                config.base_url
                and config.model
                and os.environ.get(config.api_key_env)
                and (
                    config.provider != "blade"
                    or config.blade_primary_skill_id
                )
            )
            if not configured:
                reasons.append("configuration_missing")
        if name in {"profiles", "auto_profile"} and not settings.octagon.profiles_path.is_dir():
            reasons.append("configuration_missing")
        enabled = not reasons
        features[name] = enabled
        details[name] = {
            "enabled": enabled,
            "schema_ready": not missing_tables,
            "dependencies_ready": not missing_modules,
            "unavailable_reasons": reasons,
        }
    # 纯展示部署：明确报告本节点为 display-only，前端据此隐藏执行入口。
    # 后端的拒绝是独立的（display_only 中间件），不依赖前端是否照做。
    display_only = bool(settings.octagon.display_only)
    if display_only:
        # 执行型能力在展示节点上一律报 false，理由可读——避免前端渲染出
        # 一个点了必然 403 的按钮。
        for name in ("experiments", "task_variants", "run_groups"):
            if name in features:
                features[name] = False
                details[name]["enabled"] = False
                details[name]["unavailable_reasons"] = sorted(
                    set(details[name]["unavailable_reasons"]) | {"display_only"}
                )
    return {
        "schema_version": CAPABILITY_SCHEMA_VERSION,
        "features": features,
        "details": details,
        "display_only": display_only,
        "execution_enabled": not display_only,
    }
