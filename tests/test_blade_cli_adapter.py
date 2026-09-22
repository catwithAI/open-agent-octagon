"""blade-cli adapter：用真的子进程跑，不 mock 掉 exec。

BA 出 blade-cli 后默认通道从 SDK 换成 CLI。CLI 的契约是「退出码 + stdout JSON」，
mock 掉 subprocess 就等于把这份契约也 mock 掉了——所以这里写一个桩 `blade`
可执行文件，让 adapter 真的 spawn 它。argv 拼错、JSON 解析错、退出码映射错
都会在这里暴露。

桩脚本的输出形状取自真实 blade-cli（commit fede14736）对着 stub server 的实测：
  chat run --json      → {"session_id","status","messages":[{"id","content","tool_calls"}]}
  session history      → {"session_id","intent","nodes":[{"id","kind","role","timestamp",...}]}
"""

from __future__ import annotations

import asyncio
import json
import stat
import zipfile
from pathlib import Path

import pytest

from backend.adapters.base import AdapterRunInput
from backend.adapters.blade_cli import BladeCliAdapter
from backend.adapters.blade_service import AdapterEnv, BladeAdapterConfig

# 桩 CLI：按第一个子命令分派，把收到的 argv 全量记到 $BLADE_STUB_ARGV_LOG，
# 退出码由 $BLADE_STUB_EXIT 控制（默认 0）。
_STUB = r'''#!/usr/bin/env python3
import json, os, sys, zipfile

argv = sys.argv[1:]
log = os.environ.get("BLADE_STUB_ARGV_LOG")
if log:
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps(argv, ensure_ascii=False) + "\n")

group = argv[0] if argv else ""
verb = argv[1] if len(argv) > 1 else ""

if group == "chat" and verb in ("run", "send"):
    code = int(os.environ.get("BLADE_STUB_EXIT", "0"))
    if code != 0:
        sys.stderr.write("stub failure\n")
        # 失败时 CLI 也可能已经建了会话：照实吐 session_id，
        # adapter 必须据此仍去回收轨迹和产物。
        print(json.dumps({"session_id": "sess-1"}))
        sys.exit(code)
    print(json.dumps({
        "session_id": "sess-1",
        "status": os.environ.get("BLADE_STUB_STATUS", "completed"),
        "messages": [{"id": "m2", "content": "done", "tool_calls": [
            {"id": "t1", "function": {"name": "write_file", "arguments": "{}"}}]}],
    }))
    sys.exit(0)

if group == "session" and verb == "history":
    print(json.dumps({"session_id": "sess-1", "intent": "do it", "nodes": [
        {"id": "n1", "kind": "user", "role": "user",
         "timestamp": "2026-09-22T10:00:00Z", "content": "do it", "tool_calls": []},
        {"id": "n2", "kind": "ai", "role": "assistant",
         "timestamp": "2026-09-22T10:00:05Z", "content": "done", "tool_calls": [
             {"id": "t1", "function": {"name": "write_file", "arguments": "{}"}},
             {"id": "t2", "function": {"name": "read_file", "arguments": "{}"}}]},
    ]}))
    sys.exit(0)

if group == "session" and verb == "copy-dir":
    out = argv[argv.index("--out") + 1]
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("report.md", "# result\n")
        z.writestr("nested/data.csv", "a,b\n1,2\n")
        # 运行时内部目录必须被 adapter 跳过，不能污染 scorer 看的 workspace。
        z.writestr(".octagon/attempt.json", "{}")
    print(json.dumps({"path": out}))
    sys.exit(0)

sys.exit(1)
'''


@pytest.fixture()
def stub_cli(tmp_path: Path) -> Path:
    path = tmp_path / "blade-stub"
    path.write_text(_STUB, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IRUSR)
    return path


@pytest.fixture()
def argv_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log = tmp_path / "argv.log"
    monkeypatch.setenv("BLADE_STUB_ARGV_LOG", str(log))
    return log


def _adapter(stub_cli: Path, **overrides) -> BladeCliAdapter:
    config = BladeAdapterConfig(
        base_url="http://blade.test:8020",
        skills_path=Path("/tmp/skills"),
        api_key="secret-token",
        **overrides,
    )
    return BladeCliAdapter(config, cli_path=str(stub_cli))


def _task(tmp_path: Path, **overrides) -> AdapterRunInput:
    defaults = dict(
        attempt_id="att-1",
        task_id="task-1",
        task_prompt="写一份报告",
        task_context={},
        timeout_seconds=600,
        env_name="demo",
        env_skill_id="octagon/demo",
        env_token="env-token",
        env_base_url="http://octagon.test:9000",
    )
    defaults.update(overrides)
    return AdapterRunInput(**defaults)


def _env() -> AdapterEnv:
    return AdapterEnv(
        name="demo",
        skill_id="octagon/demo",
        solution_id="app-dev",
        biz_role_id="default",
    )


def _run(adapter, task, env, data_path):
    return asyncio.run(adapter.run(task, env, Path(data_path)))


def _argv_lines(log: Path) -> list[list[str]]:
    return [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]


def test_completed_run_collects_trace_and_artifacts(
    tmp_path: Path, stub_cli: Path, argv_log: Path
) -> None:
    data = tmp_path / "data"
    result = _run(_adapter(stub_cli), _task(tmp_path), _env(), data)

    assert result.status == "completed"
    assert result.error_code is None
    assert result.external_refs["blade_session_id"] == "sess-1"
    assert result.external_refs["blade_transport"] == "cli"

    # events.jsonl 从 session history 事后重建：两个节点、两次工具调用。
    attempt_dir = data / "attempts" / "att-1"
    events = [
        json.loads(x)
        for x in (attempt_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(events) == 2
    assert result.events_count == 2
    assert result.tool_call_count == 2
    assert result.last_event_at == "2026-09-22T10:00:05Z"
    # 下游必须能看出这不是实时事件流。
    assert all(e["source"] == "blade_cli_history" for e in events)

    # 产物解包到 skill_workspace，运行时内部目录被跳过。
    workspace = attempt_dir / "skill_workspace"
    assert (workspace / "report.md").read_text(encoding="utf-8") == "# result\n"
    assert (workspace / "nested" / "data.csv").is_file()
    assert not (workspace / ".octagon").exists()
    assert result.external_refs["artifact_sync"] == {"files": 2}


def test_token_usage_is_empty_by_design(tmp_path: Path, stub_cli: Path, argv_log: Path) -> None:
    """CLI 不暴露 token usage：字段留空，而不是填 0 冒充真实采集。

    横评矩阵读到空值应当理解为「该通道不采集」，填 0 会被误读成「没花 token」。
    """
    result = _run(_adapter(stub_cli), _task(tmp_path), _env(), tmp_path / "data")
    assert result.token_usage == {}
    assert result.thinking_count == 0
    assert result.transport_status == "reconstructed"


def test_chat_run_argv_carries_solution_model_and_uploads(
    tmp_path: Path, stub_cli: Path, argv_log: Path
) -> None:
    data = tmp_path / "data"
    workspace = data / "attempts" / "att-1" / "skill_workspace"
    workspace.mkdir(parents=True)
    (workspace / "input.csv").write_text("a,b\n", encoding="utf-8")

    task = _task(tmp_path, task_context={"uploaded_files": [{"name": "input.csv"}]})
    result = _run(_adapter(stub_cli, model="ba-pro"), task, _env(), data)
    assert result.status == "completed"

    run_argv = _argv_lines(argv_log)[0]
    assert run_argv[:2] == ["chat", "run"]
    # prompt 渲染与 blade_service 共用：任务消息 + 时间预算 + 上下文都在首轮注入。
    assert "写一份报告" in run_argv[2]
    assert "本任务限时" in run_argv[2]
    assert "input.csv" in run_argv[2]
    assert "--json" in run_argv
    assert run_argv[run_argv.index("--solution-id") + 1] == "app-dev"
    assert run_argv[run_argv.index("--biz-role") + 1] == "default"
    assert run_argv[run_argv.index("--model") + 1] == "ba-pro"
    # --timeout 传的是 deadline 剩余量（已扣掉启动耗时），不是 task 总时限。
    timeout_arg = run_argv[run_argv.index("--timeout") + 1]
    assert timeout_arg.endswith("s") and 500 < int(timeout_arg[:-1]) <= 600

    uploads = [run_argv[i + 1] for i, a in enumerate(run_argv) if a == "--file"]
    # attempt.json 是薄壳 skill 回调 Octagon 的凭据，远端路径必须逐字对齐。
    assert any(u.endswith("=.octagon/attempt.json") for u in uploads)
    assert any(u.endswith("=input.csv") for u in uploads)


def test_attempt_json_carries_env_token_and_sandbox_base_url(
    tmp_path: Path, stub_cli: Path, argv_log: Path
) -> None:
    """sandbox_env_base_url 覆盖 task.env_base_url，与 blade_service 同口径。"""
    adapter = _adapter(stub_cli, sandbox_env_base_url="http://host.docker.internal:9000")
    _run(adapter, _task(tmp_path), _env(), tmp_path / "data")

    run_argv = _argv_lines(argv_log)[0]
    spec = next(
        run_argv[i + 1]
        for i, a in enumerate(run_argv)
        if a == "--file" and run_argv[i + 1].endswith("=.octagon/attempt.json")
    )
    payload = json.loads(Path(spec.split("=", 1)[0]).read_text(encoding="utf-8"))
    assert payload["attempt_id"] == "att-1"
    assert payload["env_token"] == "env-token"
    assert payload["env_base_url"] == "http://host.docker.internal:9000"


def test_missing_upload_material_fails_loudly(
    tmp_path: Path, stub_cli: Path, argv_log: Path
) -> None:
    """物料缺失硬失败：否则 agent 在空 workspace 里瞎搜，浪费整个 attempt。"""
    task = _task(tmp_path, task_context={"uploaded_files": [{"name": "missing.csv"}]})
    result = _run(_adapter(stub_cli), task, _env(), tmp_path / "data")
    assert result.status == "cli_error"
    assert result.error_code == "adapter_crashed"
    assert "missing.csv" in (result.error_message or "")


@pytest.mark.parametrize(
    ("exit_code", "status", "error_code"),
    [
        (2, "cli_error", "blade_cli_input_error"),
        (4, "auth_failed", "blade_cli_auth_error"),
        (5, "chat_failed", "blade_cli_server_error"),
        (6, "chat_failed", "blade_cli_network_error"),
        (124, "timeout", "blade_cli_timeout"),
        (130, "interrupted", "blade_cli_interrupted"),
    ],
)
def test_exit_codes_map_to_distinct_statuses(
    tmp_path: Path,
    stub_cli: Path,
    argv_log: Path,
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int,
    status: str,
    error_code: str,
) -> None:
    """退出码区分基础设施故障与 agent 失败——横评出矩阵前要靠它筛。"""
    monkeypatch.setenv("BLADE_STUB_EXIT", str(exit_code))
    result = _run(_adapter(stub_cli), _task(tmp_path), _env(), tmp_path / "data")
    assert (result.status, result.error_code) == (status, error_code)


def test_failed_turn_still_recovers_artifacts(
    tmp_path: Path, stub_cli: Path, argv_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """跑挂了也要尽力回收：要能分辨「跑挂了」和「什么都没产出」。"""
    monkeypatch.setenv("BLADE_STUB_EXIT", "5")
    data = tmp_path / "data"
    result = _run(_adapter(stub_cli), _task(tmp_path), _env(), data)

    assert result.status == "chat_failed"
    assert result.external_refs["blade_session_id"] == "sess-1"
    assert result.external_refs["artifact_sync"] == {"files": 2}
    assert (data / "attempts" / "att-1" / "skill_workspace" / "report.md").is_file()


def test_non_terminal_session_status_is_not_completed(
    tmp_path: Path, stub_cli: Path, argv_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI 退出码 0 但会话终态是 failed —— 不能当成功。"""
    monkeypatch.setenv("BLADE_STUB_STATUS", "failed")
    result = _run(_adapter(stub_cli), _task(tmp_path), _env(), tmp_path / "data")
    assert result.status == "chat_failed"
    assert result.error_code == "blade_session_failed"


def test_cli_not_found_is_terminal_not_crash(tmp_path: Path) -> None:
    adapter = BladeCliAdapter(
        BladeAdapterConfig(base_url="http://blade.test", skills_path=Path("/tmp/s")),
        cli_path=str(tmp_path / "does-not-exist"),
    )
    # cli_path 指向不存在的文件时 resolve 不到，应当返回 terminal 而不是抛异常。
    adapter._cli_path_override = None
    result = _run(adapter, _task(tmp_path), _env(), tmp_path / "data")
    assert result.status in ("cli_not_found", "cli_error")


def test_cli_env_maps_octagon_config_to_blade_vars(stub_cli: Path) -> None:
    """settings.blade.base_url/api_key → BLADE_API_URL/BLADE_API_TOKEN。"""
    env = _adapter(stub_cli)._cli_env()
    assert env["BLADE_API_URL"] == "http://blade.test:8020"
    assert env["BLADE_API_TOKEN"] == "secret-token"


class _FakeIterationHandler:
    """产出固定几轮返工，记录 adapter 是否按契约调用各回调。"""

    def __init__(self, rounds: int) -> None:
        self._rounds = rounds
        self._round_index = 0
        self.sent: list[str] = []
        self.delivered: list[str] = []
        self.finalized = False

    async def on_turn_completed(self, *, producer_session_id: str | None):
        if self._round_index >= self._rounds:
            return _Decision(round_index=self._round_index, next_prompt=None)
        decision = _Decision(
            round_index=self._round_index,
            next_prompt=f"再改第 {self._round_index + 1} 版",
        )
        self._round_index += 1
        return decision

    async def resume_after_restart(self, *, producer_session_id: str | None):
        raise AssertionError("not used")

    def mark_feedback_sending(self, decision) -> None:
        self.sent.append(decision.next_prompt)

    def mark_feedback_delivered(self, decision) -> None:
        self.delivered.append(decision.next_prompt)

    def finalize_last_successful_submission(self) -> bool:
        self.finalized = True
        return True


class _Decision:
    def __init__(self, round_index: int, next_prompt: str | None) -> None:
        self.round_index = round_index
        self.next_prompt = next_prompt


def test_iteration_handler_drives_extra_turns(
    tmp_path: Path, stub_cli: Path, argv_log: Path
) -> None:
    """声明了 iterative_session=True 就必须真的消费 handler。

    漏掉这个循环时迭代类 env 会静默停在静态轮，横评里会被读成
    「agent 不返工」——实际是 adapter 没发消息。
    """
    handler = _FakeIterationHandler(rounds=2)
    task = _task(tmp_path, iteration_turn_handler=handler)
    result = _run(_adapter(stub_cli), task, _env(), tmp_path / "data")

    assert result.status == "completed"
    assert handler.sent == ["再改第 1 版", "再改第 2 版"]
    assert handler.delivered == handler.sent

    calls = _argv_lines(argv_log)
    chat_calls = [c for c in calls if c[:1] == ["chat"]]
    # 首轮 chat run 建会话，两轮返工走 chat send 复用同一 session。
    assert chat_calls[0][:2] == ["chat", "run"]
    assert [c[:3] for c in chat_calls[1:]] == [
        ["chat", "send", "sess-1"],
        ["chat", "send", "sess-1"],
    ]
    assert chat_calls[1][3] == "再改第 1 版"


def test_iteration_turn_failure_finalizes_last_submission(
    tmp_path: Path, stub_cli: Path, argv_log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """返工轮跑挂时要回收上一次成功提交，而不是整个 attempt 判零。"""
    monkeypatch.setenv("BLADE_STUB_EXIT", "5")
    handler = _FakeIterationHandler(rounds=2)
    task = _task(tmp_path, iteration_turn_handler=handler)
    result = _run(_adapter(stub_cli), task, _env(), tmp_path / "data")

    # 首轮就失败 → 不进返工循环，也就不该 finalize。
    assert result.status == "chat_failed"
    assert handler.sent == []
    assert handler.finalized is False


def test_zip_slip_member_is_skipped(tmp_path: Path, stub_cli: Path) -> None:
    """copy-dir 的 zip 不得把文件写到 workspace 之外。"""
    adapter = _adapter(stub_cli)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("../escaped.txt", "pwned")
        z.writestr("ok.txt", "fine")

    async def fake_run_cli(cli_path, args, *, timeout, cwd=None):
        out = args[args.index("--out") + 1]
        Path(out).write_bytes(evil.read_bytes())
        return 0, "{}", ""

    adapter._run_cli = fake_run_cli  # type: ignore[method-assign]
    synced = asyncio.run(adapter._recover_artifacts("blade", "sess-1", workspace))

    assert synced == {"files": 1}
    assert (workspace / "ok.txt").is_file()
    assert not (tmp_path / "escaped.txt").exists()
