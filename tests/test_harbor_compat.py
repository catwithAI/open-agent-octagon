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
    assert f"{tmp_path / 'logs'}:/logs" in argv
    assert f"{tmp_path / 'tmp'}:/tmp" in argv
    assert not any(f"{tmp_path / 'submission'}:/app" in item for item in argv)


def test_harbor_official_reward_forces_native_scoring_even_when_external_enabled():
    from backend.scoring_queue import _scoring_backend_for_env

    settings = SimpleNamespace(
        octagon_evals=SimpleNamespace(enabled=True),
    )
    harbor_env = SimpleNamespace(
        meta={"harbor_runtime": {"score_mode": "official_reward"}},
    )
    generic_env = SimpleNamespace(meta={})
    assert _scoring_backend_for_env(settings, harbor_env) == "native"
    assert _scoring_backend_for_env(settings, generic_env) == "octagon-evals"


def test_harbor_rejects_unsupported_modes_and_non_boolean_collect_flag():
    context = _task_context()
    for field, value in (
        ("environment_mode", "shared"),
        ("execution_mode", "multi_step"),
        ("has_collect_hooks", "false"),
        ("has_collect_hooks", True),
        ("collect_hook_count", 1),
    ):
        changed = {"_harbor": dict(context["_harbor"], **{field: value})}
        with pytest.raises(HarborTaskSpecError):
            HarborTaskSpec.from_context(changed)


def test_harbor_artifact_materialization_excludes_unlisted_workspace_files(tmp_path):
    from backend.harbor_compat.verifier import materialize_artifacts

    task = HarborTaskSpec.from_context(_task_context())
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "solver.py").write_text("class Solver: pass\n", encoding="utf-8")
    (workspace / "secret.txt").write_text("must not be mounted\n", encoding="utf-8")
    attempt_dir = tmp_path / "attempt"
    logs_dir = attempt_dir / "harbor" / "verifier-logs"
    tmp_dir = attempt_dir / "harbor" / "verifier-tmp"
    submission_dir = attempt_dir / "harbor" / "submission"

    digest, mounts = materialize_artifacts(
        spec=task,
        workspace=workspace,
        attempt_dir=attempt_dir,
        submission_dir=submission_dir,
        logs_dir=logs_dir,
        tmp_dir=tmp_dir,
    )

    assert digest
    assert len(mounts) == 1
    staged, destination, readonly = mounts[0]
    assert destination == "/app/solver.py"
    assert readonly is True
    assert staged.is_file()
    assert not (submission_dir / "mounts" / "app" / "secret.txt").exists()
    assert all("secret.txt" not in str(item) for item in mounts)


def test_harbor_artifact_root_symlink_is_rejected(tmp_path):
    from backend.harbor_compat.verifier import HarborVerifierError, materialize_artifacts

    task_context = _task_context()
    task_context["_harbor"] = dict(
        task_context["_harbor"], artifacts=["/app/result.json"]
    )
    task = HarborTaskSpec.from_context(task_context)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    real = tmp_path / "real-result.json"
    real.write_text("{}", encoding="utf-8")
    (workspace / "result.json").symlink_to(real)

    with pytest.raises(HarborVerifierError, match="symlink"):
        materialize_artifacts(
            spec=task,
            workspace=workspace,
            attempt_dir=tmp_path / "attempt",
            submission_dir=tmp_path / "submission",
            logs_dir=tmp_path / "logs",
            tmp_dir=tmp_path / "tmp",
        )


def test_verifier_resource_and_artifact_argv_are_explicit(tmp_path):
    from backend.harbor_compat.verifier import build_verifier_argv

    task = HarborTaskSpec.from_context(_task_context())
    argv = build_verifier_argv(
        docker="docker",
        attempt_id="att_verify_resources",
        spec=task,
        submission_dir=tmp_path / "submission",
        logs_dir=tmp_path / "logs",
        tmp_dir=tmp_path / "tmp",
        artifact_mounts=((tmp_path / "solver.py", "/app/solver.py", True),),
    )
    assert "--storage-opt" in argv
    assert argv[argv.index("--storage-opt") + 1] == "size=10240m"
    assert f"{tmp_path / 'solver.py'}:/app/solver.py:ro" in argv
    assert f"{tmp_path / 'submission'}:/app" not in argv


def test_configured_digest_pinned_codex_runtime_is_inspected_and_reused(monkeypatch):
    import asyncio
    import backend.harbor_compat.runtime as runtime_module
    from backend.process.sandbox_preflight import SandboxImageInfo

    task = HarborTaskSpec.from_context(_task_context())
    configured = "registry.example/harbor-runtime@sha256:" + "a" * 64
    calls = []

    def inspect(reference, *, docker="docker"):
        calls.append(reference)
        return SandboxImageInfo(
            reference=reference,
            image_id="sha256:runtime",
            digest=reference,
            agents=("codex",),
            versions={"codex": "0.149.1"},
        )

    monkeypatch.setattr(runtime_module, "inspect_image", inspect)

    async def direct_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(runtime_module.asyncio, "to_thread", direct_to_thread)
    runtime = runtime_module.HarborAttemptRuntime(
        settings=SimpleNamespace(sandbox=SimpleNamespace(image=configured)),
        spec=task,
        agent_name="codex",
    )
    task_image = SandboxImageInfo(
        reference=task.agent_image, image_id="sha256:task",
        digest=task.agent_image, agents=("codex",), versions={"codex": "0.149.1"},
    )
    runtime_result = asyncio.run(runtime._ensure_codex_runtime(task_image))
    assert runtime_result.reference == configured
    assert calls == [configured]


def test_missing_configured_digest_pinned_runtime_fails_without_rebuild(monkeypatch):
    import asyncio
    import backend.harbor_compat.runtime as runtime_module
    from backend.harbor_compat.runtime import HarborRuntimeError
    from backend.process.sandbox_preflight import SandboxImageInfo

    task = HarborTaskSpec.from_context(_task_context())
    configured = "registry.example/harbor-runtime@sha256:" + "b" * 64

    def inspect(_reference, *, docker="docker"):
        raise RuntimeError("not present")

    monkeypatch.setattr(runtime_module, "inspect_image", inspect)

    async def direct_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(runtime_module.asyncio, "to_thread", direct_to_thread)
    runtime = runtime_module.HarborAttemptRuntime(
        settings=SimpleNamespace(sandbox=SimpleNamespace(image=configured)),
        spec=task,
        agent_name="codex",
    )
    task_image = SandboxImageInfo(
        reference=task.agent_image, image_id="sha256:task",
        digest=task.agent_image, agents=("codex",), versions={},
    )
    with pytest.raises(HarborRuntimeError, match="Configured Harbor Codex runtime image"):
        asyncio.run(runtime._ensure_codex_runtime(task_image))
