"""Launcher 抽象：HostLauncher 与 agent_process() 等价；adapter 经 launcher 启动。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import textwrap
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from backend.adapters.base import AdapterRunInput, ConversationTurn
from backend.adapters.claude_code import ClaudeCodeAdapter
from backend.adapters.codex import CodexAdapter
from backend.adapters.kimi_code import KimiCodeAdapter
from backend.adapters.opencode_family import OpencodeFamilyAdapter
from backend.process.launcher import (
    AttemptSpec,
    ExecSpec,
    HostAttemptSandbox,
    HostLauncher,
)


def _attempt_spec(tmp_path: Path, attempt_id: str = "att-1") -> AttemptSpec:
    return AttemptSpec(
        attempt_id=attempt_id,
        data_path=tmp_path,
        agent_name="fake",
        run_id="run-1",
        workspace=tmp_path / "attempts" / attempt_id / "skill_workspace",
    )


# ---------------------------------------------------------------------------
# HostLauncher：与现状 agent_process() 等价
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_host_launcher_exec_runs_subprocess_with_cwd_env_and_records_identity(
    tmp_path: Path,
) -> None:
    spec = _attempt_spec(tmp_path)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    argv = [
        sys.executable, "-c",
        "import os, json; print(json.dumps({'cwd': os.getcwd(), 'x': os.environ.get('OCT_X')}))",
    ]
    launcher = HostLauncher()
    assert launcher.locus == "host"

    async with launcher.attempt(spec) as sandbox:
        assert isinstance(sandbox, HostAttemptSandbox)
        assert sandbox.locus == "host"
        assert sandbox.container_id is None
        async with sandbox.exec(
            ExecSpec(argv=argv, cwd=str(cwd), env={"OCT_X": "1", "PATH": os.environ["PATH"]})
        ) as proc:
            out = await proc.stdout.read()
            await proc.wait()

    payload = json.loads(out)
    assert Path(payload["cwd"]).resolve() == cwd.resolve()
    assert payload["x"] == "1"
    assert proc.returncode == 0
    # agent_process() 的登记副作用原样保留
    recorded = json.loads(
        (tmp_path / "attempts" / "att-1" / "agent_process.json").read_text()
    )
    assert recorded["pid"] == proc.pid


@pytest.mark.asyncio
async def test_host_launcher_exec_kills_process_group_on_exit(tmp_path: Path) -> None:
    spec = _attempt_spec(tmp_path)
    async with HostLauncher().attempt(spec) as sandbox:
        async with sandbox.exec(
            ExecSpec(argv=[sys.executable, "-c", "import time; time.sleep(60)"], cwd=str(tmp_path))
        ) as proc:
            pass
    assert proc.returncode is not None and proc.returncode != 0


def test_host_build_exec_argv_is_passthrough(tmp_path: Path) -> None:
    sandbox = HostAttemptSandbox(_attempt_spec(tmp_path))
    assert sandbox.build_exec_argv(ExecSpec(argv=["a", "b"], cwd="/")) == ("a", "b")


# ---------------------------------------------------------------------------
# adapter：execution_locus 随 launcher；多轮 = 一次 attempt + 每轮一次 exec
# ---------------------------------------------------------------------------


class _RecordingLauncher:
    """记录 attempt/exec 调用；exec 忽略 argv，改跑一个假的 CLI 脚本。"""

    def __init__(self, fake_cli: Path, locus: str = "docker-sandbox") -> None:
        self.locus = locus
        self.fake_cli = fake_cli
        self.attempts: list[AttemptSpec] = []
        self.execs: list[ExecSpec] = []
        self.exited = 0

    @asynccontextmanager
    async def attempt(self, spec: AttemptSpec):
        self.attempts.append(spec)
        outer = self

        class _Sandbox(HostAttemptSandbox):
            locus = outer.locus
            container_id = "ctr-fake"

            def __init__(self) -> None:
                super().__init__(spec)

            @asynccontextmanager
            async def exec(self, spec: ExecSpec):
                outer.execs.append(spec)
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, str(outer.fake_cli),
                    cwd=spec.cwd, env=dict(spec.env),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                try:
                    yield proc
                finally:
                    if proc.returncode is None:
                        proc.kill()
                        await proc.wait()

        try:
            yield _Sandbox()
        finally:
            self.exited += 1


def _fake_claude(tmp_path: Path) -> Path:
    script = tmp_path / "fake_claude.py"
    script.write_text(textwrap.dedent(
        """
        import json
        print(json.dumps({"type": "system", "subtype": "init", "session_id": "s", "model": "m"}))
        print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                          "session_id": "s", "usage": {"input_tokens": 1, "output_tokens": 1}}))
        """
    ))
    return script


def _task(turns: tuple[ConversationTurn, ...] = ()) -> AdapterRunInput:
    return AdapterRunInput(
        attempt_id="att-1",
        task_id="t",
        task_prompt="do it",
        task_context={},
        timeout_seconds=60,
        env_name="env",
        env_skill_id="octagon/env",
        env_token="tok",
        env_base_url="http://127.0.0.1:1",
        run_id="run-1",
        conversation_turns=turns,
    )


@pytest.mark.parametrize(
    "factory",
    [
        lambda launcher: ClaudeCodeAdapter(launcher=launcher),
        lambda launcher: CodexAdapter(launcher=launcher),
        lambda launcher: KimiCodeAdapter(model="m", launcher=launcher),
        lambda launcher: OpencodeFamilyAdapter(agent_name="opencode", model="m", launcher=launcher),
        lambda launcher: OpencodeFamilyAdapter(agent_name="mimo-code", model="m", launcher=launcher),
    ],
)
def test_execution_locus_follows_launcher(tmp_path: Path, factory) -> None:
    default = factory(None)
    assert default.capabilities.execution_locus == "host"
    assert type(default).capabilities.execution_locus == "host"

    sandboxed = factory(_RecordingLauncher(tmp_path / "x"))
    assert sandboxed.capabilities.execution_locus == "docker-sandbox"
    # 类级静态声明不被实例改写
    assert type(sandboxed).capabilities.execution_locus == "host"
    assert sandboxed.agent_name


@pytest.mark.asyncio
async def test_claude_multi_turn_uses_one_attempt_and_one_exec_per_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.adapters.claude_code as cc

    monkeypatch.setattr(cc.shutil, "which", lambda name: "/fake/bin/claude")
    launcher = _RecordingLauncher(_fake_claude(tmp_path))
    adapter = ClaudeCodeAdapter(launcher=launcher)
    turns = (
        ConversationTurn(turn_id="t1", turn_index=0, prompt="one"),
        ConversationTurn(turn_id="t2", turn_index=1, prompt="two", score_after=True),
    )
    data_path = tmp_path / "data"
    env = type("Env", (), {"name": "env", "env_dir": str(tmp_path), "meta": {}})()

    result = await adapter.run(_task(turns), env, data_path)

    assert result.status == "completed", (result.error_code, result.error_message)
    assert len(launcher.attempts) == 1
    assert launcher.attempts[0].attempt_id == "att-1"
    assert launcher.attempts[0].agent_name == "claude-code"
    assert launcher.attempts[0].workspace == data_path / "attempts" / "att-1" / "skill_workspace"
    assert launcher.exited == 1
    assert [e.turn_id for e in launcher.execs] == ["t1", "t2"]
    assert all(e.argv[0] == "/fake/bin/claude" for e in launcher.execs)
    assert "--session-id" in launcher.execs[0].argv
    assert "--resume" in launcher.execs[1].argv
    assert all(e.cwd == str(data_path / "attempts" / "att-1" / "skill_workspace") for e in launcher.execs)
