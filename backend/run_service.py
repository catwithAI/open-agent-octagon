"""Run creation and dispatch admission shared by HTTP and experiment callers.

This module deliberately has no FastAPI dependency.  HTTP handlers translate
``RunServiceError`` into transport errors; coordinators can use the same
validation, persistence and job-plan construction directly.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import sqlite3
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from . import runtime_state
from .db import (
    IdempotencyConflict,
    IdempotencyInProgress,
    _now_iso,
    _open_sync,
    claim_idempotency,
    complete_idempotency,
    fail_idempotency,
)
from .experiments.hashing import canonical_hash
from .model_providers import parse_model_ref
from .run_dispatch import KNOWN_AGENTS, dispatch as dispatch_attempt
from .runner import create_attempt

logger = logging.getLogger(__name__)

CapturePolicyName = Literal["off", "metadata", "parsed", "full"]
BladeModelsLoader = Callable[[str, str | None], Awaitable[dict[str, Any]]]


class RunServiceError(ValueError):
    """Transport-neutral validation/creation failure."""

    def __init__(self, status_code: int, detail: str, *, code: str | None = None) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.code = code


@dataclass(slots=True)
class NormalizedRunRequest:
    env_name: str
    task_id: str | None = None
    prompt: str | None = field(default=None, repr=False)
    context: dict[str, Any] = field(default_factory=dict, repr=False)
    constraints: dict[str, Any] = field(default_factory=dict, repr=False)
    timeout_seconds: int | None = 1000
    # ``timeout_seconds`` has three API states: omitted follows the selected
    # task, an integer overrides it, and an explicit null disables the
    # deadline.  Optional alone cannot retain omitted-vs-null after request
    # normalization, so API callers set this field-presence bit.
    timeout_seconds_explicit: bool = False
    agents: list[str] = field(default_factory=lambda: ["blade-agent"])
    compare_mode: str = "multi-agent"
    model: str | None = None
    models: dict[str, str] | list[str] | None = None
    execution: str | None = None
    blade_model: str | None = None
    blade_enable_thinking: bool | None = None
    capture_policy: CapturePolicyName | None = None


@dataclass(slots=True)
class FrozenRunInput:
    """Execution input override whose task_id remains source lineage only."""

    prompt: str = field(repr=False)
    context: dict[str, Any] = field(default_factory=dict, repr=False)
    constraints: dict[str, Any] = field(default_factory=dict, repr=False)
    timeout_seconds: int | None = None


@dataclass(slots=True)
class CreatedRun:
    run_id: str
    task_id: str
    env_name: str
    agents: list[str]
    attempts: list[dict[str, Any]]
    execution: Literal["serial", "parallel"]
    dispatch_jobs: list[dict[str, Any]] = field(default_factory=list, repr=False)
    created: bool = True

    def response(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "env_name": self.env_name,
            "agents": self.agents,
            "attempts": self.attempts,
        }


def _normalize_model_for_agent(
    agent_name: str, model: str | None, settings: Any
) -> str | None:
    if model is None:
        return None
    normalized = model.strip()
    if not normalized:
        raise RunServiceError(400, f"{agent_name} 模型名不能为空")
    if agent_name == "blade-agent":
        return parse_model_ref(normalized, settings.model_providers).model
    return normalized


def _blade_model_ids(payload: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for item in payload.get("models") or []:
        if isinstance(item, str):
            result.add(item)
        elif isinstance(item, dict):
            value = item.get("id") or item.get("model") or item.get("name")
            if isinstance(value, str) and value:
                result.add(value)
    return result


async def _normalize_job_plan(
    settings: Any,
    job_plan: list[tuple[str, str | None]],
    blade_models_loader: BladeModelsLoader | None,
) -> list[tuple[str, str | None]]:
    normalized = [
        (agent, _normalize_model_for_agent(agent, model, settings))
        for agent, model in job_plan
    ]
    requested = {
        model for agent, model in normalized if agent == "blade-agent" and model
    }
    if not requested or blade_models_loader is None:
        return normalized

    api_key = settings.blade.api_key.get_secret_value() if settings.blade.api_key else None
    catalog = await blade_models_loader(settings.blade.base_url, api_key)
    if catalog.get("error"):
        logger.warning("skip blade model catalog check: %s", catalog["error"])
        return normalized
    off_catalog = sorted(requested - _blade_model_ids(catalog))
    if off_catalog:
        logger.info("blade models not in catalog (passthrough): %s", off_catalog)
    return normalized


def _validate_and_build_job_plan(
    request: NormalizedRunRequest, settings: Any
) -> tuple[list[tuple[str, str | None]], Literal["serial", "parallel"]]:
    agents = request.agents
    if not agents:
        raise RunServiceError(400, "agents list is empty")
    for agent in agents:
        if agent not in KNOWN_AGENTS:
            raise RunServiceError(
                400, f"unknown agent: {agent!r}, known: {KNOWN_AGENTS}"
            )
    if request.compare_mode not in ("multi-agent", "same-model", "multi-model"):
        raise RunServiceError(400, f"unknown compare_mode: {request.compare_mode!r}")
    if request.execution not in (None, "serial", "parallel"):
        raise RunServiceError(
            400,
            f"unknown execution: {request.execution!r}, 可选 serial | parallel",
        )
    execution: Literal["serial", "parallel"] = request.execution or "parallel"

    if request.compare_mode == "same-model":
        if len(agents) < 2:
            raise RunServiceError(400, "same-model 模式至少需要 2 个 agent")
        if len(set(agents)) != len(agents):
            raise RunServiceError(400, "same-model 模式 agents 不能重复")
        if isinstance(request.models, dict):
            missing = [agent for agent in agents if not request.models.get(agent)]
            if missing:
                raise RunServiceError(
                    400, f"same-model 模式 models 必须覆盖所有 agents，缺失: {missing}"
                )
            plan = [(agent, request.models[agent]) for agent in agents]
        elif request.models is not None:
            raise RunServiceError(400, "same-model 模式 models 必须是 {agent: model} 映射")
        elif request.model:
            plan = [(agent, request.model) for agent in agents]
        else:
            raise RunServiceError(400, "same-model 模式必须指定 model 或 models")
    elif request.compare_mode == "multi-model":
        if len(agents) != 1:
            raise RunServiceError(
                400, "multi-model 模式必须且只能选择一个 agent"
            )
        if not isinstance(request.models, list) or len(request.models) < 2:
            raise RunServiceError(400, "multi-model 模式 models 必须是至少 2 个元素的列表")
        plan = [(agents[0], model) for model in request.models]
    else:
        if isinstance(request.models, dict):
            missing = [agent for agent in agents if agent not in request.models]
            if missing:
                raise RunServiceError(
                    400, f"multi-agent models 缺少 agent: {missing}"
                )
            plan = [(agent, request.models[agent]) for agent in agents]
        elif request.models is not None:
            raise RunServiceError(400, "multi-agent models 必须是 {agent: model} 映射")
        else:
            plan = []
            for agent in agents:
                model = request.model
                if agent == "blade-agent":
                    model = request.blade_model or request.model
                plan.append((agent, model))

    if "blade-agent" in agents and not settings.blade.api_key:
        raise RunServiceError(
            400, "blade 凭据缺失:请配置 `blade.api_key`（环境变量 BLADE_API_KEY）。"
        )
    return plan, execution


def _raise_if_env_unavailable(env: Any) -> None:
    load_error = getattr(env, "load_error", None)
    if load_error is not None:
        raise RunServiceError(
            400,
            f"env unavailable: {env.name}. {load_error}. 请配置 "
            "octagon.selected_skills_path 或 SELECTED_SKILLS_DIR 后重启。",
        )


def _hydrate_file_task(db_path: Path, request: NormalizedRunRequest) -> tuple[str, str]:
    state = runtime_state.get()
    env = state.envs.get(request.env_name)
    if env is None:
        raise RunServiceError(404, f"env not found: {request.env_name}")
    task = env.tasks_by_id.get(request.task_id or "")
    if task is None:
        raise RunServiceError(
            404, f"task not found in env={request.env_name}: {request.task_id}"
        )
    now = _now_iso()
    context_json = json.dumps(task.context, ensure_ascii=False)
    constraints_json = json.dumps(task.constraints, ensure_ascii=False)
    with _open_sync(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO tasks(id, env_name, prompt, context_json, constraints_json,"
            " timeout_seconds, source, created_at) VALUES(?, ?, ?, ?, ?, ?, 'file', ?)",
            (
                task.id,
                task.env_name,
                task.prompt,
                context_json,
                constraints_json,
                task.timeout_seconds,
                now,
            ),
        )
        conn.execute(
            "UPDATE tasks SET env_name=?, prompt=?, context_json=?, constraints_json=?,"
            " timeout_seconds=? WHERE id=? AND source='file'",
            (
                task.env_name,
                task.prompt,
                context_json,
                constraints_json,
                task.timeout_seconds,
                task.id,
            ),
        )
        conn.commit()
    return task.id, task.env_name


def _get_or_create_task(db_path: Path, request: NormalizedRunRequest) -> tuple[str, str]:
    if request.task_id:
        env = runtime_state.get().envs.get(request.env_name)
        if env is not None and request.task_id in getattr(env, "tasks_by_id", {}):
            return _hydrate_file_task(db_path, request)
        with _open_sync(db_path) as conn:
            row = conn.execute(
                "SELECT env_name FROM tasks WHERE id=?", (request.task_id,)
            ).fetchone()
        if row is None:
            raise RunServiceError(
                404, f"task not found in env={request.env_name}: {request.task_id}"
            )
        return request.task_id, row[0]

    task_id = f"adhoc_{uuid.uuid4().hex[:12]}"
    with _open_sync(db_path) as conn:
        conn.execute(
            "INSERT INTO tasks(id, env_name, prompt, context_json, constraints_json,"
            " timeout_seconds, source, created_at) VALUES(?, ?, ?, ?, ?, ?, 'adhoc', ?)",
            (
                task_id,
                request.env_name,
                request.prompt or "",
                json.dumps(request.context, ensure_ascii=False),
                json.dumps(request.constraints, ensure_ascii=False),
                request.timeout_seconds,
                _now_iso(),
            ),
        )
        conn.commit()
    return task_id, request.env_name


def _idempotent_run_id(create_key: str) -> str:
    digest = hashlib.sha256(create_key.encode("utf-8")).hexdigest()[:12]
    return f"run_{digest}"


def _load_created_run(db_path: Path, run_id: str) -> CreatedRun | None:
    with _open_sync(db_path) as conn:
        conn.row_factory = sqlite3.Row
        run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            return None
        attempts = conn.execute(
            "SELECT id, agent_name, model, status FROM attempts "
            "WHERE run_id=? ORDER BY created_at, id",
            (run_id,),
        ).fetchall()
    return CreatedRun(
        run_id=run_id,
        task_id=run["task_id"],
        env_name=run["env_name"],
        agents=list(dict.fromkeys(row["agent_name"] for row in attempts)),
        attempts=[
            {
                "attempt_id": row["id"],
                "agent": row["agent_name"],
                "model": row["model"],
                "status": row["status"],
            }
            for row in attempts
        ],
        execution=run["execution"] or "parallel",
        created=False,
    )


_create_locks: dict[str, asyncio.Lock] = {}


def _resolve_timeout_seconds(
    task_timeout_seconds: int | None,
    request: NormalizedRunRequest,
) -> int | None:
    """Resolve omitted, explicit-value, and explicit-null timeout semantics."""
    if request.timeout_seconds_explicit:
        return request.timeout_seconds
    if task_timeout_seconds is not None:
        return task_timeout_seconds
    return request.timeout_seconds


async def create_run_plan(
    request: NormalizedRunRequest,
    *,
    settings: Any,
    create_key: str | None = None,
    parent_refs: dict[str, Any] | None = None,
    input_override: FrozenRunInput | None = None,
    blade_models_loader: BladeModelsLoader | None = None,
) -> CreatedRun:
    """Validate and persist a Run plus Attempts, returning dispatch jobs.

    ``create_key`` is an interim idempotent hook for coordinators.  RC-F-03
    replaces its deterministic run-id backing with the transactional
    ``idempotency_keys`` table without changing this service contract.
    """
    state = runtime_state.get()
    env = state.envs.get(request.env_name)
    if env is None:
        raise RunServiceError(404, f"env not found: {request.env_name}")
    _raise_if_env_unavailable(env)
    if not request.task_id and not (request.prompt or "").strip():
        raise RunServiceError(400, "task_id 或 prompt 必须提供其一（prompt 不能为空白）")
    if request.timeout_seconds is not None and request.timeout_seconds <= 0:
        raise RunServiceError(400, "timeout_seconds 必须大于 0")

    raw_plan, execution = _validate_and_build_job_plan(request, settings)
    job_plan = await _normalize_job_plan(settings, raw_plan, blade_models_loader)
    run_id = _idempotent_run_id(create_key) if create_key else f"run_{uuid.uuid4().hex[:12]}"
    lock = _create_locks.setdefault(run_id, asyncio.Lock())
    async with lock:
        if create_key:
            request_hash = canonical_hash(request)
            try:
                with _open_sync(state.db_path) as conn:
                    claim = claim_idempotency(
                        conn,
                        operation="legacy_run:create",
                        key=create_key,
                        request_hash=request_hash,
                    )
                    conn.commit()
            except IdempotencyConflict as exc:
                raise RunServiceError(
                    409, "idempotency_conflict", code="idempotency_conflict"
                ) from exc
            except IdempotencyInProgress as exc:
                raise RunServiceError(
                    409, "idempotency_in_progress", code="idempotency_in_progress"
                ) from exc
            if claim.replayed:
                existing = _load_created_run(state.db_path, run_id)
                if existing is None:
                    raise RunServiceError(
                        409,
                        "idempotency result is missing",
                        code="idempotency_result_missing",
                    )
                return existing
            existing = _load_created_run(state.db_path, run_id)
            if existing is not None:
                with _open_sync(state.db_path) as conn:
                    complete_idempotency(
                        conn,
                        operation="legacy_run:create",
                        key=create_key,
                        result_id=run_id,
                        response=existing.response(),
                    )
                    conn.commit()
                return existing

        try:
            task_id, env_name = _get_or_create_task(state.db_path, request)
        except Exception:
            if create_key:
                with _open_sync(state.db_path) as conn:
                    fail_idempotency(
                        conn, operation="legacy_run:create", key=create_key
                    )
                    conn.commit()
            raise
        with _open_sync(state.db_path) as conn:
            row = conn.execute(
                "SELECT prompt, context_json, constraints_json, timeout_seconds "
                "FROM tasks WHERE id=?",
                (task_id,),
            ).fetchone()
        prompt = row[0] if row else (request.prompt or "")
        task_context = json.loads(row[1]) if row and row[1] else {}
        context = {**task_context, **(request.context or {})}
        constraints = (
            json.loads(row[2]) if row and row[2] else (request.constraints or {})
        )
        task_timeout_seconds = row[3] if row else None
        timeout_seconds = _resolve_timeout_seconds(task_timeout_seconds, request)
        if input_override is not None:
            if request.task_id is None:
                raise RunServiceError(
                    400, "input_override requires source task_id lineage"
                )
            prompt = input_override.prompt
            context = copy.deepcopy(input_override.context)
            constraints = copy.deepcopy(input_override.constraints)
            timeout_seconds = input_override.timeout_seconds

        from .conversation.plan import (
            CONVERSATION_CONTEXT_KEY,
            ConversationPlanError,
            parse_conversation,
        )

        if CONVERSATION_CONTEXT_KEY in (context or {}):
            try:
                parse_conversation(context[CONVERSATION_CONTEXT_KEY], task_id=task_id)
            except ConversationPlanError as exc:
                raise RunServiceError(400, f"invalid conversation: {exc}") from exc

        attempts_info: list[dict[str, Any]] = []
        dispatch_jobs: list[dict[str, Any]] = []
        refs = dict(parent_refs or {})
        for agent_name, agent_model in job_plan:
            blade_thinking = (
                request.blade_enable_thinking if agent_name == "blade-agent" else None
            )
            attempt, env_token = await create_attempt(
                task_id=task_id,
                agent_name=agent_name,
                run_id=run_id,
                compare_mode=request.compare_mode,
                model=agent_model,
                input_prompt=prompt,
                input_context=copy.deepcopy(context),
                input_constraints=copy.deepcopy(constraints),
                input_timeout_seconds=timeout_seconds,
                variant_id=(str(refs["variant_id"]) if refs.get("variant_id") else None),
                external_refs=refs,
            )
            dispatch_jobs.append(
                {
                    "settings": settings,
                    "attempt_id": attempt.id,
                    "agent_name": agent_name,
                    "task_id": task_id,
                    "task_prompt": prompt,
                    "task_context": copy.deepcopy(context),
                    "timeout_seconds": timeout_seconds,
                    "env_name": env_name,
                    "env_token": env_token,
                    "model": agent_model,
                    "compare_mode": request.compare_mode,
                    "blade_enable_thinking": blade_thinking,
                    "capture_policy": request.capture_policy,
                }
            )
            attempts_info.append(
                {
                    "attempt_id": attempt.id,
                    "agent": agent_name,
                    "model": agent_model,
                    "status": attempt.status,
                }
            )

        with _open_sync(state.db_path) as conn:
            conn.execute("UPDATE runs SET execution=? WHERE id=?", (execution, run_id))
            conn.commit()

        # 成本核算起点：**必须在任何 agent 进程启动前**建 key 并落
        # usage_start——它是进程重启后能重新结算的唯一锚点（内存里的明文 key
        # 会丢，但读 usage 只需 Management Key + hash，两者重启后都能重新拿到）。
        # 放在这里而非 API 层：experiments / recovery 等入口也走 create_run_plan，
        # 挂在这里才不会漏。成本核算失败绝不阻断 run（begin_run 内部已兜底）。
        await _begin_cost_audit(
            state.db_path, run_id, settings=settings, agents=list(request.agents)
        )

        created_run = CreatedRun(
            run_id=run_id,
            task_id=task_id,
            env_name=env_name,
            agents=list(request.agents),
            attempts=attempts_info,
            execution=execution,
            dispatch_jobs=dispatch_jobs,
        )
        if create_key:
            with _open_sync(state.db_path) as conn:
                complete_idempotency(
                    conn,
                    operation="legacy_run:create",
                    key=create_key,
                    result_id=run_id,
                    response=created_run.response(),
                )
                conn.commit()
        return created_run


async def _begin_cost_audit(
    db_path: Path, run_id: str, *, settings: Any, agents: list[str]
) -> None:
    """建 run 专属 key 并记 usage_start。失败只记日志，绝不阻断 run。

    成本观测是附加能力：宁可这次 run 没有资金口径，也不能因为 OpenRouter
    Management API 抖动就让实验跑不起来。
    """
    # per-attempt 生命周期启用后**不再建旧的 run 级 key**：
    # 否则一次 run 会同时开「旧 run key + N 个 attempt key + judge key」，
    # 多花一把钱；更糟的是旧审计完成时的 clear_credential(run_id) 会清掉
    # 当前正在用的 judge 凭据（两者都以 run_id 为索引）。
    # 旧表只保留给 API 读历史记录。
    if getattr(settings.cost, "legacy_run_key_enabled", False):
        try:
            from .cost.audit import begin_run

            await begin_run(db_path, run_id, settings=settings, agents=agents)
        except Exception:
            logger.exception("run cost audit begin failed run=%s", run_id)


def _attempt_semaphore(capacity: int) -> asyncio.Semaphore:
    state = runtime_state.get()
    semaphore = state.attempt_semaphore
    if semaphore is None:
        state.attempt_semaphore = semaphore = asyncio.Semaphore(capacity)
        state.attempt_semaphore_capacity = capacity
    elif state.attempt_semaphore_capacity != capacity:
        if state.active_attempt_count:
            raise RuntimeError("cannot resize attempt lease while attempts are active")
        state.attempt_semaphore = semaphore = asyncio.Semaphore(capacity)
        state.attempt_semaphore_capacity = capacity
    return semaphore


async def _dispatch_with_lease(job: dict[str, Any]) -> None:
    capacity = int(job["settings"].octagon.max_active_attempts)
    semaphore = _attempt_semaphore(capacity)
    async with semaphore:
        state = runtime_state.get()
        state.active_attempt_count += 1
        state.max_observed_active_attempts = max(
            state.max_observed_active_attempts, state.active_attempt_count
        )
        # attempt 粒度登记：deadline sweeper 靠它把超期 attempt 的协程
        # 真正取消掉。只改 DB 状态是不够的——信号量要等下面的 await 返回才释放，
        # 协程不停就等于闸门位置不放，后续 attempt 一直排队。
        #
        # loop 一并记下：sweeper 在工作线程里跑，cancel 必须回到这个 loop。
        registered_attempt_id = job.get("attempt_id")
        if registered_attempt_id:
            state.loop = asyncio.get_running_loop()
            state.attempt_tasks[registered_attempt_id] = asyncio.current_task()
        try:
            attempt_id = job.get("attempt_id")
            if attempt_id:
                with _open_sync(state.db_path) as conn:
                    row = conn.execute(
                        "SELECT external_refs_json FROM attempts WHERE id=?",
                        (attempt_id,),
                    ).fetchone()
                    refs = json.loads(row[0] or "{}") if row else {}
                    refs["launch_active_attempts"] = state.active_attempt_count
                    refs["launch_lease_capacity"] = capacity
                    conn.execute(
                        "UPDATE attempts SET external_refs_json=? WHERE id=?",
                        (json.dumps(refs, ensure_ascii=False, sort_keys=True), attempt_id),
                    )
                    conn.commit()
            await dispatch_attempt(**job)
        finally:
            state.active_attempt_count -= 1
            if registered_attempt_id:
                state.attempt_tasks.pop(registered_attempt_id, None)
            # 登记表空了就清掉 loop 引用：它是进程级单槽，留着一个已关闭的
            # loop 会让后续 cancel 静默走 `loop.is_closed()` 分支跳过——
            # attempt 取消的修复就此悄悄失效，且只有 WARNING 日志。
            if not state.attempt_tasks:
                state.loop = None


async def _wait_for_legacy_admission() -> None:
    state = runtime_state.get()
    event = state.legacy_admission_event
    if event is None:
        event = state.legacy_admission_event = asyncio.Event()
        event.set()
    await event.wait()


async def _dispatch_all(
    run_id: str, jobs: list[dict[str, Any]], *, legacy: bool = True
) -> None:
    if legacy:
        await _wait_for_legacy_admission()
    state = runtime_state.get()
    tasks = [asyncio.create_task(_dispatch_with_lease(job)) for job in jobs]
    state.active_tasks[run_id] = tasks
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception) and not isinstance(
                result, asyncio.CancelledError
            ):
                logger.exception("dispatch task crashed", exc_info=result)
    finally:
        state.active_tasks.pop(run_id, None)


async def _dispatch_serial(
    run_id: str, jobs: list[dict[str, Any]], *, legacy: bool = True
) -> None:
    if legacy:
        await _wait_for_legacy_admission()
    state = runtime_state.get()
    try:
        for job in jobs:
            task = asyncio.create_task(_dispatch_with_lease(job))
            state.active_tasks[run_id] = [task]
            try:
                await task
            except asyncio.CancelledError:
                logger.info("serial dispatch cancelled at attempt=%s", job.get("attempt_id"))
                break
            except Exception as exc:
                logger.exception(
                    "serial dispatch crashed attempt=%s",
                    job.get("attempt_id"),
                    exc_info=exc,
                )
    finally:
        state.active_tasks.pop(run_id, None)


@asynccontextmanager
async def run_group_admission():
    """Block new legacy dispatches while a formal RunGroup is active.

    The RunGroup coordinator introduced by RC-G uses this context around its
    FIFO execution slot.  Already-running legacy attempts are still governed
    by the shared attempt lease and are allowed to finish.
    """
    state = runtime_state.get()
    event = state.legacy_admission_event
    if event is None:
        event = state.legacy_admission_event = asyncio.Event()
        event.set()
    state.active_run_groups += 1
    event.clear()
    try:
        yield
    finally:
        state.active_run_groups -= 1
        if state.active_run_groups == 0:
            event.set()
