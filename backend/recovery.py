"""进程启动时收敛遗留的 queued/running attempts。"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import runtime_state
from .adapters.base import AdapterRunInput
from .process.identity import agent_process_is_alive
from .adapters.blade_service import (
    BladeServiceAdapter,
    _RUNNING_SESSION_STATUSES,
    _TERMINAL_SESSION_STATUSES,
    _is_not_found_error,
    _write_json,
)
from .config import Settings
from .conversation.plan import (
    ConversationPlanError,
    conversation_turns_from_context,
)
from .conversation.resume import decide_resume
from .db import _open_sync
from .iteration.policy import parse_iterative_review_policy
from .run_dispatch import (
    _build_blade_adapter_env,
    _refresh_run_status,
    _resolve_scorer,
    build_blade_sdk_adapter,
)
from .runner import _finalize_no_score, run_attempt

logger = logging.getLogger(__name__)

_PENDING_CLEANUP_RETRY_DELAY_SECONDS = 30


def _mark_pending_cleanup_complete(
    checkpoint_path: Path, refs: dict[str, Any]
) -> None:
    refs["cleanup_pending"] = False
    refs["cleanup_recovered_at"] = datetime.now(timezone.utc).isoformat()
    refs["cleanup_completed_due_to_absence"] = True
    _write_json(checkpoint_path, refs)


def _pending_cleanup_checkpoints(data_path: Path) -> list[Path]:
    pending: list[Path] = []
    attempts_dir = data_path / "attempts"
    if not attempts_dir.is_dir():
        return pending
    for checkpoint in attempts_dir.glob("*/recovery.json"):
        try:
            refs = json.loads(checkpoint.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(refs, dict) and refs.get("cleanup_pending"):
            if refs.get("blade_session_id"):
                pending.append(checkpoint)
    return pending


async def _retry_pending_blade_cleanup(
    settings: Settings, checkpoint_path: Path
) -> None:
    while True:
        for attempt in range(1, 4):
            try:
                refs = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                if not isinstance(refs, dict):
                    return
                # 清理既有 blade session 要用 SDK client，CLI 无此通道。
                adapter = build_blade_sdk_adapter(settings)
                async with adapter._new_client() as client:
                    if settings.blade.keep_blade_session:
                        cleaned = await adapter._cleanup(
                            client, str(refs["blade_session_id"])
                        )
                        if cleaned:
                            refs["cleanup_pending"] = False
                            refs["cleanup_skipped_due_to_preference"] = True
                            refs["cleanup_recovered_at"] = datetime.now(
                                timezone.utc
                            ).isoformat()
                            _write_json(checkpoint_path, refs)
                        return
                    session = await adapter._retry_recovery_read(
                        str(refs["blade_session_id"]),
                        "get_session",
                        lambda: client.get_session(str(refs["blade_session_id"])),
                        remaining_seconds=None,
                    )
                    session_status = str(session.raw.get("status") or "")
                    if session_status in _RUNNING_SESSION_STATUSES:
                        await client.stop(str(refs["blade_session_id"]))
                        raise RuntimeError(
                            "Blade session is still stopping; cleanup deferred"
                        )
                    if session_status not in _TERMINAL_SESSION_STATUSES:
                        raise RuntimeError(
                            "Blade session is not in a deletable state: "
                            f"{session_status or 'unknown'}"
                        )
                    cleaned = await adapter._cleanup(
                        client, str(refs["blade_session_id"])
                    )
                if cleaned:
                    refs["cleanup_pending"] = False
                    refs["cleanup_recovered_at"] = datetime.now(
                        timezone.utc
                    ).isoformat()
                    _write_json(checkpoint_path, refs)
                    return
            except FileNotFoundError:
                if checkpoint_path.is_file():
                    try:
                        refs = json.loads(
                            checkpoint_path.read_text(encoding="utf-8")
                        )
                    except (OSError, json.JSONDecodeError):
                        return
                    if isinstance(refs, dict):
                        _mark_pending_cleanup_complete(checkpoint_path, refs)
                return
            except Exception as exc:
                if _is_not_found_error(exc):
                    try:
                        refs = json.loads(
                            checkpoint_path.read_text(encoding="utf-8")
                        )
                    except (OSError, json.JSONDecodeError):
                        return
                    if isinstance(refs, dict):
                        _mark_pending_cleanup_complete(checkpoint_path, refs)
                    return
                logger.exception(
                    "pending Blade cleanup failed attempt=%d checkpoint=%s",
                    attempt,
                    checkpoint_path,
                )
            if attempt < 3:
                await asyncio.sleep(
                    _PENDING_CLEANUP_RETRY_DELAY_SECONDS * attempt
                )
        logger.warning(
            "pending Blade cleanup exhausted a retry batch; continuing in-process "
            "retry checkpoint=%s",
            checkpoint_path,
        )
        await asyncio.sleep(_PENDING_CLEANUP_RETRY_DELAY_SECONDS)


def schedule_pending_blade_cleanup(settings: Settings) -> int:
    state = runtime_state.get()
    scheduled = 0
    active_attempts: set[str] = set()
    with _open_sync(state.db_path) as conn:
        rows = conn.execute(
            "SELECT id FROM attempts WHERE status IN "
            "('queued', 'running', 'starting_blade_session')"
        ).fetchall()
        active_attempts = {str(row[0]) for row in rows}
    for checkpoint in _pending_cleanup_checkpoints(state.data_path):
        attempt_id = checkpoint.parent.name
        if attempt_id in active_attempts:
            logger.info(
                "defer pending Blade cleanup while attempt recovery is active: %s",
                attempt_id,
            )
            continue
        key = f"blade-cleanup:{attempt_id}"
        active = [task for task in state.active_tasks.get(key, []) if not task.done()]
        if active:
            state.active_tasks[key] = active
            continue
        task = asyncio.create_task(
            _retry_pending_blade_cleanup(settings, checkpoint),
            name=key,
        )
        state.active_tasks[key] = [task]
        scheduled += 1
    return scheduled


def _stale_attempts(db_path: Path, data_path: Path | None = None) -> list[dict[str, Any]]:
    with _open_sync(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT a.* FROM attempts a "
            "WHERE a.status IN ('queued', 'running', 'starting_blade_session') "
            "ORDER BY a.created_at"
        ).fetchall()
    result = [dict(row) for row in rows]
    if data_path is None:
        return result
    from .input_snapshots import InputSnapshotError, resolve_attempt_input

    for row in result:
        try:
            frozen = resolve_attempt_input(
                data_path=data_path, db_path=db_path, attempt_id=row["id"]
            )
        except InputSnapshotError as exc:
            row["_input_error"] = str(exc)
            continue
        row["prompt"] = frozen.prompt
        row["context_json"] = json.dumps(frozen.context, ensure_ascii=False)
        row["constraints_json"] = json.dumps(
            frozen.constraints, ensure_ascii=False
        )
        row["timeout_seconds"] = frozen.timeout_seconds
        row["input_provenance"] = frozen.input_provenance
    return result


def _recovery_refs(data_path: Path, row: dict[str, Any]) -> dict[str, Any]:
    try:
        refs = json.loads(row.get("external_refs_json") or "{}")
    except json.JSONDecodeError:
        refs = {}
    checkpoint = data_path / "attempts" / row["id"] / "recovery.json"
    if checkpoint.is_file():
        try:
            stored = json.loads(checkpoint.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                refs = {**stored, **refs}
        except (json.JSONDecodeError, OSError):
            logger.warning("invalid recovery checkpoint: %s", checkpoint)
    return refs


def _mark_interrupted(row: dict[str, Any], *, code: str, message: str) -> None:
    state = runtime_state.get()
    _finalize_no_score(
        db_path=state.db_path,
        attempt_id=row["id"],
        status="interrupted",
        error_code=code,
        error_message=message,
        pass_threshold=60,
        transport_status=(
            "disconnected" if row["agent_name"] == "blade-agent" else "not_applicable"
        ),
    )
    _refresh_run_status(state.db_path, row["id"])


class _RecoveryAdapter:
    def __init__(
        self,
        adapter: BladeServiceAdapter,
        task: AdapterRunInput,
        env: Any | None,
        session_id: str,
        data_path: Path,
    ) -> None:
        self.attempt_id = task.attempt_id
        self._adapter = adapter
        self._task = task
        self._env = env
        self._session_id = session_id
        self._data_path = data_path

    async def run(self):
        if self._task.iteration_turn_handler is not None:
            if self._env is None:
                raise RuntimeError("迭代恢复缺少 AdapterEnv")
            return await self._adapter.recover_iterative_existing(
                task=self._task,
                env=self._env,
                session_id=self._session_id,
                data_path=self._data_path,
            )
        return await self._adapter.recover_existing(
            task=self._task,
            session_id=self._session_id,
            data_path=self._data_path,
        )


class _MultiTurnResumeAdapter:
    """多轮 attempt 的续跑：复用原 session，跳过已完成的轮次。

    与 `_RecoveryAdapter`（单轮"重新附着只补采、不发消息"）不同：多轮 attempt
    崩溃时可能还有未发送的轮次，必须继续把它们发完，否则 conversation 永远
    停在半途、probe 轮拿不到结果。
    """

    def __init__(
        self,
        adapter: BladeServiceAdapter,
        task: AdapterRunInput,
        env: Any,
        session_id: str,
        data_path: Path,
        resume_after_turn_index: int | None,
    ) -> None:
        self.attempt_id = task.attempt_id
        self._adapter = adapter
        self._task = task
        self._env = env
        self._session_id = session_id
        self._data_path = data_path
        self._resume_after = resume_after_turn_index

    async def run(self):
        return await self._adapter.run(
            self._task,
            self._env,
            self._data_path,
            resume_session_id=self._session_id,
            # None（一轮都没完成）用 -1 表达"不跳过任何轮"，与
            # resume_session_id 的成对约束保持一致。
            resume_after_turn_index=(
                self._resume_after if self._resume_after is not None else -1
            ),
        )


def _remaining_active_timeout(
    row: dict[str, Any], *, now: datetime | None = None
) -> int:
    """恢复 Agent 进程内 deadline，只计算 active execution 时间。

    evaluator active 时以持久化的暂停起点为参照，Judge/Reviewer 时间和后端
    重启耗时均不扣 Agent 预算。老库没有 canonical deadline 时才回退到历史的
    started_at 墙钟算法。
    """
    now = now or datetime.now(timezone.utc)
    agent_deadline = row.get("execution_agent_deadline_at")
    if isinstance(agent_deadline, str) and agent_deadline:
        reference = now
        evaluator_started = row.get("execution_evaluator_started_at")
        if isinstance(evaluator_started, str) and evaluator_started:
            try:
                reference = datetime.fromisoformat(
                    evaluator_started.replace("Z", "+00:00")
                )
            except ValueError:
                logger.warning(
                    "invalid evaluator started_at during recovery: %s",
                    evaluator_started,
                )
        try:
            deadline = datetime.fromisoformat(agent_deadline.replace("Z", "+00:00"))
            return max(1, int((deadline - reference).total_seconds()))
        except ValueError:
            logger.warning(
                "invalid execution_agent_deadline_at during recovery: %s",
                agent_deadline,
            )

    total_timeout = int(row.get("timeout_seconds") or 1000)
    started_at = row.get("started_at")
    if isinstance(started_at, str) and started_at:
        try:
            parsed = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            elapsed = max(0, (now - parsed).total_seconds())
            total_timeout = max(1, int(total_timeout - elapsed))
        except ValueError:
            logger.warning("invalid attempt started_at during recovery: %s", started_at)
    return total_timeout


async def _recover_blade_attempt(
    settings: Settings,
    row: dict[str, Any],
    refs: dict[str, Any],
) -> None:
    state = runtime_state.get()
    env = state.envs.get(row["env_name"])
    if env is None:
        _mark_interrupted(
            row,
            code="recovery_env_not_loaded",
            message=f"env not loaded during restart recovery: {row['env_name']}",
        )
        return

    # 恢复既有 blade session 只有 SDK 通道能做（resume_session_id /
    # recover_existing 是 Socket.IO 独有能力），与新 attempt 的 transport
    # 配置无关——即便新 attempt 默认走 blade-cli，这里也必须是 SDK adapter。
    adapter = build_blade_sdk_adapter(settings, model=row.get("model"))
    if refs.get("blade_base_url"):
        adapter.config.base_url = str(refs["blade_base_url"])

    context = json.loads(row.get("context_json") or "{}")
    total_timeout = _remaining_active_timeout(row)

    task_context = context if isinstance(context, dict) else {}
    # 多轮 attempt 的 conversation 定义随 task_context 持久化，恢复时重新解析：
    # 解析失败按不可恢复处理，不静默降级成单轮（会丢掉未发送的轮次）。
    try:
        conversation_turns = conversation_turns_from_context(
            task_id=row["task_id"], task_context=task_context,
        )
    except ConversationPlanError as exc:
        _mark_interrupted(
            row,
            code="recovery_conversation_invalid",
            message=f"conversation 定义在恢复时无法解析: {exc}",
        )
        return

    iteration_handler = None
    iteration_policy = parse_iterative_review_policy(env.meta)
    if iteration_policy is not None:
        submission_scorer = getattr(env.scorer_module, "evaluate_submission", None)
        if not callable(submission_scorer):
            _mark_interrupted(
                row,
                code="recovery_submission_scorer_missing",
                message=f"迭代环境缺少 evaluate_submission(): {row['env_name']}",
            )
            return
        from .iteration.controller import IterativeAttemptController

        iteration_handler = IterativeAttemptController(
            attempt_id=row["id"],
            attempt_dir=state.data_path / "attempts" / row["id"],
            task={
                "id": row["task_id"],
                "env_name": row["env_name"],
                "prompt": row.get("prompt") or "",
                "context": task_context,
                "timeout_seconds": total_timeout,
            },
            env=env,
            policy=iteration_policy,
            scorer=submission_scorer,
            judge_deadline_seconds=settings.octagon.scoring_deadline_seconds,
            execution_db_path=state.db_path,
        )

    task = AdapterRunInput(
        attempt_id=row["id"],
        task_id=row["task_id"],
        task_prompt=row.get("prompt") or "",
        task_context=task_context,
        timeout_seconds=total_timeout,
        env_name=row["env_name"],
        env_skill_id=env.skill_id,
        # 旧明文 token 不可恢复，也不应持久化；已有 BA workspace 中仍保留原
        # attempt.json，重新附着只补采，不会重新上传这个空值。
        env_token="",
        env_base_url=settings.octagon.public_base_url,
        notify_model_of_timeout=bool(
            task_context.get("_octagon_notify_model_of_timeout", True)
        ),
        conversation_turns=conversation_turns,
        iteration_turn_handler=iteration_handler,
    )
    scorer = _resolve_scorer(env)
    if scorer is None:
        _mark_interrupted(
            row,
            code="recovery_scorer_missing",
            message=f"env scorer missing during restart recovery: {row['env_name']}",
        )
        return

    session_id = str(refs["blade_session_id"])
    bound: Any
    if iteration_handler is not None:
        adapter_env = _build_blade_adapter_env(env)
        bound = _RecoveryAdapter(
            adapter, task, adapter_env, session_id, state.data_path
        )
    elif conversation_turns:
        decision = decide_resume(checkpoint=refs, turns=conversation_turns)
        if not decision.can_resume:
            # plan 变更 / checkpoint 损坏等：拒绝恢复而不是从头重放——
            # 已完成轮的外部副作用会被重复执行。
            _mark_interrupted(
                row,
                code=f"recovery_rejected_{decision.reason}",
                message=(
                    "多轮 attempt 无法安全恢复"
                    f"（{decision.reason}），不重放已完成轮次"
                ),
            )
            return
        if decision.active_turn_unknown:
            # 崩溃时有轮在飞，是否已在服务端产生副作用无从判断。只允许
            # 在 producer 能证明该轮未执行时重发；当前没有这个证明通道，
            # 保守标不可恢复而不是赌一把重发。
            _mark_interrupted(
                row,
                code="recovery_active_turn_unknown",
                message=(
                    "崩溃时有轮次正在执行，无法确认其是否已完成；"
                    "不重发以免重复副作用"
                ),
            )
            return
        adapter_env = _build_blade_adapter_env(env)
        logger.info(
            "attempt=%s 多轮恢复：从 turn_index>%s 续跑",
            row["id"], decision.resume_after_turn_index,
        )
        bound = _MultiTurnResumeAdapter(
            adapter, task, adapter_env, session_id, state.data_path,
            decision.resume_after_turn_index,
        )
    else:
        bound = _RecoveryAdapter(
            adapter, task, None, session_id, state.data_path
        )
    await run_attempt(adapter=bound, scorer=scorer)
    _refresh_run_status(state.db_path, row["id"])


def schedule_startup_recovery(settings: Settings) -> int:
    """安排恢复任务并立即收敛不可恢复项，返回发现的 stale attempt 数。"""
    state = runtime_state.get()
    rows = _stale_attempts(state.db_path, state.data_path)
    for row in rows:
        if row.get("_input_error"):
            _finalize_no_score(
                db_path=state.db_path,
                attempt_id=row["id"],
                status="input_snapshot_missing",
                error_code="input_snapshot_missing",
                error_message=str(row["_input_error"]),
                pass_threshold=60,
            )
            _refresh_run_status(state.db_path, row["id"])
            continue
        if row["agent_name"] != "blade-agent":
            # Agent 执行生命周期与 API 服务生命周期隔离。此前无条件把所有
            # 本地 attempt 标成丢失——即使 CLI 其实还在跑，正在进行的测试、
            # 工具调用和模型工作也一起丢掉。现在先查进程活性再决定。
            if agent_process_is_alive(state.data_path, row["id"]):
                # worker 仍存活：保持 running，让它自己跑完。**不重放任务**——
                # 重放可能产生副作用（写文件、调外部工具），代价高于放弃。
                # 真的卡住了，由常驻 deadline sweeper 按绝对 deadline 收敛。
                logger.warning(
                    "recovery: attempt=%s 的 %s 进程仍存活，保持 running 不重放",
                    row["id"], row["agent_name"],
                )
                continue
            _mark_interrupted(
                row,
                code="local_process_lost_on_restart",
                message=(
                    f"{row['agent_name']} local process cannot be reattached after "
                    "Octagon backend restart"
                ),
            )
            continue

        refs = _recovery_refs(state.data_path, row)
        if not refs.get("blade_session_id"):
            _mark_interrupted(
                row,
                code="blade_session_missing_on_restart",
                message="Blade attempt has no persisted session checkpoint",
            )
            continue

        run_id = str(row["run_id"])

        async def recover_one(
            recovery_row: dict[str, Any] = row,
            recovery_refs: dict[str, Any] = refs,
            recovery_run_id: str = run_id,
        ) -> None:
            try:
                await _recover_blade_attempt(settings, recovery_row, recovery_refs)
            except Exception as exc:
                logger.exception("startup recovery crashed attempt=%s", recovery_row["id"])
                _mark_interrupted(
                    recovery_row,
                    code="startup_recovery_crashed",
                    message=str(exc),
                )
            finally:
                schedule_pending_blade_cleanup(settings)
                # 本恢复任务是异步的：启动时的 wire manifest 扫描会跳过当时仍
                # running 的 attempt；这里在 attempt 到达终态后补收敛，否则
                # 该 attempt 的 wire manifest 会一直 in-progress 到下次重启。
                try:
                    from .wire.recovery import recover_wire_manifests

                    recover_wire_manifests(
                        state.data_path, state.db_path,
                        attempt_id=recovery_row["id"],
                    )
                except Exception:
                    logger.exception(
                        "wire recovery after attempt recovery failed attempt=%s",
                        recovery_row["id"],
                    )
                current = state.active_tasks.get(recovery_run_id, [])
                task = asyncio.current_task()
                remaining = [item for item in current if item is not task]
                if remaining:
                    state.active_tasks[recovery_run_id] = remaining
                else:
                    state.active_tasks.pop(recovery_run_id, None)

        task = asyncio.create_task(recover_one())
        state.active_tasks.setdefault(run_id, []).append(task)

    if rows:
        logger.warning("startup recovery found %d stale attempt(s)", len(rows))
    return len(rows)
