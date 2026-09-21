"""Durable, process-local scoring worker separated from Agent execution leases.

Phase one deliberately keeps a single service process as the worker owner. Jobs are
persisted before scheduling, and startup moves orphaned ``running`` jobs back to
``queued`` so a restart cannot silently lose scoring work.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from . import runtime_state
from .cost.credential import judge_credentials_active
from .db import _iso_after, _now_iso, _open_sync
from .scoring_snapshot import (
    ScoringInputMismatchError,
    create_scoring_snapshot,
    materialize_scoring_input,
    verify_scoring_snapshot,
)

logger = logging.getLogger(__name__)


class ScoringDeadlineExceeded(RuntimeError):
    """单 attempt 评分超过硬上限。

    与 ``scorer_unavailable`` 严格区分：**评分太慢**记 ``timed_out``，
    **评分设施缺失/不可用**才记 ``failed`` + ``scorer_unavailable``。
    周末现场把「judge 太慢」报成了后者，读起来像设施坏了。
    两种情况下 ``score_total`` 都保持 NULL，绝不写业务 0 分。
    """


# 未声明 env 专属版本时使用的兼容默认值。判分方式变化必须升版；
# 具体 env 可通过 scorer.py 的
# `__octagon_scorer_version__` 声明自己的口径版本，避免一个 env 改评分方式却
# 让所有其它 env 的版本一起漂移。
SCORER_VERSION = "batch-judge-v2"


def _scorer_version_for_env(env: Any | None) -> str:
    module = getattr(env, "scorer_module", None)
    declared = getattr(module, "__octagon_scorer_version__", None)
    if isinstance(declared, str) and declared.strip():
        return declared.strip()
    return SCORER_VERSION


def _execution_status(adapter_status: str) -> str:
    if adapter_status == "completed":
        return "completed"
    if adapter_status == "timeout":
        return "timeout"
    return "failed"


def enqueue_scoring_job(
    *,
    attempt_id: str,
    adapter_status: str,
    adapter_error_code: str | None,
    adapter_error_message: str | None,
    stats: dict[str, Any],
    security_meta: dict[str, Any],
    capacity: int,
) -> str:
    """Atomically persist execution output and its scoring job, then schedule it."""
    state = runtime_state.get()
    snapshot = create_scoring_snapshot(
        data_path=state.data_path,
        attempt_id=attempt_id,
    )
    now = _now_iso()
    job_id = f"scj_{uuid.uuid4().hex[:16]}"
    config = {
        "adapter_status": adapter_status,
        "adapter_error_code": adapter_error_code,
        "adapter_error_message": adapter_error_message,
        "security_meta": security_meta,
        "snapshot_ref": snapshot.relative_path,
    }
    refs = dict(stats.get("external_refs") or {})
    if adapter_status != "completed":
        refs.update(
            {
                "original_status": adapter_status,
                "original_error_code": adapter_error_code,
                "original_error_message": adapter_error_message,
            }
        )
    # 产物没回收到的 attempt 照常判分（分数本身是事实），但要标成
    # infrastructure：否则它会以一个「真实的低分」混进横评矩阵，看起来像
    # agent 能力差。2026-09-18 横评里 blade-agent 的 27 个 attempt 就是
    # 这样被整体误读的（需求 6.3）。
    from .adapters.blade_service import artifact_recovery_failed

    recovery_failed = adapter_status == "completed" and artifact_recovery_failed(refs)
    with _open_sync(state.db_path) as conn:
        env_row = conn.execute(
            "SELECT env_name FROM attempts WHERE id=?", (attempt_id,)
        ).fetchone()
        env = state.envs.get(str(env_row[0])) if env_row else None
        scorer_version = _scorer_version_for_env(env)
        conn.execute(
            "UPDATE attempts SET status='scoring',execution_status=?,"
            "execution_ended_at=?,execution_error_code=?,execution_error_message=?,"
            "scoring_status='queued',scoring_queued_at=?,score_total=NULL,"
            "external_refs_json=?,event_count=?,last_event_at=?,thinking_count=?,"
            "tool_call_count=?,token_usage_json=?,cost_estimate=?,duration_ms=?,"
            "transport_status=?,ended_at=NULL,error_code=NULL,error_message=NULL,"
            "failure_kind=? WHERE id=?",
            (
                _execution_status(adapter_status),
                now,
                adapter_error_code,
                adapter_error_message,
                now,
                json.dumps(refs, ensure_ascii=False, sort_keys=True),
                int(stats.get("event_count", 0)),
                stats.get("last_event_at"),
                int(stats.get("thinking_count", 0)),
                int(stats.get("tool_call_count", 0)),
                json.dumps(stats.get("token_usage") or {}, ensure_ascii=False, sort_keys=True),
                stats.get("cost_estimate"),
                int(stats.get("duration_ms", 0)),
                stats.get("transport_status", "unknown"),
                "infrastructure" if recovery_failed else None,
                attempt_id,
            ),
        )
        conn.execute(
            "INSERT INTO scoring_jobs(id,attempt_id,status,scorer_version,"
            "scorer_config_json,input_hash,created_at) VALUES(?,?,'queued',?,?,?,?)",
            (
                job_id,
                attempt_id,
                scorer_version,
                json.dumps(config, ensure_ascii=False, sort_keys=True),
                snapshot.input_hash,
                now,
            ),
        )
        conn.commit()
    schedule_scoring_job(job_id, capacity=capacity)
    return job_id


def _scoring_semaphore(capacity: int) -> asyncio.Semaphore:
    state = runtime_state.get()
    semaphore = state.scoring_semaphore
    if semaphore is None:
        state.scoring_semaphore = semaphore = asyncio.Semaphore(capacity)
        state.scoring_semaphore_capacity = capacity
    elif state.scoring_semaphore_capacity != capacity:
        active = any(
            not task.done()
            for key, tasks in state.active_tasks.items()
            if key.startswith("scoring:")
            for task in tasks
        )
        if active:
            raise RuntimeError("cannot resize scoring lease while jobs are active")
        state.scoring_semaphore = semaphore = asyncio.Semaphore(capacity)
        state.scoring_semaphore_capacity = capacity
    return semaphore


def _claim_job(
    db_path: Path, job_id: str, *, deadline_seconds: int = 0
) -> sqlite3.Row | None:
    now = _now_iso()
    # 评分有自己的 deadline，不消耗 Agent 的耐心预算。绝对时钟
    # 持久化，重启后与常驻 sweeper 都能据此判定。
    deadline = _iso_after(now, deadline_seconds) if deadline_seconds > 0 else None
    with _open_sync(db_path) as conn:
        conn.row_factory = sqlite3.Row
        changed = conn.execute(
            "UPDATE scoring_jobs SET status='running',started_at=?,heartbeat_at=?,"
            "deadline_at=?,attempt_count=attempt_count+1 WHERE id=? AND status='queued' "
            "AND cancel_requested_at IS NULL",
            (now, now, deadline, job_id),
        ).rowcount
        if not changed:
            conn.commit()
            return None
        conn.execute(
            "UPDATE attempts SET scoring_status='running',scoring_started_at=?,"
            "scoring_deadline_at=?,"
            "status='scoring' WHERE id=(SELECT attempt_id FROM scoring_jobs WHERE id=?)",
            (now, deadline, job_id),
        )
        row = conn.execute("SELECT * FROM scoring_jobs WHERE id=?", (job_id,)).fetchone()
        conn.commit()
        return row


def _invoke_scorer_cancel_hook(state: Any, attempt_id: str) -> None:
    """请求 env 的 scorer 停止后台 judge 工作。

    judge 跑在工作线程/子进程里，asyncio 的取消够不到它——必须显式通知，
    否则「停止」之后仍会有新的 judge 进程被拉起（2026-07-28 实测过）。
    """
    try:
        with _open_sync(state.db_path) as conn:
            row = conn.execute(
                "SELECT env_name FROM attempts WHERE id=?", (attempt_id,)
            ).fetchone()
        if row is None:
            return
        env = state.envs.get(str(row[0]))
        hook = getattr(getattr(env, "scorer_module", None), "cancel_scoring", None)
        if callable(hook):
            hook(attempt_id)
    except Exception:
        logger.exception("scorer cancellation hook failed attempt=%s", attempt_id)


def _job_cancelled(db_path: Path, job_id: str) -> bool:
    with _open_sync(db_path) as conn:
        row = conn.execute(
            "SELECT cancel_requested_at,status FROM scoring_jobs WHERE id=?", (job_id,)
        ).fetchone()
    return row is None or row[0] is not None or row[1] == "cancelled"


#: execution 终态 → 顶层 status 的投影。
#: 执行成功的（completed）保留 completed；执行本身就失败/超时的沿用原状态；
#: 拿不到执行结论时才回落 scoring_failed。
_EXEC_TO_STATUS = {
    "completed": "completed",
    "timeout": "timeout",
    "failed": "chat_failed",
}


def _execution_projection(db_path: Path, job_id: str) -> str:
    """评分失败时，顶层 status 应反映**执行**的结论而非评分的。

    评分结果由 `scoring_status` / `scoring_error_code` 单独承载，
    不需要也不应该覆写 `status`——否则「agent 没跑起来」和
    「agent 跑完了但 judge 挂了」在列表上长得一模一样。
    """
    try:
        with _open_sync(db_path) as conn:
            row = conn.execute(
                "SELECT a.execution_status FROM attempts a "
                "JOIN scoring_jobs j ON j.attempt_id=a.id WHERE j.id=?",
                (job_id,),
            ).fetchone()
        if row and row[0]:
            return _EXEC_TO_STATUS.get(row[0], "scoring_failed")
    except Exception:  # pragma: no cover - 投影失败不阻塞评分收尾
        logger.exception("execution projection failed job=%s", job_id)
    return "scoring_failed"


def _finish_failure(
    db_path: Path,
    job_id: str,
    *,
    status: str,
    code: str,
    message: str,
) -> None:
    now = _now_iso()
    # legacy `attempts.status` 投影。取消必须投影成 `cancelled` 而非
    # 借用任何失败态：下游聚合先按 status 分类再看 error code
    # （aggregate.py 的 INFRASTRUCTURE_STATUSES / scoring_failures），
    # 投影成 timeout 会把一次用户操作算成「Agent 没做完」，
    # 投影成 scoring_failed 会算成「判分设施坏了」——两者都是错误归因。
    # **执行成功的 attempt 不因评分失败被标成失败**：agent 干完了活、
    # 产出可评，只是 judge 没跑成——两件事。原来无条件写 scoring_failed，
    # 导致实验列表显示「失败」，看起来像 agent 挂了（2026-07-29 实测：
    # codex/opencode 明明 execution_status=completed 却被标失败）。
    # scoring_status 列已经独立记录评分结果，顶层 status 应保留执行结论。
    legacy = "cancelled" if status == "cancelled" else _execution_projection(
        db_path, job_id
    )
    # failure_kind 是 aggregate 的第一判据（先于 status 匹配），必须一并写：
    # 取消不是任何一类故障，用独立值让它不落进 agent/scoring/infrastructure
    # 任何一个失败桶。
    failure_kind = "cancelled" if status == "cancelled" else "scoring"
    with _open_sync(db_path) as conn:
        row = conn.execute(
            "SELECT attempt_id FROM scoring_jobs WHERE id=?", (job_id,)
        ).fetchone()
        if row is None:
            return
        attempt_id = row[0]
        conn.execute(
            "UPDATE scoring_jobs SET status=?,ended_at=?,error_code=?,error_message=? "
            "WHERE id=?",
            (status, now, code, message, job_id),
        )
        conn.execute(
            "UPDATE attempts SET status=?,scoring_status=?,scoring_ended_at=?,"
            "scoring_error_code=?,scoring_error_message=?,score_total=NULL,"
            "error_code=?,error_message=?,failure_kind=?,ended_at=? WHERE id=?",
            (
                legacy, status, now, code, message, code, message,
                failure_kind, now, attempt_id,
            ),
        )
        conn.commit()


async def _execute_job(
    job_id: str, *, capacity: int, deadline_seconds: int | None = None
) -> None:
    state = runtime_state.get()
    if deadline_seconds is None:
        settings = getattr(state, "settings", None)
        deadline_seconds = int(
            getattr(getattr(settings, "octagon", None), "scoring_deadline_seconds", 0)
            or 0
        )
    async with _scoring_semaphore(capacity):
        job = await asyncio.to_thread(
            _claim_job, state.db_path, job_id, deadline_seconds=deadline_seconds
        )
        if job is None:
            return
        attempt_id = str(job["attempt_id"])
        # 硬上限从**获得评分槽**起算：排队不消耗评分预算，与
        # 「排队不消耗耐心」同理。
        scoring_started = time.monotonic()

        def _remaining() -> float | None:
            if not deadline_seconds or deadline_seconds <= 0:
                return None
            return max(0.0, deadline_seconds - (time.monotonic() - scoring_started))
        try:
            if _job_cancelled(state.db_path, job_id):
                _finish_failure(
                    state.db_path,
                    job_id,
                    status="cancelled",
                    code="scoring_cancelled",
                    message="用户手动停止评分",
                )
                return
            with _open_sync(state.db_path) as conn:
                conn.row_factory = sqlite3.Row
                attempt = conn.execute(
                    "SELECT * FROM attempts WHERE id=?", (attempt_id,)
                ).fetchone()
                task = conn.execute(
                    "SELECT * FROM tasks WHERE id=(SELECT task_id FROM attempts WHERE id=?)",
                    (attempt_id,),
                ).fetchone()
            if attempt is None or task is None:
                raise RuntimeError("attempt or task missing")
            env = state.envs.get(attempt["env_name"])
            if env is None:
                raise RuntimeError(f"env not loaded: {attempt['env_name']}")
            from .input_snapshots import resolve_attempt_input
            from .run_dispatch import _resolve_scorer, _refresh_run_status
            from .runner import _row_to_task_dict, _write_security_columns_sync
            from .evaluator import ScorerUnavailableError, evaluate
            scorer = _resolve_scorer(env)
            if scorer is None:
                raise ScorerUnavailableError(f"env {attempt['env_name']} missing scorer")
            frozen_input = resolve_attempt_input(
                data_path=state.data_path,
                db_path=state.db_path,
                attempt_id=attempt_id,
            )
            config = json.loads(job["scorer_config_json"] or "{}")
            snapshot_ref = str(config.get("snapshot_ref") or "")
            input_hash = str(job["input_hash"] or "")
            if not snapshot_ref or not input_hash:
                raise ScoringInputMismatchError("scoring job has no frozen input")
            scoring_data_path = await asyncio.to_thread(
                materialize_scoring_input,
                data_path=state.data_path,
                snapshot_ref=snapshot_ref,
                attempt_id=attempt_id,
                expected_hash=input_hash,
                job_id=job_id,
            )
            # judge 成本：本 run 的评分共用一把 judge key，与 agent
            # 执行隔离——ppt-visual-repair 那类多模态 judge 可能与执行同量级，
            # 混进 agent 的账会污染性价比横向比较。
            #
            # **这里是真实评分入口**：scoring queue 直接调 evaluate()，
            # 不经过 runner.py，所以凭据必须在这条路径上注入。
            judge_run_id = _run_id_of_attempt(state.db_path, attempt_id)
            await _open_judge_key(state, judge_run_id)
            with judge_credentials_active(judge_run_id) as _judge_used_run_key:
                if not _judge_used_run_key:
                    # judge 走了自己的配置（Blade judge、非 OpenRouter provider
                    # 或凭据缺失）——那笔花费不在 run key 上，账不可审计。
                    _mark_judge_unaudited(state.db_path, judge_run_id)
                # 硬上限：到点即放弃等待并记 timed_out。evaluate 跑在
                # 工作线程里、无法被真正打断，但**不能因此让评分无限期占着
                # 槽位并把 attempt 永远挂在 scoring 上**——那正是周末现场的
                # 现象。这里先释放调度侧，再调 scorer 的取消钩子请求它收手。
                _evaluate = asyncio.to_thread(
                    evaluate,
                    attempt_id=attempt_id,
                    task=_row_to_task_dict(dict(task), frozen_input=frozen_input),
                    env=env,
                    data_path=scoring_data_path,
                    scorer=scorer,
                    security_meta=config.get("security_meta") or {},
                )
                budget = _remaining()
                if budget is None:
                    outcome = await _evaluate
                else:
                    try:
                        outcome = await asyncio.wait_for(_evaluate, timeout=budget)
                    except (asyncio.TimeoutError, TimeoutError) as exc:
                        raise ScoringDeadlineExceeded(
                            f"评分超过 {deadline_seconds}s 硬上限"
                        ) from exc
            if _job_cancelled(state.db_path, job_id):
                _finish_failure(
                    state.db_path,
                    job_id,
                    status="cancelled",
                    code="scoring_cancelled",
                    message="用户手动停止评分",
                )
                return
            # Revalidate the immutable source immediately before the score commit.
            # A mismatch invalidates the result even if the scorer itself succeeded.
            await asyncio.to_thread(
                verify_scoring_snapshot,
                data_path=state.data_path,
                snapshot_ref=snapshot_ref,
                attempt_id=attempt_id,
                expected_hash=input_hash,
            )
            await asyncio.to_thread(
                _write_security_columns_sync,
                state.db_path,
                attempt_id,
                config.get("security_meta") or {},
                outcome.security,
            )
            from .experiments.scoring import commit_scoring_result

            final_status = "completed" if outcome.passed else "gave_up"
            await asyncio.to_thread(
                commit_scoring_result,
                db_path=state.db_path,
                data_path=state.data_path,
                attempt_id=attempt_id,
                scores=outcome.scores,
                manifest=outcome.evaluation_manifest,
                status=final_status,
                score_total=outcome.score_total,
                ended_at=_now_iso(),
                external_refs={
                    "scoring_input_hash": input_hash,
                    "scoring_snapshot_ref": snapshot_ref,
                },
                event_count=int(attempt["event_count"]),
                last_event_at=attempt["last_event_at"],
                thinking_count=int(attempt["thinking_count"]),
                tool_call_count=int(attempt["tool_call_count"]),
                token_usage=json.loads(attempt["token_usage_json"] or "{}"),
                cost_estimate=attempt["cost_estimate"],
                duration_ms=int(attempt["duration_ms"]),
                transport_status=attempt["transport_status"],
                model=attempt["model"],
            )
            # Collect derived Product Judge evidence only after the immutable
            # snapshot was revalidated and the official score transaction committed.
            try:
                from .rubric_evolution.collector import record_evaluation_outcome_sync

                await asyncio.to_thread(
                    record_evaluation_outcome_sync,
                    db_path=state.db_path,
                    attempt_id=attempt_id,
                    scores=outcome.scores,
                    evaluation_manifest=outcome.evaluation_manifest,
                    metadata={"source": "scoring_queue", "scoring_job_id": job_id},
                )
            except Exception:
                logger.exception(
                    "rubric judge record collection failed after score commit attempt=%s",
                    attempt_id,
                )
            now = _now_iso()
            with _open_sync(state.db_path) as conn:
                conn.execute(
                    "UPDATE scoring_jobs SET status='completed',ended_at=?,heartbeat_at=? "
                    "WHERE id=?",
                    (now, now, job_id),
                )
                conn.execute(
                    "UPDATE attempts SET scoring_status='completed',scoring_ended_at=?,"
                    "scoring_error_code=NULL,scoring_error_message=NULL WHERE id=?",
                    (now, attempt_id),
                )
                conn.commit()
            try:
                from .experiments.leader import consume_score_outbox

                await asyncio.to_thread(consume_score_outbox, state.db_path)
            except Exception:
                logger.exception("leader outbox notification failed attempt=%s", attempt_id)
            _refresh_run_status(state.db_path, attempt_id)
        except asyncio.CancelledError:
            if _job_cancelled(state.db_path, job_id):
                _finish_failure(
                    state.db_path,
                    job_id,
                    status="cancelled",
                    code="scoring_cancelled",
                    message="用户手动停止评分",
                )
            else:
                # Shutdown recovery will requeue this durable job on next startup.
                with _open_sync(state.db_path) as conn:
                    conn.execute(
                        "UPDATE scoring_jobs SET status='queued',started_at=NULL "
                        "WHERE id=? AND status='running'",
                        (job_id,),
                    )
                    conn.execute(
                        "UPDATE attempts SET scoring_status='queued' WHERE id=? "
                        "AND scoring_status='running'",
                        (attempt_id,),
                    )
                    conn.commit()
            raise
        except ScoringInputMismatchError as exc:
            logger.error(
                "scoring input mismatch job=%s attempt=%s: %s",
                job_id,
                attempt_id,
                exc,
            )
            _finish_failure(
                state.db_path,
                job_id,
                status="failed",
                code="scoring_input_mismatch",
                message=str(exc),
            )
            from .run_dispatch import _refresh_run_status

            _refresh_run_status(state.db_path, attempt_id)
        except ScoringDeadlineExceeded as exc:
            # 评分太慢 ≠ 评分设施坏了：记 timed_out，不是
            # failed+scorer_unavailable。执行状态与已回收产物完全不受影响，
            # score_total 保持 NULL——不得填业务 0 分。
            logger.error(
                "scoring deadline exceeded job=%s attempt=%s: %s",
                job_id, attempt_id, exc,
            )
            _finish_failure(
                state.db_path,
                job_id,
                status="timed_out",
                code="scoring_deadline_exceeded",
                message=str(exc),
            )
            # 请求 scorer 收手，避免 judge 子进程在后台继续跑。
            _invoke_scorer_cancel_hook(state, attempt_id)
            from .run_dispatch import _refresh_run_status

            _refresh_run_status(state.db_path, attempt_id)
        except Exception as exc:
            logger.exception("scoring job failed job=%s attempt=%s", job_id, attempt_id)
            _finish_failure(
                state.db_path,
                job_id,
                status="failed",
                code=(
                    "scorer_unavailable"
                    if exc.__class__.__name__ == "ScorerUnavailableError"
                    else "scorer_exception"
                ),
                message=str(exc),
            )
            from .run_dispatch import _refresh_run_status

            _refresh_run_status(state.db_path, attempt_id)


def schedule_scoring_job(job_id: str, *, capacity: int) -> bool:
    state = runtime_state.get()
    key = f"scoring:{job_id}"
    if any(not task.done() for task in state.active_tasks.get(key, [])):
        return False

    async def run() -> None:
        try:
            await _execute_job(job_id, capacity=capacity)
        finally:
            state.active_tasks.pop(key, None)

    state.active_tasks[key] = [
        asyncio.create_task(run(), name=f"scoring-job:{job_id}")
    ]
    return True


def schedule_startup_scoring_recovery(*, capacity: int) -> int:
    state = runtime_state.get()
    with _open_sync(state.db_path) as conn:
        conn.execute(
            "UPDATE scoring_jobs SET status='queued',started_at=NULL "
            "WHERE status='running' AND cancel_requested_at IS NULL"
        )
        conn.execute(
            "UPDATE attempts SET scoring_status='queued',status='scoring' "
            "WHERE scoring_status='running'"
        )
        rows = conn.execute(
            "SELECT id FROM scoring_jobs WHERE status='queued' "
            "AND cancel_requested_at IS NULL ORDER BY created_at,id"
        ).fetchall()
        conn.commit()
    for (job_id,) in rows:
        schedule_scoring_job(str(job_id), capacity=capacity)
    return len(rows)


def cancel_scoring_for_runs(run_ids: list[str]) -> int:
    if not run_ids:
        return 0
    state = runtime_state.get()
    now = _now_iso()
    placeholders = ",".join("?" for _ in run_ids)
    with _open_sync(state.db_path) as conn:
        rows = conn.execute(
            f"SELECT j.id,j.attempt_id,a.env_name FROM scoring_jobs j JOIN attempts a "
            f"ON a.id=j.attempt_id WHERE a.run_id IN ({placeholders}) "
            "AND j.status IN ('queued','running')",
            tuple(run_ids),
        ).fetchall()
        job_ids = [str(row[0]) for row in rows]
        if job_ids:
            marks = ",".join("?" for _ in job_ids)
            conn.execute(
                f"UPDATE scoring_jobs SET cancel_requested_at=?,status=CASE "
                f"WHEN status='queued' THEN 'cancelled' ELSE status END,"
                f"ended_at=CASE WHEN status='queued' THEN ? ELSE ended_at END "
                f"WHERE id IN ({marks})",
                (now, now, *job_ids),
            )
            # 只收敛**评分轴**：执行已经结束的 attempt 不能因为评分被取消就把
            # 顶层 status 改写成 timeout（那会把「跑完了、判分被停」误报成
            # 「Agent 没做完」）。legacy status 与 execution 轴交给
            # convergence.converge_attempts 统一处理。
            # score_total=NULL：未知分数保持空，绝不写业务 0 分。
            conn.execute(
                f"UPDATE attempts SET scoring_status='cancelled',scoring_ended_at=?,"
                f"scoring_error_code='scoring_cancelled',"
                f"scoring_error_message='用户手动停止评分',score_total=NULL,"
                f"status=CASE WHEN status='scoring' THEN 'cancelled' ELSE status END "
                f"WHERE id IN (SELECT attempt_id FROM scoring_jobs WHERE id IN ({marks}))",
                (now, *job_ids),
            )
        conn.commit()
    for _job_id, attempt_id, env_name in rows:
        env = state.envs.get(str(env_name))
        module = getattr(env, "scorer_module", None)
        cancel_hook = getattr(module, "cancel_scoring", None)
        if callable(cancel_hook):
            try:
                cancel_hook(str(attempt_id))
            except Exception:
                logger.exception("scorer cancellation hook failed attempt=%s", attempt_id)
    for job_id in job_ids:
        for task in state.active_tasks.get(f"scoring:{job_id}", []):
            if not task.done():
                task.cancel()
    return len(job_ids)


def _run_id_of_attempt(db_path, attempt_id: str) -> str | None:
    """attempt → run_id。judge key 按 run 归属（一个 run 一把）。"""
    try:
        with _open_sync(db_path) as conn:
            row = conn.execute(
                "SELECT run_id FROM attempts WHERE id=?", (attempt_id,)
            ).fetchone()
        return row[0] if row else None
    except Exception:  # pragma: no cover - 成本核算不得拖垮评分
        return None


async def _open_judge_key(state, run_id: str | None) -> None:
    """为本 run 开 judge key（幂等——同 run 的多个 attempt 共用一把）。

    失败只记日志：成本观测不该阻断评分。
    """
    if not run_id:
        return
    settings = getattr(state, "settings", None) or getattr(
        runtime_state.get(), "settings", None
    )
    if settings is None or not getattr(settings, "cost", None):
        return
    if not settings.cost.per_attempt_enabled:
        return
    try:
        from .cost.keys import open_key

        await open_key(
            state.db_path, run_id=run_id, scope="judge", scope_id=run_id,
            settings=settings,
        )
    except Exception:  # pragma: no cover
        logger.exception("open judge key failed run=%s", run_id)


def _mark_judge_unaudited(db_path, run_id: str | None) -> None:
    """judge 未走 run key 时把 judge 账降级为不可审计。

    Blade judge、非 OpenRouter provider 的 judge 都会走到这里——它们的
    花费完全不在 run key 上，账若仍标 final 会让 run 总额少一大块却
    显示"已审计"。
    """
    if not run_id:
        return
    try:
        from .cost import ledger as L

        row = L.get_by_scope(db_path, "judge", run_id)
        if row is None or row.attribution_mode != "ephemeral_run_key":
            return
        # judge 走了自己的配置——这笔评分费用完全不在 run key 上。
        # 标 partial 而非 upper_bound：我们从没采过那把 key 的前后差
        L.update_fields(
            db_path, row.id,
            attribution_mode="partial_unattributed",
            error_code="judge_credential_fallback",
        )
        logger.info("judge ledger degraded run=%s（未走 run key）", run_id)
    except Exception:  # pragma: no cover - 成本核算不得拖垮评分
        logger.exception("mark judge unaudited failed run=%s", run_id)
