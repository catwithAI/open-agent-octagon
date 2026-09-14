"""DockerLauncher：一个 attempt 一个容器，每轮一次 ``docker exec``。

spec: docs/specs/260909-agent-sandbox（需求 1 / 4 / 5，design「Launcher 抽象」）。

边界（白名单挂载，其余一律不存在）：

| 宿主机                                    | 容器内           | 权限 |
|-------------------------------------------|------------------|------|
| ``<data>/attempts/<id>/skill_workspace``  | 同宿主机绝对路径 | rw   |
| ``<data>/attempts/<id>/sandbox_home``     | ``/home/agent``  | rw   |
| ``<data>/attempts/<id>/sandbox_ro``       | ``/attempt``     | ro   |

生命周期：``attempt()`` 进入时 ``docker run -d ... sleep infinity``；每轮
``exec()`` 是 ``docker exec -i``；``attempt()`` 退出（正常、Stop、超时、异常）先
``docker kill`` 再 ``docker wait``，dispatch 才进 scoring（D-11）。后端关停时
不杀容器（与宿主机执行「关停不杀 CLI」一致），交给重启后的回收与 sweeper。

只用 docker CLI 子进程，不引入 docker-py。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .identity import record_sandbox_container
from .launcher import AttemptSpec, ExecSpec, ExecutionLocus
from .lifecycle import is_shutting_down
from .sandbox_preflight import SandboxImageInfo, SandboxUnavailable

logger = logging.getLogger(__name__)

SANDBOX_RO_DIRNAME = "sandbox_ro"
SANDBOX_HOME_DIRNAME = "sandbox_home"
RO_MOUNT = "/attempt"
HOME_MOUNT = "/home/agent"
MCP_SUBDIR = "mcp"
LOGS_MOUNT = "/logs"
TMP_MOUNT = "/tmp"
CONTAINER_NAME_PREFIX = "octagon-agent-"
LABEL_ATTEMPT = "octagon.attempt_id"
LABEL_RUN = "octagon.run_id"
LABEL_AGENT = "octagon.agent"
HOST_GATEWAY = "host.docker.internal"
CONTAINER_RECORD_FILENAME = "sandbox_container.json"
ERROR_SANDBOX_MCP_ENTRY_UNRESOLVABLE = "sandbox_mcp_entry_unresolvable"

# 容器只拿 adapter 显式设置的变量：宿主机进程环境（其它 provider 的 key、
# 评测内部路径）不得整包透传。以下是无害且容器确实需要的直通项。
_PASSTHROUGH_ENV = ("LANG", "LC_ALL", "TZ")
_PROXY_ENV = (
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
)
# 容器内由镜像 / launcher 决定，adapter 传来的值（宿主机路径）一律丢弃。
_DROP_ENV = frozenset({"PATH", "PWD", "OLDPWD", "SHELL", "TMPDIR", "VIRTUAL_ENV",
                       "PYTHONPATH", "PYTHONHOME", "USER", "LOGNAME"})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "0.0.0.0", "::1", "[::1]"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def container_name_for(attempt_id: str) -> str:
    return f"{CONTAINER_NAME_PREFIX}{attempt_id}"


def translate_loopback_url(url: str) -> str:
    """宿主机 127.0.0.1/localhost 地址 → 容器可达的 host.docker.internal。"""
    parts = urlsplit(url)
    if parts.hostname and parts.hostname.lower() in _LOOPBACK_HOSTS:
        netloc = HOST_GATEWAY if parts.port is None else f"{HOST_GATEWAY}:{parts.port}"
        if parts.username:
            cred = parts.username + (f":{parts.password}" if parts.password else "")
            netloc = f"{cred}@{netloc}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return url


@dataclass(frozen=True)
class _Mount:
    host: Path
    container: str
    readonly: bool = False

    def arg(self) -> str:
        suffix = ":ro" if self.readonly else ""
        return f"{self.host}:{self.container}{suffix}"


@dataclass
class _ExecRecord:
    turn_id: str | None
    started_at: str
    ended_at: str | None = None
    exit_code: int | None = None


class DockerAttemptSandbox:
    """一个 attempt 的容器句柄。adapter 只通过它拿路径翻译与每轮进程。"""

    locus: ExecutionLocus = "docker-sandbox"

    def __init__(
        self,
        *,
        launcher: "DockerLauncher",
        spec: AttemptSpec,
        container_id: str,
        workspace: Path,
        home: Path,
        ro: Path,
        mounts: tuple[_Mount, ...],
        limits: dict[str, Any],
    ) -> None:
        self._launcher = launcher
        self.spec = spec
        self.container_id = container_id
        self.container_name = container_name_for(spec.attempt_id)
        self.workspace = workspace
        self.home = home
        self.ro = ro
        self.mounts = mounts
        self.limits = limits
        self.image = launcher.image
        self.server_side_tools: str = launcher.server_side_tools
        self.execs: list[_ExecRecord] = []
        self.killed = False

    # ---- adapter 侧路径 / 环境翻译 -------------------------------------------

    def host_home(self, default: Path) -> Path:
        """agent HOME 在宿主机上的目录（挂到 /home/agent）。"""
        return self.home

    def host_ro(self, default: Path) -> Path:
        """只读交付目录在宿主机上的路径（挂到 /attempt）。"""
        return self.ro

    def path(self, host_path: Path | str) -> str:
        """宿主机路径 → 容器内路径。只有三处挂载可翻译，其余抛错（白名单）。"""
        p = Path(host_path).resolve()
        for mount in self.mounts:
            try:
                rel = p.relative_to(mount.host.resolve())
            except ValueError:
                continue
            return str(Path(mount.container) / rel) if str(rel) != "." else mount.container
        raise SandboxUnavailable(
            "sandbox_path_not_mounted",
            f"路径 {p} 不在沙盒挂载白名单内，不能交给容器",
        )

    def resolve_cli(self, name: str) -> str | None:
        """CLI 由镜像 PATH 提供；不查宿主机。不在镜像 agent 列表里的返回 None。"""
        return name

    def translate_url(self, url: str) -> str:
        return translate_loopback_url(url)

    def security_fields(self) -> dict[str, Any]:
        return {
            "sandbox_image": self.image.digest,
            "sandbox_id": self.container_id,
            "sandbox_image_reference": self.image.reference,
            "agent_version": self.image.version_of(self.spec.agent_name),
            "egress_policy": (
                "none" if self._launcher.network_mode == "none" else "unrestricted"
            ),
            "network_mode": self._launcher.network_mode or "bridge",
        }

    # ---- 每轮进程 ------------------------------------------------------------

    def build_exec_argv(self, spec: ExecSpec) -> tuple[str, ...]:
        # Most adapters pass the host workspace path. dsh passes the result of
        # sandbox.path() already, so accept both forms without double-mapping.
        cwd = str(spec.cwd)
        # Prefer host-path matching before container-path matching. The Harbor
        # /tmp mount overlaps the host's usual /tmp prefix; checking container
        # prefixes first would mistake /tmp/<attempt>/skill_workspace for the
        # already-translated container path /tmp and make docker exec chdir to a
        # host-only path.
        host_path = Path(cwd)
        is_host_path = False
        if host_path.is_absolute():
            resolved = host_path.resolve()
            for mount in self.mounts:
                try:
                    resolved.relative_to(mount.host.resolve())
                except ValueError:
                    continue
                is_host_path = True
                break
        if is_host_path:
            cwd = self.path(cwd)
        elif not any(
            cwd == mount.container or cwd.startswith(mount.container.rstrip("/") + "/")
            for mount in self.mounts
        ):
            cwd = self.path(cwd)
        argv: list[str] = [self._launcher.docker, "exec", "-i", "-w", cwd]
        for key, value in self._container_env(
            spec.env, explicit_env_keys=spec.explicit_env_keys,
        ).items():
            argv += ["-e", f"{key}={value}"]
        argv.append(self.container_id)
        argv.extend(spec.argv)
        return tuple(argv)

    def _container_env(
        self, env: Mapping[str, str], *, explicit_env_keys: frozenset[str] = frozenset(),
    ) -> dict[str, str]:
        host = os.environ
        out: dict[str, str] = {}
        for key, value in env.items():
            if key in _DROP_ENV:
                continue
            if key in _PASSTHROUGH_ENV or key in _PROXY_ENV:
                out[key] = translate_loopback_url(value) if key in _PROXY_ENV else value
                continue
            # 只带 adapter 显式设置 / 改写的变量，宿主机原样的环境不透传。
            # ``docker exec`` 不继承宿主机环境，因此 adapter 明确注入的
            # credential 即使与宿主机同值也必须保留；否则 provider 会在
            # 容器内退回默认端点并以 401 失败。
            if key in host and host[key] == value and key not in explicit_env_keys:
                continue
            out[key] = value
        out.setdefault("HOME", HOME_MOUNT)
        return out

    @asynccontextmanager
    async def exec(self, spec: ExecSpec) -> AsyncIterator[asyncio.subprocess.Process]:
        argv = self.build_exec_argv(spec)
        record = _ExecRecord(turn_id=spec.turn_id, started_at=_now_iso())
        self.execs.append(record)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=10 * 1024 * 1024,
            start_new_session=True,
        )
        try:
            yield proc
        finally:
            if proc.returncode is None:
                if is_shutting_down():
                    logger.warning(
                        "后端关停：保留沙盒容器 %s 及其中进程 attempt=%s",
                        self.container_name, self.spec.attempt_id,
                    )
                else:
                    # 权威动作是杀整容器：超时是 attempt 级的，没有「只杀一轮」。
                    await self.kill()
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    await proc.wait()
            record.ended_at = _now_iso()
            record.exit_code = proc.returncode

    async def kill(self) -> None:
        if self.killed:
            return
        self.killed = True
        await self._launcher._run(["kill", self.container_id], check=False)
        await self._launcher._run(["wait", self.container_id], check=False, timeout=30)


class DockerLauncher:
    locus: ExecutionLocus = "docker-sandbox"

    def __init__(
        self,
        *,
        settings: Any,
        image: SandboxImageInfo,
        docker: str = "docker",
        workspace_container: str | None = None,
        network_mode: str | None = None,
        limits_override: dict[str, Any] | None = None,
        pull_policy: str | None = None,
        logs_host: Path | None = None,
        tmp_host: Path | None = None,
    ) -> None:
        self.settings = settings
        self.image = image
        self.docker = docker
        self.server_side_tools: str = settings.sandbox.server_side_tools
        # Normal Octagon attempts preserve the historical same-path mount. A
        # Harbor task image may require /app or /workspace instead; keeping the
        # host path in AttemptSpec while translating only at the Docker boundary
        # lets the adapter stay task-runtime agnostic.
        self.workspace_container = workspace_container
        self.network_mode = network_mode
        self.limits_override = dict(limits_override or {})
        if pull_policy not in {None, "always", "missing", "never"}:
            raise ValueError(f"unsupported Docker pull policy: {pull_policy!r}")
        self.pull_policy = pull_policy
        self.logs_host = Path(logs_host).resolve() if logs_host is not None else None
        self.tmp_host = Path(tmp_host).resolve() if tmp_host is not None else None

    # ---- docker 子进程 --------------------------------------------------------

    async def _run(
        self, args: list[str], *, check: bool = True, timeout: float = 60.0,
    ) -> str:
        proc = await asyncio.create_subprocess_exec(
            self.docker, *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"docker {' '.join(args[:2])} 超时（{timeout}s）")
        if check and proc.returncode != 0:
            raise RuntimeError(
                (err or out).decode("utf-8", errors="replace").strip()
                or f"docker {' '.join(args[:2])} 失败"
            )
        return out.decode("utf-8", errors="replace")

    # ---- attempt 上下文 -------------------------------------------------------

    def _limits_for(self, agent_name: str) -> dict[str, Any]:
        lim = self.settings.sandbox.limits_for(agent_name)
        out = {"memory": lim.memory, "cpus": lim.cpus, "pids": lim.pids}
        out.update(self.limits_override)
        return out

    def build_run_argv(
        self, spec: AttemptSpec, *, mounts: tuple[_Mount, ...], limits: dict[str, Any],
        workspace: Path, workspace_container: str | None = None,
    ) -> tuple[str, ...]:
        argv: list[str] = [
            self.docker, "run", "-d",
            "--name", container_name_for(spec.attempt_id),
            "--label", f"{LABEL_ATTEMPT}={spec.attempt_id}",
            "--label", f"{LABEL_RUN}={spec.run_id or ''}",
            "--label", f"{LABEL_AGENT}={spec.agent_name}",
            "--memory", str(limits["memory"]),
            "--memory-swap", str(limits["memory"]),
            "--cpus", str(limits["cpus"]),
            "--pids-limit", str(limits["pids"]),
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "-w", str(workspace_container or workspace),
            "-e", f"HOME={HOME_MOUNT}",
        ]
        if self.network_mode:
            argv += ["--network", self.network_mode]
        if self.pull_policy:
            argv += ["--pull", self.pull_policy]
        # host-gateway：Linux 必需；Docker Desktop（mac）自带该域名，再加一次无害。
        argv += ["--add-host", f"{HOST_GATEWAY}:host-gateway"]
        # 非 root：宿主机后端进程的 uid/gid。Linux 上这决定 workspace 产物属主；
        # mac 上 Docker Desktop 的文件共享层自己映射属主，但非 root 仍是 /etc 等
        # 系统路径不可写的前提。后端本身以 root 跑时不传（容器内 root 且属主一致）。
        if os.getuid() != 0:
            argv += ["--user", f"{os.getuid()}:{os.getgid()}"]
        for mount in mounts:
            argv += ["-v", mount.arg()]
        argv += [self.image.reference, "sleep", "infinity"]
        return tuple(argv)

    @asynccontextmanager
    async def attempt(self, spec: AttemptSpec) -> AsyncIterator[DockerAttemptSandbox]:
        attempt_dir = Path(spec.data_path) / "attempts" / spec.attempt_id
        workspace = Path(spec.workspace) if spec.workspace else attempt_dir / "skill_workspace"
        home = attempt_dir / SANDBOX_HOME_DIRNAME
        ro = attempt_dir / SANDBOX_RO_DIRNAME
        directories = [workspace, home, ro / MCP_SUBDIR]
        if self.logs_host is not None:
            directories.append(self.logs_host)
        if self.tmp_host is not None:
            directories.append(self.tmp_host)
        for d in directories:
            d.mkdir(parents=True, exist_ok=True)
        workspace = workspace.resolve()
        workspace_container = self.workspace_container or str(workspace)
        mount_list = [
            _Mount(workspace, workspace_container),
            _Mount(home.resolve(), HOME_MOUNT),
            _Mount(ro.resolve(), RO_MOUNT, readonly=True),
        ]
        if self.logs_host is not None:
            mount_list.append(_Mount(self.logs_host, LOGS_MOUNT))
        if self.tmp_host is not None:
            mount_list.append(_Mount(self.tmp_host, TMP_MOUNT))
        mounts = tuple(mount_list)
        limits = self._limits_for(spec.agent_name)
        name = container_name_for(spec.attempt_id)
        # 同一 attempt 重跑（失败重试、恢复）会撞上上次残留的同名容器：它只可能
        # 属于本 attempt，清掉是安全的。
        await self._run(["rm", "-f", name], check=False)
        try:
            run_argv = self.build_run_argv(
                spec, mounts=mounts, limits=limits, workspace=workspace,
                workspace_container=workspace_container,
            )
            out = await self._run(list(run_argv[1:]), timeout=120)  # [0] 是 docker 本身
        except Exception as exc:  # noqa: BLE001
            raise SandboxUnavailable("sandbox_unavailable", f"docker run 失败：{exc}") from exc
        container_id = out.strip()
        started_at = _now_iso()
        record_sandbox_container(spec.data_path, spec.attempt_id, container_id)
        sandbox = DockerAttemptSandbox(
            launcher=self, spec=spec, container_id=container_id,
            workspace=workspace, home=home, ro=ro, mounts=mounts, limits=limits,
        )
        try:
            yield sandbox
        finally:
            if is_shutting_down():
                logger.warning(
                    "后端关停：保留沙盒容器 %s attempt=%s，交给重启回收", name, spec.attempt_id,
                )
            else:
                await sandbox.kill()
                self._write_record(attempt_dir, sandbox, started_at)
                await self._run(["rm", "-f", container_id], check=False)

    def _write_record(
        self, attempt_dir: Path, sandbox: DockerAttemptSandbox, started_at: str,
    ) -> None:
        payload = {
            "container_id": sandbox.container_id,
            "container_name": sandbox.container_name,
            "image": {
                "reference": self.image.reference,
                "digest": self.image.digest,
                "versions": dict(self.image.versions),
            },
            "agent": sandbox.spec.agent_name,
            "agent_version": self.image.version_of(sandbox.spec.agent_name),
            "limits": sandbox.limits,
            "mounts": [
                {"host": str(m.host), "container": m.container, "readonly": m.readonly}
                for m in sandbox.mounts
            ],
            "egress_policy": (
                "none" if self.network_mode == "none" else "unrestricted"
            ),
            "network_mode": self.network_mode or "bridge",
            "server_side_tools": sandbox.server_side_tools,
            "execs": [
                {"turn_id": e.turn_id, "started_at": e.started_at,
                 "ended_at": e.ended_at, "exit_code": e.exit_code}
                for e in sandbox.execs
            ],
            "started_at": started_at,
            "ended_at": _now_iso(),
        }
        try:
            (attempt_dir / CONTAINER_RECORD_FILENAME).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
            )
        except OSError:
            logger.warning("无法写 %s attempt=%s", CONTAINER_RECORD_FILENAME,
                           sandbox.spec.attempt_id, exc_info=True)


# ---------------------------------------------------------------------------
# 场景 MCP 入口翻译（需求 5）：单文件复制进 sandbox_ro/mcp/
# ---------------------------------------------------------------------------

_SIBLING_IMPORT = re.compile(r"^\s*(?:from\s+(\.\S*|\S+)\s+import\b|import\s+(\S+))", re.M)


def check_mcp_entry_self_contained(entry: Path) -> str | None:
    """入口脚本不得 import 同目录模块（docs/environments.md 契约）。返回违规说明或 None。"""
    try:
        text = entry.read_text(encoding="utf-8")
    except OSError as exc:
        return f"无法读取 {entry}: {exc}"
    siblings = {p.stem for p in entry.parent.glob("*.py")} - {entry.stem}
    siblings |= {p.name for p in entry.parent.iterdir() if p.is_dir() and (p / "__init__.py").exists()}
    for m in _SIBLING_IMPORT.finditer(text):
        target = (m.group(1) or m.group(2) or "").split(",")[0].strip()
        if target.startswith("."):
            return f"{entry.name} 使用相对 import（{target}）"
        top = target.split(".")[0]
        if top in siblings:
            return f"{entry.name} import 了同目录模块 {top}"
    return None


def resolve_mcp_entry(raw: str, *, cwd: str | None, env_dir: Path | None) -> Path | None:
    """把 command 里的 .py 参数解析成宿主机文件。

    场景声明的是项目根视角的相对路径（`envs/<env>/mcp_server.py`），而 envs_path
    可配置到仓库外，项目根下未必有 envs/ 目录。依次尝试：绝对路径、相对 cwd、
    相对 envs_path（去掉首段 `envs/`）、场景目录下的同名文件。
    """
    p = Path(raw)
    candidates: list[Path] = []
    if p.is_absolute():
        candidates.append(p)
    else:
        if cwd:
            candidates.append(Path(cwd) / p)
        if env_dir is not None:
            parts = p.parts
            if len(parts) >= 2:
                candidates.append(env_dir.parent / Path(*parts[1:]))
            candidates.append(env_dir / p.name)
    for c in candidates:
        if c.is_file():
            return c.resolve()
    return None


def translate_mcp_specs(
    specs: tuple[Any, ...], *, attempt_dir: Path, workspace: Path, env_dir: Path | None = None,
) -> tuple[Any, ...]:
    """把 `uv run ... envs/<env>/mcp_server.py` 形态的入口翻译成容器内命令。

    只复制那一个 .py 到 sandbox_ro/mcp/；解析不到唯一入口或入口不自包含 →
    SandboxUnavailable(sandbox_mcp_entry_unresolvable)。
    """
    from ..adapters.base import McpServerSpec

    out: list[Any] = []
    ro_mcp = attempt_dir / SANDBOX_RO_DIRNAME / MCP_SUBDIR
    ro_mcp.mkdir(parents=True, exist_ok=True)
    for spec in specs:
        candidates = [a for a in (spec.command, *spec.args) if str(a).endswith(".py")]
        if len(candidates) != 1:
            raise SandboxUnavailable(
                ERROR_SANDBOX_MCP_ENTRY_UNRESOLVABLE,
                f"MCP server {spec.name}: 无法从 command 解析出唯一 .py 入口（{[spec.command, *spec.args]}）",
            )
        entry = resolve_mcp_entry(candidates[0], cwd=spec.cwd, env_dir=env_dir)
        if entry is None:
            raise SandboxUnavailable(
                ERROR_SANDBOX_MCP_ENTRY_UNRESOLVABLE,
                f"MCP server {spec.name}: 入口文件不存在 {candidates[0]}"
                f"（cwd={spec.cwd}, env_dir={env_dir}）",
            )
        violation = check_mcp_entry_self_contained(entry)
        if violation:
            raise SandboxUnavailable(
                ERROR_SANDBOX_MCP_ENTRY_UNRESOLVABLE,
                f"MCP server {spec.name}: 入口不自包含——{violation}",
            )
        target = ro_mcp / entry.name
        shutil.copyfile(entry, target)
        out.append(McpServerSpec(
            name=spec.name,
            command="python3",
            args=(f"{RO_MOUNT}/{MCP_SUBDIR}/{entry.name}",),
            cwd=str(workspace),
        ))
    return tuple(out)


# ---------------------------------------------------------------------------
# 回收（需求 4.4 / 4.5）：跨重启判活、杀、扫孤儿
# ---------------------------------------------------------------------------


def _docker_sync(args: list[str], *, docker: str = "docker", timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run([docker, *args], capture_output=True, text=True, timeout=timeout, check=False)


def container_is_running(container_id: str, *, docker: str = "docker") -> bool:
    proc = _docker_sync(["inspect", "--format", "{{.State.Running}}", container_id], docker=docker)
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def kill_container(container_id: str, *, docker: str = "docker") -> bool:
    proc = _docker_sync(["kill", container_id], docker=docker)
    return proc.returncode == 0


def list_sandbox_containers(*, docker: str = "docker") -> list[dict[str, str]]:
    """所有带 octagon.attempt_id 标签的容器（含已退出）。"""
    proc = _docker_sync(
        ["ps", "-a", "--filter", f"label={LABEL_ATTEMPT}", "--format",
         "{{.ID}}\t{{.State}}\t{{.Label \"" + LABEL_ATTEMPT + "\"}}\t{{.CreatedAt}}"],
        docker=docker,
    )
    if proc.returncode != 0:
        return []
    rows: list[dict[str, str]] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            rows.append({"id": parts[0], "state": parts[1], "attempt_id": parts[2],
                         "created_at": parts[3] if len(parts) > 3 else ""})
    return rows


def sweep_sandbox_containers(
    *, is_attempt_active: Any, docker: str = "docker",
) -> dict[str, int]:
    """清理孤儿容器：已退出的一律 rm；仍在跑但 attempt 已不活跃的 kill + rm。

    `is_attempt_active(attempt_id) -> bool` 由调用方按 DB 状态提供。
    """
    removed = killed = 0
    for row in list_sandbox_containers(docker=docker):
        if row["state"] == "running":
            if is_attempt_active(row["attempt_id"]):
                continue
            if kill_container(row["id"], docker=docker):
                killed += 1
            logger.warning("sweeper: 杀掉孤儿沙盒容器 %s attempt=%s", row["id"], row["attempt_id"])
        proc = _docker_sync(["rm", "-f", row["id"]], docker=docker)
        if proc.returncode == 0:
            removed += 1
    return {"containers_killed": killed, "containers_removed": removed}
