#!/usr/bin/env python3
"""Generic remote Blade bridge used inside a Harbor agent container."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
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
        run = await client.run(
            session_id, prompt,
            RunOptions(headless=False, timeout_secs=float(req.get("timeout_seconds") or 1200), trace=True),
        )
        trace = await client.collect_trace(run)
        # Return remote workspace changes and Harbor-style public artifacts.
        remote_files = await download_tree(client, session_id, workspace, workspace)
        for rel in tuple(remote_files):
            if rel == "logs/artifacts" or rel.startswith("logs/artifacts/"):
                src = workspace / rel
                dst = logs / rel.removeprefix("logs/")
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        events = trace.events if isinstance(trace.events, list) else []
        history = trace.history if isinstance(trace.history, list) else []
        (logs / "runtrace.json").write_text(json.dumps({
            "events": events, "history": history, "result": jsonable(trace.result),
            "config_snapshot": jsonable(trace.config_snapshot),
        }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        payload = {
            "schema_version": 1, "status": "completed", "session_id": session_id,
            "events_count": len(events), "uploaded": uploaded,
            "downloaded": remote_files,
            "external_refs": {"blade_session_id": session_id, "blade_trace": "/logs/runtrace.json"},
            "duration_ms": int((time.monotonic() - started) * 1000),
            "security_meta": {"execution_locus": "docker-sandbox", "remote_agent": "blade-agent", "sandbox_managed_by": "blade-wrapper"},
        }
    except Exception as exc:
        payload = {
            "schema_version": 1, "status": "failed", "session_id": session_id,
            "error_code": "blade_sdk_failed", "error_message": str(exc),
            "external_refs": {"blade_session_id": session_id} if session_id else {},
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
