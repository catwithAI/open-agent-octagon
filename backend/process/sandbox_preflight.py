"""docker 沙盒的启动检查与「沙盒不可用」失败语义。

spec: docs/specs/260909-agent-sandbox 需求 3.4 / 8.3。沙盒是全局强制的：
`sandbox.enabled: true` 时任何检查失败都让本机 agent 的 attempt 以
``sandbox_unavailable`` 终态失败并带明确 error_code，绝不回落宿主机执行。

检查只用 docker CLI 子进程（与 DockerLauncher 同一依赖面），不引入 docker-py。
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 沙盒强制覆盖的本机 agent 全集；镜像标签 octagon.agents 必须包含启用的每一个。
SANDBOX_AGENTS: tuple[str, ...] = (
    "claude-code", "codex", "kimi-code", "opencode", "mimo-code", "dsh",
)

ATTEMPT_STATUS_SANDBOX_UNAVAILABLE = "sandbox_unavailable"
ERROR_SANDBOX_UNAVAILABLE = "sandbox_unavailable"
ERROR_SANDBOX_IMAGE_MISSING = "sandbox_image_missing"
ERROR_SANDBOX_AGENT_MISSING = "sandbox_agent_missing"


class SandboxUnavailable(RuntimeError):
    """沙盒模式下无法安全启动 agent。dispatch 据此落 sandbox_unavailable 终态。"""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True)
class SandboxImageInfo:
    reference: str
    image_id: str
    # repo@sha256:...；本地构建未推送时 RepoDigests 为空，回落到 image_id。
    digest: str
    agents: tuple[str, ...]
    versions: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)

    def version_of(self, agent_name: str) -> str | None:
        return self.versions.get(agent_name)


@dataclass(frozen=True)
class SandboxStatus:
    enabled: bool
    ok: bool
    error_code: str | None = None
    error_message: str | None = None
    image: SandboxImageInfo | None = None
    docker_version: str | None = None
    checked_at: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "ok": self.ok,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "docker_version": self.docker_version,
            "checked_at": self.checked_at,
            "image": None if self.image is None else {
                "reference": self.image.reference,
                "digest": self.image.digest,
                "agents": list(self.image.agents),
                "versions": dict(self.image.versions),
            },
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _docker(args: list[str], *, docker: str, timeout: float = 30.0) -> str:
    proc = subprocess.run(
        [docker, *args], capture_output=True, text=True, timeout=timeout, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip() or f"docker {' '.join(args)} 失败")
    return proc.stdout


def inspect_image(reference: str, *, docker: str = "docker") -> SandboxImageInfo:
    """`docker image inspect`：取 id / digest / octagon.* 标签。镜像不存在时抛 RuntimeError。"""
    out = _docker(
        ["image", "inspect", "--format", "{{.Id}}\t{{json .RepoDigests}}\t{{json .Config.Labels}}", reference],
        docker=docker,
    )
    image_id, digests_json, labels_json = out.strip().split("\t", 2)
    digests = json.loads(digests_json) or []
    labels = json.loads(labels_json) or {}
    agents = tuple(
        a.strip() for a in str(labels.get("octagon.agents", "")).split(",") if a.strip()
    )
    versions = {
        key[len("octagon.agent."):-len(".version")]: str(value)
        for key, value in labels.items()
        if key.startswith("octagon.agent.") and key.endswith(".version")
    }
    return SandboxImageInfo(
        reference=reference,
        image_id=image_id,
        digest=str(digests[0]) if digests else image_id,
        agents=agents,
        versions=versions,
        labels={str(k): str(v) for k, v in labels.items()},
    )


def check_sandbox(
    settings: Any,
    *,
    enabled_agents: tuple[str, ...] | None = None,
    docker: str = "docker",
) -> SandboxStatus:
    """后端启动检查。返回状态而不抛，调用方决定日志级别；dispatch 用 require_* 转成失败。"""
    sandbox = settings.sandbox
    if not sandbox.enabled:
        return SandboxStatus(enabled=False, ok=True, checked_at=_now_iso())

    def fail(code: str, message: str, **extra: Any) -> SandboxStatus:
        return SandboxStatus(
            enabled=True, ok=False, error_code=code, error_message=message,
            checked_at=_now_iso(), **extra,
        )

    # 同路径挂载与 docker socket 都以后端跑在宿主机为前提。
    if Path("/.dockerenv").exists():
        return fail(
            ERROR_SANDBOX_UNAVAILABLE,
            "后端运行在容器内：沙盒模式要求后端跑在宿主机（同路径挂载 + docker CLI）",
        )
    if shutil.which(docker) is None:
        return fail(ERROR_SANDBOX_UNAVAILABLE, f"找不到 docker CLI（{docker}）")
    try:
        docker_version = _docker(["version", "--format", "{{.Server.Version}}"], docker=docker).strip()
    except Exception as exc:  # noqa: BLE001
        return fail(ERROR_SANDBOX_UNAVAILABLE, f"docker daemon 不可达：{exc}")

    if not sandbox.image:
        return fail(
            ERROR_SANDBOX_IMAGE_MISSING, "sandbox.image 未配置",
            docker_version=docker_version,
        )
    try:
        image = inspect_image(sandbox.image, docker=docker)
    except Exception as exc:  # noqa: BLE001
        return fail(
            ERROR_SANDBOX_IMAGE_MISSING,
            f"镜像 {sandbox.image} 不存在或无法 inspect：{exc}",
            docker_version=docker_version,
        )
    if not image.agents:
        return fail(
            ERROR_SANDBOX_IMAGE_MISSING,
            f"镜像 {sandbox.image} 缺少 octagon.agents 标签，不是 agent 运行时镜像",
            docker_version=docker_version, image=image,
        )
    wanted = enabled_agents if enabled_agents is not None else SANDBOX_AGENTS
    missing = [a for a in wanted if a not in image.agents]
    if missing:
        return fail(
            ERROR_SANDBOX_AGENT_MISSING,
            f"镜像 {sandbox.image} 未包含启用的 agent：{', '.join(missing)}",
            docker_version=docker_version, image=image,
        )
    return SandboxStatus(
        enabled=True, ok=True, image=image, docker_version=docker_version,
        checked_at=_now_iso(),
    )


def require_sandbox_agent(status: SandboxStatus, agent_name: str) -> SandboxImageInfo:
    """dispatch 前置：沙盒开启但不可用 / agent 不在镜像 → SandboxUnavailable。"""
    if not status.enabled:
        raise SandboxUnavailable(ERROR_SANDBOX_UNAVAILABLE, "沙盒未启用")
    if not status.ok or status.image is None:
        raise SandboxUnavailable(
            status.error_code or ERROR_SANDBOX_UNAVAILABLE,
            status.error_message or "沙盒不可用",
        )
    if agent_name not in status.image.agents:
        raise SandboxUnavailable(
            ERROR_SANDBOX_AGENT_MISSING,
            f"镜像 {status.image.reference} 未包含 agent {agent_name}",
        )
    return status.image
