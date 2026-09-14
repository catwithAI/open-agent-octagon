"""Harbor verifier execution and reward normalization.

The verifier is intentionally a separate Docker invocation. It receives a
frozen copy of the agent workspace plus an attempt-local `/logs` directory; it
never receives the Octagon backend checkout or the agent's HOME.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import time
from pathlib import Path
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
        # below /logs/verifier. The submission is a mutable verifier-local copy
        # so tests cannot modify the agent's canonical artifact snapshot.
        "-v",
        f"{_safe_mount_path(submission_dir)}:{spec.agent_workdir}",
        "-v",
        f"{_safe_mount_path(logs_dir)}:/logs",
        "-v",
        f"{_safe_mount_path(tmp_dir)}:/tmp",
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


def _reject_symlinks(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            raise HarborVerifierError(f"symlink in submission workspace: {path}")


def snapshot_workspace(source: Path, destination: Path) -> str:
    """Copy an agent workspace into an isolated verifier input directory."""
    source = source.resolve()
    if not source.is_dir():
        raise HarborVerifierError(f"agent workspace does not exist: {source}")
    _reject_symlinks(source)
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, symlinks=False)
    manifest: list[dict[str, Any]] = []
    for path in sorted(p for p in destination.rglob("*") if p.is_file()):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest.append({"path": path.relative_to(destination).as_posix(), "sha256": digest})
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(manifest_bytes).hexdigest()


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
    ) -> dict[str, Any]:
        harbor_dir = attempt_dir / "harbor"
        submission_dir = harbor_dir / "submission"
        logs_dir = harbor_dir / "verifier-logs"
        tmp_dir = harbor_dir / "verifier-tmp"
        for directory in (harbor_dir, logs_dir, tmp_dir):
            directory.mkdir(parents=True, exist_ok=True)
        # In Harbor separate-verifier mode, files written by the agent below
        # /logs/artifacts are the submission, not part of the task workspace.
        # Copy only that public artifact tree; never forward the agent HOME or
        # arbitrary runtime logs into the verifier container.
        if artifacts_dir is not None and artifacts_dir.is_dir():
            source = artifacts_dir.resolve()
            _reject_symlinks(source)
            destination = logs_dir / "artifacts"
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(source, destination, symlinks=False)
        submission_sha256 = snapshot_workspace(workspace, submission_dir)
        argv = build_verifier_argv(
            docker=self.docker,
            attempt_id=attempt_id,
            spec=spec,
            submission_dir=submission_dir,
            logs_dir=logs_dir,
            tmp_dir=tmp_dir,
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
