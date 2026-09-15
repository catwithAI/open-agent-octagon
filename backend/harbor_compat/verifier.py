"""Harbor verifier execution and reward normalization.

The verifier is intentionally a separate Docker invocation. It receives only
manifest-declared artifact mounts plus attempt-local verifier `/logs` and `/tmp`
directories; it never receives the full agent workspace, Octagon backend
checkout, or the agent's HOME.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import time
from pathlib import Path, PurePosixPath
from typing import Any

from ..process.sandbox_preflight import SandboxUnavailable
from .spec import HarborTaskSpec

logger = logging.getLogger(__name__)


class HarborVerifierError(RuntimeError):
    """The Harbor verifier could not produce a trustworthy reward."""


def _network_arg(network_mode: str | None) -> tuple[str, ...]:
    if network_mode == "no-network":
        return ("--network", "none")
    if network_mode in {None, "public"}:
        return ()
    if network_mode.startswith(("bridge", "none")):
        return ("--network", network_mode)
    raise HarborVerifierError(f"unsupported verifier network mode: {network_mode!r}")


def _resource_args(resources: dict[str, Any]) -> tuple[str, ...]:
    args: list[str] = []
    if "cpus" in resources:
        try:
            cpus = float(resources["cpus"])
        except (TypeError, ValueError) as exc:
            raise HarborVerifierError("invalid verifier cpus") from exc
        if cpus <= 0:
            raise HarborVerifierError("verifier cpus must be positive")
        args += ["--cpus", str(cpus)]
    if "memory_mb" in resources:
        try:
            memory_mb = int(resources["memory_mb"])
        except (TypeError, ValueError) as exc:
            raise HarborVerifierError("invalid verifier memory_mb") from exc
        if memory_mb <= 0:
            raise HarborVerifierError("verifier memory_mb must be positive")
        args += ["--memory", f"{memory_mb}m"]
    if "storage_mb" in resources:
        try:
            storage_mb = int(resources["storage_mb"])
        except (TypeError, ValueError) as exc:
            raise HarborVerifierError("invalid verifier storage_mb") from exc
        if storage_mb <= 0:
            raise HarborVerifierError("verifier storage_mb must be positive")
        # Docker only enforces this option when the configured storage driver
        # supports per-container size limits. If it does not, docker run fails
        # explicitly instead of silently dropping the Harbor constraint.
        args += ["--storage-opt", f"size={storage_mb}m"]
    return tuple(args)


def _safe_mount_path(path: Path) -> str:
    return str(path.resolve())


def build_verifier_argv(
    *,
    docker: str,
    attempt_id: str,
    spec: HarborTaskSpec,
    submission_dir: Path,
    logs_dir: Path,
    tmp_dir: Path,
    artifact_mounts: tuple[tuple[Path, str, bool], ...] = (),
) -> tuple[str, ...]:
    """Build the exact Docker argv; no process is started by this function."""
    submission_dir = submission_dir.resolve()
    logs_dir = logs_dir.resolve()
    tmp_dir = tmp_dir.resolve()
    argv: list[str] = [
        docker,
        "run",
        "--rm",
        "--name",
        f"harbor-verifier-{attempt_id}",
        "--label",
        f"octagon.harbor.attempt_id={attempt_id}",
        "--label",
        f"octagon.harbor.task={spec.task_name}",
        "--pull",
        "never",
        *_resource_args(spec.verifier_resources),
        *_network_arg(spec.verifier_network_mode),
        # Harbor verifier scripts conventionally write reward and diagnostics
        # below /logs/verifier. Only explicitly declared artifacts are mounted
        # below; the full agent workspace is never exposed to the verifier.
        "-v",
        f"{_safe_mount_path(logs_dir)}:/logs",
        "-v",
        f"{_safe_mount_path(tmp_dir)}:/tmp",
    ]
    for host_path, container_path, readonly in artifact_mounts:
        suffix = ":ro" if readonly else ""
        argv += ["-v", f"{_safe_mount_path(host_path)}:{container_path}{suffix}"]
    argv += [
        spec.verifier_image,
        "bash",
        "/tests/test.sh",
    ]
    return tuple(argv)


def _read_reward(logs_dir: Path) -> tuple[float, str]:
    reward_json = logs_dir / "verifier" / "reward.json"
    if reward_json.is_file():
        try:
            payload = json.loads(reward_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HarborVerifierError(f"invalid reward.json: {exc}") from exc
        if isinstance(payload, dict):
            value = payload.get("reward")
        else:
            value = payload
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise HarborVerifierError(f"reward.json has invalid reward: {value!r}")
        reward = float(value)
        raw_path = reward_json
    else:
        reward_txt = logs_dir / "verifier" / "reward.txt"
        if not reward_txt.is_file():
            raise HarborVerifierError("verifier did not write reward.json or reward.txt")
        try:
            reward = float(reward_txt.read_text(encoding="utf-8").strip())
        except (OSError, ValueError) as exc:
            raise HarborVerifierError(f"invalid reward.txt: {exc}") from exc
        raw_path = reward_txt
    if not 0 <= reward <= 1:
        raise HarborVerifierError(f"reward outside [0, 1]: {reward!r}")
    return reward, str(raw_path)


def _hash_json(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_virtual_path(value: str, field: str) -> PurePosixPath:
    if not isinstance(value, str) or not value.startswith("/"):
        raise HarborVerifierError(f"artifact {field} must be an absolute path")
    path = PurePosixPath(value)
    if path == PurePosixPath("/") or ".." in path.parts:
        raise HarborVerifierError(f"artifact {field} contains an unsafe path: {value!r}")
    return path


def _reject_symlinks(root: Path) -> None:
    # Check the source itself as well as descendants.  ``Path.rglob`` does not
    # yield ``root``; without this guard a declared symlink file could be
    # followed by ``copy2`` and escape the artifact boundary.
    if root.is_symlink():
        raise HarborVerifierError(f"symlink in artifact source: {root}")
    if root.is_dir():
        for path in root.rglob("*"):
            if path.is_symlink():
                raise HarborVerifierError(f"symlink in artifact source: {path}")


def _copy_entry(source: Path, destination: Path) -> None:
    if not source.exists() and not source.is_symlink():
        raise HarborVerifierError(f"declared Harbor artifact is missing: {source}")
    _reject_symlinks(source)
    if destination.is_symlink():
        raise HarborVerifierError(f"symlink in artifact destination: {destination}")
    if destination.exists():
        if destination.is_dir() and not destination.is_symlink():
            shutil.rmtree(destination)
        else:
            destination.unlink()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, destination, symlinks=False)
    elif source.is_file():
        shutil.copy2(source, destination)
    else:
        raise HarborVerifierError(f"unsupported Harbor artifact source: {source}")


def _artifact_files(root: Path, virtual_root: PurePosixPath):
    if root.is_file():
        yield virtual_root.as_posix(), root
        return
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        yield (virtual_root / path.relative_to(root).as_posix()).as_posix(), path


def _artifact_source(
    source_path: PurePosixPath,
    *,
    spec: HarborTaskSpec,
    workspace: Path,
    agent_tmp_dir: Path,
    agent_logs_dir: Path,
    attempt_dir: Path,
) -> Path:
    workdir = PurePosixPath(spec.agent_workdir)
    # /app, /workspace and /testbed are common Harbor task workdir aliases.
    # Octagon's agent workspace is the source for each of them.
    workspace_roots = sorted(
        {workdir, PurePosixPath("/app"), PurePosixPath("/workspace"), PurePosixPath("/testbed")},
        key=lambda root: len(root.parts), reverse=True,
    )
    for root in workspace_roots:
        if source_path == root or str(source_path).startswith(root.as_posix().rstrip("/") + "/"):
            return workspace / str(source_path.relative_to(root))
    for root, host_root in (
        (PurePosixPath("/tmp"), agent_tmp_dir),
        (PurePosixPath("/logs"), agent_logs_dir),
        (PurePosixPath("/home/agent"), attempt_dir / "sandbox_home"),
    ):
        if source_path == root or str(source_path).startswith(root.as_posix().rstrip("/") + "/"):
            return host_root / str(source_path.relative_to(root))
    raise HarborVerifierError(
        f"unsupported Harbor artifact source root: {source_path.as_posix()}"
    )


def materialize_artifacts(
    *,
    spec: HarborTaskSpec,
    workspace: Path,
    attempt_dir: Path,
    submission_dir: Path,
    logs_dir: Path,
    tmp_dir: Path,
    agent_tmp_dir: Path | None = None,
    agent_logs_dir: Path | None = None,
) -> tuple[str, tuple[tuple[Path, str, bool], ...]]:
    """Stage and hash only the artifacts declared by the Harbor task.

    The verifier receives per-artifact read-only mounts. It never receives the
    entire agent workspace, which may contain task inputs, scratch files, or
    private material not listed in the Harbor manifest.
    """
    workspace = workspace.resolve()
    attempt_dir = attempt_dir.resolve()
    submission_dir = submission_dir.resolve()
    logs_dir = logs_dir.resolve()
    tmp_dir = tmp_dir.resolve()
    agent_tmp_dir = (agent_tmp_dir or attempt_dir / "harbor" / "agent-tmp").resolve()
    agent_logs_dir = (agent_logs_dir or attempt_dir / "harbor" / "agent-logs").resolve()
    if submission_dir.exists():
        shutil.rmtree(submission_dir)
    submission_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []
    mounts: list[tuple[Path, str, bool]] = []
    seen_destinations: set[str] = set()

    for index, raw_artifact in enumerate(spec.artifacts):
        if isinstance(raw_artifact, str):
            source_raw = destination_raw = raw_artifact
        elif isinstance(raw_artifact, dict):
            source_raw = raw_artifact.get("source") or raw_artifact.get("path")
            destination_raw = raw_artifact.get("destination") or raw_artifact.get("target") or source_raw
        else:
            raise HarborVerifierError(f"invalid Harbor artifact declaration at index {index}")
        source_path = _safe_virtual_path(source_raw, "source")
        destination_path = _safe_virtual_path(destination_raw, "destination")
        destination_key = destination_path.as_posix()
        if destination_key in seen_destinations:
            raise HarborVerifierError(f"duplicate Harbor artifact destination: {destination_key}")
        seen_destinations.add(destination_key)
        source = _artifact_source(
            source_path, spec=spec, workspace=workspace,
            agent_tmp_dir=agent_tmp_dir, agent_logs_dir=agent_logs_dir,
            attempt_dir=attempt_dir,
        )

        # /logs and /tmp are already mounted as directory trees. Copy only the
        # declared child into those trees; never copy the full public tree.
        if destination_path == PurePosixPath("/logs") or str(destination_path).startswith("/logs/"):
            staged = logs_dir / str(destination_path.relative_to("/logs"))
            _copy_entry(source, staged)
        elif destination_path == PurePosixPath("/tmp") or str(destination_path).startswith("/tmp/"):
            staged = tmp_dir / str(destination_path.relative_to("/tmp"))
            _copy_entry(source, staged)
        else:
            staged = submission_dir / "mounts" / destination_path.as_posix().lstrip("/")
            _copy_entry(source, staged)
            mounts.append((staged, destination_path.as_posix(), True))

        for virtual_name, path in _artifact_files(source, destination_path):
            manifest.append({
                "path": virtual_name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            })

    manifest_bytes = json.dumps(
        sorted(manifest, key=lambda item: item["path"]),
        ensure_ascii=False, sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(manifest_bytes).hexdigest(), tuple(mounts)


class HarborVerifierRunner:
    def __init__(self, *, docker: str = "docker") -> None:
        self.docker = docker

    async def run(
        self,
        *,
        attempt_id: str,
        spec: HarborTaskSpec,
        workspace: Path,
        attempt_dir: Path,
        artifacts_dir: Path | None = None,
        agent_tmp_dir: Path | None = None,
        agent_logs_dir: Path | None = None,
    ) -> dict[str, Any]:
        harbor_dir = attempt_dir / "harbor"
        submission_dir = harbor_dir / "submission"
        logs_dir = harbor_dir / "verifier-logs"
        tmp_dir = harbor_dir / "verifier-tmp"
        for directory in (harbor_dir, logs_dir, tmp_dir):
            directory.mkdir(parents=True, exist_ok=True)
        submission_sha256, artifact_mounts = materialize_artifacts(
            spec=spec, workspace=workspace, attempt_dir=attempt_dir,
            submission_dir=submission_dir, logs_dir=logs_dir, tmp_dir=tmp_dir,
            agent_tmp_dir=agent_tmp_dir,
            agent_logs_dir=(agent_logs_dir or (
                artifacts_dir.parent if artifacts_dir is not None else None
            )),
        )
        argv = build_verifier_argv(
            docker=self.docker,
            attempt_id=attempt_id,
            spec=spec,
            submission_dir=submission_dir,
            logs_dir=logs_dir,
            tmp_dir=tmp_dir,
            artifact_mounts=artifact_mounts,
        )
        (harbor_dir / "verifier-argv.json").write_text(
            json.dumps(list(argv), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=spec.verifier_timeout_seconds
            )
        except asyncio.TimeoutError:
            timed_out = True
            try:
                os.killpg(proc.pid, 15)
            except ProcessLookupError:
                pass
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
            except asyncio.TimeoutError:
                proc.kill()
                stdout, stderr = await proc.communicate()
        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        (harbor_dir / "verifier-stdout.txt").write_text(stdout_text, encoding="utf-8")
        (harbor_dir / "verifier-stderr.txt").write_text(stderr_text, encoding="utf-8")

        result: dict[str, Any] = {
            "schema_version": 1,
            "status": "timeout" if timed_out else ("completed" if proc.returncode == 0 else "failed"),
            "release": spec.release,
            "task_name": spec.task_name,
            "task_digest": spec.task_digest,
            "agent_image": spec.agent_image,
            "verifier_image": spec.verifier_image,
            "verifier_exit_code": proc.returncode,
            "verifier_elapsed_seconds": round(time.monotonic() - started, 6),
            "verifier_network_mode": spec.verifier_network_mode,
            "verifier_resources": dict(spec.verifier_resources),
            "submission_sha256": submission_sha256,
        }
        if timed_out:
            result["error"] = "verifier timeout"
        elif proc.returncode == 0:
            try:
                reward, reward_path = _read_reward(logs_dir)
            except HarborVerifierError as exc:
                result["status"] = "failed"
                result["error"] = str(exc)
            else:
                result["reward"] = reward
                result["reward_path"] = reward_path
        else:
            result["error"] = stderr_text.strip()[-4000:] or "verifier exited non-zero"
        result["result_sha256"] = _hash_json(result)
        (harbor_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if result["status"] != "completed" or "reward" not in result:
            raise HarborVerifierError(result.get("error", "verifier failed"))
        return result
