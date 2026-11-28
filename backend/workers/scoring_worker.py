"""Standalone scoring worker daemon and isolated single-job subprocess runner.

Entry points:
- ``python -m backend.workers.scoring_worker``: long-running lease-based daemon
  (or ``--once`` to drain currently queued jobs and exit).
- ``python -m backend.workers.scoring_worker --run-job <job_id>``: isolated
  single-job execution in its own process group.
- ``python -m backend.workers.scoring_worker --render-preview <renderer> <src> <out>``:
  delegates to :func:`backend.artifact_worker.main`.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import signal
import socket
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from backend import runtime_state
from backend.config import Settings, load_settings
from backend.cost.credential import (
    API_KEY_ENV,
    BASE_URL_ENV,
    RunCredential,
    judge_credential_env,
    set_credential,
)
from backend.db import _open_sync, resolve_db_path
from backend.env_loader import EnvLoader
from backend.scoring_queue import (
    _claim_job,
    _default_lease_seconds,
    _finish_failure,
    _job_cancelled,
    _open_judge_key,
    _run_id_of_attempt,
    claim_next_scoring_job,
    execute_claimed_job,
    release_scoring_lease,
    renew_scoring_lease,
)

logger = logging.getLogger(__name__)

# Sensitive administrative or unrelated credentials that must never be
# inherited by an isolated scoring child process.
_STRIPPED_ENV_KEYS: frozenset[str] = frozenset(
    {
        "OPENROUTER_MANAGEMENT_KEY",
        "SAME_MODEL_SSH_PASSWORD",
        "SAME_MODEL_BLADE_API_KEY",
        "INSIGHTS_API_KEY",
        API_KEY_ENV,
        BASE_URL_ENV,
    }
)


def _build_scoring_subprocess_env(
    db_path: Path,
    attempt_id: str,
    *,
    settings: Any = None,
) -> dict[str, str]:
    """Construct a credential-scrubbed environment for a single scoring job."""
    env = dict(os.environ)
    strip_keys = set(_STRIPPED_ENV_KEYS)
    if settings is not None:
        cost_cfg = getattr(settings, "cost", None)
        if cost_cfg is not None and getattr(cost_cfg, "management_key_env", None):
            strip_keys.add(str(cost_cfg.management_key_env))
        insights_cfg = getattr(settings, "insights", None)
        if insights_cfg is not None and getattr(insights_cfg, "api_key_env", None):
            strip_keys.add(str(insights_cfg.api_key_env))
    for key in strip_keys:
        env.pop(key, None)

    run_id = _run_id_of_attempt(db_path, attempt_id)
    if run_id:
        env.update(judge_credential_env(run_id))

    env["PYTHONUNBUFFERED"] = "1"
    return env


def _fetch_job_row(db_path: Path, job_id: str) -> sqlite3.Row | None:
    with _open_sync(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM scoring_jobs WHERE id=?", (job_id,)
        ).fetchone()


def _job_status(db_path: Path, job_id: str) -> str | None:
    with _open_sync(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM scoring_jobs WHERE id=?", (job_id,)
        ).fetchone()
    return str(row[0]) if row and row[0] is not None else None


async def _terminate_process_group(
    pid: int,
    grace_seconds: float = 3.0,
    *,
    proc: asyncio.subprocess.Process | None = None,
) -> None:
    """Terminate an entire process group: SIGTERM -> grace -> SIGKILL."""
    if pid <= 0:
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        if proc is not None and proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except (asyncio.TimeoutError, TimeoutError):
                pass
        return

    if proc is not None:
        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=max(0.0, grace_seconds))
            except (asyncio.TimeoutError, TimeoutError):
                pass
    else:
        deadline = time.monotonic() + max(0.0, grace_seconds)
        while time.monotonic() < deadline:
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                return
            except (PermissionError, OSError):
                break
            await asyncio.sleep(0.05)

    # Always follow up with SIGKILL on the process group so stubborn children
    # or grandchildren cannot survive in the background.
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass

    if proc is not None and proc.returncode is None:
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except (asyncio.TimeoutError, TimeoutError):
            pass


async def _drain_stream_tail(
    stream: asyncio.StreamReader | None, *, max_bytes: int = 4096
) -> str:
    if stream is None:
        return ""
    buf = bytearray()
    try:
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > max_bytes * 2:
                del buf[: len(buf) - max_bytes]
    except Exception:
        pass
    tail = bytes(buf[-max_bytes:])
    return tail.decode("utf-8", errors="replace").strip()


def _safe_refresh_run_status(db_path: Path, attempt_id: str) -> None:
    try:
        from backend.run_dispatch import _refresh_run_status

        _refresh_run_status(db_path, attempt_id)
    except Exception:
        logger.exception("refresh run status failed attempt=%s", attempt_id)


async def run_job_in_subprocess(
    job_id: str,
    *,
    db_path: Path | None = None,
    data_path: Path | None = None,
    envs_path: Path | None = None,
    config_path: Path | None = None,
    lease_owner: str | None = None,
    lease_seconds: int | None = None,
    deadline_seconds: int | None = None,
    already_claimed: bool = False,
    grace_seconds: float = 3.0,
) -> int:
    """Execute one scoring job in an isolated child process group.

    Enforces lease renewal, cancellation detection, hard OS-level process-group
    deadline termination, and crash recovery if the child exits before finalizing DB.
    """
    state: Any = None
    try:
        state = runtime_state.get()
    except RuntimeError:
        state = None

    settings = getattr(state, "settings", None)
    resolved_data_path = (
        Path(data_path)
        if data_path is not None
        else (
            Path(state.data_path)
            if state is not None
            else (Path(db_path).parent if db_path is not None else Path("./data"))
        )
    )
    resolved_db_path = (
        Path(db_path)
        if db_path is not None
        else (
            Path(state.db_path)
            if state is not None
            else resolve_db_path(resolved_data_path)
        )
    )
    resolved_envs_path = (
        Path(envs_path)
        if envs_path is not None
        else getattr(getattr(settings, "octagon", None), "envs_path", None)
    )
    effective_lease_seconds = max(
        1,
        int(lease_seconds)
        if lease_seconds is not None
        else _default_lease_seconds(),
    )
    if deadline_seconds is None:
        deadline_seconds = int(
            getattr(getattr(settings, "octagon", None), "scoring_deadline_seconds", 0)
            or 0
        )
    effective_deadline = max(0, int(deadline_seconds))
    owner = lease_owner or f"scoring-worker:{os.getpid()}:{uuid.uuid4().hex[:8]}"

    if already_claimed:
        job = await asyncio.to_thread(_fetch_job_row, resolved_db_path, job_id)
        if job is None or str(job["status"]) != "running":
            return 0
        if lease_owner and str(job["lease_owner"] or "") != owner:
            return 0
    else:
        job = await asyncio.to_thread(
            _claim_job,
            resolved_db_path,
            job_id,
            deadline_seconds=effective_deadline,
            lease_owner=owner,
            lease_seconds=effective_lease_seconds,
        )
        if job is None:
            return 0

    attempt_id = str(job["attempt_id"])
    run_id = await asyncio.to_thread(_run_id_of_attempt, resolved_db_path, attempt_id)

    if await asyncio.to_thread(_job_cancelled, resolved_db_path, job_id):
        await asyncio.to_thread(
            _finish_failure,
            resolved_db_path,
            job_id,
            status="cancelled",
            code="scoring_cancelled",
            message="用户手动停止评分",
        )
        await asyncio.to_thread(_safe_refresh_run_status, resolved_db_path, attempt_id)
        return 0

    if state is not None and run_id:
        await _open_judge_key(state, run_id)

    cmd = [
        sys.executable,
        "-m",
        "backend.workers.scoring_worker",
        "--run-job",
        job_id,
        "--already-claimed",
        "--lease-owner",
        owner,
        "--lease-seconds",
        str(effective_lease_seconds),
        "--deadline-seconds",
        str(effective_deadline),
        "--data-path",
        str(resolved_data_path),
    ]
    if resolved_db_path.resolve() != resolve_db_path(resolved_data_path).resolve():
        cmd.extend(["--db-path", str(resolved_db_path)])
    if resolved_envs_path is not None:
        cmd.extend(["--envs-path", str(resolved_envs_path)])
    if config_path is not None:
        cmd.extend(["--config", str(config_path)])

    env = _build_scoring_subprocess_env(
        resolved_db_path, attempt_id, settings=settings
    )
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )

    stdout_task = asyncio.create_task(_drain_stream_tail(proc.stdout))
    stderr_task = asyncio.create_task(_drain_stream_tail(proc.stderr))
    wait_task = asyncio.create_task(proc.wait())

    started_mono = time.monotonic()
    last_renew_mono = started_mono
    renew_interval = max(1.0, min(float(effective_lease_seconds) / 3.0, 5.0))
    poll_interval = 0.2

    try:
        while not wait_task.done():
            now_mono = time.monotonic()
            if effective_deadline > 0 and (now_mono - started_mono) >= effective_deadline:
                logger.error(
                    "scoring subprocess exceeded deadline=%ss job=%s pid=%s",
                    effective_deadline,
                    job_id,
                    proc.pid,
                )
                await _terminate_process_group(
                    proc.pid, grace_seconds=grace_seconds, proc=proc
                )
                if await asyncio.to_thread(_job_status, resolved_db_path, job_id) == "running":
                    await asyncio.to_thread(
                        _finish_failure,
                        resolved_db_path,
                        job_id,
                        status="timed_out",
                        code="scoring_deadline_exceeded",
                        message=f"评分超过 {effective_deadline}s 硬上限",
                    )
                    await asyncio.to_thread(
                        _safe_refresh_run_status, resolved_db_path, attempt_id
                    )
                if run_id:
                    runtime_state.notify_run_update(run_id)
                return 124

            if await asyncio.to_thread(_job_cancelled, resolved_db_path, job_id):
                logger.info(
                    "scoring job cancelled, killing subprocess group job=%s pid=%s",
                    job_id,
                    proc.pid,
                )
                await _terminate_process_group(
                    proc.pid, grace_seconds=grace_seconds, proc=proc
                )
                if await asyncio.to_thread(_job_status, resolved_db_path, job_id) == "running":
                    await asyncio.to_thread(
                        _finish_failure,
                        resolved_db_path,
                        job_id,
                        status="cancelled",
                        code="scoring_cancelled",
                        message="用户手动停止评分",
                    )
                    await asyncio.to_thread(
                        _safe_refresh_run_status, resolved_db_path, attempt_id
                    )
                if run_id:
                    runtime_state.notify_run_update(run_id)
                return -1

            if (now_mono - last_renew_mono) >= renew_interval:
                renewed = await asyncio.to_thread(
                    renew_scoring_lease,
                    resolved_db_path,
                    job_id,
                    lease_owner=owner,
                    lease_seconds=effective_lease_seconds,
                )
                last_renew_mono = now_mono
                if not renewed:
                    if await asyncio.to_thread(_job_cancelled, resolved_db_path, job_id):
                        await _terminate_process_group(
                            proc.pid, grace_seconds=grace_seconds, proc=proc
                        )
                        if (
                            await asyncio.to_thread(
                                _job_status, resolved_db_path, job_id
                            )
                            == "running"
                        ):
                            await asyncio.to_thread(
                                _finish_failure,
                                resolved_db_path,
                                job_id,
                                status="cancelled",
                                code="scoring_cancelled",
                                message="用户手动停止评分",
                            )
                            await asyncio.to_thread(
                                _safe_refresh_run_status, resolved_db_path, attempt_id
                            )
                        if run_id:
                            runtime_state.notify_run_update(run_id)
                        return -1
                    cur_status = await asyncio.to_thread(
                        _job_status, resolved_db_path, job_id
                    )
                    if cur_status == "running":
                        # Lease lost to another worker; abort our child to avoid split-brain.
                        logger.warning(
                            "scoring lease lost job=%s owner=%s, killing subprocess pid=%s",
                            job_id,
                            owner,
                            proc.pid,
                        )
                        await _terminate_process_group(
                            proc.pid, grace_seconds=grace_seconds, proc=proc
                        )
                        return -1

            sleep_for = poll_interval
            if effective_deadline > 0:
                remaining = effective_deadline - (time.monotonic() - started_mono)
                sleep_for = max(0.01, min(sleep_for, remaining))
            await asyncio.wait({wait_task}, timeout=sleep_for)

        returncode = wait_task.result()
        stderr_tail = await stderr_task
        await stdout_task

        cur_status = await asyncio.to_thread(_job_status, resolved_db_path, job_id)
        if cur_status == "running":
            if await asyncio.to_thread(_job_cancelled, resolved_db_path, job_id):
                await asyncio.to_thread(
                    _finish_failure,
                    resolved_db_path,
                    job_id,
                    status="cancelled",
                    code="scoring_cancelled",
                    message="用户手动停止评分",
                )
            else:
                detail = f"scoring worker exited unexpectedly (code={returncode})"
                if stderr_tail:
                    detail = f"{detail}: {stderr_tail[-300:]}"
                logger.error(
                    "scoring subprocess crashed before finalizing job=%s code=%s",
                    job_id,
                    returncode,
                )
                await asyncio.to_thread(
                    _finish_failure,
                    resolved_db_path,
                    job_id,
                    status="failed",
                    code="scoring_worker_crashed",
                    message=detail[:500],
                )
            await asyncio.to_thread(
                _safe_refresh_run_status, resolved_db_path, attempt_id
            )
        if run_id:
            runtime_state.notify_run_update(run_id)
        return returncode
    except asyncio.CancelledError:
        await _terminate_process_group(
            proc.pid, grace_seconds=grace_seconds, proc=proc
        )
        if await asyncio.to_thread(_job_cancelled, resolved_db_path, job_id):
            if await asyncio.to_thread(_job_status, resolved_db_path, job_id) == "running":
                await asyncio.to_thread(
                    _finish_failure,
                    resolved_db_path,
                    job_id,
                    status="cancelled",
                    code="scoring_cancelled",
                    message="用户手动停止评分",
                )
                await asyncio.to_thread(
                    _safe_refresh_run_status, resolved_db_path, attempt_id
                )
        else:
            await asyncio.to_thread(
                release_scoring_lease,
                resolved_db_path,
                job_id,
                lease_owner=owner,
            )
        if run_id:
            runtime_state.notify_run_update(run_id)
        raise
    finally:
        for task in (wait_task, stdout_task, stderr_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(wait_task, stdout_task, stderr_task, return_exceptions=True)


def _release_all_owned_leases(db_path: Path, lease_owner: str) -> int:
    with _open_sync(db_path, immediate=True) as conn:
        rows = conn.execute(
            "SELECT id, attempt_id FROM scoring_jobs "
            "WHERE lease_owner=? AND status='running' AND cancel_requested_at IS NULL",
            (lease_owner,),
        ).fetchall()
        if not rows:
            conn.commit()
            return 0
        job_ids = [str(r[0]) for r in rows]
        attempt_ids = [str(r[1]) for r in rows if r[1]]
        job_marks = ",".join("?" for _ in job_ids)
        conn.execute(
            f"UPDATE scoring_jobs SET status='queued',started_at=NULL,"
            f"lease_owner=NULL,lease_expires_at=NULL WHERE id IN ({job_marks})",
            tuple(job_ids),
        )
        if attempt_ids:
            att_marks = ",".join("?" for _ in attempt_ids)
            conn.execute(
                f"UPDATE attempts SET scoring_status='queued' "
                f"WHERE id IN ({att_marks}) AND scoring_status='running'",
                tuple(attempt_ids),
            )
        conn.commit()
    return len(job_ids)


class ScoringWorker:
    """Standalone lease-based scoring daemon that isolates each job in a child process group."""

    def __init__(
        self,
        *,
        db_path: Path,
        data_path: Path,
        envs_path: Path,
        config_path: Path | None = None,
        concurrency: int = 2,
        lease_seconds: int = 60,
        deadline_seconds: int = 300,
        poll_interval: float = 1.0,
        lease_owner: str | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.data_path = Path(data_path)
        self.envs_path = Path(envs_path)
        self.config_path = Path(config_path) if config_path is not None else None
        self.concurrency = max(1, int(concurrency))
        self.lease_seconds = max(1, int(lease_seconds))
        self.deadline_seconds = max(0, int(deadline_seconds))
        self.poll_interval = max(0.05, float(poll_interval))
        self.lease_owner = lease_owner or (
            f"scoring-worker:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        )
        self.stop_event = asyncio.Event()
        self._active_tasks: dict[str, asyncio.Task[int]] = {}

    def request_stop(self) -> None:
        self.stop_event.set()

    def install_signal_handlers(self, loop: asyncio.AbstractEventLoop) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.request_stop)
            except (NotImplementedError, RuntimeError):
                try:
                    signal.signal(sig, lambda *_args: loop.call_soon_threadsafe(self.request_stop))
                except ValueError:
                    pass

    def _reap_finished(self) -> None:
        for job_id, task in list(self._active_tasks.items()):
            if task.done():
                self._active_tasks.pop(job_id, None)
                if not task.cancelled():
                    exc = task.exception()
                    if exc is not None:
                        logger.exception(
                            "scoring worker task failed job=%s", job_id, exc_info=exc
                        )

    async def run(self, *, once: bool = False) -> int:
        processed = 0
        try:
            while True:
                self._reap_finished()
                while (
                    not self.stop_event.is_set()
                    and len(self._active_tasks) < self.concurrency
                ):
                    job = await asyncio.to_thread(
                        claim_next_scoring_job,
                        self.db_path,
                        lease_owner=self.lease_owner,
                        lease_seconds=self.lease_seconds,
                        deadline_seconds=self.deadline_seconds,
                    )
                    if job is None:
                        break
                    job_id = str(job["id"])
                    task = asyncio.create_task(
                        run_job_in_subprocess(
                            job_id,
                            db_path=self.db_path,
                            data_path=self.data_path,
                            envs_path=self.envs_path,
                            config_path=self.config_path,
                            lease_owner=self.lease_owner,
                            lease_seconds=self.lease_seconds,
                            deadline_seconds=self.deadline_seconds,
                            already_claimed=True,
                        ),
                        name=f"scoring-worker-job:{job_id}",
                    )
                    self._active_tasks[job_id] = task
                    processed += 1

                if once:
                    if not self._active_tasks:
                        break
                    if self.stop_event.is_set():
                        break
                    await asyncio.wait(
                        self._active_tasks.values(),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    continue

                if self.stop_event.is_set():
                    break

                waiters: set[asyncio.Task[Any]] = set(self._active_tasks.values())
                stop_wait = asyncio.create_task(self.stop_event.wait())
                waiters.add(stop_wait)
                try:
                    await asyncio.wait(
                        waiters,
                        timeout=self.poll_interval,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    if not stop_wait.done():
                        stop_wait.cancel()
                        try:
                            await stop_wait
                        except asyncio.CancelledError:
                            pass
        finally:
            pending = [t for t in self._active_tasks.values() if not t.done()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            self._active_tasks.clear()
            try:
                await asyncio.to_thread(
                    _release_all_owned_leases, self.db_path, self.lease_owner
                )
            except Exception:
                logger.exception("failed releasing leases on worker shutdown")
        return processed


def _load_worker_settings(
    *,
    config_path: str | None,
    data_path: str | None,
    envs_path: str | None,
) -> Settings:
    settings = load_settings(Path(config_path) if config_path else None)
    if data_path:
        settings.octagon.data_path = Path(data_path)
    if envs_path:
        settings.octagon.envs_path = Path(envs_path)
    return settings


def _register_env_judge_credential(db_path: Path, attempt_id: str) -> None:
    """Register isolated per-job judge credential from env vars into runtime_state."""
    api_key = os.environ.get(API_KEY_ENV)
    if not api_key:
        return
    run_id = _run_id_of_attempt(db_path, attempt_id)
    if not run_id:
        return
    base_url = os.environ.get(BASE_URL_ENV) or None
    key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]
    set_credential(
        RunCredential(
            run_id=run_id,
            api_key=api_key,
            api_key_hash=f"env:{key_hash}",
            base_url=base_url,
        )
    )


async def _run_single_job_cli(args: argparse.Namespace) -> int:
    settings = _load_worker_settings(
        config_path=args.config,
        data_path=args.data_path,
        envs_path=args.envs_path,
    )
    data_path = settings.octagon.data_path
    db_path = Path(args.db_path) if args.db_path else resolve_db_path(data_path)
    envs_path = settings.octagon.envs_path
    envs = EnvLoader(envs_path).load_all(allow_unavailable_core=True)
    runtime_state.bind(
        data_path=data_path,
        db_path=db_path,
        envs=envs,
        settings=settings,
    )

    lease_seconds = (
        int(args.lease_seconds)
        if args.lease_seconds is not None
        else int(settings.octagon.scoring_lease_seconds)
    )
    deadline_seconds = (
        int(args.deadline_seconds)
        if args.deadline_seconds is not None
        else int(settings.octagon.scoring_deadline_seconds)
    )
    lease_owner = args.lease_owner or f"scoring-worker:{os.getpid()}"

    if args.already_claimed:
        job = _fetch_job_row(db_path, args.run_job)
        if job is None:
            logger.warning("scoring job not found job=%s", args.run_job)
            return 0
        if str(job["status"]) != "running":
            logger.info(
                "scoring job no longer running job=%s status=%s",
                args.run_job,
                job["status"],
            )
            return 0
        if args.lease_owner and str(job["lease_owner"] or "") != args.lease_owner:
            logger.warning(
                "scoring job lease owner mismatch job=%s expected=%s actual=%s",
                args.run_job,
                args.lease_owner,
                job["lease_owner"],
            )
            return 0
    else:
        job = _claim_job(
            db_path,
            args.run_job,
            deadline_seconds=deadline_seconds,
            lease_owner=lease_owner,
            lease_seconds=lease_seconds,
        )
        if job is None:
            return 0

    _register_env_judge_credential(db_path, str(job["attempt_id"]))

    await execute_claimed_job(
        job,
        deadline_seconds=deadline_seconds,
        lease_owner=lease_owner,
        lease_seconds=lease_seconds,
    )
    return 0


async def _run_daemon_cli(args: argparse.Namespace) -> int:
    settings = _load_worker_settings(
        config_path=args.config,
        data_path=args.data_path,
        envs_path=args.envs_path,
    )
    data_path = settings.octagon.data_path
    db_path = Path(args.db_path) if args.db_path else resolve_db_path(data_path)
    envs_path = settings.octagon.envs_path

    concurrency = (
        int(args.concurrency)
        if args.concurrency is not None
        else int(settings.octagon.max_active_scoring_jobs)
    )
    lease_seconds = (
        int(args.lease_seconds)
        if args.lease_seconds is not None
        else int(settings.octagon.scoring_lease_seconds)
    )
    deadline_seconds = (
        int(args.deadline_seconds)
        if args.deadline_seconds is not None
        else int(settings.octagon.scoring_deadline_seconds)
    )

    worker = ScoringWorker(
        db_path=db_path,
        data_path=data_path,
        envs_path=envs_path,
        config_path=Path(args.config) if args.config else None,
        concurrency=concurrency,
        lease_seconds=lease_seconds,
        deadline_seconds=deadline_seconds,
        poll_interval=float(args.poll_interval),
    )
    worker.install_signal_handlers(asyncio.get_running_loop())
    await worker.run(once=bool(args.once))
    return 0


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m backend.workers.scoring_worker",
        description="Octagon standalone scoring worker & isolated job runner",
    )
    parser.add_argument("--config", default=None, help="Path to octagon.yaml")
    parser.add_argument("--data-path", default=None, help="Override octagon.data_path")
    parser.add_argument("--db-path", default=None, help="Override SQLite DB path")
    parser.add_argument("--envs-path", default=None, help="Override octagon.envs_path")

    # Single-job isolated execution mode
    parser.add_argument("--run-job", default=None, help="Execute a single scoring_job ID")
    parser.add_argument("--lease-owner", default=None, help="Lease owner identifier")
    parser.add_argument("--lease-seconds", type=int, default=None, help="Lease TTL in seconds")
    parser.add_argument("--deadline-seconds", type=int, default=None, help="Hard scoring deadline in seconds")
    parser.add_argument(
        "--already-claimed",
        action="store_true",
        help="Indicates parent already claimed this job with --lease-owner",
    )

    # Standalone daemon mode
    parser.add_argument("--concurrency", type=int, default=None, help="Max concurrent scoring subprocesses")
    parser.add_argument("--poll-interval", type=float, default=1.0, help="Idle poll interval in seconds")
    parser.add_argument("--once", action="store_true", help="Drain queued jobs and exit")

    # Unified out-of-process preview entrypoint
    parser.add_argument(
        "--render-preview",
        nargs=3,
        metavar=("RENDERER", "SOURCE", "OUTPUT"),
        default=None,
        help="Delegate to backend.artifact_worker.main(renderer, source, output)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if args.render_preview is not None:
        from backend.artifact_worker import main as artifact_worker_main

        return artifact_worker_main(list(args.render_preview))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    try:
        if args.run_job:
            return asyncio.run(_run_single_job_cli(args))
        return asyncio.run(_run_daemon_cli(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    raise SystemExit(main())
