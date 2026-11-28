"""Tests for Phase 4 T4.1: Standalone scoring-worker process & preview process-group isolation."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

from backend import artifact_preview, artifact_worker, runtime_state
from backend.config import Settings
from backend.cost.credential import RunCredential, set_credential
from backend.db import _init_db_sync, _iso_after, _now_iso, resolve_db_path
from backend.env_loader import EnvLoader
from backend.scoring_queue import (
    _claim_job,
    _persist_scoring_job,
    cancel_scoring_for_runs,
    claim_next_scoring_job,
    reclaim_expired_scoring_leases,
    release_scoring_lease,
    renew_scoring_lease,
    schedule_scoring_job,
    schedule_startup_scoring_recovery,
)
from backend.workers.scoring_worker import ScoringWorker, run_job_in_subprocess


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_pid_dead(pid: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return not _pid_alive(pid)


def _create_env(
    envs_root: Path,
    env_name: str,
    scorer_code: str,
) -> Path:
    env_dir = envs_root / env_name
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "meta.yaml").write_text(
        textwrap.dedent(
            f"""\
            name: {env_name}
            description: "Test env for scoring worker isolation"
            category: baseline
            pass_threshold: 60
            dimensions:
              - name: quality
                weight: 1.0
                description: "Quality score"
            """
        ),
        encoding="utf-8",
    )
    (env_dir / "core.py").write_text("# empty core for scoring tests\n", encoding="utf-8")
    (env_dir / "scorer.py").write_text(textwrap.dedent(scorer_code), encoding="utf-8")
    tasks_dir = env_dir / "tasks"
    tasks_dir.mkdir(exist_ok=True)
    (tasks_dir / "task_1.json").write_text(
        json.dumps({"task_id": "t1", "query": "Produce output"}),
        encoding="utf-8",
    )
    return env_dir


def _seed_attempt_and_job(
    data_path: Path,
    envs_path: Path,
    env_name: str,
    *,
    attempt_id: str = "att_1",
    run_id: str = "run_1",
    task_id: str = "t1",
    scoring_worker_mode: str = "in_process",
    scoring_lease_seconds: int = 60,
    scoring_deadline_seconds: int = 300,
) -> tuple[Path, str]:
    db_path = resolve_db_path(data_path)
    _init_db_sync(db_path)
    now = _now_iso()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO tasks(id, env_name, prompt, created_at) VALUES(?,?,?,?)",
            (task_id, env_name, "Produce output", now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO runs(id, task_id, env_name, status, created_at) VALUES(?,?,?,?,?)",
            (run_id, task_id, env_name, "scoring", now),
        )
        conn.execute(
            "INSERT INTO attempts(id, run_id, task_id, env_name, agent_name, model,"
            " status, env_session_id, env_token_hash, execution_status, scoring_status,"
            " external_refs_json, event_count, thinking_count, tool_call_count,"
            " token_usage_json, duration_ms, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                attempt_id,
                run_id,
                task_id,
                env_name,
                "fake-agent",
                "fake-model",
                "scoring",
                f"env_{attempt_id}",
                "tok_hash",
                "completed",
                "queued",
                "{}",
                0,
                0,
                0,
                "{}",
                100,
                now,
            ),
        )
        conn.commit()

    attempt_dir = data_path / "attempts" / attempt_id
    attempt_dir.mkdir(parents=True, exist_ok=True)
    (attempt_dir / "workspace").mkdir(exist_ok=True)
    (attempt_dir / "workspace" / "answer.txt").write_text("hello", encoding="utf-8")

    settings = Settings()
    settings.octagon.data_path = data_path
    settings.octagon.envs_path = envs_path
    settings.octagon.scoring_worker_mode = scoring_worker_mode  # type: ignore[assignment]
    settings.octagon.scoring_lease_seconds = scoring_lease_seconds
    settings.octagon.scoring_deadline_seconds = scoring_deadline_seconds

    envs = EnvLoader(envs_path).load_all(allow_unavailable_core=True)
    runtime_state.bind(
        data_path=data_path,
        db_path=db_path,
        envs=envs,
        settings=settings,
    )

    job_id, _ = _persist_scoring_job(
        attempt_id=attempt_id,
        adapter_status="completed",
        adapter_error_code=None,
        adapter_error_message=None,
        stats={
            "external_refs": {},
            "event_count": 0,
            "last_event_at": None,
            "thinking_count": 0,
            "tool_call_count": 0,
            "token_usage": {},
            "cost_estimate": None,
            "duration_ms": 100,
            "transport_status": "ok",
        },
        security_meta={},
    )
    return db_path, job_id


@pytest.fixture(autouse=True)
def _reset_runtime_state():
    yield
    runtime_state.reset()


# ---------------------------------------------------------------------------
# 1. Lease claim, heartbeat renewal, release, and expired lease takeover
# ---------------------------------------------------------------------------


def test_scoring_lease_claim_renew_release_and_takeover(tmp_path: Path) -> None:
    data_path = tmp_path / "data"
    envs_path = tmp_path / "envs"
    _create_env(
        envs_path,
        "lease_env",
        """
        def score(*, attempt_id, task, env_db, trace, final_state):
            return [{"dimension": "quality", "value": 90, "detail": "ok"}]
        """,
    )
    db_path, job_id = _seed_attempt_and_job(
        data_path, envs_path, "lease_env", attempt_id="att_lease"
    )

    # Claim oldest queued job
    claimed = claim_next_scoring_job(
        db_path, lease_owner="worker-a", lease_seconds=30, deadline_seconds=120
    )
    assert claimed is not None
    assert claimed["id"] == job_id
    assert claimed["status"] == "running"
    assert claimed["lease_owner"] == "worker-a"
    assert claimed["lease_expires_at"] is not None
    assert claimed["heartbeat_at"] is not None
    assert claimed["attempt_count"] == 1

    # Another worker cannot steal an active (unexpired) lease
    assert (
        claim_next_scoring_job(db_path, lease_owner="worker-b", lease_seconds=30)
        is None
    )

    # Renewal succeeds for owner and fails for non-owner
    assert renew_scoring_lease(
        db_path, job_id, lease_owner="worker-a", lease_seconds=60
    )
    assert not renew_scoring_lease(
        db_path, job_id, lease_owner="worker-b", lease_seconds=60
    )

    # Release returns job back to queued and clears lease metadata
    assert not release_scoring_lease(db_path, job_id, lease_owner="worker-b")
    assert release_scoring_lease(db_path, job_id, lease_owner="worker-a")
    with sqlite3.connect(db_path) as conn:
        status, owner, expires, started = conn.execute(
            "SELECT status, lease_owner, lease_expires_at, started_at FROM scoring_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        att_scoring = conn.execute(
            "SELECT scoring_status FROM attempts WHERE id='att_lease'"
        ).fetchone()[0]
    assert (status, owner, expires, started) == ("queued", None, None, None)
    assert att_scoring == "queued"

    # Re-claim and then simulate expired lease for reclaim_expired_scoring_leases
    claimed2 = claim_next_scoring_job(db_path, lease_owner="worker-dead", lease_seconds=30)
    assert claimed2 is not None and claimed2["attempt_count"] == 2
    past = _iso_after(_now_iso(), -120)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE scoring_jobs SET lease_expires_at=? WHERE id=?", (past, job_id)
        )
        conn.commit()

    # Direct expired-lease takeover via claim_next_scoring_job
    taken = claim_next_scoring_job(db_path, lease_owner="worker-c", lease_seconds=30)
    assert taken is not None
    assert taken["id"] == job_id
    assert taken["lease_owner"] == "worker-c"
    assert taken["attempt_count"] == 3

    # Expire again and verify reclaim_expired_scoring_leases resets it to queued
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE scoring_jobs SET lease_expires_at=? WHERE id=?", (past, job_id)
        )
        conn.commit()
    reclaimed = reclaim_expired_scoring_leases(db_path)
    assert reclaimed == 1
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT status, lease_owner, lease_expires_at FROM scoring_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
    assert row == ("queued", None, None)

    # _claim_job also supports claiming with lease_owner/lease_seconds
    direct = _claim_job(db_path, job_id, lease_owner="direct-1", lease_seconds=45)
    assert direct is not None and direct["lease_owner"] == "direct-1"


# ---------------------------------------------------------------------------
# 2. scoring_worker_mode="external" behavior & startup recovery safety
# ---------------------------------------------------------------------------


def test_external_worker_mode_skips_in_process_and_preserves_healthy_leases(
    tmp_path: Path,
) -> None:
    data_path = tmp_path / "data"
    envs_path = tmp_path / "envs"
    _create_env(
        envs_path,
        "ext_env",
        """
        def score(*, attempt_id, task, env_db, trace, final_state):
            return [{"dimension": "quality", "value": 80, "detail": "ok"}]
        """,
    )
    db_path, job_queued = _seed_attempt_and_job(
        data_path,
        envs_path,
        "ext_env",
        attempt_id="att_ext_1",
        run_id="run_ext_1",
        scoring_worker_mode="external",
    )

    # schedule_scoring_job must return False without creating any asyncio task
    assert schedule_scoring_job(job_queued, capacity=2, attempt_id="att_ext_1") is False
    state = runtime_state.get()
    assert not state.scoring_tasks
    assert not state.active_tasks

    # Seed two running external jobs: one healthy, one expired
    _, job_healthy = _seed_attempt_and_job(
        data_path,
        envs_path,
        "ext_env",
        attempt_id="att_ext_2",
        run_id="run_ext_2",
        scoring_worker_mode="external",
    )
    _, job_expired = _seed_attempt_and_job(
        data_path,
        envs_path,
        "ext_env",
        attempt_id="att_ext_3",
        run_id="run_ext_3",
        scoring_worker_mode="external",
    )
    future = _iso_after(_now_iso(), 300)
    past = _iso_after(_now_iso(), -300)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE scoring_jobs SET status='running', lease_owner='scoring-worker:host:1:abcd',"
            " lease_expires_at=? WHERE id=?",
            (future, job_healthy),
        )
        conn.execute(
            "UPDATE attempts SET scoring_status='running' WHERE id='att_ext_2'"
        )
        conn.execute(
            "UPDATE scoring_jobs SET status='running', lease_owner='scoring-worker:host:2:ef01',"
            " lease_expires_at=? WHERE id=?",
            (past, job_expired),
        )
        conn.execute(
            "UPDATE attempts SET scoring_status='running' WHERE id='att_ext_3'"
        )
        conn.commit()

    recovered = schedule_startup_scoring_recovery(capacity=2)
    assert recovered == 0
    assert not runtime_state.get().scoring_tasks

    with sqlite3.connect(db_path) as conn:
        healthy_row = conn.execute(
            "SELECT status, lease_owner FROM scoring_jobs WHERE id=?", (job_healthy,)
        ).fetchone()
        expired_row = conn.execute(
            "SELECT status, lease_owner FROM scoring_jobs WHERE id=?", (job_expired,)
        ).fetchone()
    assert healthy_row == ("running", "scoring-worker:host:1:abcd")
    assert expired_row == ("queued", None)


# ---------------------------------------------------------------------------
# 3. ScoringWorker --once end-to-end with credential scrubbing & judge overlay
# ---------------------------------------------------------------------------


def test_scoring_worker_once_completes_job_in_isolated_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_path = tmp_path / "data"
    envs_path = tmp_path / "envs"
    probe_file = tmp_path / "env_probe.json"

    _create_env(
        envs_path,
        "ok_env",
        f"""
        import json
        import os
        from pathlib import Path
        from backend.cost.credential import current_judge_credential

        def score(*, attempt_id, task, env_db, trace, final_state):
            cred = current_judge_credential()
            Path({str(probe_file)!r}).write_text(
                json.dumps({{
                    "pid": os.getpid(),
                    "has_mgmt": "OPENROUTER_MANAGEMENT_KEY" in os.environ,
                    "has_insights": "INSIGHTS_API_KEY" in os.environ,
                    "judge_key": cred.api_key if cred else None,
                    "judge_base_url": cred.base_url if cred else None,
                }}),
                encoding="utf-8",
            )
            return [{{"dimension": "quality", "value": 88, "detail": "great"}}]
        """,
    )
    monkeypatch.setenv("OPENROUTER_MANAGEMENT_KEY", "secret-mgmt-key")
    monkeypatch.setenv("INSIGHTS_API_KEY", "secret-insights-key")

    db_path, job_id = _seed_attempt_and_job(
        data_path,
        envs_path,
        "ok_env",
        attempt_id="att_ok",
        run_id="run_ok",
        scoring_worker_mode="external",
    )
    set_credential(
        RunCredential(
            run_id="run_ok",
            api_key="sk-judge-only-for-run-ok",
            api_key_hash="hash_ok",
            base_url="https://openrouter.ai/api/v1",
        )
    )

    worker = ScoringWorker(
        db_path=db_path,
        data_path=data_path,
        envs_path=envs_path,
        concurrency=1,
        lease_seconds=30,
        deadline_seconds=60,
        poll_interval=0.2,
    )
    processed = asyncio.run(worker.run(once=True))
    assert processed == 1

    with sqlite3.connect(db_path) as conn:
        job_status = conn.execute(
            "SELECT status FROM scoring_jobs WHERE id=?", (job_id,)
        ).fetchone()[0]
        att_row = conn.execute(
            "SELECT status, scoring_status, score_total FROM attempts WHERE id='att_ok'"
        ).fetchone()
        score_rows = conn.execute(
            "SELECT dimension, value FROM scores WHERE attempt_id='att_ok'"
        ).fetchall()

    assert job_status == "completed"
    assert att_row == ("completed", "completed", 88)
    assert score_rows == [("quality", 88)]

    probe = json.loads(probe_file.read_text(encoding="utf-8"))
    assert probe["pid"] != os.getpid()
    assert probe["has_mgmt"] is False
    assert probe["has_insights"] is False
    assert probe["judge_key"] == "sk-judge-only-for-run-ok"
    assert probe["judge_base_url"] == "https://openrouter.ai/api/v1"


# ---------------------------------------------------------------------------
# 4. OS-level hard deadline kill (process group)
# ---------------------------------------------------------------------------


def test_subprocess_hard_deadline_kills_process_group_and_marks_timed_out(
    tmp_path: Path,
) -> None:
    data_path = tmp_path / "data"
    envs_path = tmp_path / "envs"
    pid_file = tmp_path / "child_pids.json"

    _create_env(
        envs_path,
        "hang_env",
        f"""
        import json
        import os
        import subprocess
        import sys
        import time
        from pathlib import Path

        def score(*, attempt_id, task, env_db, trace, final_state):
            grandchild = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            Path({str(pid_file)!r}).write_text(
                json.dumps({{"scorer_pid": os.getpid(), "grandchild_pid": grandchild.pid}}),
                encoding="utf-8",
            )
            time.sleep(30)
            return [{{"dimension": "quality", "value": 100, "detail": "unreachable"}}]
        """,
    )

    db_path, job_id = _seed_attempt_and_job(
        data_path,
        envs_path,
        "hang_env",
        attempt_id="att_hang",
        run_id="run_hang",
        scoring_worker_mode="subprocess",
        scoring_deadline_seconds=1,
    )

    started = time.monotonic()
    rc = asyncio.run(
        run_job_in_subprocess(
            job_id,
            db_path=db_path,
            data_path=data_path,
            envs_path=envs_path,
            lease_owner="test-deadline",
            lease_seconds=30,
            deadline_seconds=1,
            grace_seconds=0.5,
        )
    )
    elapsed = time.monotonic() - started
    assert elapsed < 8.0
    assert rc != 0

    with sqlite3.connect(db_path) as conn:
        job_row = conn.execute(
            "SELECT status, error_code FROM scoring_jobs WHERE id=?", (job_id,)
        ).fetchone()
        att_row = conn.execute(
            "SELECT scoring_status, scoring_error_code, score_total FROM attempts WHERE id='att_hang'"
        ).fetchone()

    assert job_row == ("timed_out", "scoring_deadline_exceeded")
    assert att_row == ("timed_out", "scoring_deadline_exceeded", None)

    if pid_file.exists():
        pids = json.loads(pid_file.read_text(encoding="utf-8"))
        assert _wait_pid_dead(int(pids["scorer_pid"])), "scorer process survived deadline kill"
        assert _wait_pid_dead(int(pids["grandchild_pid"])), "grandchild process survived killpg"


# ---------------------------------------------------------------------------
# 5. Cancellation kills running subprocess process group
# ---------------------------------------------------------------------------


def test_cancel_scoring_kills_running_subprocess_process_group(
    tmp_path: Path,
) -> None:
    data_path = tmp_path / "data"
    envs_path = tmp_path / "envs"
    ready_file = tmp_path / "cancel_ready.json"

    _create_env(
        envs_path,
        "cancel_env",
        f"""
        import json
        import os
        import subprocess
        import sys
        import time
        from pathlib import Path

        def score(*, attempt_id, task, env_db, trace, final_state):
            grandchild = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            Path({str(ready_file)!r}).write_text(
                json.dumps({{"scorer_pid": os.getpid(), "grandchild_pid": grandchild.pid}}),
                encoding="utf-8",
            )
            time.sleep(30)
            return [{{"dimension": "quality", "value": 100, "detail": "unreachable"}}]
        """,
    )

    db_path, job_id = _seed_attempt_and_job(
        data_path,
        envs_path,
        "cancel_env",
        attempt_id="att_cancel",
        run_id="run_cancel",
        scoring_worker_mode="subprocess",
        scoring_deadline_seconds=60,
    )

    async def _run_and_cancel() -> None:
        task = asyncio.create_task(
            run_job_in_subprocess(
                job_id,
                db_path=db_path,
                data_path=data_path,
                envs_path=envs_path,
                lease_owner="test-cancel",
                lease_seconds=30,
                deadline_seconds=60,
                grace_seconds=0.5,
            )
        )
        deadline = time.monotonic() + 10.0
        while not ready_file.exists() and time.monotonic() < deadline:
            assert not task.done(), "subprocess exited before becoming ready"
            await asyncio.sleep(0.05)
        assert ready_file.exists(), "scorer did not start in time"

        cancelled = cancel_scoring_for_runs(["run_cancel"])
        assert cancelled == 1
        await asyncio.wait_for(task, timeout=8.0)

    asyncio.run(_run_and_cancel())

    pids = json.loads(ready_file.read_text(encoding="utf-8"))
    assert _wait_pid_dead(int(pids["scorer_pid"])), "scorer process survived cancel"
    assert _wait_pid_dead(int(pids["grandchild_pid"])), "grandchild survived cancel killpg"

    with sqlite3.connect(db_path) as conn:
        job_row = conn.execute(
            "SELECT status, error_code FROM scoring_jobs WHERE id=?", (job_id,)
        ).fetchone()
        att_row = conn.execute(
            "SELECT scoring_status, score_total FROM attempts WHERE id='att_cancel'"
        ).fetchone()
    assert job_row == ("cancelled", "scoring_cancelled")
    assert att_row == ("cancelled", None)


# ---------------------------------------------------------------------------
# 6. artifact_preview._worker_result process-group timeout cleanup & env limits
# ---------------------------------------------------------------------------


def test_artifact_preview_worker_timeout_kills_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sample = tmp_path / "sample.xlsx"
    sample.write_bytes(b"PK\x03\x04dummy-ooxml-bytes")
    pid_file = tmp_path / "preview_pids.json"

    fake_script = tmp_path / "slow_preview_worker.py"
    fake_script.write_text(
        textwrap.dedent(
            f"""\
            import json, os, subprocess, sys, time
            from pathlib import Path
            grandchild = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            Path({str(pid_file)!r}).write_text(
                json.dumps({{"worker_pid": os.getpid(), "grandchild_pid": grandchild.pid}}),
                encoding="utf-8",
            )
            time.sleep(30)
            """
        ),
        encoding="utf-8",
    )

    real_popen = subprocess.Popen

    def _wrapped_popen(cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.Popen[Any]:
        assert kwargs.get("start_new_session") is True
        return real_popen([sys.executable, str(fake_script)], *args, **kwargs)

    monkeypatch.setattr(artifact_preview.subprocess, "Popen", _wrapped_popen)

    result = artifact_preview._worker_result(
        sample,
        renderer="xlsx-structural",
        cache_dir=None,
        timeout_seconds=0.8,
    )
    assert result["ok"] is False
    assert result["transient"] is True
    assert result["error"]["code"] == "renderer_timeout"

    assert pid_file.exists()
    pids = json.loads(pid_file.read_text(encoding="utf-8"))
    assert _wait_pid_dead(int(pids["worker_pid"])), "preview worker survived timeout"
    assert _wait_pid_dead(int(pids["grandchild_pid"])), "preview grandchild survived killpg"


def test_artifact_worker_limit_env_clamping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OCTAGON_PREVIEW_CPU_LIMIT", raising=False)
    assert (
        artifact_worker._parse_clamped_int_env(
            "OCTAGON_PREVIEW_CPU_LIMIT", 12, minimum=1, maximum=300
        )
        == 12
    )
    monkeypatch.setenv("OCTAGON_PREVIEW_CPU_LIMIT", "99999")
    assert (
        artifact_worker._parse_clamped_int_env(
            "OCTAGON_PREVIEW_CPU_LIMIT", 12, minimum=1, maximum=300
        )
        == 300
    )
    monkeypatch.setenv("OCTAGON_PREVIEW_CPU_LIMIT", "0")
    assert (
        artifact_worker._parse_clamped_int_env(
            "OCTAGON_PREVIEW_CPU_LIMIT", 12, minimum=1, maximum=300
        )
        == 1
    )
    monkeypatch.setenv("OCTAGON_PREVIEW_CPU_LIMIT", "not-a-number")
    assert (
        artifact_worker._parse_clamped_int_env(
            "OCTAGON_PREVIEW_CPU_LIMIT", 12, minimum=1, maximum=300
        )
        == 12
    )


def test_scoring_worker_cli_once_and_render_preview_delegation(tmp_path: Path) -> None:
    # 1) --render-preview delegates cleanly to backend.artifact_worker.main
    src = tmp_path / "dummy.bin"
    src.write_bytes(b"not-an-office-file")
    out = tmp_path / "preview_out.json"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "backend.workers.scoring_worker",
            "--render-preview",
            "unknown-renderer",
            str(src),
            str(out),
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert proc.returncode == 2
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["ok"] is False
    assert payload["error"]["code"] == "renderer_unknown"

    # 2) --once CLI drains a queued job end-to-end in a fresh process
    data_path = tmp_path / "cli_data"
    envs_path = tmp_path / "cli_envs"
    _create_env(
        envs_path,
        "cli_env",
        """
        def score(*, attempt_id, task, env_db, trace, final_state):
            return [{"dimension": "quality", "value": 77, "detail": "cli ok"}]
        """,
    )
    db_path, job_id = _seed_attempt_and_job(
        data_path,
        envs_path,
        "cli_env",
        attempt_id="att_cli",
        run_id="run_cli",
        scoring_worker_mode="external",
    )
    cli_proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "backend.workers.scoring_worker",
            "--data-path",
            str(data_path),
            "--envs-path",
            str(envs_path),
            "--once",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert cli_proc.returncode == 0, cli_proc.stderr
    with sqlite3.connect(db_path) as conn:
        job_status = conn.execute(
            "SELECT status FROM scoring_jobs WHERE id=?", (job_id,)
        ).fetchone()[0]
        score_total = conn.execute(
            "SELECT score_total FROM attempts WHERE id='att_cli'"
        ).fetchone()[0]
    assert job_status == "completed"
    assert score_total == 77


def test_subprocess_crash_marks_job_scoring_worker_crashed(tmp_path: Path) -> None:
    data_path = tmp_path / "data"
    envs_path = tmp_path / "envs"
    _create_env(
        envs_path,
        "crash_env",
        """
        import os

        def score(*, attempt_id, task, env_db, trace, final_state):
            os._exit(42)
        """,
    )
    db_path, job_id = _seed_attempt_and_job(
        data_path,
        envs_path,
        "crash_env",
        attempt_id="att_crash",
        run_id="run_crash",
        scoring_worker_mode="subprocess",
    )

    rc = asyncio.run(
        run_job_in_subprocess(
            job_id,
            db_path=db_path,
            data_path=data_path,
            envs_path=envs_path,
            lease_owner="test-crash",
            lease_seconds=30,
            deadline_seconds=30,
        )
    )
    assert rc == 42

    with sqlite3.connect(db_path) as conn:
        job_row = conn.execute(
            "SELECT status, error_code FROM scoring_jobs WHERE id=?", (job_id,)
        ).fetchone()
        att_row = conn.execute(
            "SELECT scoring_status, scoring_error_code, score_total FROM attempts WHERE id='att_crash'"
        ).fetchone()
    assert job_row == ("failed", "scoring_worker_crashed")
    assert att_row == ("failed", "scoring_worker_crashed", None)


def test_subprocess_mode_schedule_scoring_job_completes(tmp_path: Path) -> None:
    data_path = tmp_path / "data"
    envs_path = tmp_path / "envs"
    _create_env(
        envs_path,
        "sub_env",
        """
        def score(*, attempt_id, task, env_db, trace, final_state):
            return [{"dimension": "quality", "value": 95, "detail": "sub ok"}]
        """,
    )
    db_path, job_id = _seed_attempt_and_job(
        data_path,
        envs_path,
        "sub_env",
        attempt_id="att_sub",
        run_id="run_sub",
        scoring_worker_mode="subprocess",
    )

    async def _run() -> None:
        assert schedule_scoring_job(job_id, capacity=2, attempt_id="att_sub") is True
        task = runtime_state.get().scoring_tasks["att_sub"]
        await asyncio.wait_for(task, timeout=20.0)

    asyncio.run(_run())

    with sqlite3.connect(db_path) as conn:
        job_status = conn.execute(
            "SELECT status FROM scoring_jobs WHERE id=?", (job_id,)
        ).fetchone()[0]
        score_total = conn.execute(
            "SELECT score_total FROM attempts WHERE id='att_sub'"
        ).fetchone()[0]
    assert job_status == "completed"
    assert score_total == 95

