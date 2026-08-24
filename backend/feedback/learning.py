"""Bounded recommendation feedback learning with deterministic rebuild."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.db import _now_iso, _open_sync
from backend.experiments.hashing import canonical_hash, content_id
from backend.profiles.recommend import ProfileRecommendation


LEARNING_VERSION = "octagon-recommendation-learning-v1"


class LearningConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class LearnedState:
    scope_key: str
    version: int
    epoch: int
    feedback_count: int
    profile_ema: dict[str, float]
    profile_nudges: dict[str, float]
    cold_start_threshold: int
    alpha: float
    max_weight: float
    feedback_watermark: str | None
    reset_after: str | None
    state_hash: str

    def projection(self) -> dict[str, Any]:
        return {
            "scope_key": self.scope_key,
            "version": self.version,
            "epoch": self.epoch,
            "feedback_count": self.feedback_count,
            "profile_ema": self.profile_ema,
            "profile_nudges": self.profile_nudges,
            "cold_start_threshold": self.cold_start_threshold,
            "alpha": self.alpha,
            "max_weight": self.max_weight,
            "feedback_watermark": self.feedback_watermark,
            "reset_after": self.reset_after,
            "state_hash": self.state_hash,
        }


def _state_hash(payload: dict[str, Any]) -> str:
    return canonical_hash(
        {
            key: value
            for key, value in payload.items()
            if key not in {"version", "state_hash"}
        }
    )


def rebuild_state(
    db_path: Path,
    scope_key: str,
    *,
    version: int = 1,
    epoch: int = 0,
    reset_after: str | None = None,
    cold_start_threshold: int = 5,
    alpha: float = 0.2,
    max_weight: float = 0.5,
) -> LearnedState:
    if cold_start_threshold < 1 or not 0 < alpha <= 1 or not 0 <= max_weight <= 0.5:
        raise ValueError("invalid recommendation learning bounds")
    with _open_sync(Path(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT f.signal,f.scope_json,f.created_at,f.id,r.recommendation_json "
            "FROM research_feedback f JOIN profile_recommendations r ON r.id=f.target_id "
            "WHERE f.target_type='profile-recommendation' ORDER BY f.created_at,f.id"
        ).fetchall()
    ema: dict[str, float] = {}
    count = 0
    watermark = reset_after
    for row in rows:
        row_watermark = f"{row['created_at']}|{row['id']}"
        if reset_after is not None and row_watermark <= reset_after:
            continue
        scope = json.loads(row["scope_json"])
        if scope.get("scope_key") != scope_key:
            continue
        signal = 1.0 if row["signal"] in {"helpful", "accepted"} else -1.0
        profile_id = str(json.loads(row["recommendation_json"])["profile_id"])
        previous = ema.get(profile_id, 0.0)
        ema[profile_id] = round(alpha * signal + (1 - alpha) * previous, 12)
        count += 1
        watermark = row_watermark
    nudges = {
        profile: (round(max(-max_weight, min(max_weight, value * max_weight)), 12) if count >= cold_start_threshold else 0.0)
        for profile, value in sorted(ema.items())
    }
    payload = {
        "scope_key": scope_key,
        "version": version,
        "epoch": epoch,
        "feedback_count": count,
        "profile_ema": dict(sorted(ema.items())),
        "profile_nudges": nudges,
        "cold_start_threshold": cold_start_threshold,
        "alpha": alpha,
        "max_weight": max_weight,
        "feedback_watermark": watermark,
        "reset_after": reset_after,
    }
    return LearnedState(**payload, state_hash=_state_hash(payload))


@dataclass(frozen=True)
class LearningRepository:
    db_path: Path

    def get(self, scope_key: str) -> LearnedState | None:
        with _open_sync(Path(self.db_path)) as conn:
            row = conn.execute(
                "SELECT state_json FROM recommendation_states WHERE scope_key=? "
                "AND algorithm_version=?",
                (scope_key, LEARNING_VERSION),
            ).fetchone()
        return LearnedState(**json.loads(row[0])) if row else None

    def save(self, state: LearnedState, *, expected_version: int) -> LearnedState:
        next_state = LearnedState(
            **{**state.projection(), "version": expected_version + 1}
        )
        # state_hash excludes optimistic version, so replay/rebuild identity stays stable.
        with _open_sync(Path(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT state_json FROM recommendation_states WHERE scope_key=? "
                "AND algorithm_version=?",
                (state.scope_key, LEARNING_VERSION),
            ).fetchone()
            current_version = json.loads(row[0])["version"] if row else 0
            if current_version != expected_version:
                raise LearningConflict(
                    f"learning_version_conflict: expected={expected_version} current={current_version}"
                )
            payload = json.dumps(next_state.projection(), sort_keys=True)
            if row is None:
                conn.execute(
                    "INSERT INTO recommendation_states(id,scope_key,algorithm_version,state_json,"
                    "feedback_watermark,schema_version,producer_version,input_refs_json,created_at) "
                    "VALUES(?,?,?,?,?,'octagon-recommendation-state-v1',?,'{}',?)",
                    (
                        content_id("recommendation_state", {"scope": state.scope_key}),
                        state.scope_key,
                        LEARNING_VERSION,
                        payload,
                        state.feedback_watermark,
                        LEARNING_VERSION,
                        _now_iso(),
                    ),
                )
            else:
                conn.execute(
                    "UPDATE recommendation_states SET state_json=?,feedback_watermark=?,"
                    "producer_version=?,created_at=? WHERE scope_key=? AND algorithm_version=?",
                    (
                        payload,
                        state.feedback_watermark,
                        LEARNING_VERSION,
                        _now_iso(),
                        state.scope_key,
                        LEARNING_VERSION,
                    ),
                )
            conn.commit()
        return next_state

    def rebuild_and_save(self, scope_key: str) -> LearnedState:
        current = self.get(scope_key)
        expected = current.version if current else 0
        rebuilt = rebuild_state(
            Path(self.db_path),
            scope_key,
            version=expected,
            epoch=current.epoch if current else 0,
            reset_after=current.reset_after if current else None,
            cold_start_threshold=current.cold_start_threshold if current else 5,
            alpha=current.alpha if current else 0.2,
            max_weight=current.max_weight if current else 0.5,
        )
        return self.save(rebuilt, expected_version=expected)

    def reset(self, scope_key: str) -> LearnedState:
        current = self.get(scope_key)
        if current is None:
            current = rebuild_state(Path(self.db_path), scope_key, version=0)
        marker = current.feedback_watermark
        reset = rebuild_state(
            Path(self.db_path),
            scope_key,
            version=current.version,
            epoch=current.epoch + 1,
            reset_after=marker,
            cold_start_threshold=current.cold_start_threshold,
            alpha=current.alpha,
            max_weight=current.max_weight,
        )
        return self.save(reset, expected_version=current.version)


def apply_learned_nudge(
    recommendation: ProfileRecommendation,
    state: LearnedState | None,
) -> ProfileRecommendation:
    if state is None or state.scope_key != recommendation.scope_key:
        return recommendation
    nudge = state.profile_nudges.get(recommendation.profile_id, 0.0)
    provenance = dict(recommendation.provenance)
    provenance["learned_nudge"] = {
        "algorithm_version": LEARNING_VERSION,
        "state_hash": state.state_hash,
        "epoch": state.epoch,
        "feedback_count": state.feedback_count,
        "profile": recommendation.profile_id,
        "weight": nudge,
    }
    return recommendation.model_copy(
        update={
            "confidence": max(0.0, min(1.0, recommendation.confidence + nudge * 0.2)),
            "provenance": provenance,
        }
    )
