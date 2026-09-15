#!/usr/bin/env python3
"""Generic remote Blade bridge used inside a Harbor agent container."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import posixpath
import shutil
import sys
import time
from pathlib import Path, PurePosixPath
from typing import Any

from blade_agent_kit import BladeAgentClient, RunOptions


def safe_rel(raw: str) -> str | None:
    p = PurePosixPath(raw.lstrip("/"))
    if not raw or p.is_absolute() or ".." in p.parts or any(part.startswith(".") for part in p.parts):
        return None
    return p.as_posix()


def jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def rewrite_prompt(prompt: str) -> str:
    # The remote Blade workspace is represented as `/app` in the outer
    # container. Files are uploaded relative to the remote workspace root;
    # `logs/artifacts` is copied back to the outer /logs mount.
    return (prompt.replace("/app/", "")
                 .replace("/app", ".")
                 .replace("/logs/", "logs/")
                 .replace("/logs", "logs"))


async def upload_tree(client: BladeAgentClient, session_id: str, root: Path) -> list[str]:
    uploaded: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root).as_posix()
        if rel.startswith("."):
            continue
        await client.upload_file(session_id, str(path), remote_path=rel)
        uploaded.append(rel)
    return uploaded


async def download_tree(client: BladeAgentClient, session_id: str, root: Path, destination: Path) -> list[str]:
    destination.mkdir(parents=True, exist_ok=True)
    queue = ["."]
    downloaded: list[str] = []
    while queue:
        current = queue.pop(0)
        for entry in await client.list_dir(session_id, current):
            rel = entry.path or (entry.name if current == "." else f"{current}/{entry.name}")
            rel = rel.lstrip("./")
            clean = safe_rel(rel)
            if clean is None:
                continue
            if entry.is_dir:
                if clean.startswith("."):
                    continue
                queue.append(clean)
                continue
            target = destination / clean
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(await client.download_file(session_id, clean))
            downloaded.append(clean)
    return downloaded


def artifact_paths(req: dict[str, Any]) -> list[str]:
    raw_items = req.get("artifact_paths") or ["/logs/artifacts/response.txt"]
    paths: list[str] = []
    for item in raw_items:
        if isinstance(item, dict):
            item = item.get("source") or item.get("path") or item.get("destination")
        if not isinstance(item, str) or not item.startswith("/"):
            continue
        # The remote session root corresponds to the outer container's /app.
        # /logs is represented as a sibling directory in that remote workspace.
        if item == "/app":
            rel = "."
        elif item.startswith("/app/"):
            rel = item.removeprefix("/app/")
        else:
            rel = item.lstrip("/")
        clean = safe_rel(rel)
        if clean and clean not in paths:
            paths.append(clean)
    return paths


async def remote_file_exists(client: BladeAgentClient, session_id: str, rel: str) -> bool:
    parent, name = posixpath.split(rel)
    try:
        entries = await client.list_dir(session_id, parent or ".")
    except Exception:
        return False
    for entry in entries:
        entry_name = getattr(entry, "name", "")
        entry_path = (getattr(entry, "path", "") or entry_name).lstrip("./")
        if not getattr(entry, "is_dir", False) and (entry_name == name or entry_path == rel):
            return True
    return False


async def wait_for_artifacts(
    client: BladeAgentClient,
    session_id: str,
    paths: list[str],
    timeout_secs: float,
) -> bool:
    if not paths:
        return False
    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        if all(await remote_file_exists(client, session_id, path) for path in paths):
            return True
        await asyncio.sleep(min(2.0, max(0.1, deadline - time.monotonic())))
    return False


async def main() -> None:
    req = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    logs = Path("/logs")
    workspace = Path("/app")
    logs.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    client = BladeAgentClient(
        req["base_url"], token=req.get("api_key"), timeout=30.0,
        reconnect_grace_seconds=30.0,
    )
    session_id: str | None = None
    run = None
    artifact_ready = False
    remote_files: list[str] = []
    paths = artifact_paths(req)
    # The remote run receives the Harbor budget. The outer adapter adds a
    # cleanup/download grace period, so a successful file-based answer is not
    # killed at the exact deadline while the SDK is closing its stream.
    requested_timeout = float(req.get("timeout_seconds") or 1200)
    run_timeout = max(30.0, requested_timeout)
    try:
        kwargs: dict[str, Any] = {"memory_enabled": False}
        if req.get("model"):
            kwargs["model"] = req["model"]
        if req.get("solution_id"):
            kwargs["solution_id"] = req["solution_id"]
        elif req.get("primary_skill_id"):
            kwargs["primary_skill_id"] = req["primary_skill_id"]
        session = await client.create_session(intent="harbor task", **kwargs)
        session_id = session.id
        uploaded = await upload_tree(client, session_id, workspace)
        prompt = rewrite_prompt(req["task_prompt"])
        run_task = asyncio.create_task(client.run(
            session_id,
            prompt,
            RunOptions(headless=False, timeout_secs=run_timeout, trace=True),
        ))
        artifact_task = asyncio.create_task(
            wait_for_artifacts(client, session_id, paths, run_timeout)
        )
        done, _ = await asyncio.wait(
            {run_task, artifact_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if artifact_task in done and artifact_task.result():
            artifact_ready = True
            # The Harbor contract is file-based. Once every declared output is
            # present, stop a remote chat that may keep streaming indefinitely.
            try:
                await client.stop(session_id)
            except Exception:
                pass
            try:
                run = await asyncio.wait_for(asyncio.shield(run_task), timeout=15)
            except Exception:
                run_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await run_task
        else:
            artifact_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await artifact_task
            run = await run_task

        if run is not None:
            trace = await client.collect_trace(run)
            events = trace.events if isinstance(trace.events, list) else []
            history = trace.history if isinstance(trace.history, list) else []
            trace_result = jsonable(trace.result)
            trace_config = jsonable(trace.config_snapshot)
        else:
            # Artifact-first completion can intentionally stop before chat:end.
            # Preserve whatever history the service has made available.
            try:
                history_obj = await client.get_history(session_id)
                history = history_obj.nodes if isinstance(history_obj.nodes, list) else []
            except Exception:
                history = []
            events = []
            trace_result = None
            trace_config = {}

        # Return remote workspace changes and Harbor-style public artifacts.
        remote_files = await download_tree(client, session_id, workspace, workspace)
        for rel in tuple(remote_files):
            if rel == "logs/artifacts" or rel.startswith("logs/artifacts/"):
                src = workspace / rel
                dst = logs / rel.removeprefix("logs/")
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        (logs / "runtrace.json").write_text(json.dumps({
            "events": events, "history": history, "result": trace_result,
            "config_snapshot": trace_config,
        }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        downloaded_artifacts = [
            path for path in paths
            if (workspace / path).is_file() or (logs / path.removeprefix("logs/")).is_file()
        ]
        artifact_ready = artifact_ready or len(downloaded_artifacts) == len(paths)
        if not artifact_ready:
            raise TimeoutError("Blade run ended without all declared Harbor artifacts")
        payload = {
            "schema_version": 1, "status": "completed", "session_id": session_id,
            "events_count": len(events), "uploaded": uploaded,
            "downloaded": remote_files, "artifact_ready": True,
            "external_refs": {"blade_session_id": session_id, "blade_trace": "/logs/runtrace.json"},
            "duration_ms": int((time.monotonic() - started) * 1000),
            "security_meta": {"execution_locus": "docker-sandbox", "remote_agent": "blade-agent", "sandbox_managed_by": "blade-wrapper"},
        }
    except Exception as exc:
        # A remote run can fail after writing the Harbor output. Try to recover
        # that declared file before reporting failure; this is important for
        # providers that lose the final websocket event after tool completion.
        if session_id:
            try:
                remote_files = await download_tree(client, session_id, workspace, workspace)
                for rel in tuple(remote_files):
                    if rel == "logs/artifacts" or rel.startswith("logs/artifacts/"):
                        src = workspace / rel
                        dst = logs / rel.removeprefix("logs/")
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dst)
            except Exception:
                pass
        def local_artifact_exists(path: str) -> bool:
            if path.startswith("logs/"):
                return (logs / path.removeprefix("logs/")).is_file()
            return (workspace / path).is_file()

        recovered = bool(paths) and all(local_artifact_exists(path) for path in paths)
        payload = {
            "schema_version": 1,
            "status": "completed" if recovered else "failed",
            "session_id": session_id,
            "error_code": None if recovered else "blade_sdk_failed",
            "error_message": None if recovered else str(exc),
            "artifact_ready": recovered,
            "external_refs": {"blade_session_id": session_id} if session_id else {},
            "downloaded": remote_files,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    finally:
        if session_id:
            try:
                await client.delete_session(session_id)
            except Exception:
                pass
        await client.close()
    (logs / "blade-result.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if payload["status"] != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
