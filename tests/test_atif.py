"""ATIF-v1.7 还原（backend/atif）单元测试。

覆盖:
- schema:合法 trajectory / step_id 不连续 / source_call_id 不配对 / user step 带 agent 字段
- claude_code 转换:thinking→reasoning_content、tool_use→tool_calls、
  tool_result→observation 配对、isMeta 跳过、多 slug 选对 session
- codex 转换:session_meta/turn_context/token_count/response_item → header + steps + final_metrics
- emitter:codex 无 session → not_available;ready 产物过 schema
- API 路由:200 ready / 200 not_available / 404
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.atif.emitter import emit_attempt_atif
from backend.atif.schema import Agent, Step, Trajectory
from backend.db import _init_db_sync, _now_iso


def _attempt_dir(data: Path, attempt_id: str) -> Path:
    d = data / "attempts" / attempt_id
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------- schema -------------------------------------------------------------


def _valid_trajectory() -> Trajectory:
    return Trajectory(
        schema_version="ATIF-v1.7",
        trajectory_id="att_1",
        agent=Agent(name="claude-code", version="2.1.0"),
        steps=[
            Step(step_id=1, source="user", message="hello"),
            Step(
                step_id=2,
                source="agent",
                message="",
                reasoning_content="let me check",
                tool_calls=[{"tool_call_id": "call_1", "function_name": "Bash", "arguments": {}}],
                observation={"results": [{"source_call_id": "call_1", "content": "ok"}]},
            ),
        ],
    )


def test_schema_valid() -> None:
    traj = _valid_trajectory()
    assert traj.to_json_dict()["steps"][1]["reasoning_content"] == "let me check"


def test_schema_rejects_non_sequential_step_ids() -> None:
    traj = _valid_trajectory()
    traj.steps[1].step_id = 3  # 1, 3 —— 不连续
    with pytest.raises(ValidationError, match="sequential from 1"):
        Trajectory.model_validate(traj.model_dump(mode="json"))


def test_schema_rejects_dangling_source_call_id() -> None:
    traj = _valid_trajectory()
    traj.steps[1].observation.results[0].source_call_id = "call_missing"
    with pytest.raises(ValidationError, match="source_call_id"):
        Trajectory.model_validate(traj.model_dump(mode="json"))


def test_schema_rejects_agent_fields_on_user_step() -> None:
    base = _valid_trajectory().model_dump(mode="json")
    base["steps"][0]["tool_calls"] = [{"tool_call_id": "c", "function_name": "Bash", "arguments": {}}]
    with pytest.raises(ValidationError, match="source='agent'"):
        Trajectory.model_validate(base)


# ---------- claude-code 转换 ---------------------------------------------------


def _cc_session_line(event_type: str, **kwargs) -> str:
    msg = kwargs.pop("message", None)
    line: dict = {"type": event_type, "timestamp": kwargs.pop("timestamp", "2026-01-01T00:00:00Z")}
    if msg is not None:
        line["message"] = msg
    line.update(kwargs)
    return json.dumps(line, ensure_ascii=False)


def _cc_session() -> list[str]:
    return [
        _cc_session_line("user", message={"role": "user", "content": "compute sum"}),
        _cc_session_line(
            "assistant",
            message={
                "role": "assistant",
                "model": "claude-3-5-sonnet",
                "id": "msg_1",
                "content": [
                    {"type": "thinking", "thinking": "need bash"},
                    {"type": "tool_use", "id": "call_bash", "name": "Bash", "input": {"command": "echo 1"}},
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        ),
        _cc_session_line(
            "user",
            message={
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call_bash",
                        "content": [{"type": "text", "text": "1"}],
                    }
                ],
            },
        ),
        _cc_session_line("user", isMeta=True, message={"role": "user", "content": "keep going"}),
        _cc_session_line(
            "assistant",
            message={
                "role": "assistant",
                "model": "claude-3-5-sonnet",
                "id": "msg_2",
                "content": [{"type": "text", "text": "answer: 1"}],
            },
        ),
    ]


def _write_cc_sandbox(data: Path, attempt_id: str, *, home_root: str = ".cc-iso-home") -> Path:
    """构造 <home_root>/.claude/projects/<slug>/<session>.jsonl（沙箱 home_root=sandbox_home）。"""
    attempt_dir = _attempt_dir(data, attempt_id)
    cc = attempt_dir / home_root / ".claude" / "projects"
    (attempt_dir / "skill_workspace").mkdir(parents=True, exist_ok=True)
    workspace = (attempt_dir / "skill_workspace").resolve().as_posix()

    good_slug = cc / f"-home-x-attempts-{attempt_id}-skill-workspace"
    bad_slug = cc / "-home-x-memory"
    good_slug.mkdir(parents=True, exist_ok=True)
    bad_slug.mkdir(parents=True, exist_ok=True)
    lines = _cc_session()
    # 正确 slug 的 session：事件 cwd 指向 skill_workspace
    good_path = good_slug / f"{attempt_id}.jsonl"
    good_events = [
        {**json.loads(l), "cwd": workspace, "version": "2.1.246", "sessionId": "sess_main"}
        for l in lines
    ]
    good_path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in good_events))
    # 干扰 slug：不同 cwd，mtime 更新（验证按 cwd 而非最新选中）
    bad_path = bad_slug / f"{attempt_id}-other.jsonl"
    bad_events = [
        {**json.loads(l), "cwd": "/elsewhere", "sessionId": "sess_bad"}
        for l in lines
    ]
    bad_path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in bad_events))
    return attempt_dir


def test_cc_converter_builds_steps() -> None:
    data = Path(tempfile.mkdtemp())
    attempt_dir = _write_cc_sandbox(data, "att_cc1")

    from backend.atif.converters.claude_code import find_session_events, convert_events_to_trajectory

    events = find_session_events(attempt_dir / ".cc-iso-home", attempt_dir)
    traj = convert_events_to_trajectory(events, attempt_id="att_cc1")
    assert traj is not None
    # 3 steps：user → agent(tool，thinking+tool_use 同一次推理合并) → agent(text)；isMeta 被跳过
    assert [s.source for s in traj.steps] == ["user", "agent", "agent"]
    assert traj.steps[0].message == "compute sum"
    assert traj.steps[1].reasoning_content == "need bash"
    assert traj.steps[1].tool_calls[0].function_name == "Bash"
    assert traj.steps[1].observation.results[0].source_call_id == "call_bash"
    assert traj.steps[1].observation.results[0].content == "1"
    assert traj.steps[2].message == "answer: 1"
    assert traj.agent.name == "claude-code"
    assert traj.agent.version == "2.1.246"
    assert traj.agent.model_name == "claude-3-5-sonnet"
    # 产物过 schema（构造期已校验，这里再显式验一遍）
    Trajectory.model_validate(traj.model_dump(mode="json"))


def test_cc_converter_selects_correct_slug() -> None:
    data = Path(tempfile.mkdtemp())
    attempt_dir = _write_cc_sandbox(data, "att_cc2")

    from backend.atif.converters.claude_code import find_session_events

    events = find_session_events(attempt_dir / ".cc-iso-home", attempt_dir)
    # 应选 cwd 指向 skill_workspace 的 session，而不是 mtime 更新的 bad slug
    assert any(e.get("sessionId") == "sess_main" for e in events)
    assert not any(e.get("sessionId") == "sess_bad" for e in events)


# ---------- codex 转换 ---------------------------------------------------------


def _codex_session() -> list[dict]:
    return [
        {"type": "session_meta", "payload": {"id": "sess_codex", "cli_version": "0.42.0", "cwd": "/w"}},
        {"type": "turn_context", "payload": {"turn_id": "turn_1", "model": "gpt-5"}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn_1"}},
        {"type": "response_item", "payload": {"type": "reasoning", "summary": [{"text": "think step"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "text", "text": "calling"}]}},
        {"type": "response_item", "payload": {"type": "function_call", "call_id": "fc_1", "name": "shell", "arguments": '{"cmd": "ls"}'}},
        {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "fc_1", "output": {"output": "file.txt"}}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 50, "output_tokens": 20, "cached_input_tokens": 5, "total_tokens": 75}, "total_token_usage": {"input_tokens": 50, "output_tokens": 20, "cached_input_tokens": 5, "total_tokens": 75}}}},
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn_1"}},
    ]


def _write_codex_rollout(data: Path, attempt_id: str) -> Path:
    attempt_dir = _attempt_dir(data, attempt_id)
    session = attempt_dir / ".codex-iso-home" / "sessions" / "workspace1"
    session.mkdir(parents=True, exist_ok=True)
    (session / f"{attempt_id}.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in _codex_session())
    )
    return attempt_dir


def test_codex_converter_builds_steps() -> None:
    data = Path(tempfile.mkdtemp())
    attempt_dir = _write_codex_rollout(data, "att_cx1")

    from backend.atif.converters.codex import find_session_events, convert_events_to_trajectory

    events = find_session_events(attempt_dir / ".codex-iso-home")
    traj = convert_events_to_trajectory(events, attempt_id="att_cx1")
    assert traj is not None
    assert traj.agent.name == "codex"
    assert traj.agent.version == "0.42.0"
    assert traj.agent.model_name == "gpt-5"
    # 一个 bundled agent step：message + reasoning + tool_call + observation
    assert len(traj.steps) == 1
    step = traj.steps[0]
    assert step.source == "agent"
    assert step.message == "calling"
    assert step.reasoning_content == "think step"
    assert step.tool_calls[0].function_name == "shell"
    assert step.observation.results[0].content == "file.txt"
    assert step.metrics.prompt_tokens == 50
    assert traj.final_metrics.total_completion_tokens == 20
    Trajectory.model_validate(traj.model_dump(mode="json"))


# ---------- emitter ------------------------------------------------------------


def test_emitter_codex_no_session_not_available() -> None:
    data = Path(tempfile.mkdtemp())
    attempt_dir = _attempt_dir(data, "att_none")
    (attempt_dir / ".codex-iso-home").mkdir()  # 有 home 但无 sessions（历史单轮）
    out = emit_attempt_atif(attempt_dir, attempt_id="att_none")
    assert out.status == "not_available"
    assert "ephemeral" in (out.reason or "")
    assert out.trajectory is None


def test_emitter_cc_ready() -> None:
    data = Path(tempfile.mkdtemp())
    attempt_dir = _write_cc_sandbox(data, "att_ready")
    out = emit_attempt_atif(attempt_dir, attempt_id="att_ready")
    assert out.status == "ready"
    assert out.trajectory is not None
    assert out.trajectory["schema_version"] == "ATIF-v1.7"
    # ready 产物能过 schema 校验（validate_trajectory）
    from backend.atif.emitter import validate_trajectory

    validate_trajectory(out.trajectory)


def test_emitter_discovers_sandbox_home_cc() -> None:
    """沙箱模式：CC 转录在 sandbox_home/.claude/projects，emitter 应找到。"""
    data = Path(tempfile.mkdtemp())
    attempt_dir = _write_cc_sandbox(data, "att_sbx_cc", home_root="sandbox_home")
    out = emit_attempt_atif(attempt_dir, attempt_id="att_sbx_cc")
    assert out.status == "ready"
    assert out.trajectory is not None
    assert out.trajectory["agent"]["name"] == "claude-code"
    assert len(out.trajectory["steps"]) >= 2


def test_emitter_discovers_sandbox_home_codex() -> None:
    """沙箱模式：codex 会话在 sandbox_home/sessions，emitter 应找到并推断 codex。"""
    data = Path(tempfile.mkdtemp())
    attempt_dir = _attempt_dir(data, "att_sbx_cx")
    session = attempt_dir / "sandbox_home" / "sessions" / "w"
    session.mkdir(parents=True, exist_ok=True)
    (session / "rollout.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in _codex_session())
    )
    out = emit_attempt_atif(attempt_dir, attempt_id="att_sbx_cx")
    assert out.status == "ready"
    assert out.trajectory is not None
    assert out.trajectory["agent"]["name"] == "codex"
    assert out.trajectory["steps"][0]["source"] == "agent"


# ---------- API 路由 -----------------------------------------------------------


def _insert_run_attempt(db: Path, run_id: str, attempt_id: str) -> None:
    now = _now_iso()
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO tasks(id, env_name, prompt, created_at) VALUES(?,?,?,?)",
            ("t1", "demo", "do it", now),
        )
        conn.execute(
            "INSERT INTO runs(id, task_id, env_name, status, model, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (run_id, "t1", "demo", "queued", "gpt-5", now),
        )
        conn.execute(
            "INSERT INTO attempts(id, run_id, task_id, env_name, agent_name, model,"
            " status, env_session_id, env_token_hash, event_count, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,0,?)",
            (attempt_id, run_id, "t1", "demo", "claude-code", "claude-3-5-sonnet",
             "completed", f"env_{attempt_id}", "token", now),
        )
        conn.commit()


def _atif_client(db: Path, data: Path) -> TestClient:
    import backend.api as api

    class _State:
        db_path = db
        data_path = data

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(api.runtime_state, "get", lambda: _State())
    full = api.build_router()
    router = APIRouter()
    for route in full.routes:
        if "atif" in getattr(route, "path", ""):
            router.routes.append(route)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    client._mp = monkeypatch  # type: ignore[attr-defined]
    return client


def test_api_atif_ready() -> None:
    tmp = Path(tempfile.mkdtemp())
    db = tmp / "octagon.db"
    _init_db_sync(db)
    _insert_run_attempt(db, "run_1", "att_api1")
    _write_cc_sandbox(tmp, "att_api1")
    client = _atif_client(db, tmp)
    resp = client.get("/runs/run_1/attempts/att_api1/atif")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["trajectory"]["agent"]["name"] == "claude-code"


def test_api_atif_not_available() -> None:
    tmp = Path(tempfile.mkdtemp())
    db = tmp / "octagon.db"
    _init_db_sync(db)
    _insert_run_attempt(db, "run_1", "att_api2")
    attempt_dir = _attempt_dir(tmp, "att_api2")
    (attempt_dir / ".codex-iso-home").mkdir()
    client = _atif_client(db, tmp)
    resp = client.get("/runs/run_1/attempts/att_api2/atif")
    assert resp.status_code == 200
    assert resp.json()["status"] == "not_available"


def test_api_atif_attempt_not_found() -> None:
    tmp = Path(tempfile.mkdtemp())
    db = tmp / "octagon.db"
    _init_db_sync(db)
    client = _atif_client(db, tmp)
    resp = client.get("/runs/run_1/attempts/att_missing/atif")
    assert resp.status_code == 404


# ---------- /agents 沙箱感知 ---------------------------------------------------


class _BladeStub:
    api_key = "sk-blade-x"
    transport = "cli"
    cli_path = None


class _SettingsStub:
    blade = _BladeStub()


def _fake_sandbox_status(agent_tuple: tuple[str, ...]) -> Any:
    Image = type("Image", (), {"agents": agent_tuple})
    return type("Status", (), {"enabled": True, "ok": True, "image": Image()})()


def test_list_agents_sandbox_aware(monkeypatch) -> None:
    """沙箱 ok 时 6 个 agent 报 available（CLI 在镜像里，宿主机 PATH 没有）。"""
    import backend.api as api

    agents = ("claude-code", "codex", "kimi-code", "opencode", "mimo-code", "dsh")
    monkeypatch.setattr(
        api.runtime_state, "get",
        lambda: type("S", (), {"sandbox_status": _fake_sandbox_status(agents)})(),
    )
    listing = api._list_agents(_SettingsStub())
    by_name = {a["name"]: a for a in listing}
    for name in agents:
        assert by_name[name]["status"] == "available", name
        assert by_name[name]["locus"] == "docker-sandbox", name
    # blade 不在沙箱围栏内：transport=cli 且无 blade 二进制 → not_configured
    assert by_name["blade-agent"]["status"] == "not_configured"


def test_list_agents_sandbox_off_falls_back_to_host(monkeypatch) -> None:
    """沙箱未开/不可用：走宿主机 PATH 判据。"""
    import backend.api as api

    class _Status:
        enabled = False
        ok = False
        image = None

    monkeypatch.setattr(api.runtime_state, "get", lambda: type("S", (), {"sandbox_status": _Status()})())
    listing = api._list_agents(_SettingsStub())
    by_name = {a["name"]: a for a in listing}
    # 宿主机缺 kimi/opencode/mimo → not_found
    for name in ("kimi-code", "opencode", "mimo-code"):
        assert by_name[name]["status"] == "not_found", name
        assert by_name[name]["locus"] == "host"


# ---------- opencode / mimo / kimi 事件流型转换器 ----------------------------


def _write_events(attempt_dir: Path, events: list[dict]) -> Path:
    (attempt_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events)
    )
    return attempt_dir


def test_opencode_converter_groups_steps() -> None:
    data = Path(tempfile.mkdtemp())
    ad = _attempt_dir(data, "att_oc1")
    _write_events(ad, [
        {"type": "user", "sessionID": "ses_1", "payload": {"prompt": "sort numbers"}},
        {"type": "step_start", "sessionID": "ses_1", "timestamp": 1700000000000},
        {"type": "reasoning", "sessionID": "ses_1", "part": {"type": "reasoning", "text": "need bash"}},
        {"type": "tool_use", "sessionID": "ses_1", "part": {
            "type": "tool", "tool": "Bash", "callID": "c1",
            "state": {"input": {"command": "ls"}, "output": "file.txt"}}},
        {"type": "text", "sessionID": "ses_1", "part": {"type": "text", "text": "done"}},
        {"type": "step_finish", "sessionID": "ses_1", "part": {
            "tokens": {"input": 100, "output": 20, "cache": {"read": 5}}, "cost": 0.01}},
    ])
    out = emit_attempt_atif(ad, agent_name="opencode", attempt_id="att_oc1")
    assert out.status == "ready"
    steps = out.trajectory["steps"]
    assert [s["source"] for s in steps] == ["user", "agent"]
    assert steps[1]["reasoning_content"] == "need bash"
    assert steps[1]["tool_calls"][0]["function_name"] == "Bash"
    assert steps[1]["observation"]["results"][0]["content"] == "file.txt"
    assert steps[1]["metrics"]["prompt_tokens"] == 105  # 100 input + 5 cache read
    assert out.trajectory["final_metrics"]["total_cost_usd"] == 0.01


def test_opencode_converter_mimo_name() -> None:
    data = Path(tempfile.mkdtemp())
    ad = _attempt_dir(data, "att_m1")
    _write_events(ad, [
        {"type": "step_start", "sessionID": "ses_m", "timestamp": 1700000000000},
        {"type": "text", "sessionID": "ses_m", "part": {"type": "text", "text": "hi"}},
        {"type": "step_finish", "sessionID": "ses_m", "part": {}},
    ])
    out = emit_attempt_atif(ad, agent_name="mimo-code", attempt_id="att_m1")
    assert out.status == "ready"
    assert out.trajectory["agent"]["name"] == "mimo-code"


def test_kimi_converter_role_content() -> None:
    data = Path(tempfile.mkdtemp())
    ad = _attempt_dir(data, "att_k1")
    _write_events(ad, [
        {"role": "user", "content": "compute sum"},
        {"role": "meta", "type": "session.resume_hint", "session_id": "k_ses"},
        {"role": "thinking", "content": "use bash"},
        {"role": "assistant", "content": "checking",
         "tool_calls": [{"id": "t1", "name": "Bash", "arguments": {"command": "echo 1"}}]},
        {"role": "assistant", "content": "answer: 385"},
    ])
    out = emit_attempt_atif(ad, agent_name="kimi-code", attempt_id="att_k1")
    assert out.status == "ready"
    steps = out.trajectory["steps"]
    assert [s["source"] for s in steps] == ["user", "agent", "agent"]
    assert steps[1]["reasoning_content"] == "use bash"
    assert steps[1]["tool_calls"][0]["function_name"] == "Bash"
    assert steps[2]["message"] == "answer: 385"
    assert out.trajectory["session_id"] == "k_ses"


def test_events_agent_requires_explicit_agent_name() -> None:
    """kimi/opencode/mimo 读 events.jsonl，无 home 标记可判别 → 必须显式 agent。"""
    data = Path(tempfile.mkdtemp())
    ad = _attempt_dir(data, "att_anon")
    _write_events(ad, [{"type": "step_start", "sessionID": "s", "timestamp": 1700000000000}])
    out = emit_attempt_atif(ad, attempt_id="att_anon")  # 不传 agent_name
    assert out.status == "not_available"
