"""Deterministic advisory Auto Profile rules and persistence."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.db import _now_iso, _open_sync
from backend.experiments.hashing import content_id

from .features import FEATURE_VERSION, ProfileFeatures


RULES_VERSION = "octagon-auto-profile-rules-v1"


class RecommendationReason(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    message: str
    blocking: bool = False


class ProfileRecommendation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["octagon-profile-recommendation-v1"]
    id: str
    scope_key: str
    status: Literal["advisory", "insufficient-data"]
    profile_id: Literal["smoke", "standard", "deep", "forensic"]
    patch: dict[str, Any] = Field(default_factory=dict)
    reasons: tuple[RecommendationReason, ...]
    confidence: float = Field(ge=0, le=1)
    provenance: dict[str, Any]


def recommend(features: ProfileFeatures) -> ProfileRecommendation:
    reasons: list[RecommendationReason] = []
    patch: dict[str, Any] = {}
    profile_id: Literal["smoke", "standard", "deep", "forensic"] = "standard"
    confidence = 0.65
    if features.danger_tool_count:
        profile_id = "forensic"
        confidence = 0.9
        reasons.append(
            RecommendationReason(
                code="danger-tools",
                message="danger-tool metadata suggests a small forensic comparison",
            )
        )
        # Advisory only: never auto-elevate capture to full.
        patch["capture_policy"] = "metadata"
    elif features.multi_turn or features.material_total_bytes >= 25 * 1024 * 1024:
        profile_id = "deep"
        confidence = 0.8
        reasons.append(
            RecommendationReason(
                code="context-complexity",
                message="multi-turn or large-material metadata benefits from repeated variants",
            )
        )
    elif features.category == "coding" or features.deterministic_task:
        profile_id = "standard"
        reasons.append(
            RecommendationReason(
                code="deterministic-task",
                message="deterministic task metadata supports standard surface variants",
            )
        )
    else:
        reasons.append(
            RecommendationReason(
                code="general-default",
                message="no high-risk metadata signal; standard profile is advisory default",
            )
        )
    if features.duration_p90_ms is not None:
        timeout = max(300, min(3600, (features.duration_p90_ms * 2 + 999) // 1000))
        patch["timeout_seconds"] = timeout
        reasons.append(
            RecommendationReason(
                code="historical-duration",
                message="timeout patch derives from historical p90 duration metadata",
            )
        )
    if features.prerequisite_warning_count:
        confidence = min(confidence, 0.4)
        reasons.append(
            RecommendationReason(
                code="prerequisite-missing",
                message="environment has unresolved prerequisite warnings",
                blocking=True,
            )
        )
    status: Literal["advisory", "insufficient-data"] = (
        "advisory" if features.historical_count >= 5 else "insufficient-data"
    )
    if status == "insufficient-data":
        confidence = min(confidence, 0.6)
    scope_key = f"{features.env_name}:{features.task_id or '*'}"
    identity = {
        "scope_key": scope_key,
        "features": features.feature_hash,
        "rules": RULES_VERSION,
        "profile_id": profile_id,
        "patch": patch,
    }
    return ProfileRecommendation(
        schema_version="octagon-profile-recommendation-v1",
        id=content_id("recommendation_state", identity),
        scope_key=scope_key,
        status=status,
        profile_id=profile_id,
        patch=patch,
        reasons=tuple(reasons),
        confidence=confidence,
        provenance={
            "rules_version": RULES_VERSION,
            "feature_version": FEATURE_VERSION,
            "feature_hash": features.feature_hash,
            "history_count": features.historical_count,
            "content_access": "metadata-only",
        },
    )


@dataclass(frozen=True)
class RecommendationRepository:
    db_path: Path

    def put(self, recommendation: ProfileRecommendation) -> None:
        now = _now_iso()
        payload = recommendation.model_dump(mode="json")
        with _open_sync(Path(self.db_path)) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO profile_recommendations(id,scope_key,feature_hash,"
                "algorithm_version,recommendation_json,schema_version,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    recommendation.id,
                    recommendation.scope_key,
                    str(recommendation.provenance["feature_hash"]),
                    RULES_VERSION,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    recommendation.schema_version,
                    now,
                ),
            )
            conn.commit()

    def get(self, recommendation_id: str) -> dict[str, Any] | None:
        with _open_sync(Path(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT recommendation_json,created_at FROM profile_recommendations WHERE id=?",
                (recommendation_id,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["recommendation_json"])
        payload["created_at"] = row["created_at"]
        return payload
