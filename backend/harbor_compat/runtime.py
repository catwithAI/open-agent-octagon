"""Attempt-scoped Harbor compatibility runtime.

The Harbor task image is the source of truth for the task filesystem and the
Harbor verifier.  Octagon agents are injected through a small local overlay
image; the task image itself is never treated as if it contained ``codex``.
Task preparation may pull only the selected task's two digest-pinned images;
it never performs a catalog-wide pull. Runtime and wrapper images are built
lazily and cached locally.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from ..process.sandbox_preflight import SandboxImageInfo, SandboxUnavailable, inspect_image
from .launcher import build_harbor_agent_launcher
from .spec import HarborTaskSpec
from .verifier import HarborVerifierRunner

logger = logging.getLogger(__name__)


class HarborRuntimeError(RuntimeError):
    """Harbor runtime setup or finalization failed."""


_AGENT_COPY_PATHS: dict[str, tuple[str, ...]] = {
    # Codex is a static musl binary and is the only agent enabled in the pilot.
    "codex": ("/usr/local/bin/codex",),
    "claude-code": ("/usr/local/bin/claude", "/opt/claude"),
    "kimi-code": ("/usr/local/bin/kimi",),
    "opencode": ("/usr/local/bin/opencode",),
    "mimo-code": ("/usr/local/bin/mimo",),
    "dsh": ("/opt/dsh",),
}


def _safe_tag(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def _tree_sha256(root: Path) -> str:
    """Hash a workspace tree without depending on mtimes or filesystem order."""
    entries: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise HarborRuntimeError(f"materialized workspace contains symlink: {rel}")
        if path.is_dir():
            entries.append({"path": rel, "kind": "directory"})
        elif path.is_file():
            entries.append({
                "path": rel, "kind": "file",
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            })
        else:
            raise HarborRuntimeError(f"unsupported workspace entry: {rel}")
    payload = json.dumps(entries, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class HarborAttemptRuntime:
    """Prepare one task and connect its verifier to the Octagon runner."""

    def __init__(self, *, settings: Any, spec: HarborTaskSpec, agent_name: str,
                 docker: str = "docker") -> None:
        self.settings = settings
        self.spec = spec
        self.agent_name = agent_name
        self.docker = docker
        self.overlay_image: SandboxImageInfo | None = None
        self.attempt_dir: Path | None = None
        self.workspace: Path | None = None
        self.agent_logs: Path | None = None
        self.agent_tmp: Path | None = None

    async def _run(self, args: list[str], *, timeout: float = 120.0,
                   check: bool = True) -> str:
        proc = await asyncio.create_subprocess_exec(
            self.docker, *args, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise HarborRuntimeError(f"docker {' '.join(args[:2])} timeout")
        if check and proc.returncode != 0:
            message = (err or out).decode("utf-8", errors="replace").strip()
            raise HarborRuntimeError(message or f"docker {' '.join(args[:2])} failed")
        return out.decode("utf-8", errors="replace")

    async def _require_local_image(self, reference: str, role: str) -> SandboxImageInfo:
        try:
            return await asyncio.to_thread(inspect_image, reference, docker=self.docker)
        except Exception as exc:  # noqa: BLE001
            raise SandboxUnavailable(
                "harbor_image_missing",
                f"Harbor {role} image is not available locally: {reference}",
            ) from exc

    async def _ensure_selected_image(self, reference: str, role: str) -> SandboxImageInfo:
        """Ensure only the currently selected task image exists locally.

        The manifest contains digest-pinned references. Auto-preparation is
        deliberately scoped to those two references (agent + verifier) and is
        never a catalog-wide prefetch.
        """
        try:
            return await asyncio.to_thread(inspect_image, reference, docker=self.docker)
        except Exception as initial_exc:  # noqa: BLE001
            if (
                not self.spec.auto_prepare
                or self.spec.image_pull_policy != "selected-task-only"
            ):
                raise SandboxUnavailable(
                    "harbor_image_missing",
                    f"Harbor {role} image is not available locally (pull disabled): {reference}",
                ) from initial_exc
            logger.info("Harbor auto-prepare: pulling selected %s image %s", role, reference)
            try:
                await self._run(["pull", "--quiet", reference], timeout=1800)
                return await asyncio.to_thread(inspect_image, reference, docker=self.docker)
            except Exception as exc:  # noqa: BLE001
                raise SandboxUnavailable(
                    "harbor_image_prepare_failed",
                    f"Harbor could not prepare the selected {role} image: {reference}: {exc}",
                ) from exc

    @staticmethod
    def _repo_root() -> Path:
        return Path(__file__).resolve().parents[2]

    async def _ensure_codex_runtime(self, task_image: SandboxImageInfo) -> SandboxImageInfo:
        """Build the small Codex runtime lazily on the first Harbor Codex run."""
        configured = (
            os.environ.get("HARBOR_OCTAGON_RUNTIME_IMAGE")
            or getattr(getattr(self.settings, "sandbox", None), "image", None)
        )
        version = os.environ.get("HARBOR_CODEX_VERSION", "0.149.1")
        # A configured digest is an immutable runtime selection. Inspect and
        # reuse it exactly; never replace it with an auto tag or rebuild a
        # different image, otherwise the declared runtime pin is meaningless.
        target = configured or f"harbor-octagon-codex-runtime:auto-{version}"
        configured_digest = bool(configured and "@sha256:" in configured)
        try:
            return await asyncio.to_thread(inspect_image, target, docker=self.docker)
        except Exception as exc:
            if configured_digest:
                raise HarborRuntimeError(
                    f"Configured Harbor Codex runtime image is unavailable: {target}"
                ) from exc
            if not self.spec.auto_prepare:
                raise HarborRuntimeError(
                    f"Harbor Codex runtime is not available locally: {target}"
                )
        dockerfile = self._repo_root() / "docker" / "harbor-codex-runtime" / "Dockerfile"
        if not dockerfile.is_file():
            raise HarborRuntimeError(f"Harbor Codex runtime Dockerfile not found: {dockerfile}")
        logger.info("Harbor auto-prepare: building Codex runtime %s", target)
        await self._run([
            "build", "--pull=false",
            "--build-arg", f"BASE_IMAGE={task_image.reference}",
            "--build-arg", f"CODEX_VERSION={version}",
            "--file", str(dockerfile), "--tag", target, str(self._repo_root()),
        ], timeout=1800)
        try:
            return await asyncio.to_thread(inspect_image, target, docker=self.docker)
        except Exception as exc:
            raise HarborRuntimeError(f"Harbor Codex runtime inspect failed: {target}") from exc

    async def _ensure_blade_wrapper(self) -> SandboxImageInfo:
        """Build the Blade bridge lazily, without requiring a manual build step."""
        target = os.environ.get(
            "HARBOR_BLADE_WRAPPER_IMAGE", "harbor-octagon-blade-wrapper:auto"
        )
        try:
            return await asyncio.to_thread(inspect_image, target, docker=self.docker)
        except Exception:
            if not self.spec.auto_prepare:
                raise HarborRuntimeError(f"Blade wrapper is not available locally: {target}")
        dockerfile = self._repo_root() / "docker" / "harbor-blade-wrapper" / "Dockerfile"
        if not dockerfile.is_file():
            raise HarborRuntimeError(f"Blade wrapper Dockerfile not found: {dockerfile}")
        base = os.environ.get("HARBOR_BLADE_WRAPPER_BASE_IMAGE", "python:3.12-slim")
        try:
            await asyncio.to_thread(inspect_image, base, docker=self.docker)
        except Exception:
            logger.info("Harbor auto-prepare: pulling Blade wrapper base %s", base)
            await self._run(["pull", "--quiet", base], timeout=900)
        logger.info("Harbor auto-prepare: building Blade wrapper %s", target)
        await self._run([
            "build", "--pull=false", "--build-arg", f"BASE_IMAGE={base}",
            "--file", str(dockerfile), "--tag", target, str(self._repo_root()),
        ], timeout=1800)
        try:
            return await asyncio.to_thread(inspect_image, target, docker=self.docker)
        except Exception as exc:
            raise HarborRuntimeError(f"Blade wrapper inspect failed: {target}") from exc

    async def _build_overlay(self, task_image: SandboxImageInfo) -> SandboxImageInfo:
        runtime_image = await self._ensure_codex_runtime(task_image)
        if self.agent_name not in _AGENT_COPY_PATHS:
            raise HarborRuntimeError(f"unsupported Harbor agent overlay: {self.agent_name}")
        key = "\n".join((task_image.digest, runtime_image.digest, self.agent_name))
        tag = f"harbor-octagon-overlay:{_safe_tag(key)}"
        try:
            return await asyncio.to_thread(inspect_image, tag, docker=self.docker)
        except Exception:
            pass

        context = Path(self.attempt_dir or Path("/tmp")) / "harbor-overlay-build"
        if context.exists():
            shutil.rmtree(context)
        context.mkdir(parents=True)
        paths = _AGENT_COPY_PATHS[self.agent_name]
        copies = "\n".join(f"COPY --from=octagon {path} {path}" for path in paths)
        dockerfile = f"""FROM {task_image.reference} AS task\nFROM {runtime_image.reference} AS octagon\nFROM task\n{copies}\nRUN mkdir -p /home/agent /logs /tmp && chmod 0777 /home/agent /logs /tmp\nENV HOME=/home/agent PATH=/usr/local/bin:/usr/bin:/bin\nLABEL harbor.compat="true" harbor.task.image="{task_image.digest}" harbor.octagon.runtime.image="{runtime_image.digest}" harbor.agent="{self.agent_name}"\n"""
        (context / "Dockerfile").write_text(dockerfile, encoding="utf-8")
        (context / "metadata.json").write_text(json.dumps({
            "task_image": task_image.reference,
            "task_digest": task_image.digest,
            "runtime_image": runtime_image.reference,
            "runtime_digest": runtime_image.digest,
            "agent": self.agent_name,
            "overlay_tag": tag,
        }, indent=2) + "\n", encoding="utf-8")
        await self._run(["build", "--pull=false", "--tag", tag, str(context)], timeout=600)
        try:
            return await asyncio.to_thread(inspect_image, tag, docker=self.docker)
        except Exception as exc:
            raise HarborRuntimeError(f"overlay image inspect failed: {tag}") from exc

    async def _materialize_workspace(self, image: SandboxImageInfo) -> None:
        assert self.workspace is not None
        self.workspace.mkdir(parents=True, exist_ok=True)
        container = f"harbor-materialize-{self.spec.task_name.rsplit('/', 1)[-1]}-{_safe_tag(str(self.workspace))}"
        await self._run(["rm", "-f", container], check=False)
        # Start with the image's original ENTRYPOINT/CMD. Some pilot images
        # (LabBench) generate randomized, task-visible files in entrypoint.sh;
        # bypassing it would not reproduce Harbor's initial state. The static
        # images override their command with ``sleep infinity`` below.
        await self._run(["create", "--pull", "never", "--name", container,
                         image.reference, "sleep", "infinity"])
        try:
            await self._run(["start", container], timeout=120)
            await self._run(["cp", f"{container}:{self.spec.agent_workdir.rstrip('/')}/.", str(self.workspace)], timeout=300)
        finally:
            await self._run(["rm", "-f", container], check=False)
        (self.attempt_dir / "harbor" / "initial-workspace.json").write_text(json.dumps({
            "source_image": image.reference,
            "source_image_digest": image.digest,
            "source_path": self.spec.agent_workdir,
            "workspace": str(self.workspace),
            "tree_sha256": _tree_sha256(self.workspace),
        }, indent=2) + "\n", encoding="utf-8")

    async def prepare(self, *, attempt_dir: Path) -> None:
        self.attempt_dir = attempt_dir.resolve()
        self.workspace = self.attempt_dir / "skill_workspace"
        self.agent_logs = self.attempt_dir / "harbor" / "agent-logs"
        self.agent_tmp = self.attempt_dir / "harbor" / "agent-tmp"
        self.agent_logs.mkdir(parents=True, exist_ok=True)
        self.agent_tmp.mkdir(parents=True, exist_ok=True)
        task_image = await self._ensure_selected_image(self.spec.agent_image, "agent")
        # The verifier is also prepared lazily, but only for this selected task.
        verifier_image = await self._ensure_selected_image(self.spec.verifier_image, "verifier")
        if self.agent_name == "blade-agent":
            self.overlay_image = await self._ensure_blade_wrapper()
        else:
            self.overlay_image = await self._build_overlay(task_image)
        await self._materialize_workspace(task_image)
        (self.attempt_dir / "harbor" / "overlay-image.json").write_text(json.dumps({
            "task_image": task_image.reference,
            "task_image_digest": task_image.digest,
            "overlay_image": self.overlay_image.reference,
            "overlay_image_digest": self.overlay_image.digest,
            "agent": self.agent_name,
            "verifier_image": verifier_image.reference,
            "verifier_image_digest": verifier_image.digest,
        }, indent=2) + "\n", encoding="utf-8")

    def launcher(self):
        if self.overlay_image is None or self.attempt_dir is None:
            raise HarborRuntimeError("Harbor runtime.prepare() must run first")
        return build_harbor_agent_launcher(
            settings=self.settings, agent_name=self.agent_name, task=self.spec,
            image=self.overlay_image, logs_host=self.agent_logs, tmp_host=self.agent_tmp,
            docker=self.docker,
        )

    async def finalize(self, _adapter_result: Any) -> dict[str, Any]:
        if self.attempt_dir is None or self.workspace is None:
            raise HarborRuntimeError("Harbor runtime was not prepared")
        return await HarborVerifierRunner(docker=self.docker).run(
            attempt_id=self.attempt_dir.name, spec=self.spec,
            workspace=self.workspace, attempt_dir=self.attempt_dir,
            artifacts_dir=(self.agent_logs / "artifacts")
            if self.agent_logs is not None else None,
            agent_tmp_dir=self.agent_tmp,
            agent_logs_dir=self.agent_logs,
        )
