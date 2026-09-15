from __future__ import annotations

from typing import Any

from ..process.docker_launcher import DockerLauncher
from ..process.sandbox_preflight import SandboxImageInfo
from .spec import HarborTaskSpec


def _memory_limit(resources: dict[str, Any], fallback: str) -> str:
    value = resources.get("memory_mb")
    if value is None:
        return fallback
    try:
        mb = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid Harbor memory_mb: {value!r}") from exc
    if mb <= 0:
        raise ValueError("Harbor memory_mb must be positive")
    return f"{mb}m"


def _resource_limits(settings: Any, spec: HarborTaskSpec) -> dict[str, Any]:
    defaults = settings.sandbox.limits_for("codex")
    resources = spec.agent_resources
    cpus = resources.get("cpus", defaults.cpus)
    pids = resources.get("pids", defaults.pids)
    try:
        cpus = float(cpus)
        pids = int(pids)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid Harbor agent resource limits") from exc
    if cpus <= 0 or pids <= 0:
        raise ValueError("Harbor agent resource limits must be positive")
    return {
        "memory": _memory_limit(resources, defaults.memory),
        "cpus": cpus,
        "pids": pids,
    }


def _image_info(reference: str, agent_name: str) -> SandboxImageInfo:
    # The manifest already requires a digest-pinned reference. Docker image
    # inspection is performed by the runtime preflight; this object is only the
    # immutable identity passed to the existing launcher.
    digest = reference if "@sha256:" in reference else reference
    return SandboxImageInfo(
        reference=reference,
        image_id=digest,
        digest=digest,
        agents=(agent_name,),
        versions={},
        labels={"harbor.compat": "true"},
    )


def build_harbor_agent_launcher(
    *, settings: Any, agent_name: str, task: HarborTaskSpec,
    image: SandboxImageInfo | None = None,
    logs_host: Any = None,
    tmp_host: Any = None,
    docker: str = "docker",
) -> DockerLauncher:
    """Build an Octagon Docker launcher using the task's pinned agent image.

    This is intentionally an agent-container launcher, not a verifier runner.
    The Harbor verifier must be launched by the post-agent attempt runtime after
    the agent container has been torn down.
    """
    network_mode = task.agent_network_mode
    if network_mode == "no-network":
        network_mode = "none"
    elif network_mode in {None, "public"}:
        # Docker's default bridge gives the same broad egress class as the
        # existing Octagon sandbox. Provider allowlisting is handled by the
        # task image/runtime layer in the next phase.
        network_mode = None
    elif network_mode.startswith("bridge") or network_mode.startswith("none"):
        pass
    else:
        raise ValueError(f"unsupported Harbor agent network_mode: {task.agent_network_mode!r}")
    return DockerLauncher(
        settings=settings,
        image=image or _image_info(task.agent_image, agent_name),
        docker=docker,
        workspace_container=task.agent_workdir,
        network_mode=network_mode,
        limits_override=_resource_limits(settings, task),
        # Runtime preparation may pull only this selected task's pinned
        # images before constructing the launcher; the launcher itself remains
        # pull-free and can never expand that to the full catalog.
        pull_policy="never",
        logs_host=logs_host,
        tmp_host=tmp_host,
    )
