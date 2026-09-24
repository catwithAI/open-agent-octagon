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
    # cli_path=None 时 _list_agents 回落到 shutil.which("blade")（api.py:96），
    # 读的是**真实 PATH**——评测机上装了 blade 二进制，blade-agent 就被判成
    # available，下面那条断言随之落空。桩掉 which，让本用例只测判定逻辑、
    # 不测跑在哪台机器上。
    monkeypatch.setattr(api.shutil, "which", lambda _name: None)
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
    # 同上：宿主机 PATH 上真有什么二进制，不该决定这个用例的成败。
    monkeypatch.setattr(api.shutil, "which", lambda _name: None)
    listing = api._list_agents(_SettingsStub())
    by_name = {a["name"]: a for a in listing}
    # 宿主机缺 kimi/opencode/mimo → not_found
    for name in ("kimi-code", "opencode", "mimo-code"):
        assert by_name[name]["status"] == "not_found", name
        assert by_name[name]["locus"] == "host"


# ---------- opencode / mimo 事件流型转换器 -----------------------------------


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


def test_events_agent_requires_explicit_agent_name() -> None:
    """opencode/mimo 读 events.jsonl，无 home 标记可判别 → 必须显式 agent。"""
    data = Path(tempfile.mkdtemp())
    ad = _attempt_dir(data, "att_anon")
    _write_events(ad, [{"type": "step_start", "sessionID": "s", "timestamp": 1700000000000}])
    out = emit_attempt_atif(ad, attempt_id="att_anon")  # 不传 agent_name
    assert out.status == "not_available"


# ---------- dsh ----------------------------------------------------------------


def _dsh_session(*, turn_failed: bool = False, no_steps: bool = False) -> list[dict]:
    """一段最小但结构真实的 dsh 会话（对齐 att_24a673bb5a23 的实际形态）。"""
    events: list[dict] = [
        {"type": "session", "version": 0, "id": "octagon-att_dsh1",
         "createdAt": 1790153666682, "cwd": "/w/skill_workspace", "delegationDepth": 0},
        {"type": "turn/start", "seq": 1, "time": 1790153666687, "data": {"turn": 1}},
        {"type": "request/header", "seq": 6, "time": 1790153666713, "data": {"header": {
            "config": {"provider": "octagon", "model": "deepseek-4.1-flash", "maxTokens": 32768},
            "system": "You are an AI agent powered by DeepSeek Harness.",
            "tools": [{"name": "bash", "description": "run a command"}]}}},
        {"type": "request/context", "seq": 7, "time": 1790153666713,
         "data": {"provider": "octagon", "model": "deepseek-4.1-flash", "contextWindow": 200000}},
        {"type": "user/message", "seq": 4, "time": 1790153666710,
         "data": {"content": [{"type": "text", "text": "do the thing"}]}},
    ]
    if not no_steps:
        events += [
            {"type": "step/start", "seq": 3, "time": 1790153666710, "data": {"turn": 1, "step": 1}},
            # 流式增量：转换器必须忽略，否则内容会和 assistant/message 重复一遍。
            {"type": "reasoning-chunks", "seq0": 9, "time0": 1790153667925,
             "data": {"turn": 1, "step": 1, "texts": ["think", "ing"]}},
            {"type": "text-chunks", "seq0": 10, "time0": 1790153667926,
             "data": {"turn": 1, "step": 1, "texts": ["say", "ing"]}},
            {"type": "assistant/message", "seq": 127, "time": 1790153669948, "data": {
                "turn": 1, "step": 1, "usage": {"inputTokens": 100, "outputTokens": 40},
                "message": {"role": "assistant", "id": "m1", "content": [
                    {"type": "reasoning", "text": "thinking"},
                    {"type": "text", "text": "saying"},
                    {"type": "tool-call", "id": "call_1", "name": "bash",
                     "arguments": '{"command":"ls -la"}'}]}}},
            {"type": "tool/call", "seq": 128, "time": 1790153669949, "data": {
                "turn": 1, "step": 1, "callId": "call_1", "name": "bash",
                "arguments": '{"command":"ls -la"}'}},
            {"type": "tool/result", "seq": 129, "time": 1790153670394, "data": {
                "turn": 1, "step": 1, "message": {
                    "source": {"kind": "tool", "callId": "call_1"},
                    "content": [{"type": "tool-result", "toolCallId": "call_1",
                                 "content": [{"type": "text", "text": "total 0"}]}]}}},
            {"type": "step/end", "seq": 132, "time": 1790153670405, "data": {"turn": 1, "step": 1}},
        ]
    reason = ({"kind": "error", "error": {"message": "404: model_not_found"}}
              if turn_failed else {"kind": "completed"})
    events.append({"type": "turn/end", "seq": 999, "time": 1790153919354,
                   "data": {"turn": 1, "reason": reason}})
    return events


def _write_dsh_session(data: Path, attempt_id: str, events: list[dict], *,
                       sandbox: bool = True) -> Path:
    attempt_dir = _attempt_dir(data, attempt_id)
    root = attempt_dir / ("sandbox_home" if sandbox else ".")
    session = root / "dsh_sessions" / "--w-skill_workspace--" / f"octagon-{attempt_id}"
    session.mkdir(parents=True, exist_ok=True)
    (session / "session.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events), encoding="utf-8")
    return attempt_dir


def test_dsh_converter_builds_steps() -> None:
    data = Path(tempfile.mkdtemp())
    attempt_dir = _write_dsh_session(data, "att_dsh1", _dsh_session())

    from backend.atif.converters.dsh import (
        convert_events_to_trajectory,
        find_session_events,
    )

    events = find_session_events(attempt_dir / "sandbox_home", "att_dsh1")
    trajectory = convert_events_to_trajectory(events, attempt_id="att_dsh1")
    assert trajectory is not None
    Trajectory.model_validate(trajectory.to_json_dict())

    assert trajectory.agent.name == "dsh"
    assert trajectory.agent.model_name == "deepseek-4.1-flash"
    assert trajectory.agent.tool_definitions == [{"name": "bash", "description": "run a command"}]
    assert trajectory.agent.extra["provider"] == "octagon"

    assert len(trajectory.steps) == 1
    step = trajectory.steps[0]
    assert step.source == "agent"
    # 流式 chunk 被忽略：内容只来自 assistant/message，没有重复。
    assert step.reasoning_content == "thinking"
    assert step.message == "saying"
    assert step.tool_calls is not None and len(step.tool_calls) == 1
    assert step.tool_calls[0].function_name == "bash"
    # arguments 是 JSON 字符串，必须解析成 dict（schema 要求）。
    assert step.tool_calls[0].arguments == {"command": "ls -la"}
    assert step.observation is not None
    assert step.observation.results[0].source_call_id == "call_1"
    assert step.observation.results[0].content == "total 0"
    assert step.metrics is not None and step.metrics.prompt_tokens == 100
    assert trajectory.final_metrics.total_completion_tokens == 40


def test_dsh_converter_keys_steps_by_turn_and_step() -> None:
    """step 编号在每个 turn 内从 1 重来——只按 step 归并会把两轮并成一步。"""
    data = Path(tempfile.mkdtemp())
    events = _dsh_session()
    second_turn = [
        {"type": "step/start", "seq": 200, "time": 1790153680000, "data": {"turn": 2, "step": 1}},
        {"type": "assistant/message", "seq": 201, "time": 1790153680001, "data": {
            "turn": 2, "step": 1, "usage": {"inputTokens": 7, "outputTokens": 3},
            "message": {"role": "assistant", "id": "m2",
                        "content": [{"type": "text", "text": "second turn"}]}}},
        {"type": "step/end", "seq": 202, "time": 1790153680002, "data": {"turn": 2, "step": 1}},
    ]
    attempt_dir = _write_dsh_session(data, "att_dsh2", events + second_turn)

    from backend.atif.converters.dsh import (
        convert_events_to_trajectory,
        find_session_events,
    )

    trajectory = convert_events_to_trajectory(
        find_session_events(attempt_dir / "sandbox_home", "att_dsh2"), attempt_id="att_dsh2")
    assert trajectory is not None
    assert [s.step_id for s in trajectory.steps] == [1, 2]
    assert trajectory.steps[1].message == "second turn"
    assert trajectory.steps[0].extra["turn"] == 1
    assert trajectory.steps[1].extra["turn"] == 2


def test_dsh_converter_tolerates_unparsable_arguments() -> None:
    """畸形 arguments 不能毁掉整条 trajectory——包进 _raw 保留原文。"""
    data = Path(tempfile.mkdtemp())
    events = _dsh_session()
    for e in events:
        if e.get("type") == "tool/call":
            e["data"]["arguments"] = "{not json"
    attempt_dir = _write_dsh_session(data, "att_dsh3", events)

    from backend.atif.converters.dsh import (
        convert_events_to_trajectory,
        find_session_events,
    )

    trajectory = convert_events_to_trajectory(
        find_session_events(attempt_dir / "sandbox_home", "att_dsh3"), attempt_id="att_dsh3")
    assert trajectory is not None
    assert trajectory.steps[0].tool_calls[0].arguments == {"_raw": "{not json"}


def test_emitter_dsh_ready_from_sandbox_home() -> None:
    data = Path(tempfile.mkdtemp())
    _write_dsh_session(data, "att_dsh4", _dsh_session())
    out = emit_attempt_atif(data / "attempts" / "att_dsh4")
    assert out.status == "ready", out.reason
    assert out.trajectory["agent"]["name"] == "dsh"
    Trajectory.model_validate(out.trajectory)


def test_emitter_dsh_reports_turn_error_instead_of_missing_transcript() -> None:
    """转录在、但那一轮以错误收场——理由必须说出真实原因。

    报成「no usable session transcript」会把上游/设施故障伪装成采集缺陷，
    而把设施故障误读成 agent 表现正是本项目反复踩的坑。
    """
    data = Path(tempfile.mkdtemp())
    _write_dsh_session(data, "att_dsh5", _dsh_session(no_steps=True, turn_failed=True))
    out = emit_attempt_atif(data / "attempts" / "att_dsh5")
    assert out.status == "not_available"
    assert "session transcript present" in out.reason
    assert "404: model_not_found" in out.reason
    assert "no usable session transcript" not in out.reason


def test_emitter_dsh_missing_transcript_stays_generic() -> None:
    """转录真的不存在时，仍报采集缺失——两类原因不能混为一谈。"""
    data = Path(tempfile.mkdtemp())
    attempt_dir = _attempt_dir(data, "att_dsh6")
    (attempt_dir / "sandbox_home" / "dsh_sessions").mkdir(parents=True)
    out = emit_attempt_atif(attempt_dir)
    assert out.status == "not_available"
    assert "no usable session transcript" in out.reason


# ---------- kimi-code ----------------------------------------------------------


def _kimi_wire(*, no_steps: bool = False) -> list[dict]:
    """一段最小但结构真实的 kimi wire（对齐 att_5b9d7a896d8c 的实际形态）。"""
    events: list[dict] = [
        {"protocol_version": "1.10", "type": "metadata"},
        {"timestamp": 1790153668.25, "message": {
            "type": "TurnBegin", "payload": {"user_input": "do the thing"}}},
    ]
    if no_steps:
        events.append({"timestamp": 1790153918.17,
                       "message": {"type": "TurnEnd", "payload": {}}})
        return events
    events += [
        {"timestamp": 1790153668.26, "message": {"type": "StepBegin", "payload": {"n": 1}}},
        {"timestamp": 1790153670.23, "message": {"type": "ContentPart", "payload": {
            "type": "think", "think": "thinking", "encrypted": None}}},
        {"timestamp": 1790153670.50, "message": {"type": "ContentPart", "payload": {
            "type": "text", "text": "saying"}}},
        {"timestamp": 1790153671.21, "message": {"type": "ToolCall", "payload": {
            "type": "function", "id": "call_1",
            "function": {"name": "Shell", "arguments": '{"command": "ls -la"}'}}}},
        {"timestamp": 1790153671.22, "message": {"type": "StatusUpdate", "payload": {
            "context_tokens": 12087, "max_context_tokens": 200000,
            "token_usage": {"input_other": 12087, "output": 121,
                            "input_cache_read": 64, "input_cache_creation": 0}}}},
        {"timestamp": 1790153671.49, "message": {"type": "ToolResult", "payload": {
            "tool_call_id": "call_1",
            "return_value": {"is_error": False, "output": "total 0", "display": "",
                             "message": "", "extras": {}}}}},
        {"timestamp": 1790153918.17, "message": {"type": "TurnEnd", "payload": {}}},
    ]
    return events


def _write_kimi_session(data: Path, attempt_id: str, wire: list[dict], *,
                        session_id: str = "sess-uuid",
                        manifest_session: str | None = "sess-uuid",
                        system_prompt: str | None = "You are Kimi Code CLI.") -> Path:
    attempt_dir = _attempt_dir(data, attempt_id)
    kimi = attempt_dir / "sandbox_home" / ".kimi"
    session = kimi / "sessions" / "workdirhash" / session_id
    session.mkdir(parents=True, exist_ok=True)
    (session / "wire.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in wire), encoding="utf-8")
    if system_prompt is not None:
        (session / "context.jsonl").write_text(
            json.dumps({"role": "_system_prompt", "content": system_prompt},
                       ensure_ascii=False), encoding="utf-8")
    if manifest_session is not None:
        (kimi / "kimi.json").write_text(json.dumps(
            {"work_dirs": [{"path": "/w", "kaos": "local",
                            "last_session_id": manifest_session}]}), encoding="utf-8")
    return attempt_dir


def test_kimi_converter_builds_steps() -> None:
    data = Path(tempfile.mkdtemp())
    attempt_dir = _write_kimi_session(data, "att_km1", _kimi_wire())

    from backend.atif.converters.kimi_code import (
        convert_events_to_trajectory,
        find_session_events,
        find_system_prompt,
    )

    home = attempt_dir / "sandbox_home"
    trajectory = convert_events_to_trajectory(
        find_session_events(home), attempt_id="att_km1",
        system_prompt=find_system_prompt(home))
    assert trajectory is not None
    Trajectory.model_validate(trajectory.to_json_dict())

    assert trajectory.agent.name == "kimi-code"
    # 转录里没有模型名——宁可留空也不猜。
    assert trajectory.agent.model_name is None
    assert trajectory.agent.extra["wire_protocol_version"] == "1.10"
    assert trajectory.agent.extra["system_prompt"] == "You are Kimi Code CLI."

    assert len(trajectory.steps) == 1
    step = trajectory.steps[0]
    assert step.reasoning_content == "thinking"
    assert step.message == "saying"
    assert step.tool_calls[0].function_name == "Shell"
    assert step.tool_calls[0].arguments == {"command": "ls -la"}
    assert step.observation.results[0].source_call_id == "call_1"
    assert step.observation.results[0].content == "total 0"
    # StatusUpdate 上报的是**真实**分步 token，不是累计值做差。
    assert step.metrics.prompt_tokens == 12087
    assert step.metrics.completion_tokens == 121
    assert step.metrics.cached_tokens == 64
    assert step.timestamp.startswith("2026-")


def test_kimi_converter_picks_session_from_manifest() -> None:
    """kimi.json 的 last_session_id 指哪个，就读哪个——不靠 mtime 猜。"""
    data = Path(tempfile.mkdtemp())
    attempt_dir = _write_kimi_session(data, "att_km2", _kimi_wire(),
                                      session_id="wanted", manifest_session="wanted")
    # 再塞一个更晚写入的干扰会话。
    other = attempt_dir / "sandbox_home" / ".kimi" / "sessions" / "workdirhash" / "decoy"
    other.mkdir(parents=True, exist_ok=True)
    decoy = [{"protocol_version": "9.9", "type": "metadata"},
             {"timestamp": 1790153999.0,
              "message": {"type": "StepBegin", "payload": {"n": 1}}},
             {"timestamp": 1790153999.1, "message": {"type": "ContentPart",
                                                     "payload": {"type": "text", "text": "decoy"}}}]
    (other / "wire.jsonl").write_text(
        "\n".join(json.dumps(e) for e in decoy), encoding="utf-8")

    from backend.atif.converters.kimi_code import find_session_dir

    assert find_session_dir(attempt_dir / "sandbox_home").name == "wanted"


def test_kimi_converter_tolerates_unparsable_arguments() -> None:
    data = Path(tempfile.mkdtemp())
    wire = _kimi_wire()
    for e in wire:
        msg = e.get("message") or {}
        if msg.get("type") == "ToolCall":
            msg["payload"]["function"]["arguments"] = "{not json"
    attempt_dir = _write_kimi_session(data, "att_km3", wire)

    from backend.atif.converters.kimi_code import (
        convert_events_to_trajectory,
        find_session_events,
    )

    trajectory = convert_events_to_trajectory(
        find_session_events(attempt_dir / "sandbox_home"), attempt_id="att_km3")
    assert trajectory.steps[0].tool_calls[0].arguments == {"_raw": "{not json"}


def test_emitter_kimi_ready() -> None:
    data = Path(tempfile.mkdtemp())
    _write_kimi_session(data, "att_km4", _kimi_wire())
    out = emit_attempt_atif(data / "attempts" / "att_km4")
    assert out.status == "ready", out.reason
    assert out.trajectory["agent"]["name"] == "kimi-code"
    Trajectory.model_validate(out.trajectory)


def test_emitter_kimi_reports_no_stepbegin() -> None:
    """wire 在、但那一轮没走到模型——理由要说出这件事，不能报成转录缺失。"""
    data = Path(tempfile.mkdtemp())
    _write_kimi_session(data, "att_km5", _kimi_wire(no_steps=True))
    out = emit_attempt_atif(data / "attempts" / "att_km5")
    assert out.status == "not_available"
    assert "no StepBegin" in out.reason
    assert "no usable session transcript" not in out.reason


def test_emitter_explicit_agent_name_wins_over_inference() -> None:
    """显式 agent_name 是权威值；API 从库里取，不该退回目录推断。"""
    data = Path(tempfile.mkdtemp())
    _write_kimi_session(data, "att_km6", _kimi_wire())
    out = emit_attempt_atif(data / "attempts" / "att_km6", agent_name="kimi-code")
    assert out.status == "ready", out.reason
    # 传一个该目录里没有痕迹的 agent：不应被目录标记"纠正"回 kimi。
    other = emit_attempt_atif(data / "attempts" / "att_km6", agent_name="codex")
    assert other.status == "not_available"


# ---------- blade-agent --------------------------------------------------------


def _blade_events(*, no_assistant: bool = False, only_context: bool = False) -> list[dict]:
    """一段最小但结构真实的 blade-agent 会话（对齐 att_8455a549f371 的实际形态）。"""
    if only_context:
        return [
            {"timestamp": "2026-09-23T08:54:26.486637+00:00", "type": "context",
             "role": "user", "content": "<platform-services>…"},
            {"timestamp": "2026-09-23T08:54:26.487068+00:00", "type": "memory_inject",
             "role": "user", "content": "…"},
        ]
    events: list[dict] = [
        # 平台注入的上下文快照：不是 agent 的行为，不该计入步骤
        {"timestamp": "2026-09-23T08:54:26.486637+00:00", "type": "context",
         "role": "user", "content": "<network-reach>…"},
        {"timestamp": "2026-09-23T08:54:26.489047+00:00", "type": "message",
         "role": "user", "content": "Prepare a structured Excel profit and loss report."},
    ]
    if no_assistant:
        return events
    events += [
        {"timestamp": "2026-09-23T08:54:30.239269+00:00", "type": "message",
         "role": "assistant", "content": "I'll examine the reference file.",
         "tool_calls": [{"id": "call_a", "function": {
             "name": "Bash", "arguments": '{"command":"ls -la","description":"列目录"}'}}]},
        {"timestamp": "2026-09-23T08:54:30.748333+00:00", "type": "message",
         "role": "tool",
         "content": '{"command":"ls -la","exit_code":0,"output":"total 28"}'},
        {"timestamp": "2026-09-23T08:54:40.000000+00:00", "type": "message",
         "role": "assistant", "content": "Report written.",
         "tool_calls": [{"id": "call_b", "function": {
             "name": "Write", "arguments": "not-json-at-all"}}]},
        {"timestamp": "2026-09-23T08:54:41.000000+00:00", "type": "message",
         "role": "tool", "content": '{"exit_code":0,"output":"ok"}'},
    ]
    return events


def test_blade_agent_converter_builds_steps() -> None:
    from backend.atif.converters.blade_agent import convert_events_to_trajectory

    t = convert_events_to_trajectory(_blade_events(), attempt_id="att_b1")
    assert t is not None
    assert [s.source for s in t.steps] == ["user", "agent", "agent"]
    assert t.agent.name == "blade-agent"
    assert t.final_metrics.total_steps == 3
    # step_id 连续
    assert [s.step_id for s in t.steps] == [1, 2, 3]


def test_blade_agent_context_events_are_not_behavior() -> None:
    """平台注入的 context / memory_inject 是注入，不是 agent 做的事。"""
    from backend.atif.converters.blade_agent import convert_events_to_trajectory

    t = convert_events_to_trajectory(_blade_events(), attempt_id="att_b2")
    assert all("network-reach" not in (s.message or "") for s in t.steps)


def test_blade_agent_pairs_tool_results_by_order() -> None:
    """结果事件不带 tool_call_id —— 按顺序与上一步的调用配对。"""
    from backend.atif.converters.blade_agent import convert_events_to_trajectory

    t = convert_events_to_trajectory(_blade_events(), attempt_id="att_b3")
    first_agent = t.steps[1]
    assert first_agent.tool_calls[0].function_name == "Bash"
    assert first_agent.tool_calls[0].arguments["command"] == "ls -la"
    assert first_agent.observation.results[0].source_call_id == "call_a"
    assert "total 28" in first_agent.observation.results[0].content


def test_blade_agent_tolerates_unparsable_arguments() -> None:
    """arguments 解不开就原样留着 —— 丢掉这次调用比留个原始串更糟。"""
    from backend.atif.converters.blade_agent import convert_events_to_trajectory

    t = convert_events_to_trajectory(_blade_events(), attempt_id="att_b4")
    assert t.steps[2].tool_calls[0].arguments == {"_raw": "not-json-at-all"}


def test_emitter_blade_agent_ready(tmp_path) -> None:
    attempt_dir = tmp_path / "attempts" / "att_b5"
    attempt_dir.mkdir(parents=True)
    (attempt_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in _blade_events()),
        encoding="utf-8")
    out = emit_attempt_atif(attempt_dir, agent_name="blade-agent", attempt_id="att_b5")
    assert out.status == "ready", out.reason
    assert out.trajectory["agent"]["name"] == "blade-agent"


def test_emitter_blade_agent_reports_real_reason(tmp_path) -> None:
    """「转录在但没 agent 行为」不能说成「没有转录」。"""
    attempt_dir = tmp_path / "attempts" / "att_b6"
    attempt_dir.mkdir(parents=True)
    (attempt_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in _blade_events(only_context=True)),
        encoding="utf-8")
    out = emit_attempt_atif(attempt_dir, agent_name="blade-agent", attempt_id="att_b6")
    assert out.status == "not_available"
    assert "无 message 事件" in out.reason
    assert "no usable event transcript" not in out.reason
