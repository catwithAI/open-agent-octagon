#!/usr/bin/env python3
"""Generic remote Blade bridge used inside a Harbor agent container."""
from __future__ import annotations

import asyncio
import hashlib
import json
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
    remote_files: list[str] = []
    paths = artifact_paths(req)
    requested_timeout = float(req.get("timeout_seconds") or 1800)
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
        # Do not stop when an output file first appears. An agent can create a
        # placeholder and continue refining it; Harbor's 30-minute Blade pilot
        # must wait for the remote run's actual terminal event.
        run = await client.run(
            session_id, prompt,
            RunOptions(headless=False, timeout_secs=requested_timeout, trace=True),
        )
        trace = await client.collect_trace(run)
        events = trace.events if isinstance(trace.events, list) else []
        history = trace.history if isinstance(trace.history, list) else []
        remote_files = await download_tree(client, session_id, workspace, workspace)
        for rel in tuple(remote_files):
            if rel == "logs/artifacts" or rel.startswith("logs/artifacts/"):
                src = workspace / rel
                dst = logs / rel.removeprefix("logs/")
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        (logs / "runtrace.json").write_text(json.dumps({
            "events": events, "history": history, "result": jsonable(trace.result),
            "config_snapshot": jsonable(trace.config_snapshot),
        }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

        def local_artifact_exists(path: str) -> bool:
            if path.startswith("logs/"):
                return (logs / path.removeprefix("logs/")).is_file()
            return (workspace / path).is_file()

        missing = [path for path in paths if not local_artifact_exists(path)]
        if missing:
            raise RuntimeError(
                "Blade run completed without declared Harbor artifacts: "
                + ", ".join(missing)
            )
        payload = {
            "schema_version": 1, "status": "completed", "session_id": session_id,
            "events_count": len(events), "uploaded": uploaded,
            "downloaded": remote_files, "artifact_ready": True,
            "timeout_seconds": requested_timeout,
            "external_refs": {"blade_session_id": session_id, "blade_trace": "/logs/runtrace.json"},
            "duration_ms": int((time.monotonic() - started) * 1000),
            "security_meta": {"execution_locus": "docker-sandbox", "remote_agent": "blade-agent", "sandbox_managed_by": "blade-wrapper"},
        }
    except Exception as exc:
        # If the SDK loses the final websocket event after the agent has already
        # finished, recover the workspace once. We still never synthesize an
        # answer from chat text: only the declared file can make this completed.
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
            "timeout_seconds": requested_timeout,
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
