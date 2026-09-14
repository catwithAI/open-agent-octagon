from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.process.launcher import ExecSpec
from backend.harbor_compat.launcher import build_harbor_agent_launcher
from backend.harbor_compat.spec import HarborTaskSpec, HarborTaskSpecError
from backend.process.docker_launcher import DockerAttemptSandbox
from backend.process.launcher import AttemptSpec


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "harbor_index_task.json"


def _task_context() -> dict:
    import json

    task = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return task["context"]


def _settings():
    limits = SimpleNamespace(memory="4g", cpus=2.0, pids=1024)
    return SimpleNamespace(
        sandbox=SimpleNamespace(
            server_side_tools="allow",
            limits_for=lambda _agent: limits,
        )
    )


def test_harbor_context_is_strictly_digest_pinned():
    spec = HarborTaskSpec.from_context(_task_context())
    assert spec.release == "1.4"
    assert spec.task_name == "harbor-index/algotune-optimize-lti-sim"
    assert "@sha256:" in spec.agent_image
    assert "@sha256:" in spec.verifier_image
    assert spec.agent_workdir == "/app"


def test_harbor_context_rejects_unpinned_images():
    context = _task_context()
    context = {"_harbor": dict(context["_harbor"], agent_image="latest")}
    with pytest.raises(HarborTaskSpecError, match="digest-pinned"):
        HarborTaskSpec.from_context(context)


def test_launcher_uses_task_image_workdir_network_and_limits(tmp_path):
    task = HarborTaskSpec.from_context(_task_context())
    launcher = build_harbor_agent_launcher(
        settings=_settings(), agent_name="codex", task=task, docker="docker"
    )
    attempt = AttemptSpec(
        attempt_id="att_harbor_test",
        data_path=tmp_path,
        agent_name="codex",
        workspace=tmp_path / "workspace",
    )
    workspace = (tmp_path / "workspace").resolve()
    mounts = ()
    # Use the public builder contract; the actual Docker process is not started.
    argv = launcher.build_run_argv(
        attempt,
        mounts=mounts,
        limits=launcher._limits_for("codex"),
        workspace=workspace,
        workspace_container=task.agent_workdir,
    )
    assert "-w" in argv
    assert argv[argv.index("-w") + 1] == "/app"
    assert argv[-3] == task.agent_image
    assert "--cpus" in argv and argv[argv.index("--cpus") + 1] == "4.0"


def test_docker_exec_maps_host_workspace_to_task_workdir(tmp_path):
    task = HarborTaskSpec.from_context(_task_context())
    launcher = build_harbor_agent_launcher(
        settings=_settings(), agent_name="codex", task=task, docker="docker"
    )
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    sandbox = DockerAttemptSandbox(
        launcher=launcher,
        spec=AttemptSpec(
            attempt_id="att_harbor_exec",
            data_path=tmp_path,
            agent_name="codex",
            workspace=workspace,
        ),
        container_id="container",
        workspace=workspace,
        home=tmp_path / "home",
        ro=tmp_path / "ro",
        mounts=(
            # private constructor details are intentionally exercised here to
            # protect the host-to-container path boundary.
            __import__("backend.process.docker_launcher", fromlist=["_Mount"])._Mount(
                workspace, "/app"
            ),
            __import__("backend.process.docker_launcher", fromlist=["_Mount"])._Mount(
                tmp_path / "logs", "/logs"
            ),
            __import__("backend.process.docker_launcher", fromlist=["_Mount"])._Mount(
                tmp_path / "tmp", "/tmp"
            ),
        ),
        limits={"memory": "4g", "cpus": 2.0, "pids": 1024},
    )
    argv = sandbox.build_exec_argv(
        ExecSpec(argv=("pwd",), cwd=str(workspace))
    )
    assert argv[argv.index("-w") + 1] == "/app"


def test_verifier_argv_is_pull_free_and_uses_separate_submission(tmp_path):
    from backend.harbor_compat.verifier import build_verifier_argv

    task = HarborTaskSpec.from_context(_task_context())
    argv = build_verifier_argv(
        docker="docker",
        attempt_id="att_verify",
        spec=task,
        submission_dir=tmp_path / "submission",
        logs_dir=tmp_path / "logs",
        tmp_dir=tmp_path / "tmp",
    )
    assert argv[0:3] == ("docker", "run", "--rm")
    assert argv[argv.index("--pull") + 1] == "never"
    assert argv[-2:] == ("bash", "/tests/test.sh")
    assert str(tmp_path / "submission") in argv[argv.index("-v") + 1]
