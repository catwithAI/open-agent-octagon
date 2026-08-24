"""FIFO RunGroup coordinator with bounded cell provisioning."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from backend import runtime_state
from backend.db import _now_iso, _open_sync
from backend.run_service import (
    FrozenRunInput,
    NormalizedRunRequest,
    RunServiceError,
    _dispatch_all,
    _dispatch_serial,
    create_run_plan,
    run_group_admission,
)

from .models import ExperimentProtocol
from .repository import ExperimentRepository

logger = logging.getLogger(__name__)


class CoordinatorError(RuntimeError):
    pass


def _safe_payload(data_path: Path, ref: str) -> str:
    root = Path(data_path).resolve()
    path = (root / ref).resolve()
    if not path.is_relative_to(root):
        raise CoordinatorError("variant prompt ref escapes data path")
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CoordinatorError(f"variant prompt unavailable: {ref}") from exc


def _run_request(protocol: ExperimentProtocol, env_name: str, task_id: str) -> NormalizedRunRequest:
    if protocol.compare_mode == "multi-model":
        agents = [protocol.agents[0].agent]
        models: dict[str, str] | list[str] = [
            item.model or "" for item in protocol.agents
        ]
    else:
        agents = [item.agent for item in protocol.agents]
        models = {
            item.agent: item.model
            for item in protocol.agents
            if item.model is not None
        }
    return NormalizedRunRequest(
        env_name=env_name,
        task_id=task_id,
        agents=agents,
        compare_mode=protocol.compare_mode,
        models=models,
        execution=protocol.execution,
        timeout_seconds=protocol.timeout_seconds,
        capture_policy=protocol.capture_policy,
    )


def _terminal_from_run(db_path: Path, run_id: str) -> tuple[str, str | None]:
    with _open_sync(db_path) as conn:
        row = conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        failed = conn.execute(
            "SELECT status,error_code,failure_kind FROM attempts WHERE run_id=? "
            "AND status!='completed' ORDER BY created_at,id LIMIT 1",
            (run_id,),
        ).fetchone()
    status = row[0] if row else "failed"
    if status == "completed":
        return "completed", None
    if status == "cancelled":
        return "cancelled", "cancelled"
    failure_values = (failed[1], failed[2], failed[0]) if failed else ()
    error_code = next((value for value in failure_values if value), "run_failed")
    return "failed", str(error_code)


async def _stagger_launch(settings: Any) -> None:
    state = runtime_state.get()
    stagger_seconds = settings.octagon.research_launch_stagger_ms / 1000
    if stagger_seconds <= 0:
        return
    if state.run_group_launch_lock is None:
        state.run_group_launch_lock = asyncio.Lock()
    async with state.run_group_launch_lock:
        loop = asyncio.get_running_loop()
        delay = state.next_group_launch_at - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        state.next_group_launch_at = loop.time() + stagger_seconds


async def _provision_cell(
    *,
    settings: Any,
    experiment: dict[str, Any],
    group_id: str,
    cell: dict[str, Any],
    variant: dict[str, Any],
    protocol: ExperimentProtocol,
) -> None:
    state = runtime_state.get()
    repository = ExperimentRepository(state.db_path, state.data_path)
    if not repository.transition_cell(
        cell["id"], expected={"queued"}, target="provisioning"
    ):
        return
    try:
        await _stagger_launch(settings)
        with _open_sync(state.db_path) as conn:
            source = conn.execute(
                "SELECT context_json,constraints_json,timeout_seconds FROM tasks WHERE id=?",
                (experiment["source_task_id"],),
            ).fetchone()
        if source is None:
            raise CoordinatorError("source task missing")
        if not variant.get("prompt_ref"):
            raise CoordinatorError("variant execution payload missing")
        context = json.loads(source[0] or "{}")
        context.update(json.loads(variant["context_delta_json"] or "{}"))
        context["_octagon_experiment"] = {
            "experiment_id": experiment["id"],
            "run_group_id": group_id,
            "variant_id": variant["id"],
            "repeat_index": cell["repeat_index"],
        }
        # 跟随 immutable input snapshot 持久化，保证进程恢复后仍能保持实验协议
        # 的披露语义；prompt_context 会过滤下划线键，不会把内部配置本身泄露给 agent。
        context["_octagon_notify_model_of_timeout"] = (
            protocol.notify_model_of_timeout
        )
        frozen_input = FrozenRunInput(
            prompt=_safe_payload(state.data_path, variant["prompt_ref"]),
            context=context,
            constraints=json.loads(source[1] or "{}"),
            timeout_seconds=protocol.timeout_seconds or source[2],
        )
        created = await create_run_plan(
            _run_request(protocol, experiment["env_name"], experiment["source_task_id"]),
            settings=settings,
            create_key=f"cell:{cell['id']}",
            parent_refs={
                "experiment_id": experiment["id"],
                "run_group_id": group_id,
                "cell_id": cell["id"],
                "variant_id": variant["id"],
                "repeat_index": cell["repeat_index"],
            },
            input_override=frozen_input,
        )
        repository.transition_cell(
            cell["id"],
            expected={"provisioning"},
            target="running",
            run_id=created.run_id,
        )
        if created.dispatch_jobs:
            dispatch = _dispatch_serial if created.execution == "serial" else _dispatch_all
            await dispatch(created.run_id, created.dispatch_jobs, legacy=False)
        target, error_code = _terminal_from_run(state.db_path, created.run_id)
        repository.transition_cell(
            cell["id"],
            expected={"running"},
            target=target,
            error_code=error_code,
        )
    except asyncio.CancelledError:
        repository.transition_cell(
            cell["id"],
            expected={"provisioning", "running"},
            target="cancelled",
        )
        raise
    except Exception as exc:
        if isinstance(exc, RunServiceError):
            error_code = exc.code or "run_provisioning_failed"
        elif isinstance(exc, CoordinatorError):
            error_code = "variant_input_missing"
        else:
            error_code = type(exc).__name__
        repository.transition_cell(
            cell["id"],
            expected={"provisioning", "running"},
            target="failed",
            error_code=error_code,
        )


async def run_group(settings: Any, group_id: str) -> None:
    """Run one group under the process-wide FIFO slot."""
    state = runtime_state.get()
    if state.run_group_lock is None:
        state.run_group_lock = asyncio.Lock()
    async with state.run_group_lock:
        async with run_group_admission():
            repository = ExperimentRepository(state.db_path, state.data_path)
            with _open_sync(state.db_path) as conn:
                conn.row_factory = sqlite3.Row
                group = conn.execute(
                    "SELECT * FROM run_groups WHERE id=?", (group_id,)
                ).fetchone()
                if group is None:
                    raise CoordinatorError(f"run group not found: {group_id}")
                experiment = conn.execute(
                    "SELECT * FROM experiments WHERE id=?", (group["experiment_id"],)
                ).fetchone()
                cells = conn.execute(
                    "SELECT * FROM run_group_cells WHERE run_group_id=? "
                    "ORDER BY created_at,id",
                    (group_id,),
                ).fetchall()
                variants = {
                    row["id"]: dict(row)
                    for row in conn.execute(
                        "SELECT * FROM task_variants WHERE experiment_id=?",
                        (group["experiment_id"],),
                    ).fetchall()
                }
            if experiment is None:
                raise CoordinatorError("experiment missing")
            protocol = ExperimentProtocol.model_validate_json(experiment["protocol_json"])
            concurrency = min(
                protocol.max_concurrency or settings.octagon.max_active_attempts,
                settings.octagon.max_active_attempts,
            )
            cell_workers = (
                1
                if protocol.execution == "serial"
                else max(1, min(len(cells), concurrency // len(protocol.agents)))
            )
            runtime_snapshot = {
                "effective_concurrency": concurrency,
                "cell_workers": cell_workers,
                "launch_stagger_ms": settings.octagon.research_launch_stagger_ms,
                "active_attempts_at_start": state.active_attempt_count,
                "started_at": _now_iso(),
            }
            with _open_sync(state.db_path) as conn:
                conn.execute(
                    "UPDATE run_groups SET stop_policy_json=? WHERE id=?",
                    (json.dumps({"runtime_snapshot": runtime_snapshot}), group_id),
                )
                conn.commit()
            if group["status"] == "queued":
                if not repository.transition_group(
                    group_id, expected={"queued"}, target="running"
                ):
                    return
            elif group["status"] != "running":
                return

            queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
            for cell in cells:
                await queue.put(dict(cell))
            for _ in range(cell_workers):
                await queue.put(None)

            async def worker() -> None:
                while (cell := await queue.get()) is not None:
                    await _provision_cell(
                        settings=settings,
                        experiment=dict(experiment),
                        group_id=group_id,
                        cell=cell,
                        variant=variants[cell["variant_id"]],
                        protocol=protocol,
                    )

            workers = [asyncio.create_task(worker()) for _ in range(cell_workers)]
            await asyncio.gather(*workers)
            from .recovery import rebuild_group_projection

            rebuild_group_projection(state.db_path, group_id)


def schedule_run_group(settings: Any, group_id: str) -> bool:
    """Schedule a committed group once in this process.

    The task is registered in the shared runtime registry so shutdown can cancel
    it and a second create/replay path cannot enqueue the same group again.
    Startup recovery remains the durable fallback if the process exits after the
    database commit but before this task finishes.
    """
    state = runtime_state.get()
    task_key = f"group:{group_id}"
    active = [task for task in state.active_tasks.get(task_key, []) if not task.done()]
    if active:
        state.active_tasks[task_key] = active
        return False

    async def execute() -> None:
        try:
            await run_group(settings, group_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("run group execution failed: %s", group_id)
        finally:
            current = asyncio.current_task()
            remaining = [
                task
                for task in state.active_tasks.get(task_key, [])
                if task is not current and not task.done()
            ]
            if remaining:
                state.active_tasks[task_key] = remaining
            else:
                state.active_tasks.pop(task_key, None)

    task = asyncio.create_task(execute(), name=f"research-run-group:{group_id}")
    state.active_tasks[task_key] = [task]
    return True


def stop_group(group_id: str, *, experiment_id: str | None = None) -> dict[str, int]:
    """Stop queued/running cells using the legacy Run cancellation semantics."""
    state = runtime_state.get()
    now = _now_iso()
    with _open_sync(state.db_path) as conn:
        params: tuple[Any, ...] = (group_id,)
        predicate = "id=?"
        if experiment_id is not None:
            predicate += " AND experiment_id=?"
            params = (group_id, experiment_id)
        group = conn.execute(
            f"SELECT status FROM run_groups WHERE {predicate}", params
        ).fetchone()
        if group is None:
            raise CoordinatorError(f"run group not found: {group_id}")
        active = conn.execute(
            "SELECT id,run_id FROM run_group_cells WHERE run_group_id=? "
            "AND status IN ('queued','provisioning','running','scoring')",
            (group_id,),
        ).fetchall()
        run_ids = [row[1] for row in active if row[1]]
        from backend.convergence import cancel_dispatch_tasks
        from backend.scoring_queue import cancel_scoring_for_runs

        cancelled_scoring = cancel_scoring_for_runs(run_ids)
        cancelled_tasks = cancel_dispatch_tasks(run_ids)
        # attempt 级收敛在事务外统一做（见下方 cancel_attempts_for_runs）：
        # 它要写三条状态轴并重建投影，与本事务里的 cell/group 写入分开，
        # 避免嵌套连接。此前这里只写 legacy status='timeout'，与 cell 的
        # 'cancelled' 自相矛盾，正是这里要修的现象。
        conn.execute(
            "UPDATE run_group_cells SET status='cancelled',error_code='user_stopped',"
            "updated_at=? WHERE run_group_id=? "
            "AND status IN ('queued','provisioning','running','scoring')",
            (now, group_id),
        )
        for cell_id, run_id in active:
            ExperimentRepository.append_group_event(
                conn,
                group_id,
                "cell.stopped",
                {
                    "cell_id": cell_id,
                    "run_id": run_id,
                    "status": "cancelled",
                    "error_code": "user_stopped",
                },
                now=now,
            )
        conn.execute(
            "UPDATE run_groups SET status='cancelled',ended_at=? WHERE id=? "
            "AND status IN ('queued','running')",
            (now, group_id),
        )
        if group[0] in {"queued", "running"}:
            ExperimentRepository.append_group_event(
                conn,
                group_id,
                "group.stopped",
                {"status": "cancelled"},
                now=now,
            )
        conn.commit()
    # attempt 事实是权威数据：三条状态轴一次收敛到 cancelled，并从 attempt
    # 重建 Run→Cell→RunGroup→Experiment 全部投影。cell 的 cancelled
    # 是 sticky 的，重投影不会把它改回去。
    from backend.convergence import (
        cancel_attempts_for_runs,
        kill_orphaned_agent_processes,
    )

    # 跨重启存活、已无内存协程的 Agent：与 /runs/{id}/stop 同一处理，
    # 否则停掉的 group 里仍有进程在烧 token。必须在收敛之前——收敛后
    # attempt 不再是 open 状态，就找不到该杀谁了。
    killed_orphans = kill_orphaned_agent_processes(state.db_path, run_ids)
    converged = cancel_attempts_for_runs(state.db_path, run_ids)
    from .recovery import rebuild_group_projection

    rebuild_group_projection(state.db_path, group_id)
    return {
        "cells": len(active),
        "dispatch_tasks": cancelled_tasks,
        "scoring_jobs": cancelled_scoring,
        "killed_orphan_processes": killed_orphans,
        "converged_attempts": converged,
    }
