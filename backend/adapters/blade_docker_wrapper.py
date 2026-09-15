"""Docker-side bridge adapter for the remote Blade Agent.

The adapter itself only starts a generic wrapper process in the task container.
The wrapper owns Blade SDK calls, file upload/download, and run-trace retrieval;
there is no Harbor-task-specific agent implementation here.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from .base import AdapterCapabilities, AdapterResult, AdapterRunInput
from ..process.launcher import AttemptSpec, ExecSpec


class BladeDockerWrapperAdapter:
    capabilities = AdapterCapabilities(
        execution_locus="docker-sandbox",
        network_required="public_internet",
        iterative_session=False,
        interaction_answer=False,
    )

    def __init__(self, *, launcher: Any, config: Any) -> None:
        self.launcher = launcher
        self.config = config

    async def run(self, task: AdapterRunInput, env: Any, data_path: Path) -> AdapterResult:
        started = time.monotonic()
        data_path = Path(data_path).resolve()
        attempt_dir = data_path / "attempts" / task.attempt_id
        attempt_dir.mkdir(parents=True, exist_ok=True)
        request_host = attempt_dir / "sandbox_ro" / "blade-request.json"
        request_host.parent.mkdir(parents=True, exist_ok=True)
        token = self.config.api_key
        if not token:
            return AdapterResult(
                attempt_id=task.attempt_id, status="auth_failed",
                error_code="api_key_missing", error_message="blade.api_key not configured",
            )
        harbor_meta = task.task_context.get("_harbor") or {}
        declared_artifacts = harbor_meta.get("artifacts")
        if not isinstance(declared_artifacts, list) or not declared_artifacts:
            declared_artifacts = ["/logs/artifacts/response.txt"]
        artifact_paths = []
        for item in declared_artifacts:
            if isinstance(item, str):
                artifact_paths.append(item)
            elif isinstance(item, dict):
                source = item.get("source") or item.get("path")
                if isinstance(source, str):
                    artifact_paths.append(source)
        request = {
            "attempt_id": task.attempt_id,
            "base_url": self.config.base_url,
            "api_key": token,
            "model": self.config.model,
            "task_prompt": task.task_prompt,
            "task_context": task.task_context,
            "artifact_paths": artifact_paths,
            "timeout_seconds": task.timeout_seconds or 1200,
            # A Harbor task has no registered Octagon skill. General chat is
            # the neutral Blade surface unless an operator explicitly sets a
            # Blade entry in task context.
            "surface": "chat",
            "solution_id": task.task_context.get("_blade_solution_id", "general_chat"),
            "primary_skill_id": task.task_context.get("_blade_primary_skill_id"),
            "remote_workspace": ".",
        }
        request_host.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        # The wrapper is deliberately the only process that receives the cloud
        # API token. It reads the attempt-scoped, read-only request file; the
        # token never appears in argv or in the model prompt.
        attempt_spec = AttemptSpec(
            attempt_id=task.attempt_id, data_path=data_path, agent_name="blade-agent",
            run_id=task.run_id, workspace=attempt_dir / "skill_workspace",
        )
        command = ("python3", "/usr/local/bin/harbor-blade-wrapper", "/attempt/blade-request.json")
        requested_timeout = float(task.timeout_seconds or 1200)
        # The remote run has the Harbor budget; the local wrapper gets a small
        # cleanup/download grace period so a successful file-based answer is
        # not killed at the exact deadline while the SDK is closing its stream.
        wrapper_timeout = requested_timeout + 90.0
        try:
            async with self.launcher.attempt(attempt_spec) as sandbox:
                async with sandbox.exec(ExecSpec(
                    argv=command, cwd=str(attempt_dir / "skill_workspace"),
                    env={}, turn_id="blade-wrapper",
                )) as proc:
                    try:
                        stdout, stderr = await asyncio.wait_for(
                            proc.communicate(), timeout=wrapper_timeout
                        )
                    except asyncio.TimeoutError:
                        return AdapterResult(
                            attempt_id=task.attempt_id, status="timeout",
                            error_code="blade_wrapper_timeout",
                            error_message="Blade Docker wrapper timed out",
                            duration_ms=int((time.monotonic() - started) * 1000),
                        )
            logs = attempt_dir / "harbor" / "agent-logs"
            result_path = logs / "blade-result.json"
            if not result_path.is_file():
                return AdapterResult(
                    attempt_id=task.attempt_id, status="chat_failed",
                    error_code="blade_wrapper_result_missing",
                    error_message=(stderr.decode("utf-8", errors="replace")[-4000:]
                                   or stdout.decode("utf-8", errors="replace")[-4000:]
                                   or "wrapper did not produce blade-result.json"),
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            status = str(payload.get("status", "chat_failed"))
            if status not in {"completed", "ok"}:
                return AdapterResult(
                    attempt_id=task.attempt_id, status="chat_failed",
                    external_refs=payload.get("external_refs", {}),
                    error_code=payload.get("error_code", "blade_wrapper_failed"),
                    error_message=payload.get("error_message", "Blade wrapper failed"),
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            return AdapterResult(
                attempt_id=task.attempt_id, status="completed",
                external_refs=payload.get("external_refs", {}),
                events_count=int(payload.get("events_count", 0)),
                thinking_count=int(payload.get("thinking_count", 0)),
                tool_call_count=int(payload.get("tool_call_count", 0)),
                token_usage=payload.get("token_usage", {}),
                duration_ms=int((time.monotonic() - started) * 1000),
                transport_status="completed",
                security_meta=payload.get("security_meta", {}),
            )
        except Exception as exc:  # adapter protocol: do not leak process errors
            return AdapterResult(
                attempt_id=task.attempt_id, status="chat_failed",
                error_code="blade_wrapper_crashed", error_message=str(exc),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
