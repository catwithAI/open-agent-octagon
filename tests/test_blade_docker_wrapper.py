from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from backend.adapters.base import AdapterRunInput
from backend.adapters.blade_docker_wrapper import BladeDockerWrapperAdapter
from backend.process.launcher import ExecSpec


class _Proc:
    returncode = 0

    async def communicate(self):
        return b"", b""


class _Sandbox:
    def __init__(self, attempt_dir: Path, commands: list[ExecSpec]):
        self.attempt_dir = attempt_dir
        self.commands = commands

    def host_ro(self, _default: Path) -> Path:
        return self.attempt_dir / "sandbox_ro"

    @asynccontextmanager
    async def exec(self, spec: ExecSpec):
        self.commands.append(spec)
        logs = self.attempt_dir / "harbor" / "agent-logs"
        logs.mkdir(parents=True, exist_ok=True)
        yield _Proc()
        (logs / "blade-result.json").write_text(json.dumps({
            "status": "completed",
            "external_refs": {"blade_session_id": "session-test"},
            "events_count": 3,
            "security_meta": {"remote_agent": "blade-agent"},
        }), encoding="utf-8")


class _Launcher:
    def __init__(self, commands: list[ExecSpec]):
        self.commands = commands

    @asynccontextmanager
    async def attempt(self, spec):
        yield _Sandbox(Path(spec.data_path) / "attempts" / spec.attempt_id, self.commands)


def test_blade_wrapper_request_is_attempt_scoped_and_token_not_in_argv(tmp_path):
    commands: list[ExecSpec] = []
    adapter = BladeDockerWrapperAdapter(
        launcher=_Launcher(commands),
        config=SimpleNamespace(
            api_key="secret-token", base_url="https://blade.example.test", model="model-x"
        ),
    )
    task = AdapterRunInput(
        attempt_id="att-wrapper-test", task_id="task", task_prompt="do task",
        task_context={"_harbor": {"task": "x"}}, timeout_seconds=10,
        env_name="env", env_skill_id="env", env_token="env-token",
        env_base_url="http://env.test",
    )

    async def run():
        return await adapter.run(task, None, tmp_path)

    result = asyncio.run(run())
    assert result.status == "completed"
    request = json.loads(
        (tmp_path / "attempts" / task.attempt_id / "sandbox_ro" / "blade-request.json")
        .read_text(encoding="utf-8")
    )
    assert request["api_key"] == "secret-token"
    assert all("secret-token" not in part for part in commands[0].argv)
    assert commands[0].argv[-1] == "/attempt/blade-request.json"
