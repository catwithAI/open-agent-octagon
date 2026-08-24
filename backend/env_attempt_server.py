"""Env Attempt Server——给 blade skill `tools.py` 用的 HTTP 端点。

挂载点固定:`POST /attempts/{attempt_id}/tools/{tool_name}` 等三个路由,前端
绝不会跨过这层直接动 env DB 或 trace 文件。

鉴权契约:

- URL 中带 `attempt_id`,Header `Authorization: Bearer <env_token>`。
- `env_token` 明文与 DB 中 `attempts.env_token_hash` 用 sha256 比对。
- attempt 已进入 terminal status(completed / gave_up / timeout / 各类失败)→ 401,
  不允许"补登" trace,避免评分后 attempt 仍可被改写。
- `attempt_id` 不存在 → 404。

派发流程:

1. 鉴权后从 attempt 查 env_name + env_session_id。
2. 找 env.db 文件(`<data_path>/attempts/{attempt_id}/env.db`),不存在按
   `envs/<env_name>/schema.sql` 初始化(空文件 → executescript)。
3. 从 `app.state.envs` 拿 `LoadedEnv`,查 tool;不存在 → 404。
4. 构造 `EnvContext`,调 `@env_tool` 包装层(自动写 trace / 计时)。
5. 工具内部抛异常 → 包装层已写 `is_error=true` trace + 重抛 → 这里返回 500。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request

from .db import _open_sync, hash_env_token
from .env_loader import LoadedEnv

from octagon.env_api import EnvContext, RegisteredTool, TraceWriter

logger = logging.getLogger(__name__)

# 终态——不再接受工具调用。复用 models 的单一真源（由 AttemptStatus 全集推导），
# 不再手抄：手抄曾漏 cli_not_found/cli_error/capture_infrastructure_failed，使这些
# 终态仍能凭原 token 调用工具、改 env DB 与 trace。
from .models import TERMINAL_ATTEMPT_STATUSES as _TERMINAL_STATUSES


# ---------- 鉴权 ----------------------------------------------------------


class AuthorizedAttempt:
    """通过鉴权的 attempt 上下文。"""

    __slots__ = ("attempt_id", "env_name", "env_session_id", "status")

    def __init__(
        self, attempt_id: str, env_name: str, env_session_id: str, status: str
    ) -> None:
        self.attempt_id = attempt_id
        self.env_name = env_name
        self.env_session_id = env_session_id
        self.status = status


def _parse_bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing or malformed Bearer token")
    return authorization[len("Bearer ") :].strip()


def _query_attempt_sync(
    db_path: Path, attempt_id: str
) -> tuple[str, str, str, str] | None:
    """返回 (env_name, env_session_id, env_token_hash, status),不存在 None。"""
    if not db_path.exists():
        return None
    with _open_sync(db_path) as conn:
        row = conn.execute(
            "SELECT env_name, env_session_id, env_token_hash, status FROM attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()
    return tuple(row) if row else None


def _verify_attempt_token(
    request: Request,
    attempt_id: str,
    authorization: str | None = Header(default=None),
) -> AuthorizedAttempt:
    """FastAPI Depends:验证 Bearer token,返回授权后的 attempt 上下文。"""
    token = _parse_bearer(authorization)
    db_path = _resolve_main_db_path(request)
    found = _query_attempt_sync(db_path, attempt_id)
    if found is None:
        raise HTTPException(status_code=404, detail=f"attempt not found: {attempt_id}")
    env_name, env_session_id, token_hash, status = found
    # token 错误和 terminal 都返回 401(语义都是"无权访问"),只是 detail 区分:
    if hash_env_token(token) != token_hash:
        raise HTTPException(status_code=401, detail="invalid env token")
    if status in _TERMINAL_STATUSES:
        raise HTTPException(
            status_code=401, detail=f"attempt is terminal: status={status}"
        )
    return AuthorizedAttempt(
        attempt_id=attempt_id,
        env_name=env_name,
        env_session_id=env_session_id,
        status=status,
    )


# ---------- 派发与 env.db 初始化 ------------------------------------------


def _resolve_main_db_path(request: Request) -> Path:
    settings = request.app.state.settings
    return Path(settings.octagon.data_path) / "octagon.db"


def _resolve_data_path(request: Request) -> Path:
    return Path(request.app.state.settings.octagon.data_path)


def _resolve_env(request: Request, env_name: str) -> LoadedEnv:
    envs: dict[str, LoadedEnv] = request.app.state.envs
    env = envs.get(env_name)
    if env is None:
        raise HTTPException(status_code=500, detail=f"env not loaded: {env_name}")
    if env.load_error is not None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"env unavailable: {env.name}. "
                f"{env.load_error}. 请配置 octagon.selected_skills_path 或 "
                "SELECTED_SKILLS_DIR 后重启。"
            ),
        )
    return env


def _attempt_dir(data_path: Path, attempt_id: str) -> Path:
    p = data_path / "attempts" / attempt_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def _open_env_db(data_path: Path, attempt_id: str, env_dir: Path) -> sqlite3.Connection:
    """返回该 attempt 的 env DB 连接;不存在则按 env 的 schema.sql init。"""
    env_db_path = _attempt_dir(data_path, attempt_id) / "env.db"
    needs_init = not env_db_path.exists()
    conn = sqlite3.connect(env_db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    if needs_init:
        schema_path = env_dir / "schema.sql"
        if schema_path.is_file():
            conn.executescript(schema_path.read_text(encoding="utf-8"))
            conn.commit()
        else:
            logger.warning("env %s 没有 schema.sql,跳过 init", env_dir.name)
    return conn


def _resolve_tool(env: LoadedEnv, tool_name: str) -> RegisteredTool:
    tool = env.tools.get(tool_name)
    if tool is None:
        raise HTTPException(
            status_code=404,
            detail=f"tool not found in env={env.name}: {tool_name}",
        )
    return tool


def _serialize_result(result: Any) -> Any:
    """env tool 返回值序列化保险:dict / list / 标量直接返回;dataclass 转 dict。"""
    if is_dataclass(result) and not isinstance(result, type):
        return asdict(result)
    return result


# ---------- inbound 采集辅助（全程 fail-open）--------------------------

# 每 attempt 的 evidence 序号（evidence ID 去重用）。线程安全。
# 进程重启后续接同一 attempt，序号从**已落盘 spool 的行数**恢复，
# 不从 0 重来——否则重启后新 evidence 与旧行撞 raw_ref → 重复 evidence/record ID。
_inbound_seq: dict[str, int] = {}
_inbound_seq_lock = threading.Lock()


def _recover_inbound_seq(data_path: Path, attempt_id: str) -> int:
    """从已落盘的 env-inbound spool 恢复排序用起始序号。

    evidence ID 的**唯一性**由进程 generation anchor 保证（见
    env_capture._PROCESS_GENERATION）——即便旧数据稀疏、seq 复用也不会撞 ID。
    这里的 seq 只用于 extensions 排序，因此恢复是**尽力而为**：新格式取
    max(seq)+1、旧格式回退 http_exchange 计数，混合取更大者，让续写序号大致
    单调；恢复不准也不会造成 ID 冲突。
    """
    from backend.wire import paths as _wpaths
    from backend.wire.spool import find_spool_file, read_spool

    try:
        final = _wpaths.source_spool_file(data_path, attempt_id, "env-inbound")
        existing = find_spool_file(final)
        if existing is None:
            return 0
        max_seq = -1
        http_count = 0
        for rec in read_spool(existing).records:
            if rec.get("evidence_type") != "http_exchange":
                continue
            http_count += 1
            seq = (rec.get("extensions") or {}).get("x-octagon.env-inbound-seq")
            if isinstance(seq, int) and seq > max_seq:
                max_seq = seq
        return max(max_seq + 1, http_count)
    except Exception:
        return 0


def _next_inbound_seq(attempt_id: str, data_path: Path | None = None) -> int:
    with _inbound_seq_lock:
        if attempt_id not in _inbound_seq and data_path is not None:
            _inbound_seq[attempt_id] = _recover_inbound_seq(data_path, attempt_id)
        n = _inbound_seq.get(attempt_id, 0)
        _inbound_seq[attempt_id] = n + 1
        return n


def _json_bytes(obj: Any) -> int | None:
    try:
        return len(json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))
    except Exception:
        return None


def _now_iso_utc() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def _wire_inbound_start(
    data_path: Path, attempt_id: str, body: dict[str, Any] | None
):
    """请求到达时：快照 (capture_enabled, phase) + 登记 in-flight
    （close 的 drain 等它 end）。返回 entry 供 finish 用。"""
    from backend.wire.env_capture import begin_request, snapshot_capture_state

    try:
        enabled, phase = snapshot_capture_state(data_path, attempt_id)
    except Exception:
        enabled, phase = False, "unknown"
    entry = begin_request(data_path, attempt_id, enabled)
    return _now_iso_utc(), time.monotonic(), _json_bytes(body), enabled, phase, entry


def _wire_inbound_finish(
    *, entry, data_path: Path, attempt_id: str, tool_name: str,
    started_at: str, t0: float, request_bytes: int | None,
    response: Any, status_code: int, capture_enabled: bool, phase: str,
) -> None:
    """采集收尾：算 timing/size → http_exchange evidence（phase 用 start 快照）；
    最后 end_request 递减 in-flight。"""
    from backend.wire.env_capture import end_request, record_inbound_tool_call

    try:
        if capture_enabled:
            finished_at = _now_iso_utc()
            duration_ms = (time.monotonic() - t0) * 1000.0
            record_inbound_tool_call(
                entry=entry,
                data_path=data_path, attempt_id=attempt_id, tool_name=tool_name,
                request_bytes=request_bytes,
                response_bytes=_json_bytes(response) if response is not None else None,
                status_code=status_code, started_at=started_at, finished_at=finished_at,
                duration_ms=duration_ms, seq=_next_inbound_seq(attempt_id, data_path),
                phase=phase, capture_enabled=capture_enabled,
            )
    except Exception:
        logger.exception("wire inbound finish 失败 attempt=%s", attempt_id)
    finally:
        end_request(entry)  # in-flight 递减，drain 才能完成


# ---------- 路由 ----------------------------------------------------------


def build_router() -> APIRouter:
    router = APIRouter(prefix="/attempts", tags=["env-attempt"])

    @router.post("/{attempt_id}/tools/{tool_name}")
    async def call_tool(
        attempt_id: str,
        tool_name: str,
        body: dict[str, Any] | None,
        request: Request,
        auth: AuthorizedAttempt = Depends(_verify_attempt_token),
    ) -> Any:
        env = _resolve_env(request, auth.env_name)
        tool = _resolve_tool(env, tool_name)
        data_path = _resolve_data_path(request)

        # inbound 工具请求采集（size/timing/attempt/phase）。采集全程
        # fail-open——用独立 try/finally 保证成功/异常两路都记录，且异常发生时
        # 先记 metadata 再向上抛，不吞掉工具错误。
        started_at, t0, req_bytes, cap_enabled, cap_phase, cap_entry = _wire_inbound_start(
            data_path, attempt_id, body
        )
        status_code = 200
        serialized: Any = None
        try:
            env_db = _open_env_db(data_path, attempt_id, env.env_dir)
            try:
                ctx = EnvContext(
                    attempt_id=attempt_id,
                    env_session_id=auth.env_session_id,
                    db=env_db,
                    trace=TraceWriter(
                        data_path=data_path,
                        attempt_id=attempt_id,
                        env_session_id=auth.env_session_id,
                    ),
                )
                try:
                    result = await tool.acall(ctx, **(body or {}))
                except Exception as exc:
                    # @env_tool 包装层已写 is_error=true trace,这里只负责 HTTP 形态。
                    logger.exception("env tool 调用失败 attempt=%s tool=%s", attempt_id, tool_name)
                    status_code = 500
                    raise HTTPException(
                        status_code=500,
                        detail={
                            "error": "tool_execution_error",
                            "tool": tool_name,
                            "type": exc.__class__.__name__,
                            "message": str(exc),
                        },
                    ) from exc
            finally:
                env_db.close()
            serialized = _serialize_result(result)
            return serialized
        finally:
            _wire_inbound_finish(
                entry=cap_entry,
                data_path=data_path, attempt_id=attempt_id, tool_name=tool_name,
                started_at=started_at, t0=t0, request_bytes=req_bytes,
                response=serialized, status_code=status_code,
                capture_enabled=cap_enabled, phase=cap_phase,
            )

    @router.get("/{attempt_id}/trace")
    async def get_trace(
        attempt_id: str,
        request: Request,
        auth: AuthorizedAttempt = Depends(_verify_attempt_token),
    ) -> dict[str, Any]:
        data_path = _resolve_data_path(request)
        path = data_path / "attempts" / attempt_id / "trace.jsonl"
        items: list[dict[str, Any]] = []
        if path.exists():
            with path.open("r", encoding="utf-8") as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        items.append(json.loads(line))
                    except json.JSONDecodeError:
                        # 损坏行跳过——M1 不让单行毒坏整个 trace 接口
                        logger.warning("trace.jsonl 损坏行 attempt=%s", attempt_id)
        return {"items": items}

    @router.get("/{attempt_id}/final_state")
    async def get_final_state(
        attempt_id: str,
        request: Request,
        auth: AuthorizedAttempt = Depends(_verify_attempt_token),
    ) -> dict[str, Any]:
        # 文件不存在时返回空 dict(在任务里钉死)。前端按 {} 渲染空状态即可。
        data_path = _resolve_data_path(request)
        path = data_path / "attempts" / attempt_id / "final_state.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("final_state.json 解析失败 attempt=%s", attempt_id)
            return {}

    return router


def register_routes(app: FastAPI) -> None:
    """T1 main.py 在 create_app 里调用,把路由挂到 app 上。"""
    app.include_router(build_router())
