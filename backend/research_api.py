"""Capability endpoint and error boundary for new research APIs."""

from __future__ import annotations

import sqlite3
import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, FastAPI, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import runtime_state
from .capabilities import detect_capabilities
from .db import _open_sync
from .models import TERMINAL_ATTEMPT_STATUSES
from .experiments.models import ExperimentProtocol
from .experiments.events import group_events, group_snapshot
from .experiments.coordinator import CoordinatorError, schedule_run_group, stop_group
from .experiments.repository import (
    ExperimentRepository,
    RepositoryConflict,
    RepositoryInProgress,
)
from .experiments.robustness import (
    build_robustness_snapshot,
    cell_drilldown,
    filter_snapshot,
)
from .experiments.service import (
    ExperimentServiceError,
    clone_experiment,
    create_experiment,
    preview_experiment as prepare_experiment_preview,
)
from .models import ApiError
from .insights.anchors import EvidenceAnchor, EvidenceResolver
from .derived_payloads import DerivedPayloadStore
from .insights.bundle import build_evidence_bundle, build_result_evidence_records
from .insights.selectors import EvidenceBudget
from .insights.providers import configured_provider
from .insights.repository import InsightRepository
from .insights.service import (
    InsightConflict,
    InsightInProgress,
    generate_idempotent,
)
from .profiles.loader import load_profiles
from .profiles.features import extract_features
from .profiles.recommend import RecommendationRepository, recommend
from .feedback.models import FeedbackCreate
from .feedback.repository import FeedbackConflict, FeedbackRepository
from .feedback.learning import (
    LearningConflict,
    LearningRepository,
    apply_learned_nudge,
)
from .normalization.pipeline import PIPELINE_HASH
from .normalization.repository import NormalizationRepository
from .normalization.service import (
    NormalizationConflict,
    current_source_hash,
    generate_idempotent as generate_normalized_idempotent,
)
from .attack_coverage import build_attack_coverage, forensic_rerun_preview
from .security_policy import append_research_audit
from .mutations.preview import VariantPreview


class ExperimentPreviewRequest(BaseModel):
    """A preview always uses a server-loaded env contract and a frozen source."""

    model_config = ConfigDict(extra="forbid")

    env_name: str = Field(min_length=1)
    task_id: str | None = Field(default=None, min_length=1)
    prompt: str | None = Field(default=None, min_length=1, repr=False)
    context: dict[str, Any] = Field(default_factory=dict, repr=False)
    constraints: dict[str, Any] = Field(default_factory=dict, repr=False)
    # None（未传）= 跟随所选任务自身的 timeout_seconds（自由 prompt 时退回
    # 600s）；显式传值时覆盖任务默认——之前这里默认必填 600，导致 task_id
    # 模式下调用方即便显式传了别的值，也永远被 600 这个"看似显式"的默认值
    # 悄悄替换成任务自身值（prepare_preview 内部按 None 判断是否覆盖）。
    timeout_seconds: int | None = Field(default=None, gt=0)
    protocol: ExperimentProtocol

    @model_validator(mode="after")
    def _one_source(self) -> "ExperimentPreviewRequest":
        if (self.task_id is None) == (self.prompt is None):
            raise ValueError("exactly one of task_id or prompt is required")
        if self.task_id is not None and (self.context or self.constraints):
            raise ValueError("context/constraints are only valid with free prompt")
        return self


def _schedule_created_group(request: Request, created: Any) -> bool:
    """Start a newly committed group; idempotency replays never start it twice."""
    if created.replayed:
        return False
    starter = getattr(request.app.state, "run_group_starter", schedule_run_group)
    return bool(starter(request.app.state.settings, created.run_group_id))


class ExperimentCreateRequest(ExperimentPreviewRequest):
    title: str = Field(min_length=1)
    question: str = Field(min_length=1)
    preview_token: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class ExperimentCloneRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1)
    question: str | None = Field(default=None, min_length=1)


class AutoProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    env_name: str = Field(min_length=1)
    task_id: str | None = Field(default=None, min_length=1)


class FeedbackBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signal: str = Field(pattern=r"^(helpful|unhelpful|accepted|rejected|annotation)$")
    reason: str | None = Field(default=None, max_length=200)
    rationale: str | None = Field(default=None, max_length=4000, repr=False)
    supersedes: str | None = None


@dataclass
class ResearchApiException(Exception):
    status_code: int
    code: str
    message: str
    subsystem: str
    retryable: bool = False


def _request_id(request: Request) -> str:
    value = getattr(request.state, "request_id", None)
    if isinstance(value, str) and value:
        return value
    value = f"req_{uuid.uuid4().hex}"
    request.state.request_id = value
    return value


def require_feature(request: Request, feature: str) -> None:
    projection = detect_capabilities(
        request.app.state.settings, runtime_state.get().db_path
    )
    if not projection["features"].get(feature, False):
        raise ResearchApiException(
            status_code=404,
            code="feature_unavailable",
            message=f"research feature is unavailable: {feature}",
            subsystem=feature,
        )


def build_router() -> APIRouter:
    router = APIRouter(tags=["research"])

    def insight_repository() -> InsightRepository:
        state = runtime_state.get()
        return InsightRepository(
            state.db_path,
            DerivedPayloadStore(state.data_path),
        )

    def _append_feedback(
        payload: FeedbackCreate, idempotency_key: str
    ) -> dict[str, Any]:
        try:
            result = FeedbackRepository(runtime_state.get().db_path).append(
                payload, idempotency_key=idempotency_key
            )
        except FeedbackConflict as exc:
            raise ResearchApiException(
                409, str(exc), str(exc), "feedback"
            ) from exc
        return result.model_dump(mode="json")

    async def generate_insight_response(
        request: Request,
        experiment_id: str,
        idempotency_key: str,
        *,
        source_version: int | None = None,
    ) -> dict[str, Any]:
        require_feature(request, "insights")
        state = runtime_state.get()
        with _open_sync(state.db_path) as conn:
            experiment = conn.execute(
                "SELECT status FROM experiments WHERE id=?", (experiment_id,)
            ).fetchone()
            nonterminal = conn.execute(
                "SELECT COUNT(*) FROM run_groups WHERE experiment_id=? AND status NOT IN "
                "('completed','partial','failed','cancelled')",
                (experiment_id,),
            ).fetchone()[0]
        if experiment is None:
            raise ResearchApiException(
                404, "experiment_not_found", "experiment not found", "insights"
            )
        if experiment[0] not in {"completed", "partial", "failed", "cancelled"} or nonterminal:
            raise ResearchApiException(
                409,
                "insight_inputs_not_finalized",
                "experiment attempts/evaluation must be terminal before generation",
                "insights",
                retryable=True,
            )
        bundle = build_evidence_bundle(
            db_path=state.db_path,
            data_path=state.data_path,
            experiment_id=experiment_id,
        )
        factory = getattr(request.app.state, "insight_provider_factory", None)
        provider = (
            factory(request.app.state.settings.insights)
            if factory is not None
            else configured_provider(request.app.state.settings.insights)
        )
        try:
            return await generate_idempotent(
                experiment_id=experiment_id,
                bundle=bundle,
                provider=provider,
                repository=insight_repository(),
                idempotency_key=idempotency_key,
                source_version=source_version,
            )
        except InsightConflict as exc:
            raise ResearchApiException(
                409, str(exc), str(exc), "insights"
            ) from exc
        except InsightInProgress as exc:
            raise ResearchApiException(
                409, str(exc), str(exc), "insights", retryable=True
            ) from exc

    @router.get("/capabilities")
    async def capabilities(request: Request) -> dict[str, Any]:
        # detect_capabilities 做同步 sqlite 查询；直接调用会阻塞事件循环，
        # 拖慢同一进程里所有并发请求（每页都调这个接口，是最高频路径）。
        return await asyncio.to_thread(
            detect_capabilities, request.app.state.settings, runtime_state.get().db_path
        )

    @router.get("/profiles")
    async def list_profiles(request: Request) -> dict[str, Any]:
        require_feature(request, "profiles")
        try:
            catalog = load_profiles(request.app.state.settings.octagon.profiles_path)
        except ValueError as exc:
            raise ResearchApiException(
                409, "profile_catalog_invalid", str(exc), "profiles"
            ) from exc
        return {
            "schema_version": "octagon-profile-catalog-v1",
            "items": catalog.list(),
        }

    @router.get("/profiles/{profile_id}")
    async def get_profile(
        request: Request, profile_id: str, version: str | None = None
    ) -> dict[str, Any]:
        require_feature(request, "profiles")
        try:
            item = load_profiles(
                request.app.state.settings.octagon.profiles_path
            ).get(profile_id, version)
        except ValueError as exc:
            raise ResearchApiException(
                409, "profile_catalog_invalid", str(exc), "profiles"
            ) from exc
        if item is None:
            raise ResearchApiException(
                404, "profile_not_found", "profile not found", "profiles"
            )
        return item.projection(detail=True)

    @router.post("/profiles/recommend")
    async def recommend_profile(
        request: Request, body: AutoProfileRequest
    ) -> dict[str, Any]:
        require_feature(request, "auto_profile")
        state = runtime_state.get()
        env = state.envs.get(body.env_name)
        if env is None:
            raise ResearchApiException(
                404, "env_not_found", "environment not found", "auto_profile"
            )
        task = None
        if body.task_id is not None:
            task = getattr(env, "tasks_by_id", {}).get(body.task_id)
            if task is None:
                raise ResearchApiException(
                    404, "task_not_found", "task not found", "auto_profile"
                )
        try:
            result = recommend(extract_features(env=env, task=task, db_path=state.db_path))
            result = apply_learned_nudge(
                result, LearningRepository(state.db_path).get(result.scope_key)
            )
            RecommendationRepository(state.db_path).put(result)
        except Exception as exc:
            raise ResearchApiException(
                409,
                "profile_recommendation_failed",
                f"metadata recommendation failed: {exc.__class__.__name__}",
                "auto_profile",
            ) from exc
        return result.model_dump(mode="json")

    @router.get("/profiles/recommendations/{recommendation_id}")
    async def get_profile_recommendation(
        request: Request, recommendation_id: str
    ) -> dict[str, Any]:
        require_feature(request, "auto_profile")
        result = RecommendationRepository(runtime_state.get().db_path).get(
            recommendation_id
        )
        if result is None:
            raise ResearchApiException(
                404,
                "profile_recommendation_not_found",
                "profile recommendation not found",
                "auto_profile",
            )
        return result

    @router.post("/profiles/recommendations/{recommendation_id}/feedback")
    async def feedback_on_recommendation(
        request: Request,
        recommendation_id: str,
        body: FeedbackBody,
        idempotency_key: str = Header(min_length=1, alias="Idempotency-Key"),
        actor: str = Header(default="local:anonymous", alias="X-Octagon-Actor"),
    ) -> dict[str, Any]:
        require_feature(request, "research_feedback")
        recommendation = RecommendationRepository(runtime_state.get().db_path).get(
            recommendation_id
        )
        if recommendation is None:
            raise ResearchApiException(
                404, "profile_recommendation_not_found", "recommendation not found", "feedback"
            )
        payload = FeedbackCreate(
            target_type="profile-recommendation",
            target_id=recommendation_id,
            scope={"scope_key": recommendation["scope_key"]},
            actor=actor,
            **body.model_dump(),
        )
        response = _append_feedback(payload, idempotency_key)
        try:
            LearningRepository(runtime_state.get().db_path).rebuild_and_save(
                recommendation["scope_key"]
            )
        except LearningConflict:
            # Feedback is authoritative and already committed; a concurrent
            # learner will rebuild from the append-only log on its next pass.
            pass
        append_research_audit(
            runtime_state.get().db_path,
            action="feedback.append",
            target_type="profile-recommendation",
            target_id=recommendation_id,
            actor=actor,
            request_id=_request_id(request),
            metadata={"signal": body.signal, "feedback_id": response["id"]},
        )
        return response

    @router.get("/experiments")
    async def list_experiments(request: Request) -> dict[str, Any]:
        require_feature(request, "experiments")
        with _open_sync(runtime_state.get().db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id,title,question,env_name,source_task_id,protocol_hash,status,"
                "protocol_json,created_at,updated_at "
                "FROM experiments ORDER BY created_at DESC,id DESC"
            ).fetchall()
            # 每个 agent 在该实验全部 repeat 里的最佳分——列表页展示用，
            # 一条 GROUP BY 覆盖所有 experiment 避免 N+1。只取有分数的 attempt
            # （score_total 非空，即已评分终态 completed/gave_up）。
            best_rows = conn.execute(
                "SELECT g.experiment_id,a.agent_name,MAX(a.score_total) AS best_score "
                "FROM attempts a "
                "JOIN run_group_cells c ON c.run_id=a.run_id "
                "JOIN run_groups g ON g.id=c.run_group_id "
                "WHERE a.score_total IS NOT NULL "
                "GROUP BY g.experiment_id,a.agent_name"
            ).fetchall()
        best_by_experiment: dict[str, dict[str, int]] = {}
        for row in best_rows:
            best_by_experiment.setdefault(row["experiment_id"], {})[row["agent_name"]] = (
                row["best_score"]
            )
        items = []
        for row in rows:
            item = dict(row)
            protocol_json = item.pop("protocol_json")
            try:
                agents = [
                    a["agent"] for a in json.loads(protocol_json).get("agents", [])
                    if isinstance(a, dict) and a.get("agent")
                ]
            except (json.JSONDecodeError, AttributeError, TypeError):
                agents = []
            best_for_experiment = best_by_experiment.get(row["id"], {})
            # 参与该实验的每个 agent 都出一行，即便还没有任何 repeat 评上分
            # （None）——前端据此区分"agent 完全没跑通"和"跑通但低分"。
            item["best_scores"] = [
                {"agent": agent, "best_score": best_for_experiment.get(agent)}
                for agent in agents
            ]
            items.append(item)
        return {"items": items}

    @router.post("/experiments/preview", response_model=VariantPreview)
    async def preview_experiment(
        request: Request, body: ExperimentPreviewRequest
    ) -> VariantPreview:
        require_feature(request, "experiments")
        require_feature(request, "task_variants")
        try:
            prepared = prepare_experiment_preview(
                settings=request.app.state.settings,
                env_name=body.env_name,
                task_id=body.task_id,
                protocol=body.protocol,
                prompt=body.prompt,
                context=body.context,
                constraints=body.constraints,
                timeout_seconds=body.timeout_seconds,
            )
        except ExperimentServiceError as exc:
            raise ResearchApiException(
                status_code=404
                if exc.code in {"env_not_found", "task_not_found"}
                else 400,
                code=exc.code,
                message=exc.message,
                subsystem="experiments",
            ) from exc
        return prepared.preview

    @router.post("/experiments", status_code=201)
    async def create_experiment_route(
        request: Request,
        body: ExperimentCreateRequest,
        idempotency_key: str = Header(min_length=1, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        for feature in ("experiments", "task_variants", "run_groups"):
            require_feature(request, feature)
        try:
            created = create_experiment(
                settings=request.app.state.settings,
                title=body.title,
                question=body.question,
                env_name=body.env_name,
                task_id=body.task_id,
                protocol=body.protocol,
                preview_token=body.preview_token,
                idempotency_key=idempotency_key,
                prompt=body.prompt,
                context=body.context,
                constraints=body.constraints,
                timeout_seconds=body.timeout_seconds,
            )
        except ExperimentServiceError as exc:
            status = 404 if exc.code in {"env_not_found", "task_not_found"} else 409
            raise ResearchApiException(
                status_code=status,
                code=exc.code,
                message=exc.message,
                subsystem="experiments",
            ) from exc
        except (RepositoryConflict, RepositoryInProgress) as exc:
            raise ResearchApiException(
                status_code=409,
                code=str(exc),
                message=str(exc),
                subsystem="experiments",
                retryable=isinstance(exc, RepositoryInProgress),
            ) from exc
        execution_scheduled = _schedule_created_group(request, created)
        return {
            **created.response(),
            "replayed": created.replayed,
            "execution_scheduled": execution_scheduled,
        }

    @router.get("/experiments/{experiment_id}")
    async def get_experiment(request: Request, experiment_id: str) -> dict[str, Any]:
        require_feature(request, "experiments")
        state = runtime_state.get()
        bundle = ExperimentRepository(state.db_path, state.data_path).get_bundle(
            experiment_id
        )
        if bundle is None:
            raise ResearchApiException(
                status_code=404,
                code="experiment_not_found",
                message=f"experiment not found: {experiment_id}",
                subsystem="experiments",
            )
        return bundle

    @router.post("/experiments/{experiment_id}/clone", status_code=201)
    async def clone_experiment_route(
        request: Request,
        experiment_id: str,
        body: ExperimentCloneRequest,
        idempotency_key: str = Header(min_length=1, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        for feature in ("experiments", "task_variants", "run_groups"):
            require_feature(request, feature)
        try:
            created = clone_experiment(
                experiment_id=experiment_id,
                idempotency_key=idempotency_key,
                title=body.title,
                question=body.question,
            )
        except ExperimentServiceError as exc:
            raise ResearchApiException(
                status_code=404,
                code=exc.code,
                message=exc.message,
                subsystem="experiments",
            ) from exc
        except (RepositoryConflict, RepositoryInProgress) as exc:
            raise ResearchApiException(
                status_code=409,
                code=str(exc),
                message=str(exc),
                subsystem="experiments",
                retryable=isinstance(exc, RepositoryInProgress),
            ) from exc
        execution_scheduled = _schedule_created_group(request, created)
        return {
            **created.response(),
            "replayed": created.replayed,
            "execution_scheduled": execution_scheduled,
        }

    @router.post("/experiments/{experiment_id}/groups/{group_id}/stop")
    async def stop_run_group_route(
        request: Request, experiment_id: str, group_id: str
    ) -> dict[str, Any]:
        require_feature(request, "run_groups")
        try:
            stopped = stop_group(group_id, experiment_id=experiment_id)
        except CoordinatorError as exc:
            raise ResearchApiException(
                status_code=404,
                code="run_group_not_found",
                message=str(exc),
                subsystem="run_groups",
            ) from exc
        append_research_audit(
            runtime_state.get().db_path,
            action="run_group.stop",
            target_type="run_group",
            target_id=group_id,
            request_id=_request_id(request),
            metadata={"experiment_id": experiment_id, "stopped_cells": stopped["cells"]},
        )
        return {"experiment_id": experiment_id, "run_group_id": group_id, **stopped}

    @router.get("/experiments/{experiment_id}/groups")
    async def list_run_groups(
        request: Request, experiment_id: str
    ) -> dict[str, Any]:
        require_feature(request, "run_groups")
        state = runtime_state.get()
        with _open_sync(state.db_path) as conn:
            rows = conn.execute(
                "SELECT id FROM run_groups WHERE experiment_id=? ORDER BY created_at,id",
                (experiment_id,),
            ).fetchall()
        return {
            "items": [
                group_snapshot(
                    state.db_path, row[0], experiment_id=experiment_id
                )
                for row in rows
            ]
        }

    @router.get("/experiments/{experiment_id}/groups/{group_id}")
    async def get_run_group(
        request: Request, experiment_id: str, group_id: str
    ) -> dict[str, Any]:
        require_feature(request, "run_groups")
        snapshot = group_snapshot(
            runtime_state.get().db_path,
            group_id,
            experiment_id=experiment_id,
        )
        if snapshot is None:
            raise ResearchApiException(
                status_code=404,
                code="run_group_not_found",
                message=f"run group not found: {group_id}",
                subsystem="run_groups",
            )
        return snapshot

    @router.get("/experiments/{experiment_id}/groups/{group_id}/stream")
    async def stream_run_group(
        request: Request,
        experiment_id: str,
        group_id: str,
        once: bool = False,
    ) -> StreamingResponse:
        require_feature(request, "run_groups")
        state = runtime_state.get()
        initial = group_snapshot(
            state.db_path, group_id, experiment_id=experiment_id
        )
        if initial is None:
            raise ResearchApiException(
                status_code=404,
                code="run_group_not_found",
                message=f"run group not found: {group_id}",
                subsystem="run_groups",
            )

        async def event_stream():
            cursor = initial["cursor"]
            snapshot_event = {
                "schema_version": "octagon-run-group-event-v1",
                "sequence": cursor,
                "event_type": "snapshot",
                "data": initial,
            }
            yield (
                f"id: {cursor}\nevent: snapshot\ndata: "
                f"{json.dumps(snapshot_event, ensure_ascii=False, sort_keys=True)}\n\n"
            )
            if once:
                return
            while not await request.is_disconnected():
                events = group_events(state.db_path, group_id, after=cursor)
                for event in events:
                    cursor = event["sequence"]
                    yield (
                        f"id: {cursor}\nevent: {event['event_type']}\ndata: "
                        f"{json.dumps(event, ensure_ascii=False, sort_keys=True)}\n\n"
                    )
                await asyncio.sleep(0.5)

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @router.get("/experiments/{experiment_id}/groups/{group_id}/robustness")
    async def get_robustness(
        request: Request,
        experiment_id: str,
        group_id: str,
        metric: str | None = None,
        mutator: str | None = None,
        agent: str | None = None,
        model: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        require_feature(request, "robustness")
        if offset < 0 or not 1 <= limit <= 1000:
            raise ResearchApiException(
                400, "pagination_invalid", "invalid offset/limit", "robustness"
            )
        snapshot = group_snapshot(
            runtime_state.get().db_path, group_id, experiment_id=experiment_id
        )
        if snapshot is None:
            raise ResearchApiException(
                404, "run_group_not_found", "run group not found", "robustness"
            )
        try:
            result = build_robustness_snapshot(runtime_state.get().db_path, group_id)
            return filter_snapshot(
                result,
                metric=metric,
                mutator=mutator,
                agent=agent,
                model=model,
                offset=offset,
                limit=limit,
            )
        except ValueError as exc:
            raise ResearchApiException(
                409, "robustness_unavailable", str(exc), "robustness"
            ) from exc

    @router.get("/experiments/{experiment_id}/groups/{group_id}/cells/{cell_id}")
    async def get_cell_drilldown(
        request: Request, experiment_id: str, group_id: str, cell_id: str
    ) -> dict[str, Any]:
        require_feature(request, "robustness")
        if group_snapshot(
            runtime_state.get().db_path, group_id, experiment_id=experiment_id
        ) is None:
            raise ResearchApiException(
                404, "run_group_not_found", "run group not found", "robustness"
            )
        result = cell_drilldown(runtime_state.get().db_path, group_id, cell_id)
        if result is None:
            raise ResearchApiException(
                404, "cell_not_found", "cell not found", "robustness"
            )
        return result

    @router.post("/experiments/{experiment_id}/rerun-preview")
    async def rerun_preview(request: Request, experiment_id: str) -> dict[str, Any]:
        require_feature(request, "experiments")
        state = runtime_state.get()
        bundle = ExperimentRepository(state.db_path, state.data_path).get_bundle(
            experiment_id
        )
        if bundle is None:
            raise ResearchApiException(
                404, "experiment_not_found", "experiment not found", "experiments"
            )
        selected = [
            cell["id"]
            for cell in bundle["cells"]
            if cell["status"] in {"failed", "partial"}
        ]
        return {
            "schema_version": "octagon-rerun-preview-v1",
            "experiment_id": experiment_id,
            "source_group_ids": [group["id"] for group in bundle["groups"]],
            "selected_cell_ids": selected,
            "protocol": json.loads(bundle["experiment"]["protocol_json"]),
            "warning": "draft only; explicit create confirmation is required",
        }

    @router.get("/experiments/{experiment_id}/insights")
    async def list_insights(
        request: Request,
        experiment_id: str,
        cursor: int | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        require_feature(request, "insights")
        try:
            items = insight_repository().list(
                experiment_id, before_version=cursor, limit=limit
            )
        except ValueError as exc:
            raise ResearchApiException(
                400, "pagination_invalid", str(exc), "insights"
            ) from exc
        return {
            "items": items,
            "next_cursor": items[-1]["version"] if len(items) == limit else None,
        }

    @router.get("/experiments/{experiment_id}/insights/{version}")
    async def get_insight(
        request: Request, experiment_id: str, version: int
    ) -> dict[str, Any]:
        require_feature(request, "insights")
        item = insight_repository().get(experiment_id, version)
        if item is None:
            raise ResearchApiException(
                404, "insight_not_found", "insight version not found", "insights"
            )
        return item

    @router.post("/experiments/{experiment_id}/insights/generate")
    async def generate_insight(
        request: Request,
        experiment_id: str,
        idempotency_key: str = Header(min_length=1, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        return await generate_insight_response(
            request, experiment_id, idempotency_key
        )

    @router.post("/experiments/{experiment_id}/insights/{version}/regenerate")
    async def regenerate_insight(
        request: Request,
        experiment_id: str,
        version: int,
        idempotency_key: str = Header(min_length=1, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        require_feature(request, "insights")
        if insight_repository().get(experiment_id, version) is None:
            raise ResearchApiException(
                404, "insight_not_found", "source insight version not found", "insights"
            )
        result = await generate_insight_response(
            request,
            experiment_id,
            idempotency_key,
            source_version=version,
        )
        append_research_audit(
            runtime_state.get().db_path,
            action="insight.regenerate",
            target_type="experiment",
            target_id=experiment_id,
            request_id=_request_id(request),
            metadata={"source_version": version, "result_version": result.get("version")},
        )
        return result

    @router.post("/experiments/{experiment_id}/insights/{version}/feedback")
    async def feedback_on_insight(
        request: Request,
        experiment_id: str,
        version: int,
        body: FeedbackBody,
        idempotency_key: str = Header(min_length=1, alias="Idempotency-Key"),
        actor: str = Header(default="local:anonymous", alias="X-Octagon-Actor"),
    ) -> dict[str, Any]:
        require_feature(request, "research_feedback")
        insight = insight_repository().get(experiment_id, version)
        if insight is None:
            raise ResearchApiException(
                404, "insight_not_found", "insight version not found", "feedback"
            )
        payload = FeedbackCreate(
            target_type="insight",
            target_id=insight["id"],
            scope={"experiment_id": experiment_id, "version": version},
            actor=actor,
            **body.model_dump(),
        )
        result = _append_feedback(payload, idempotency_key)
        append_research_audit(
            runtime_state.get().db_path,
            action="feedback.append",
            target_type="insight",
            target_id=insight["id"],
            actor=actor,
            request_id=_request_id(request),
            metadata={"signal": body.signal, "feedback_id": result["id"]},
        )
        return result

    @router.get("/research-feedback/export")
    async def export_research_feedback(
        request: Request,
        target_type: str | None = None,
        target_id: str | None = None,
        include_rationale: bool = False,
        local_admin: str | None = Header(default=None, alias="X-Octagon-Local-Admin"),
    ) -> dict[str, Any]:
        require_feature(request, "research_feedback")
        if include_rationale and local_admin != "true":
            raise ResearchApiException(
                403,
                "rationale_access_denied",
                "rationale export requires explicit local-admin policy",
                "feedback",
            )
        result = {
            "schema_version": "octagon-feedback-export-v1",
            "items": FeedbackRepository(runtime_state.get().db_path).export(
                target_type=target_type,
                target_id=target_id,
                include_rationale=include_rationale,
            ),
            "rationale_included": include_rationale,
        }
        append_research_audit(
            runtime_state.get().db_path,
            action="feedback.export",
            target_type=target_type or "feedback",
            target_id=target_id or "all",
            request_id=_request_id(request),
            metadata={
                "export_mode": "full" if include_rationale else "metadata",
                "item_count": len(result["items"]),
            },
        )
        return result

    @router.get("/attempts/{attempt_id}/normalized")
    async def get_normalized_output(
        request: Request, attempt_id: str
    ) -> dict[str, Any]:
        require_feature(request, "normalized_output")
        state = runtime_state.get()
        with _open_sync(state.db_path) as conn:
            attempt = conn.execute(
                "SELECT status FROM attempts WHERE id=?", (attempt_id,)
            ).fetchone()
        if attempt is None:
            raise ResearchApiException(
                404, "attempt_not_found", "attempt not found", "normalized_output"
            )
        source = state.data_path / "attempts" / attempt_id / "final_state.json"
        source_available = source.is_file() and not source.is_symlink()
        source_finalized = attempt[0] in TERMINAL_ATTEMPT_STATUSES
        repository = NormalizationRepository(
            state.db_path, DerivedPayloadStore(state.data_path)
        )
        item = repository.get(
            attempt_id,
            pipeline_hash=PIPELINE_HASH,
            current_source_hash=current_source_hash(state.data_path, attempt_id),
        )
        return {
            "attempt_id": attempt_id,
            "status": item["status"] if item else "not_generated",
            "raw_ref": f"attempts/{attempt_id}/final_state.json",
            "generation_available": source_available and source_finalized,
            "generation_unavailable_reason": (
                None
                if source_available and source_finalized
                else "source_not_finalized"
                if not source_finalized
                else "final_output_unavailable"
            ),
            "normalized": item,
            "search_projection": {
                "source": "normalized-derived" if item and item["status"] == "current" else None,
                "raw_source": "final_state",
            },
        }

    @router.post("/attempts/{attempt_id}/normalized/generate")
    async def generate_normalized_output(
        request: Request,
        attempt_id: str,
        idempotency_key: str = Header(min_length=1, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        require_feature(request, "normalized_output")
        state = runtime_state.get()
        repository = NormalizationRepository(
            state.db_path, DerivedPayloadStore(state.data_path)
        )
        try:
            result = generate_normalized_idempotent(
                db_path=state.db_path,
                data_path=state.data_path,
                attempt_id=attempt_id,
                repository=repository,
                idempotency_key=idempotency_key,
            )
            append_research_audit(
                state.db_path,
                action="normalization.generate",
                target_type="attempt",
                target_id=attempt_id,
                request_id=_request_id(request),
                metadata={"status": result.get("status"), "output_id": result.get("id")},
            )
            return result
        except NormalizationConflict as exc:
            raise ResearchApiException(
                409, str(exc), str(exc), "normalized_output"
            ) from exc
        except ValueError as exc:
            status = 404 if str(exc) == "attempt not found" else 409
            raise ResearchApiException(
                status,
                "normalization_unavailable",
                str(exc),
                "normalized_output",
            ) from exc

    @router.get("/experiments/{experiment_id}/attack-coverage")
    async def get_attack_coverage(
        request: Request, experiment_id: str
    ) -> dict[str, Any]:
        require_feature(request, "attack_coverage")
        try:
            return build_attack_coverage(
                db_path=runtime_state.get().db_path,
                data_path=runtime_state.get().data_path,
                experiment_id=experiment_id,
            )
        except ValueError as exc:
            raise ResearchApiException(
                404, "experiment_not_found", str(exc), "attack_coverage"
            ) from exc

    @router.post("/experiments/{experiment_id}/attack-coverage/rerun-preview")
    async def preview_attack_forensic_rerun(
        request: Request, experiment_id: str
    ) -> dict[str, Any]:
        require_feature(request, "attack_coverage")
        try:
            coverage = build_attack_coverage(
                db_path=runtime_state.get().db_path,
                data_path=runtime_state.get().data_path,
                experiment_id=experiment_id,
            )
        except ValueError as exc:
            raise ResearchApiException(
                404, "experiment_not_found", str(exc), "attack_coverage"
            ) from exc
        return forensic_rerun_preview(coverage)

    @router.get("/experiments/{experiment_id}/evidence/resolve")
    async def resolve_evidence(
        request: Request, experiment_id: str, anchor: str
    ) -> dict[str, Any]:
        """Resolve an anchor under the metadata-only, fail-closed API policy."""

        require_feature(request, "experiments")
        try:
            parsed = EvidenceResolver(
                db_path=runtime_state.get().db_path,
                data_path=runtime_state.get().data_path,
            )
            evidence_anchor = EvidenceAnchor.parse(anchor)
        except ValueError as exc:
            raise ResearchApiException(
                400, "evidence_anchor_invalid", str(exc), "evidence"
            ) from exc
        if evidence_anchor.experiment_id != experiment_id:
            raise ResearchApiException(
                404,
                "evidence_anchor_foreign",
                "anchor does not belong to the requested experiment",
                "evidence",
            )
        return {
            "schema_version": "octagon-evidence-resolution-v1",
            "anchor": evidence_anchor.uri(),
            "resolution": parsed.resolve(evidence_anchor),
        }

    @router.get("/experiments/{experiment_id}/evidence")
    async def list_evidence(
        request: Request,
        experiment_id: str,
        category: str | None = None,
        source: str | None = None,
        attempt_id: str | None = None,
        status: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Return a bounded, metadata-only Evidence index for UI navigation."""

        require_feature(request, "experiments")
        if offset < 0 or not 1 <= limit <= 200:
            raise ResearchApiException(
                400, "pagination_invalid", "invalid offset/limit", "evidence"
            )
        if category == "result" and source is None:
            try:
                # 同步 sqlite 查询 + 文件 I/O；to_thread 避免阻塞事件循环
                # （见下方 build_evidence_bundle 分支同款注释）。
                records = await asyncio.to_thread(
                    build_result_evidence_records,
                    db_path=runtime_state.get().db_path,
                    data_path=runtime_state.get().data_path,
                    experiment_id=experiment_id,
                )
            except ValueError as exc:
                raise ResearchApiException(
                    404, "experiment_not_found", str(exc), "evidence"
                ) from exc
            filtered = [
                record
                for record in records
                if (attempt_id is None or record["attempt_id"] == attempt_id)
                and (status is None or record["status"] == status)
            ]
            page = filtered[offset : offset + limit]
            next_offset = offset + len(page)
            return {
                "schema_version": "octagon-evidence-index-v1",
                "experiment_id": experiment_id,
                "items": page,
                "anchors": {},
                "total": len(filtered),
                "offset": offset,
                "limit": limit,
                "next_offset": next_offset if next_offset < len(filtered) else None,
                "selection_manifest": {
                    "selected_records": len(filtered),
                    "selected_by_category": {"result": len(filtered)},
                    "omitted_by_category": {},
                    "truncated": False,
                },
                "capture_policy": {
                    "payload_included": False,
                    "allowed_sources": [],
                },
            }
        try:
            # build_evidence_bundle 全量重扫 experiments/run_groups/attempts/
            # scores 多表 JOIN + 逐 attempt 文件读取，是同步阻塞调用；这是
            # evidence 接口偶发 10-20s 延迟的主因（评测批量写库时更明显，
            # 阻塞的是整个进程而非仅本请求）。to_thread 挪出事件循环。
            bundle = await asyncio.to_thread(
                build_evidence_bundle,
                db_path=runtime_state.get().db_path,
                data_path=runtime_state.get().data_path,
                experiment_id=experiment_id,
                budget=EvidenceBudget(max_records=2_000, max_tokens=2_000_000),
            )
        except ValueError as exc:
            raise ResearchApiException(
                404, "experiment_not_found", str(exc), "evidence"
            ) from exc

        def matches(record: dict[str, Any]) -> bool:
            if category is not None and record.get("category") != category:
                return False
            if source is not None and record.get("source") != source:
                return False
            if attempt_id is not None and record.get("attempt_id") != attempt_id:
                return False
            record_status = record.get("status")
            resolution = record.get("resolution")
            resolution_status = (
                resolution.get("status") if isinstance(resolution, dict) else None
            )
            return status is None or status in {record_status, resolution_status}

        filtered = [record for record in bundle["records"] if matches(record)]
        page = filtered[offset : offset + limit]
        page_anchors = {
            record["anchor"]
            for record in page
            if isinstance(record.get("anchor"), str)
        }
        next_offset = offset + len(page)
        return {
            "schema_version": "octagon-evidence-index-v1",
            "experiment_id": experiment_id,
            "items": page,
            "anchors": {
                anchor: bundle["anchors"][anchor]
                for anchor in sorted(page_anchors)
                if anchor in bundle["anchors"]
            },
            "total": len(filtered),
            "offset": offset,
            "limit": limit,
            "next_offset": next_offset if next_offset < len(filtered) else None,
            "selection_manifest": bundle["manifest"],
            "capture_policy": bundle["capture_policy"],
        }

    return router


def register_routes(app: FastAPI) -> None:
    app.include_router(build_router(), prefix="/api")

    @app.exception_handler(ResearchApiException)
    async def research_error_handler(
        request: Request, exc: ResearchApiException
    ) -> JSONResponse:
        error = ApiError(
            code=exc.code,
            message=exc.message,
            request_id=_request_id(request),
            subsystem=exc.subsystem,
            retryable=exc.retryable,
        )
        return JSONResponse(status_code=exc.status_code, content=error.model_dump())
