"""Tests for T4.2: Env Scorer / Core Subprocess Isolation."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import evaluator, runtime_state
from backend.config import OctagonSection, Settings
from backend.cost.credential import (
    RunCredential,
    judge_credentials_active,
    set_credential,
)
from backend.db import hash_env_token, open_db, resolve_db_path
from backend.env_attempt_server import register_routes as register_env_attempt_routes
from backend.env_loader import (
    EnvLoader,
    EnvLoadError,
    IsolatedRegisteredTool,
    IsolatedScorerModule,
)
from backend.workers.env_worker import (
    build_isolated_worker_env,
    call_tool_in_subprocess,
    EnvWorkerTimeoutError,
)


def _create_synthetic_env(
    envs_root: Path,
    name: str = "iso-probe",
    *,
    core_code: str | None = None,
    scorer_code: str | None = None,
) -> Path:
    env_dir = envs_root / name
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "meta.yaml").write_text(
        f"""
name: {name}
schema_version: "2"
description: Synthetic env for subprocess isolation tests
pass_threshold: 60
dimensions:
  - name: isolation
    weight: 100
""".lstrip(),
        encoding="utf-8",
    )
    (env_dir / "schema.sql").write_text(
        """
CREATE TABLE IF NOT EXISTS probes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note TEXT NOT NULL
);
""".lstrip(),
        encoding="utf-8",
    )
    tasks_dir = env_dir / "tasks"
    tasks_dir.mkdir(exist_ok=True)
    (tasks_dir / "t1.json").write_text(
        json.dumps(
            {
                "id": "t1",
                "env_name": name,
                "prompt": "Run probe tool.",
                "timeout_seconds": 60,
            }
        ),
        encoding="utf-8",
    )

    if core_code is None:
        core_code = """
import os
import time
from octagon.env_api import EnvContext, env_tool

# Noise on stdout must never corrupt worker JSON protocol.
print("CORE_STDOUT_NOISE_ON_IMPORT")


@env_tool(
    name="probe_env",
    description="Probe environment variables, runtime_state, and DB.",
    parameters={
        "type": "object",
        "properties": {"note": {"type": "string"}},
        "required": ["note"],
    },
)
def probe_env(ctx: EnvContext, note: str):
    print("TOOL_STDOUT_NOISE_ON_CALL")
    ctx.db.execute("INSERT INTO probes(note) VALUES (?)", (note,))
    ctx.db.commit()
    count = ctx.db.execute("SELECT COUNT(*) FROM probes").fetchone()[0]

    creds_seen = None
    try:
        from backend import runtime_state
        creds_seen = list(runtime_state.get().run_credentials.keys())
    except RuntimeError as exc:
        creds_seen = f"unbound:{exc.__class__.__name__}"

    return {
        "pid": os.getpid(),
        "count": count,
        "has_mgmt_key": "OPENROUTER_MANAGEMENT_KEY" in os.environ,
        "has_blade_key": "BLADE_API_KEY" in os.environ,
        "has_insights_key": "INSIGHTS_API_KEY" in os.environ,
        "has_ssh_pw": "SAME_MODEL_SSH_PASSWORD" in os.environ,
        "has_openai_key": "OPENAI_API_KEY" in os.environ,
        "has_run_llm_key": "OCTAGON_RUN_LLM_API_KEY" in os.environ,
        "selected_skills_dir": os.environ.get("SELECTED_SKILLS_DIR"),
        "creds_seen": creds_seen,
    }


@env_tool(
    name="fail_tool",
    description="Always raises ValueError.",
    parameters={"type": "object", "properties": {}},
)
def fail_tool(ctx: EnvContext):
    raise ValueError("boom")


@env_tool(
    name="hang_tool",
    description="Sleeps for a long time to test process group timeout.",
    parameters={"type": "object", "properties": {"seconds": {"type": "number"}}},
)
def hang_tool(ctx: EnvContext, seconds: float = 30.0):
    time.sleep(seconds)
    return {"slept": seconds}
""".lstrip()
    (env_dir / "core.py").write_text(core_code, encoding="utf-8")

    if scorer_code is None:
        scorer_code = """
import os
import sqlite3
from pathlib import Path
from typing import Any

from backend.evaluator import ScorerUnavailableError

__octagon_scorer_version__ = "iso-scorer-v1"
print("SCORER_STDOUT_NOISE_ON_IMPORT")


def score(
    *,
    attempt_id: str,
    task: dict[str, Any],
    env_db: Path,
    trace: list[dict[str, Any]],
    final_state: dict[str, Any],
    events: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    print("SCORER_STDOUT_NOISE_ON_RUN")
    if task.get("trigger_unavailable"):
        raise ScorerUnavailableError("upstream judge down")

    note_count = 0
    if env_db and Path(env_db).is_file():
        conn = sqlite3.connect(str(env_db))
        try:
            note_count = conn.execute("SELECT COUNT(*) FROM probes").fetchone()[0]
        finally:
            conn.close()

    creds_seen = None
    try:
        from backend import runtime_state
        creds_seen = list(runtime_state.get().run_credentials.keys())
    except RuntimeError:
        creds_seen = "unbound"

    detail = {
        "pid": os.getpid(),
        "run_llm_key": os.environ.get("OCTAGON_RUN_LLM_API_KEY"),
        "run_llm_base_url": os.environ.get("OCTAGON_RUN_LLM_BASE_URL"),
        "has_mgmt_key": "OPENROUTER_MANAGEMENT_KEY" in os.environ,
        "has_ssh_pw": "SAME_MODEL_SSH_PASSWORD" in os.environ,
        "has_blade_key": "BLADE_API_KEY" in os.environ,
        "creds_seen": creds_seen,
        "note_count": note_count,
        "events_count": len(events or []),
    }
    return [
        {
            "dimension": "isolation",
            "value": 92,
            "detail": detail,
        }
    ]


score.__version__ = "1.2.3"
score.__octagon_evaluation_manifest__ = lambda: {
    "judge_model": "test-judge",
    "api_key": "should-be-redacted",
}
""".lstrip()
    if scorer_code != "":
        (env_dir / "scorer.py").write_text(scorer_code, encoding="utf-8")
    return env_dir


def _setup_attempt_db(
    data_path: Path,
    *,
    attempt_id: str,
    env_name: str,
    token: str,
    env_session_id: str = "sess_1",
) -> Path:
    async def _init() -> None:
        conn = await open_db(data_path)
        await conn.close()

    asyncio.run(_init())
    db_path = resolve_db_path(data_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO runs(id, env_name, task_id, status, created_at)
            VALUES('run_1', ?, 't1', 'running', '2026-01-01T00:00:00Z')
            """,
            (env_name,),
        )
        conn.execute(
            """
            INSERT INTO attempts(
                id, run_id, agent_name, env_name, task_id, status,
                env_session_id, env_token_hash, created_at
            )
            VALUES(?, 'run_1', 'test-agent', ?, 't1', 'running', ?, ?, '2026-01-01T00:00:00Z')
            """,
            (attempt_id, env_name, env_session_id, hash_env_token(token)),
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _build_test_client(
    *,
    data_path: Path,
    envs_path: Path,
    envs: dict[str, Any],
    isolation_mode: str = "subprocess",
    tool_timeout_seconds: int = 120,
) -> TestClient:
    settings = Settings(
        octagon=OctagonSection(
            data_path=data_path,
            envs_path=envs_path,
            env_isolation_mode=isolation_mode,  # type: ignore[arg-type]
            env_tool_timeout_seconds=tool_timeout_seconds,
        )
    )
    app = FastAPI()
    app.state.settings = settings
    app.state.envs = envs
    register_env_attempt_routes(app)
    return TestClient(app)


# ---------------------------------------------------------------------------
# 1. Environment scrubbing unit tests
# ---------------------------------------------------------------------------


def test_build_isolated_worker_env_scrubbing():
    parent_env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/tester",
        "SELECTED_SKILLS_DIR": "/opt/skills",
        "OPENROUTER_MANAGEMENT_KEY": "mgmt-secret",
        "SAME_MODEL_SSH_PASSWORD": "ssh-secret",
        "SAME_MODEL_BLADE_API_KEY": "same-blade-secret",
        "INSIGHTS_API_KEY": "insights-secret",
        "BLADE_API_KEY": "blade-secret",
        "OPENAI_API_KEY": "openai-secret",
        "OPENROUTER_API_KEY": "openrouter-secret",
        "OCTAGON_RUN_LLM_API_KEY": "stale-run-key",
        "CUSTOM_MGMT_KEY": "custom-mgmt-secret",
    }

    tool_env = build_isolated_worker_env(
        action="call-tool",
        base_env=parent_env,
        extra_deny=("CUSTOM_MGMT_KEY",),
    )
    assert tool_env["PATH"] == "/usr/bin:/bin"
    assert tool_env["SELECTED_SKILLS_DIR"] == "/opt/skills"
    for forbidden in (
        "OPENROUTER_MANAGEMENT_KEY",
        "SAME_MODEL_SSH_PASSWORD",
        "SAME_MODEL_BLADE_API_KEY",
        "INSIGHTS_API_KEY",
        "BLADE_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "OCTAGON_RUN_LLM_API_KEY",
        "CUSTOM_MGMT_KEY",
    ):
        assert forbidden not in tool_env

    scorer_env = build_isolated_worker_env(
        action="run-scorer",
        base_env=parent_env,
        extra_env={
            "OCTAGON_RUN_LLM_API_KEY": "ephemeral-run-a",
            "OCTAGON_RUN_LLM_BASE_URL": "https://openrouter.ai/api/v1",
            # Attempt to smuggle hard-denied keys via extra_env must fail:
            "OPENROUTER_MANAGEMENT_KEY": "smuggled",
            "CUSTOM_MGMT_KEY": "smuggled-custom",
        },
        extra_deny=("CUSTOM_MGMT_KEY",),
    )
    assert scorer_env["OCTAGON_RUN_LLM_API_KEY"] == "ephemeral-run-a"
    assert scorer_env["OCTAGON_RUN_LLM_BASE_URL"] == "https://openrouter.ai/api/v1"
    assert scorer_env["OPENAI_API_KEY"] == "openai-secret"
    for forbidden in (
        "OPENROUTER_MANAGEMENT_KEY",
        "SAME_MODEL_SSH_PASSWORD",
        "SAME_MODEL_BLADE_API_KEY",
        "INSIGHTS_API_KEY",
        "BLADE_API_KEY",
        "CUSTOM_MGMT_KEY",
    ):
        assert forbidden not in scorer_env


# ---------------------------------------------------------------------------
# 2. No in-process import when isolate=True
# ---------------------------------------------------------------------------


def test_env_loader_isolate_does_not_import_in_parent(tmp_path: Path):
    envs_root = tmp_path / "envs"
    _create_synthetic_env(envs_root, name="iso-probe")

    core_mod_name = "_octagon_env_iso_probe_core"
    scorer_mod_name = "_octagon_env_iso_probe_scorer"
    sys.modules.pop(core_mod_name, None)
    sys.modules.pop(scorer_mod_name, None)

    loaded = EnvLoader(envs_root).load_all(isolate=True)
    assert "iso-probe" in loaded
    env = loaded["iso-probe"]

    assert env.isolated is True
    assert core_mod_name not in sys.modules
    assert scorer_mod_name not in sys.modules

    assert set(env.tools) == {"probe_env", "fail_tool", "hang_tool"}
    probe_tool = env.tools["probe_env"]
    assert isinstance(probe_tool, IsolatedRegisteredTool)
    assert probe_tool.name == "probe_env"
    assert probe_tool.parameters_schema == probe_tool.parameters
    assert "note" in probe_tool.parameters_schema["properties"]

    assert isinstance(env.scorer_module, IsolatedScorerModule)
    assert env.scorer_module.__octagon_scorer_version__ == "iso-scorer-v1"
    assert callable(env.scorer_module.score)
    assert not hasattr(env.scorer_module, "evaluate_submission")


def test_env_loader_isolate_error_handling(tmp_path: Path):
    envs_root = tmp_path / "envs"
    _create_synthetic_env(
        envs_root,
        name="bad-core",
        core_code="raise RuntimeError('core broken')\n",
        scorer_code="",
    )

    with pytest.raises(EnvLoadError) as exc_info:
        EnvLoader(envs_root).load_all(isolate=True, allow_unavailable_core=False)
    assert exc_info.value.env_name == "bad-core"
    assert exc_info.value.stage == "core"

    deferred = EnvLoader(envs_root).load_all(isolate=True, allow_unavailable_core=True)
    assert deferred["bad-core"].load_error is not None
    assert deferred["bad-core"].isolated is True
    assert deferred["bad-core"].tools == {}

    envs_root_2 = tmp_path / "envs2"
    _create_synthetic_env(
        envs_root_2,
        name="bad-scorer",
        scorer_code="raise RuntimeError('scorer broken')\n",
    )
    with pytest.raises(EnvLoadError) as scorer_exc:
        EnvLoader(envs_root_2).load_all(isolate=True, allow_unavailable_core=True)
    assert scorer_exc.value.env_name == "bad-scorer"
    assert scorer_exc.value.stage == "scorer"


# ---------------------------------------------------------------------------
# 3. Credential & memory isolation for core.py tool via FastAPI TestClient
# ---------------------------------------------------------------------------


def test_isolated_tool_call_strips_secrets_and_isolates_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    envs_root = tmp_path / "envs"
    data_path = tmp_path / "data"
    _create_synthetic_env(envs_root, name="iso-probe")
    loaded = EnvLoader(envs_root).load_all(isolate=True)

    db_path = _setup_attempt_db(
        data_path,
        attempt_id="att_iso_1",
        env_name="iso-probe",
        token="secret-env-token",
    )

    runtime_state.bind(data_path=data_path, db_path=db_path, envs=loaded)
    try:
        set_credential(
            RunCredential(
                run_id="run_secret",
                api_key="sk-or-super-secret-run-key",
                api_key_hash="hash_run_secret",
                base_url="https://openrouter.ai/api/v1",
            )
        )
        monkeypatch.setenv("OPENROUTER_MANAGEMENT_KEY", "super-secret-mgmt")
        monkeypatch.setenv("BLADE_API_KEY", "super-secret-blade")
        monkeypatch.setenv("INSIGHTS_API_KEY", "super-secret-insights")
        monkeypatch.setenv("SAME_MODEL_SSH_PASSWORD", "super-secret-ssh")
        monkeypatch.setenv("OPENAI_API_KEY", "super-secret-openai")
        monkeypatch.setenv("SELECTED_SKILLS_DIR", "/tmp/selected-skills-test")

        client = _build_test_client(
            data_path=data_path,
            envs_path=envs_root,
            envs=loaded,
            isolation_mode="subprocess",
        )
        resp = client.post(
            "/attempts/att_iso_1/tools/probe_env",
            headers={"Authorization": "Bearer secret-env-token"},
            json={"note": "hello-isolated"},
        )
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["pid"] != os.getpid()
        assert payload["count"] == 1
        assert payload["has_mgmt_key"] is False
        assert payload["has_blade_key"] is False
        assert payload["has_insights_key"] is False
        assert payload["has_ssh_pw"] is False
        assert payload["has_openai_key"] is False
        assert payload["has_run_llm_key"] is False
        assert payload["selected_skills_dir"] == "/tmp/selected-skills-test"
        assert str(payload["creds_seen"]).startswith("unbound:")

        # Verify env.db and trace.jsonl were written by the isolated worker.
        env_db_path = data_path / "attempts" / "att_iso_1" / "env.db"
        assert env_db_path.is_file()
        conn = sqlite3.connect(env_db_path)
        try:
            rows = conn.execute("SELECT note FROM probes").fetchall()
            assert rows == [("hello-isolated",)]
        finally:
            conn.close()

        trace_path = data_path / "attempts" / "att_iso_1" / "trace.jsonl"
        assert trace_path.is_file()
        entries = [
            json.loads(line)
            for line in trace_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(entries) == 1
        assert entries[0]["tool_name"] == "probe_env"
        assert entries[0]["is_error"] is False
        assert "_octagon_env_iso_probe_core" not in sys.modules
    finally:
        runtime_state.reset()


# ---------------------------------------------------------------------------
# 4. Tool error & timeout handling
# ---------------------------------------------------------------------------


def test_isolated_tool_error_and_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    envs_root = tmp_path / "envs"
    data_path = tmp_path / "data"
    _create_synthetic_env(envs_root, name="iso-probe")
    loaded = EnvLoader(envs_root).load_all(isolate=True)

    _setup_attempt_db(
        data_path,
        attempt_id="att_err_1",
        env_name="iso-probe",
        token="tok-err",
    )

    client = _build_test_client(
        data_path=data_path,
        envs_path=envs_root,
        envs=loaded,
        isolation_mode="subprocess",
        tool_timeout_seconds=5,
    )

    # 4a. Application error -> HTTP 500 + trace is_error=true
    err_resp = client.post(
        "/attempts/att_err_1/tools/fail_tool",
        headers={"Authorization": "Bearer tok-err"},
        json={},
    )
    assert err_resp.status_code == 500
    assert err_resp.json()["detail"] == {
        "error": "tool_execution_error",
        "tool": "fail_tool",
        "type": "ValueError",
        "message": "boom",
    }
    trace_path = data_path / "attempts" / "att_err_1" / "trace.jsonl"
    entries = [
        json.loads(line)
        for line in trace_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(entries) == 1
    assert entries[0]["tool_name"] == "fail_tool"
    assert entries[0]["is_error"] is True

    # 4b. Timeout -> killed via process group + HTTP 504
    import backend.env_attempt_server as attempt_server_mod

    monkeypatch.setattr(attempt_server_mod, "_tool_timeout_seconds", lambda _req: 0.6)
    timeout_resp = client.post(
        "/attempts/att_err_1/tools/hang_tool",
        headers={"Authorization": "Bearer tok-err"},
        json={"seconds": 30},
    )
    assert timeout_resp.status_code == 504
    detail = timeout_resp.json()["detail"]
    assert detail["error"] == "tool_execution_timeout"
    assert detail["tool"] == "hang_tool"
    assert detail["type"] == "TimeoutError"

    # Also verify direct call_tool_in_subprocess raises EnvWorkerTimeoutError
    with pytest.raises(EnvWorkerTimeoutError):
        call_tool_in_subprocess(
            env_name="iso-probe",
            env_dir=envs_root / "iso-probe",
            tool_name="hang_tool",
            data_path=data_path,
            attempt_id="att_err_1",
            env_session_id="sess_1",
            arguments={"seconds": 30},
            timeout_seconds=0.5,
        )


# ---------------------------------------------------------------------------
# 5. Isolated scorer execution + credential scoping
# ---------------------------------------------------------------------------


def test_isolated_scorer_execution_and_credential_scoping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    envs_root = tmp_path / "envs"
    data_path = tmp_path / "data"
    _create_synthetic_env(envs_root, name="iso-probe")
    loaded = EnvLoader(envs_root).load_all(isolate=True)
    isolated_env = loaded["iso-probe"]

    db_path = _setup_attempt_db(
        data_path,
        attempt_id="att_score_1",
        env_name="iso-probe",
        token="tok-score",
    )

    # Seed env.db via isolated tool call and write one event to events.jsonl
    call_tool_in_subprocess(
        env_name="iso-probe",
        env_dir=isolated_env.env_dir,
        tool_name="probe_env",
        data_path=data_path,
        attempt_id="att_score_1",
        env_session_id="sess_1",
        arguments={"note": "for-scorer"},
    )
    events_path = data_path / "attempts" / "att_score_1" / "events.jsonl"
    events_path.write_text(json.dumps({"type": "ping"}) + "\n", encoding="utf-8")

    settings = Settings(
        octagon=OctagonSection(
            data_path=data_path,
            envs_path=envs_root,
            env_isolation_mode="subprocess",
        )
    )
    runtime_state.bind(
        data_path=data_path, db_path=db_path, envs=loaded, settings=settings
    )
    try:
        set_credential(
            RunCredential(
                run_id="run_a",
                api_key="sk-or-run-a-key",
                api_key_hash="hash_a",
                base_url="https://openrouter.ai/api/v1",
            )
        )
        set_credential(
            RunCredential(
                run_id="run_b",
                api_key="sk-or-run-b-key",
                api_key_hash="hash_b",
                base_url="https://openrouter.ai/api/v1",
            )
        )
        monkeypatch.setenv("OPENROUTER_MANAGEMENT_KEY", "super-secret-mgmt")
        monkeypatch.setenv("SAME_MODEL_SSH_PASSWORD", "super-secret-ssh")
        monkeypatch.setenv("BLADE_API_KEY", "super-secret-blade")

        with judge_credentials_active("run_a") as used_run_key:
            assert used_run_key is True
            outcome = evaluator.evaluate(
                attempt_id="att_score_1",
                task={"id": "t1", "prompt": "Run probe tool."},
                env=isolated_env,
                data_path=data_path,
                scorer=isolated_env.scorer_module.score,
            )

        assert outcome.score_total == 92
        assert outcome.passed is True
        assert len(outcome.scores) == 1
        detail = outcome.scores[0]["detail"]
        assert detail["pid"] != os.getpid()
        assert detail["run_llm_key"] == "sk-or-run-a-key"
        assert detail["run_llm_base_url"] == "https://openrouter.ai/api/v1"
        assert detail["has_mgmt_key"] is False
        assert detail["has_ssh_pw"] is False
        assert detail["has_blade_key"] is False
        assert detail["creds_seen"] == "unbound"
        assert detail["note_count"] == 1
        assert detail["events_count"] == 1

        # Verify declared evaluation manifest propagated & sanitized
        declared = outcome.evaluation_manifest["declared"]
        assert declared["judge_model"] == "test-judge"
        assert declared["api_key"] == "<redacted>"
        assert str(declared["isolated_scorer_source_hash"]).startswith("sha256:")
        assert "_octagon_env_iso_probe_scorer" not in sys.modules

        # Verify ScorerUnavailableError propagates as ScorerUnavailableError
        with pytest.raises(evaluator.ScorerUnavailableError, match="upstream judge down"):
            evaluator.evaluate(
                attempt_id="att_score_1",
                task={"id": "t1", "trigger_unavailable": True},
                env=isolated_env,
                data_path=data_path,
                scorer=isolated_env.scorer_module.score,
            )
    finally:
        runtime_state.reset()


def test_isolated_scorer_signature_filtering_and_evaluate_submission(tmp_path: Path):
    """Scorers without `events`/`**kwargs` and `evaluate_submission` with Path args work."""
    envs_root = tmp_path / "envs"
    data_path = tmp_path / "data"
    _create_synthetic_env(
        envs_root,
        name="strict-sig",
        core_code="""
from dataclasses import dataclass
from octagon.env_api import EnvContext, env_tool


@dataclass
class PingOut:
    ok: bool
    echo: str


@env_tool(
    name="async_ping",
    description="Async tool returning dataclass.",
    parameters={"type": "object", "properties": {"msg": {"type": "string"}}},
)
async def async_ping(ctx: EnvContext, msg: str = "hi") -> PingOut:
    return PingOut(ok=True, echo=msg)
""".lstrip(),
        scorer_code="""
from pathlib import Path
from typing import Any


def score(*, attempt_id: str, task: dict[str, Any], env_db: Path, trace: list, final_state: dict):
    assert isinstance(env_db, Path)
    return [{"dimension": "isolation", "value": 80, "detail": f"strict:{attempt_id}"}]


def evaluate_submission(*, attempt_id: str, workspace: Path, artifact_dir: Path, **_kw: Any):
    assert isinstance(workspace, Path)
    assert isinstance(artifact_dir, Path)
    return {"scores": [{"dimension": "isolation", "value": 85, "detail": str(workspace.name)}]}
""".lstrip(),
    )

    loaded = EnvLoader(envs_root).load_all(isolate=True)
    env = loaded["strict-sig"]
    assert env.isolated is True
    assert "_octagon_env_strict_sig_core" not in sys.modules
    assert "_octagon_env_strict_sig_scorer" not in sys.modules

    # Direct IsolatedRegisteredTool.acall with async dataclass-returning tool
    from octagon.env_api import EnvContext, TraceWriter

    ctx = EnvContext(
        attempt_id="att_async_1",
        env_session_id="sess_async",
        db=sqlite3.connect(":memory:"),
        trace=TraceWriter(
            data_path=data_path,
            attempt_id="att_async_1",
            env_session_id="sess_async",
        ),
    )
    try:
        res = asyncio.run(env.tools["async_ping"].acall(ctx, msg="pong"))
    finally:
        ctx.db.close()
    assert res == {"ok": True, "echo": "pong"}

    # Scorer without `events` parameter still succeeds via evaluator.evaluate
    outcome = evaluator.evaluate(
        attempt_id="att_async_1",
        task={"id": "t1"},
        env=env,
        data_path=data_path,
        scorer=env.scorer_module.score,
    )
    assert outcome.score_total == 80
    assert outcome.scores[0]["detail"] == "strict:att_async_1"

    # evaluate_submission preserves Path arguments across subprocess boundary
    ws = tmp_path / "ws_snap"
    art = tmp_path / "art_out"
    ws.mkdir()
    art.mkdir()
    sub_res = env.scorer_module.evaluate_submission(
        attempt_id="att_async_1",
        workspace=ws,
        artifact_dir=art,
    )
    assert sub_res["scores"][0]["value"] == 85
    assert sub_res["scores"][0]["detail"] == "ws_snap"


def test_real_checked_in_envs_load_isolated_and_in_process():
    repo_envs = Path(__file__).resolve().parents[1] / "envs"
    iso_envs = EnvLoader(repo_envs).load_all(allow_unavailable_core=True, isolate=True)
    assert "example-tool-use" in iso_envs
    assert iso_envs["example-tool-use"].isolated is True
    assert {"add_note", "list_notes"} <= set(iso_envs["example-tool-use"].tools)
    assert callable(iso_envs["example-tool-use"].scorer_module.score)

    in_proc = EnvLoader(repo_envs).load_all(allow_unavailable_core=True, isolate=False)
    assert in_proc["example-tool-use"].isolated is False
    assert {"add_note", "list_notes"} <= set(in_proc["example-tool-use"].tools)
