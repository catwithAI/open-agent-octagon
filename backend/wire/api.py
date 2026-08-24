"""Wire API。

四条只读路由，挂在 frontend router 下（前缀 /api 由 main 决定）：

    GET /runs/{run_id}/attempts/{attempt_id}/wire
    GET /runs/{run_id}/attempts/{attempt_id}/wire/manifest
    GET /runs/{run_id}/attempts/{attempt_id}/wire/trajectory
    GET /runs/{run_id}/attempts/{attempt_id}/wire/blobs/{ref}

- cursor 是 base64url 的 ``{"offset": <byte>, "generation": <n>}``；signature 用
  manifest 的 finalize 计数 ``generation`` 而非裸 mtime——rebuild 后
  即使 record count/文件大小相同，旧 cursor 也会 409 ``wire_changed``；
- blob 只接受白名单 ref，且 effective policy 必须 parsed/full，metadata 档 404
  ；
- canonical wire/manifest 只经本 API 访问，source spool 与 blob 不进普通
  artifact 列表（api.py 侧排除）；
- 错误文本一律过 scrub。
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .. import runtime_state
from . import paths
from .redaction import scrub_text

DEFAULT_LIMIT = 100
MAX_LIMIT = 500


def _attempt_in_run(db_path: Path, run_id: str, attempt_id: str) -> bool:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT run_id FROM attempts WHERE id=?", (attempt_id,)
        ).fetchone()
    return row is not None and row[0] == run_id


def _load_manifest(data_path: Path, attempt_id: str) -> dict[str, Any] | None:
    try:
        path = paths.manifest_file(data_path, attempt_id)
    except paths.WirePathError:
        return None
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _encode_cursor(offset: int, generation: int) -> str:
    raw = json.dumps({"offset": offset, "generation": generation}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> dict[str, Any]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded))
        if not isinstance(data.get("offset"), int) or not isinstance(
            data.get("generation"), int
        ):
            raise ValueError("cursor 字段缺失")
        return data
    except Exception:
        raise HTTPException(status_code=400, detail="invalid cursor") from None


def _hash_and_size(fh: Any, chunk: int = 1 << 20) -> tuple[int, str]:
    """分块读同一 fd 算 (bytes, sha256)，不整份进内存；读完 seek 回 0。"""
    hasher = hashlib.sha256()
    total = 0
    fh.seek(0)
    while True:
        block = fh.read(chunk)
        if not block:
            break
        total += len(block)
        hasher.update(block)
    fh.seek(0)
    return total, hasher.hexdigest()


def _scan_page(
    wire_path: Path,
    fingerprint: dict[str, Any],
    offset: int,
    generation: int,
    limit: int,
    filters: dict[str, str | None],
) -> tuple[list[dict[str, Any]], str | None]:
    """同步执行（调用方放线程池）：持同一 fd 分块校验指纹 + seek 分页扫描。

    全程钉住同一 inode（atomic rename 不影响本次读），不整份读入内存。
    抛 HTTPException（409/400）由 FastAPI 传播。
    """
    try:
        fh = wire_path.open("rb")
    except FileNotFoundError:
        raise HTTPException(status_code=409, detail="wire_changed") from None
    try:
        total_bytes, digest = _hash_and_size(fh)
        if fingerprint and (
            total_bytes != int(fingerprint.get("bytes", -1))
            or digest != fingerprint.get("sha256")
        ):
            raise HTTPException(status_code=409, detail="wire_changed")
        # 边界校验：负数/越界拒绝；offset 必须落在整行边界。
        if offset < 0 or offset > total_bytes:
            raise HTTPException(status_code=400, detail="invalid cursor")
        if offset > 0:
            fh.seek(offset - 1)
            if fh.read(1) != b"\n":
                raise HTTPException(status_code=400, detail="invalid cursor")

        items: list[dict[str, Any]] = []
        next_cursor: str | None = None
        fh.seek(offset)
        while True:
            line = fh.readline()
            if not line:
                break
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if _matches(record, filters):
                items.append(record)
            if len(items) >= limit:
                next_cursor = _encode_cursor(fh.tell(), generation)
                break
        return items, next_cursor
    finally:
        fh.close()


def _matches(record: dict[str, Any], filters: dict[str, str | None]) -> bool:
    if filters["record_type"] and record.get("record_type") != filters["record_type"]:
        return False
    if filters["phase"] and record.get("phase") != filters["phase"]:
        return False
    if filters["protocol"] and (record.get("data") or {}).get("protocol") != filters["protocol"]:
        return False
    if (
        filters["logical_call_id"]
        and (record.get("correlation") or {}).get("logical_call_id")
        != filters["logical_call_id"]
    ):
        return False
    ts = (record.get("time") or {}).get("timestamp") or ""
    if filters["after"] and ts <= filters["after"]:
        return False
    if filters["before"] and ts >= filters["before"]:
        return False
    return True


def build_wire_router() -> APIRouter:
    router = APIRouter(tags=["wire"])

    def _guard(run_id: str, attempt_id: str) -> Any:
        state = runtime_state.get()
        if not _attempt_in_run(state.db_path, run_id, attempt_id):
            raise HTTPException(
                status_code=404,
                detail=f"attempt not found under run={run_id}: {attempt_id}",
            )
        return state

    @router.get("/runs/{run_id}/attempts/{attempt_id}/wire")
    async def get_wire(
        run_id: str,
        attempt_id: str,
        record_type: str | None = None,
        phase: str | None = None,
        protocol: str | None = None,
        logical_call_id: str | None = None,
        after: str | None = None,
        before: str | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        state = _guard(run_id, attempt_id)
        limit = max(1, min(int(limit), MAX_LIMIT))
        manifest = _load_manifest(state.data_path, attempt_id)
        wire_path = paths.wire_file(state.data_path, attempt_id)
        if manifest is None or not wire_path.exists():
            return {"items": [], "next_cursor": None, "manifest_status": "not_available"}

        generation = int(manifest.get("generation", 0))
        fingerprint = manifest.get("wire_file") or {}

        filters = {
            "record_type": record_type,
            "phase": phase,
            "protocol": protocol,
            "logical_call_id": logical_call_id,
            "after": after,
            "before": before,
        }
        offset = 0
        if cursor:
            decoded = _decode_cursor(cursor)
            if decoded["generation"] != generation:
                # rebuild 后旧 cursor 无效：即使 record 数相同也不能续读
                raise HTTPException(status_code=409, detail="wire_changed")
            offset = decoded["offset"]

        # fd 读取 + 分块 hash + 逐行扫描全部是阻塞 I/O/CPU，放线程池执行，
        # 避免大文件每翻一页 O(file) hash 阻塞事件循环。
        items, next_cursor = await asyncio.to_thread(
            _scan_page, wire_path, fingerprint, offset, generation, limit, filters
        )

        # 读后复核：内容已被指纹钉住，这里只需确认 generation
        # 没有在扫描期间前进——变了说明 cursor 已过期，让客户端从头刷新。
        manifest_after = _load_manifest(state.data_path, attempt_id)
        if (
            manifest_after is None
            or int(manifest_after.get("generation", 0)) != generation
        ):
            raise HTTPException(status_code=409, detail="wire_changed")
        return {
            "items": items,
            "next_cursor": next_cursor,
            "manifest_status": manifest.get("status"),
        }

    @router.get("/runs/{run_id}/attempts/{attempt_id}/wire/manifest")
    async def get_wire_manifest(run_id: str, attempt_id: str) -> dict[str, Any]:
        state = _guard(run_id, attempt_id)
        manifest = _load_manifest(state.data_path, attempt_id)
        if manifest is None:
            return {"status": "not_available"}
        return manifest

    @router.get("/runs/{run_id}/attempts/{attempt_id}/wire/trajectory")
    async def get_wire_trajectory(run_id: str, attempt_id: str) -> dict[str, Any]:
        """读取 native normalizer 的最小 trajectory，供 wire UI 做双向关联。

        trajectory 是框架语义索引，不含 prompt/response 正文；缺失或损坏时返回
        明确 unavailable/空 steps，避免影响 canonical wire 主视图。
        """
        state = _guard(run_id, attempt_id)
        trajectory_path = paths.attempt_dir(state.data_path, attempt_id) / "trajectory.json"
        if not trajectory_path.exists():
            return {"status": "not_available", "steps": []}

        def _read() -> dict[str, Any]:
            try:
                value = json.loads(trajectory_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return {"status": "partial", "steps": []}
            steps = value.get("steps")
            if not isinstance(steps, list):
                return {"status": "partial", "steps": []}
            return {
                "status": "complete",
                "schema_version": value.get("schema_version"),
                "attempt_id": value.get("attempt_id"),
                "steps": steps,
            }

        return await asyncio.to_thread(_read)

    @router.get("/runs/{run_id}/attempts/{attempt_id}/wire/blobs/{ref}")
    async def get_wire_blob(request: Request, run_id: str, attempt_id: str, ref: str) -> Any:
        from fastapi.responses import Response

        state = _guard(run_id, attempt_id)
        # 无用户级 auth 时 blob API 默认禁用（配置显式打开才
        # 提供），且以 404 响应不泄漏存在性。
        settings = getattr(request.app.state, "settings", None)
        if not getattr(
            getattr(settings, "octagon", None), "wire_blob_api_enabled", False
        ):
            raise HTTPException(status_code=404, detail="blob not available")
        manifest = _load_manifest(state.data_path, attempt_id)
        # policy 门控：metadata/off 或无 manifest 一律 404，
        # 不泄漏 blob 是否存在。
        effective = ((manifest or {}).get("policy") or {}).get("effective")
        if effective not in ("parsed", "full"):
            raise HTTPException(status_code=404, detail="blob not available")
        try:
            blob_path = paths.resolve_blob_path(state.data_path, attempt_id, ref)
        except paths.WirePathError:
            raise HTTPException(status_code=404, detail="blob not available") from None
        if not blob_path.exists():
            raise HTTPException(status_code=404, detail="blob not available")
        try:
            data = gzip.decompress(blob_path.read_bytes())
        except (OSError, gzip.BadGzipFile) as exc:
            raise HTTPException(
                status_code=500, detail=scrub_text(f"blob read failed: {exc}")
            ) from None
        return Response(content=data, media_type="application/json")

    return router
