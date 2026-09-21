"""DockerLauncher：argv / 挂载 / env 翻译 / MCP 入口翻译（不依赖 docker），
以及一组真 docker 集成测试（无 docker 时 skip）。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import textwrap
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from backend.adapters.base import McpServerSpec
from backend.config import Settings
from backend.process.docker_launcher import (
    CONTAINER_RECORD_FILENAME,
    HOME_MOUNT,
    RO_MOUNT,
    DockerLauncher,
    check_mcp_entry_self_contained,
    container_is_running,
    sweep_sandbox_containers,
    translate_loopback_url,
    translate_mcp_specs,
)
from backend.process.identity import agent_process_is_alive, read_agent_process
from backend.process.launcher import AttemptSpec, ExecSpec
from backend.process.lifecycle import kill_recorded_agent_process
from backend.process.sandbox_preflight import (
    SandboxImageInfo,
    SandboxUnavailable,
    check_sandbox,
    require_sandbox_agent,
)

FAKE_IMAGE = SandboxImageInfo(
    reference="octagon-agent-runtime:test",
    image_id="sha256:abc",
    digest="repo@sha256:abc",
    agents=("claude-code", "codex"),
    versions={"claude-code": "2.1.245", "codex": "0.149.1"},
)


def _settings(**sandbox) -> Settings:
    return Settings.model_validate({"sandbox": {"enabled": True, "image": FAKE_IMAGE.reference, **sandbox}})


def _spec(tmp_path: Path, attempt_id: str = "att-1", agent: str = "claude-code") -> AttemptSpec:
    return AttemptSpec(
        attempt_id=attempt_id, data_path=tmp_path, agent_name=agent, run_id="run-1",
        workspace=tmp_path / "attempts" / attempt_id / "skill_workspace",
    )


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("http://127.0.0.1:8100", "http://host.docker.internal:8100"),
        ("http://localhost:8100/internal/wire-proxy/a/b", "http://host.docker.internal:8100/internal/wire-proxy/a/b"),
        ("http://0.0.0.0:18100", "http://host.docker.internal:18100"),
        ("https://openrouter.ai/api/v1", "https://openrouter.ai/api/v1"),
        ("http://192.168.1.2:8100", "http://192.168.1.2:8100"),
    ],
)
def test_translate_loopback_url(url: str, expected: str) -> None:
    assert translate_loopback_url(url) == expected


def test_build_run_argv_has_whitelist_mounts_limits_and_labels(tmp_path: Path) -> None:
    launcher = DockerLauncher(settings=_settings(agents={"claude-code": {"limits": {"memory": "6g"}}}), image=FAKE_IMAGE)
    spec = _spec(tmp_path)
    ws = spec.workspace
    from backend.process.docker_launcher import _Mount

    mounts = (_Mount(ws, str(ws)), _Mount(tmp_path / "h", HOME_MOUNT), _Mount(tmp_path / "r", RO_MOUNT, readonly=True))
    argv = launcher.build_run_argv(spec, mounts=mounts, limits=launcher._limits_for("claude-code"), workspace=ws)
    s = " ".join(argv)
    assert argv[:3] == ("docker", "run", "-d")
    assert "--name octagon-agent-att-1" in s
    assert "--label octagon.attempt_id=att-1" in s and "--label octagon.run_id=run-1" in s
    assert "--memory 6g --memory-swap 6g" in s and "--cpus 2.0" in s and "--pids-limit 1024" in s
    assert "--cap-drop ALL" in s and "--security-opt no-new-privileges" in s
    assert f"-v {ws}:{ws}" in s                      # 同路径
    assert f"-v {tmp_path / 'h'}:{HOME_MOUNT}" in s
    assert f"-v {tmp_path / 'r'}:{RO_MOUNT}:ro" in s
    assert argv[-3:] == (FAKE_IMAGE.reference, "sleep", "infinity")
    # 只有三处**宿主机**挂载。匿名卷（`-v /home/agent/lo`）没有宿主机侧，
    # 不构成白名单缺口——这里按「参数里带宿主机路径」来数。
    host_mounts = [
        value
        for flag, value in zip(argv, argv[1:], strict=False)
        if flag == "-v" and ":" in value
    ]
    assert len(host_mounts) == 3
    assert "--add-host host.docker.internal:host-gateway" in s
    if os.getuid() != 0:
        assert f"--user {os.getuid()}:{os.getgid()}" in s


def _mount_targets(argv: tuple[str, ...], flag: str) -> list[str]:
    return [v for f, v in zip(argv, argv[1:], strict=False) if f == flag]


def test_ephemeral_home_dirs_stay_off_the_bind_mount(tmp_path: Path) -> None:
    """可重建的运行时目录不得落到 sandbox_home。

    330 个 attempt 各存一份 LibreOffice 运行时 = 33G。这些目录挂成匿名卷 /
    tmpfs 后随容器销毁，attempt 目录里只剩 agent 真正的产出。
    """
    launcher = DockerLauncher(settings=_settings(), image=FAKE_IMAGE)
    spec = _spec(tmp_path)
    ws = spec.workspace
    from backend.process.docker_launcher import _Mount

    mounts = (_Mount(ws, str(ws)), _Mount(tmp_path / "h", HOME_MOUNT), _Mount(tmp_path / "r", RO_MOUNT, readonly=True))
    argv = launcher.build_run_argv(
        spec, mounts=mounts, limits=launcher._limits_for("claude-code"), workspace=ws
    )

    anonymous = set(_mount_targets(argv, "-v"))
    tmpfs = " ".join(_mount_targets(argv, "--tmpfs"))
    # 纯运行期缓存必须被挡住，且没有一项带宿主机路径。
    for name in (".npm", ".tmp", "lo", "loroot", "sysroot", "fonts"):
        assert f"{HOME_MOUNT}/{name}" in anonymous
    # adapter 在容器启动前写过的目录一律不挡——盖住会让 agent 读不到自己的
    # provider 配置/凭据，直接跑不起来（见 test_adapter_written_dirs_...）。
    for name in (".config", ".claude", "config", ".local"):
        assert f"{HOME_MOUNT}/{name}" not in anonymous
    assert f"{HOME_MOUNT}/apt:rw,size=512m" in tmpfs
    # `.cache` 必须走匿名卷而不是 tmpfs：presentbench 下 ms-playwright 的
    # 浏览器二进制有 658MB，512m tmpfs 会 ENOSPC，并发 6 还要吃 4G 内存。
    assert f"{HOME_MOUNT}/.cache" in anonymous
    assert f"{HOME_MOUNT}/.cache" not in tmpfs
    assert str(tmp_path / "h") not in tmpfs


def test_keep_home_dirs_opts_a_scenario_out(tmp_path: Path) -> None:
    """需求 1.4：确实要留证据的场景可以按目录放开，而非全局关掉。"""
    launcher = DockerLauncher(settings=_settings(), image=FAKE_IMAGE)
    spec = replace(_spec(tmp_path), keep_home_dirs=("lo", "fonts"))
    ws = spec.workspace
    from backend.process.docker_launcher import _Mount

    mounts = (_Mount(ws, str(ws)), _Mount(tmp_path / "h", HOME_MOUNT), _Mount(tmp_path / "r", RO_MOUNT, readonly=True))
    argv = launcher.build_run_argv(
        spec, mounts=mounts, limits=launcher._limits_for("claude-code"), workspace=ws
    )

    anonymous = set(_mount_targets(argv, "-v"))
    # 放开的两项重新落回 bind mount（不再被盖住），其余仍被挡。
    assert f"{HOME_MOUNT}/lo" not in anonymous
    assert f"{HOME_MOUNT}/fonts" not in anonymous
    assert f"{HOME_MOUNT}/loroot" in anonymous


def test_ephemeral_dirs_reject_path_traversal(tmp_path: Path) -> None:
    """配置里的目录名不得把挂载点顶出 /home/agent。"""
    launcher = DockerLauncher(
        settings=_settings(ephemeral_home_dirs=["../../etc", "/etc", "lo", ""]),
        image=FAKE_IMAGE,
    )
    spec = _spec(tmp_path)
    ws = spec.workspace
    from backend.process.docker_launcher import _Mount

    mounts = (_Mount(ws, str(ws)), _Mount(tmp_path / "h", HOME_MOUNT), _Mount(tmp_path / "r", RO_MOUNT, readonly=True))
    argv = launcher.build_run_argv(
        spec, mounts=mounts, limits=launcher._limits_for("claude-code"), workspace=ws
    )

    targets = _mount_targets(argv, "-v")
    assert f"{HOME_MOUNT}/lo" in targets
    assert all(".." not in t for t in targets)
    # `/etc` 被剥成 `etc` 后仍在 /home/agent 之下，不会盖住宿主机 /etc。
    assert all(t == "/etc" or not t.startswith("/etc") for t in targets if ":" not in t)
    assert "/etc" not in targets


def _sandbox(tmp_path: Path, **sandbox_cfg):
    from backend.process.docker_launcher import DockerAttemptSandbox, _Mount

    launcher = DockerLauncher(settings=_settings(**sandbox_cfg), image=FAKE_IMAGE)
    spec = _spec(tmp_path)
    ws = spec.workspace
    home = tmp_path / "attempts" / "att-1" / "sandbox_home"
    ro = tmp_path / "attempts" / "att-1" / "sandbox_ro"
    for d in (ws, home, ro):
        d.mkdir(parents=True, exist_ok=True)
    mounts = (_Mount(ws.resolve(), str(ws.resolve())), _Mount(home.resolve(), HOME_MOUNT), _Mount(ro.resolve(), RO_MOUNT, readonly=True))
    return DockerAttemptSandbox(
        launcher=launcher, spec=spec, container_id="ctr123", workspace=ws, home=home, ro=ro,
        mounts=mounts, limits=launcher._limits_for("claude-code"),
    )


def test_exec_argv_only_passes_adapter_set_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOST_SECRET", "s3cr3t")
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:7890")
    sb = _sandbox(tmp_path)
    env = {**os.environ, "ANTHROPIC_AUTH_TOKEN": "tok", "CLAUDE_CONFIG_DIR": "/home/agent/.claude",
           "HOME": "/home/agent", "PATH": "/host/bin"}
    argv = sb.build_exec_argv(ExecSpec(argv=["claude", "-p", "hi"], cwd=str(sb.workspace), env=env))
    s = " ".join(argv)
    assert argv[:3] == ("docker", "exec", "-i")
    assert f"-w {sb.workspace}" in s
    assert "-e ANTHROPIC_AUTH_TOKEN=tok" in s and "-e CLAUDE_CONFIG_DIR=/home/agent/.claude" in s
    assert "HOST_SECRET" not in s                         # 宿主机环境不透传
    assert "-e PATH=" not in s                            # 镜像 PATH
    assert "-e LANG=C.UTF-8" in s                         # 无害直通
    assert "-e http_proxy=http://host.docker.internal:7890" in s
    assert argv[-3:] == ("claude", "-p", "hi")
    assert argv[argv.index("ctr123") - 1] != "-e"


def test_exec_cwd_is_resolved_to_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """octagon.yaml 默认 data_path ./data：相对 cwd 必须解析成绝对路径（docker exec 要求）。"""
    monkeypatch.chdir(tmp_path)
    sb = _sandbox(tmp_path)
    rel = os.path.relpath(sb.workspace, tmp_path)
    argv = sb.build_exec_argv(ExecSpec(argv=["x"], cwd=rel, env={}))
    assert argv[argv.index("-w") + 1] == str(sb.workspace.resolve())


def test_path_translation_is_whitelist(tmp_path: Path) -> None:
    sb = _sandbox(tmp_path)
    assert sb.path(sb.workspace / "a.txt") == f"{sb.workspace.resolve()}/a.txt"
    assert sb.path(sb.home / ".claude") == f"{HOME_MOUNT}/.claude"
    assert sb.path(sb.ro / "mcp_config.json") == f"{RO_MOUNT}/mcp_config.json"
    assert sb.host_home(tmp_path / "x") == sb.home
    assert sb.host_ro(tmp_path / "x") == sb.ro
    assert sb.resolve_cli("claude") == "claude"
    with pytest.raises(SandboxUnavailable):
        sb.path(tmp_path / "attempts" / "att-1" / "events.jsonl")
    fields = sb.security_fields()
    assert fields["sandbox_image"] == FAKE_IMAGE.digest and fields["sandbox_id"] == "ctr123"
    assert fields["agent_version"] == "2.1.245" and fields["egress_policy"] == "unrestricted"


def test_translate_mcp_specs_copies_single_file(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    env_dir = project / "envs" / "demo"
    env_dir.mkdir(parents=True)
    (env_dir / "mcp_server.py").write_text("import httpx\nfrom mcp.server.fastmcp import FastMCP\n")
    (env_dir / "scorer.py").write_text("SECRET = 1\n")
    (env_dir / "core.py").write_text("x = 1\n")
    spec = McpServerSpec(name="octagon-demo", command="uv",
                         args=("run", "--project", ".", "python", "envs/demo/mcp_server.py"),
                         cwd=str(project))
    attempt_dir = tmp_path / "attempts" / "att-1"
    out = translate_mcp_specs((spec,), attempt_dir=attempt_dir, workspace=attempt_dir / "skill_workspace")
    assert len(out) == 1
    assert out[0].command == "python3" and out[0].args == (f"{RO_MOUNT}/mcp/mcp_server.py",)
    assert out[0].cwd == str(attempt_dir / "skill_workspace")
    copied = list((attempt_dir / "sandbox_ro" / "mcp").iterdir())
    assert [p.name for p in copied] == ["mcp_server.py"]          # 只有那一个文件


def test_translate_mcp_specs_resolves_external_envs_path(tmp_path: Path) -> None:
    """command 写的是项目根视角的 envs/<env>/x.py，而 envs_path 在仓库外。"""
    env_dir = tmp_path / "external-envs" / "demo"
    env_dir.mkdir(parents=True)
    (env_dir / "mcp_server.py").write_text("import httpx\n")
    project = tmp_path / "proj"
    project.mkdir()
    spec = McpServerSpec(name="d", command="uv", args=("run", "python", "envs/demo/mcp_server.py"), cwd=str(project))
    out = translate_mcp_specs((spec,), attempt_dir=tmp_path / "a", workspace=tmp_path / "a" / "w", env_dir=env_dir)
    assert out[0].args == (f"{RO_MOUNT}/mcp/mcp_server.py",)
    assert (tmp_path / "a" / "sandbox_ro" / "mcp" / "mcp_server.py").exists()


def test_translate_mcp_specs_rejects_sibling_import(tmp_path: Path) -> None:
    env_dir = tmp_path / "envs" / "demo"
    env_dir.mkdir(parents=True)
    (env_dir / "core.py").write_text("x = 1\n")
    (env_dir / "mcp_server.py").write_text("from core import x\n")
    assert check_mcp_entry_self_contained(env_dir / "mcp_server.py")
    spec = McpServerSpec(name="d", command="python", args=("envs/demo/mcp_server.py",), cwd=str(tmp_path))
    with pytest.raises(SandboxUnavailable) as exc:
        translate_mcp_specs((spec,), attempt_dir=tmp_path / "a", workspace=tmp_path / "a" / "w")
    assert exc.value.error_code == "sandbox_mcp_entry_unresolvable"

    spec2 = McpServerSpec(name="d", command="uv", args=("run", "something"), cwd=str(tmp_path))
    with pytest.raises(SandboxUnavailable):
        translate_mcp_specs((spec2,), attempt_dir=tmp_path / "b", workspace=tmp_path / "b" / "w")


def test_require_sandbox_agent_failure_codes() -> None:
    from backend.process.sandbox_preflight import SandboxStatus

    ok = SandboxStatus(enabled=True, ok=True, image=FAKE_IMAGE)
    assert require_sandbox_agent(ok, "codex") is FAKE_IMAGE
    with pytest.raises(SandboxUnavailable) as e:
        require_sandbox_agent(ok, "dsh")
    assert e.value.error_code == "sandbox_agent_missing"
    with pytest.raises(SandboxUnavailable) as e:
        require_sandbox_agent(SandboxStatus(enabled=True, ok=False, error_code="sandbox_image_missing", error_message="x"), "codex")
    assert e.value.error_code == "sandbox_image_missing"
    with pytest.raises(SandboxUnavailable):
        require_sandbox_agent(SandboxStatus(enabled=False, ok=True), "codex")


def test_check_sandbox_without_docker_cli(tmp_path: Path) -> None:
    status = check_sandbox(_settings(), docker=str(tmp_path / "no-such-docker"))
    assert not status.ok and status.error_code == "sandbox_unavailable"
    assert check_sandbox(Settings()).enabled is False


def test_identity_roundtrip_docker(tmp_path: Path) -> None:
    from backend.process.identity import record_sandbox_container

    record_sandbox_container(tmp_path, "att-1", "ctr-xyz")
    ident = read_agent_process(tmp_path, "att-1")
    assert ident is not None and ident.kind == "docker" and ident.container_id == "ctr-xyz"
    payload = json.loads((tmp_path / "attempts" / "att-1" / "agent_process.json").read_text())
    assert payload == {"kind": "docker", "container_id": "ctr-xyz", "recorded_at": ident.recorded_at}


# ---------------------------------------------------------------------------
# docker 集成（无 docker / 拉不到基础镜像时 skip）
# ---------------------------------------------------------------------------

_BASE = "python:3.12-slim"


def _docker_ok() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(["docker", "image", "inspect", _BASE], capture_output=True, timeout=20, check=True)
        return True
    except Exception:
        return False


needs_docker = pytest.mark.skipif(not _docker_ok(), reason=f"需要 docker 与本地镜像 {_BASE}")


def _real_launcher(tmp_path: Path) -> DockerLauncher:
    image = SandboxImageInfo(reference=_BASE, image_id="x", digest=_BASE, agents=("claude-code",), versions={})
    settings = Settings.model_validate({"sandbox": {"enabled": True, "image": _BASE, "limits": {"memory": "512m", "cpus": 1.0, "pids": 256}}})
    return DockerLauncher(settings=settings, image=image)


@needs_docker
@pytest.mark.asyncio
async def test_docker_attempt_visibility_lifecycle_and_record(tmp_path: Path) -> None:
    attempt_id = f"att-{uuid.uuid4().hex[:8]}"
    data = tmp_path / "data"
    # 作弊面：场景答案、兄弟 attempt、DB 都在宿主机 data 旁边
    (tmp_path / "envs" / "demo").mkdir(parents=True)
    (tmp_path / "envs" / "demo" / "scorer.py").write_text("SECRET\n")
    (data / "attempts" / "other" / "skill_workspace").mkdir(parents=True)
    (data / "octagon.db").write_text("db\n")
    spec = AttemptSpec(attempt_id=attempt_id, data_path=data, agent_name="claude-code", run_id="r",
                       workspace=data / "attempts" / attempt_id / "skill_workspace")
    launcher = _real_launcher(tmp_path)
    probe = textwrap.dedent(
        """
        import json, os, subprocess, pathlib
        out = {
          "cwd": os.getcwd(),
          "home": os.environ.get("HOME"),
          "x": os.environ.get("OCT_X"),
          "leak": os.environ.get("HOST_ONLY"),
          "scorer": subprocess.run(["sh","-c","find / -name scorer.py 2>/dev/null"], capture_output=True, text=True).stdout,
          "db": os.path.exists("%s"),
          "sibling": os.path.exists("%s"),
          "ro_list": sorted(os.listdir("/attempt")),
        }
        pathlib.Path("out.txt").write_text("written")
        try:
            pathlib.Path("/attempt/x").write_text("x"); out["ro_write"] = "allowed"
        except OSError as e: out["ro_write"] = "denied"
        try:
            pathlib.Path("/etc/x").write_text("x"); out["etc_write"] = "allowed"
        except OSError as e: out["etc_write"] = "denied"
        pathlib.Path(os.environ["HOME"], "state.txt").write_text("s")
        print(json.dumps(out))
        """ % (data / "octagon.db", data / "attempts" / "other")
    )
    os.environ["HOST_ONLY"] = "leak"
    try:
        async with launcher.attempt(spec) as sb:
            container_id = sb.container_id
            assert container_is_running(container_id)
            (sb.ro / "prompt.md").write_text("hello")
            # 跨重启身份：agent_process.json 记的是容器
            assert agent_process_is_alive(data, attempt_id)
            results = []
            for turn in ("t1", "t2"):
                async with sb.exec(ExecSpec(argv=["python3", "-c", probe], cwd=str(sb.workspace),
                                            env={**os.environ, "OCT_X": turn}, turn_id=turn)) as proc:
                    out = await proc.stdout.read()
                    await proc.wait()
                    assert proc.returncode == 0, (await proc.stderr.read()).decode()
                results.append(json.loads(out))
            assert container_is_running(container_id)          # 两轮之间容器活着
    finally:
        os.environ.pop("HOST_ONLY", None)

    for r in results:
        assert Path(r["cwd"]) == sb.workspace.resolve()
        assert r["home"] == HOME_MOUNT and r["leak"] is None
        assert r["scorer"].strip() == "" and r["db"] is False and r["sibling"] is False
        assert r["ro_list"] == ["mcp", "prompt.md"]
        assert r["ro_write"] == "denied" and r["etc_write"] == "denied"
    assert results[0]["x"] == "t1" and results[1]["x"] == "t2"
    assert (sb.workspace / "out.txt").read_text() == "written"
    assert (sb.home / "state.txt").exists()                     # HOME 状态留在宿主机侧
    # 退出后：容器已杀并删除、记录落盘
    assert not container_is_running(container_id)
    assert not agent_process_is_alive(data, attempt_id)
    rec = json.loads((data / "attempts" / attempt_id / CONTAINER_RECORD_FILENAME).read_text())
    assert rec["container_id"] == container_id and [e["turn_id"] for e in rec["execs"]] == ["t1", "t2"]
    assert all(e["exit_code"] == 0 for e in rec["execs"])
    assert rec["limits"] == {"memory": "512m", "cpus": 1.0, "pids": 256}


@needs_docker
@pytest.mark.asyncio
async def test_docker_exec_timeout_kills_whole_container(tmp_path: Path) -> None:
    attempt_id = f"att-{uuid.uuid4().hex[:8]}"
    data = tmp_path / "data"
    spec = AttemptSpec(attempt_id=attempt_id, data_path=data, agent_name="claude-code",
                       workspace=data / "attempts" / attempt_id / "skill_workspace")
    launcher = _real_launcher(tmp_path)
    async with launcher.attempt(spec) as sb:
        async with sb.exec(ExecSpec(argv=["sleep", "60"], cwd=str(sb.workspace))) as proc:
            try:
                await asyncio.wait_for(proc.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass
        # exec 上下文退出即杀整容器
        assert proc.returncode is not None
        assert not container_is_running(sb.container_id)
    assert not container_is_running(sb.container_id)


@needs_docker
@pytest.mark.asyncio
async def test_docker_recorded_container_kill_and_sweep(tmp_path: Path) -> None:
    """模拟后端崩溃：attempt() 没退出就丢了句柄——按落盘身份杀、按标签扫。"""
    attempt_id = f"att-{uuid.uuid4().hex[:8]}"
    data = tmp_path / "data"
    spec = AttemptSpec(attempt_id=attempt_id, data_path=data, agent_name="claude-code",
                       workspace=data / "attempts" / attempt_id / "skill_workspace")
    launcher = _real_launcher(tmp_path)
    cm = launcher.attempt(spec)
    sb = await cm.__aenter__()
    try:
        assert kill_recorded_agent_process(data, attempt_id) is True
        assert not container_is_running(sb.container_id)
        swept = sweep_sandbox_containers(is_attempt_active=lambda _a: False)
        assert swept["containers_removed"] >= 1
        assert subprocess.run(["docker", "inspect", sb.container_id], capture_output=True).returncode != 0
    finally:
        await cm.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# dsh：runtime 走 docker exec -i，宿主机走 SDK 默认
# ---------------------------------------------------------------------------


def test_dsh_launch_args_override(tmp_path: Path) -> None:
    from backend.adapters.dsh import SANDBOX_RUNTIME_BIN, _launch_args_override
    from backend.process.launcher import HostAttemptSandbox

    env = {"DSH_CWD": "/w", "DSH_SESSION_ROOT": "/home/agent/dsh_sessions",
           "DSH_CORDIS_CONFIG": "/attempt/dsh_cordis.yml", "DEEPSEEK_API_KEY": "k"}
    assert _launch_args_override(HostAttemptSandbox(_spec(tmp_path)), cwd="/w", env=env) is None
    sb = _sandbox(tmp_path)
    argv = _launch_args_override(sb, cwd=str(sb.workspace), env=env)
    assert argv[:3] == ["docker", "exec", "-i"] and argv[-1] == SANDBOX_RUNTIME_BIN
    s = " ".join(argv)
    for k, v in env.items():
        assert f"-e {k}={v}" in s
    assert "ctr123" in argv


def test_dsh_adapter_locus_follows_launcher(tmp_path: Path) -> None:
    from backend.adapters.dsh import DshAdapter

    launcher = DockerLauncher(settings=_settings(), image=FAKE_IMAGE)
    adapter = DshAdapter(model="p/m", octagon_project_path=tmp_path, launcher=launcher)
    assert adapter.capabilities.execution_locus == "docker-sandbox"
    assert DshAdapter(model="p/m", octagon_project_path=tmp_path).capabilities.execution_locus == "host"


_SANDBOX_IMAGE = os.environ.get("SANDBOX_IMAGE", "octagon-agent-runtime:dev")


def _sandbox_image_ok() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(
            ["docker", "image", "inspect", _SANDBOX_IMAGE],
            capture_output=True, timeout=20, check=True,
        )
        return True
    except Exception:
        return False


@pytest.mark.skipif(
    not _sandbox_image_ok(),
    reason=f"需要本地沙盒镜像 {_SANDBOX_IMAGE}（make sandbox-image）",
)
def test_ephemeral_home_dirs_are_writable_by_a_non_root_agent(tmp_path: Path) -> None:
    """匿名卷的属主/权限**继承镜像里该路径的目录**。

    镜像里不存在该目录时，匿名卷是 root:root 0755；而容器以宿主机 uid 跑，
    agent 写不进去，LibreOffice 直接起不来——office 类场景会整体失败，且
    表现为 agent 能力问题。Dockerfile 必须预建这些目录并放开权限。
    """
    argv = [
        "docker", "run", "--rm", "--user", "4242:4242",
        "-v", f"{HOME_MOUNT}/lo",
        "-v", f"{HOME_MOUNT}/fonts",
        "--tmpfs", f"{HOME_MOUNT}/.cache:rw,size=64m",
        _SANDBOX_IMAGE, "sh", "-c",
        f"for d in lo fonts .cache; do touch {HOME_MOUNT}/$d/probe || exit 1; done; "
        "echo ALL_WRITABLE",
    ]
    done = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    assert "ALL_WRITABLE" in done.stdout, (
        f"ephemeral 目录在非 root uid 下不可写；stdout={done.stdout!r} stderr={done.stderr!r}"
    )


def test_dockerfile_precreates_every_ephemeral_dir() -> None:
    """镜像必须预建 `ephemeral_home_dirs` 里的每一项。

    匿名卷的属主/权限继承镜像里该路径的目录；镜像里没有就是 root:root 0755，
    而容器以宿主机 uid 跑 —— agent 写不进去，该 agent 直接跑不起来。
    这条把「Dockerfile 与配置默认值必须同步」变成测试，而不是注释里的约定。
    """
    dockerfile = (
        Path(__file__).resolve().parent.parent / "docker" / "agent-runtime" / "Dockerfile"
    ).read_text(encoding="utf-8")
    defaults = Settings().sandbox.ephemeral_home_dirs
    assert defaults, "默认清单不该为空"
    missing = [d for d in defaults if f"/home/agent/{d}" not in dockerfile]
    assert not missing, (
        f"这些 ephemeral 目录没在 Dockerfile 里预建，匿名卷会不可写：{missing}"
    )


def test_adapter_written_dirs_are_never_ephemeral() -> None:
    """adapter 在容器启动前写过的家目录子目录，绝不能挂成匿名卷。

    沙盒模式下 `host_home()` 返回的就是 `sandbox_home`，这些文件写在宿主机
    侧；被匿名卷盖住后 agent 启动时读到的是空目录 —— provider 配置、API key、
    模型路由全部消失，agent 直接跑不起来。

    对照 adapter 里的实际写入点（沙盒模式下 host_home() 忽略传入的
    `.xxx-iso-home`，一律返回 sandbox_home 本身，所以这些路径就落在家目录一级）：
      claude_code.py:212/219   .claude            CLAUDE_CONFIG_DIR（宿主机 mkdir + 写 settings.json）
      opencode_family.py:430   config             <PREFIX>_CONFIG_DIR（宿主机 mkdir）
      opencode_family.py:434   .config            XDG_CONFIG_HOME
      opencode_family.py:435/437 .local           XDG_DATA_HOME / XDG_STATE_HOME
      dsh.py:591               .dsh               DSH_HOME（宿主机 mkdir）
      dsh.py:591               .agents            DSH_AGENTS_HOME（宿主机 mkdir）
      dsh.py:591               dsh_sessions       DSH_SESSION_ROOT——**会话 jsonl 证据**，
                                                  盖住就随容器销毁，后端再也读不到
    """
    forbidden = {
        ".config", ".claude", "config", ".local",
        ".dsh", ".agents", "dsh_sessions",
    }
    defaults = set(Settings().sandbox.ephemeral_home_dirs)
    leaked = forbidden & defaults
    assert not leaked, (
        f"这些目录被 adapter 在容器启动前写入，挂匿名卷会让 agent 读不到配置：{leaked}"
    )
