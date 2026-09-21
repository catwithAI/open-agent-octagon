"""前端面向的 REST API。

挂载方式:`register_routes(app)` 在 `main.create_app()` 末尾被调用。

API 设计:

- `GET /agents` —— 返回 `[{"id": "blade-agent", "enabled": True}]`,空 enum 占位,
  方便后续接入对照组扩展。
- `GET /blade/config` —— 返回当前 blade 接入参数 + `health` 子结构。token 字段
  脱敏成布尔。
- `POST /runs` —— 创建一次评测。支持 `task_id`(预置 task)与自由 `prompt`
  (创建 `adhoc_<uuid>` task)。`agent != "blade-agent"` → 400。**当前不在
  请求路径上启动 adapter**(adapter 需要真实 blade server),只把
  attempt 写进 DB 等运行;启动逻辑放到 `backend.runner` 里,后续接入。
- `GET /runs` —— 历史列表。
- `GET /runs/{id}` —— run 概况 + attempts 概况。
- `GET /runs/{id}/attempts/{aid}` —— 完整 attempt 详情:scores / tool_calls /
  blade events / final_state。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import APIRouter, BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

from . import runtime_state
from .artifact_preview import inspect_artifact, scheduled_preview_descriptor
from .artifact_scope import iter_artifact_files
from .db import _now_iso, _open_sync, list_attempt_observations
from .run_service import (
    NormalizedRunRequest,
    RunServiceError,
    _dispatch_all,
    _dispatch_serial,
    _normalize_model_for_agent as _service_normalize_model_for_agent,
    create_run_plan,
)

logger = logging.getLogger(__name__)


# ---------- /agents -------------------------------------------------------


def _list_agents(settings) -> list[dict[str, Any]]:
    agents = []
    # blade-agent
    blade_available = bool(settings.blade.api_key)
    agents.append({
        "name": "blade-agent",
        "status": "available" if blade_available else "not_configured",
        "detail": None if blade_available else "blade.api_key not set",
    })
    # claude-code
    claude_path = shutil.which("claude")
    agents.append({
        "name": "claude-code",
        "status": "available" if claude_path else "not_found",
        "cli_path": claude_path,
    })
    # codex
    codex_path = shutil.which("codex")
    agents.append({
        "name": "codex",
        "status": "available" if codex_path else "not_found",
        "detail": None if codex_path else "codex CLI not found in PATH",
        "cli_path": codex_path,
    })
    # kimi-code / opencode / mimo-code：可用性判据与 CC/codex 一致（CLI 在
    # PATH 上即可用）。三者的模型必须显式配置到各自 provider，故 detail 里
    # 不额外探测登录态——缺 key 由 adapter 启动前 fail fast 报 auth_failed。
    for name, executable in (
        ("kimi-code", "kimi"),
        ("opencode", "opencode"),
        ("mimo-code", "mimo"),
    ):
        cli_path = shutil.which(executable)
        agents.append({
            "name": name,
            "status": "available" if cli_path else "not_found",
            "detail": None if cli_path else f"{executable} CLI not found in PATH",
            "cli_path": cli_path,
        })
    # dsh：装的是 pip 包（deepseek-harness-sdk），PATH 上没有可执行文件，
    # 所以判据是模块能否被找到。共享实现与 experiments/protocol.py 同源。
    from .adapters.dsh_events import module_available

    dsh_installed = module_available("deepseek_harness")
    agents.append({
        "name": "dsh",
        "status": "available" if dsh_installed else "not_found",
        "detail": None if dsh_installed else (
            "deepseek-harness-sdk 未安装（uv pip install 'agent-octagon[dsh]'）"
        ),
    })
    return agents


# ---------- /blade/config -------------------------------------------------


async def _blade_health(base_url: str) -> dict[str, Any]:
    """ping `{base_url}/healthz`(blade-agent 默认健康检查)。

    容错:任何异常都映射成 `{"reachable": False, "error": ...}`,前端禁用
    Submit 按钮即可。
    """
    url = f"{base_url.rstrip('/')}/healthz"
    try:
        async with httpx.AsyncClient(timeout=2.0) as cli:
            resp = await cli.get(url)
        return {
            "reachable": resp.status_code == 200,
            "status_code": resp.status_code,
        }
    except Exception as exc:
        return {"reachable": False, "error": str(exc)}


async def _blade_models(base_url: str, api_key: str | None) -> dict[str, Any]:
    if not api_key:
        return {"default": None, "models": [], "error": "blade.api_key not configured"}
    url = f"{base_url.rstrip('/')}/api/config/models"
    try:
        async with httpx.AsyncClient(
            timeout=5.0,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        ) as cli:
            resp = await cli.get(url)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise TypeError(f"unexpected models response: {type(data).__name__}")
        models = data.get("models")
        if not isinstance(models, list):
            models = []
        return {
            "default": data.get("default"),
            "models": models,
        }
    except Exception as exc:
        return {"default": None, "models": [], "error": str(exc)}


# OpenRouter 全量模型列表：给 same-model 下拉列裸模型名（cc/codex/ba 三方共用
# OpenRouter，前端按选中 agent 拼 provider 前缀）。几百个模型 + 接口有延迟，
# 模块级 TTL 缓存 + Lock 防并发穿透；失败时回退旧缓存（stale-while-error）。
_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
_OR_CACHE_TTL_SECONDS = 600.0
_or_models_cache: dict[str, Any] = {"data": None, "expires_at": 0.0}
_or_models_lock = asyncio.Lock()


def _arch_modalities(m: dict[str, Any], key: str) -> list[str]:
    arch = m.get("architecture")
    if not isinstance(arch, dict):
        return []
    values = arch.get(key)
    if not isinstance(values, list):
        return []
    return [v for v in values if isinstance(v, str)]


async def _openrouter_models() -> dict[str, Any]:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return {"models": [], "error": "OPENROUTER_API_KEY not set"}
    now = time.monotonic()
    cached = _or_models_cache.get("data")
    if cached is not None and _or_models_cache.get("expires_at", 0.0) > now:
        return {"models": cached, "error": None}
    async with _or_models_lock:
        # 双检：等锁期间可能已被别的请求刷新。
        now = time.monotonic()
        cached = _or_models_cache.get("data")
        if cached is not None and _or_models_cache.get("expires_at", 0.0) > now:
            return {"models": cached, "error": None}
        try:
            async with httpx.AsyncClient(
                timeout=10.0,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
            ) as cli:
                resp = await cli.get(_OPENROUTER_MODELS_URL)
            resp.raise_for_status()
            data = resp.json()
            raw = data.get("data") if isinstance(data, dict) else None
            if not isinstance(raw, list):
                raise TypeError("unexpected OpenRouter models response")
            models = [
                {
                    "id": m["id"],
                    "name": m.get("name") or m["id"],
                    "context_length": m.get("context_length"),
                    # architecture.input/output_modalities：前端展示模型能力
                    # 徽标 + 与 env 声明的 agent_modalities 交叉预警（如
                    # ppt-visual-repair 需要 image 输入，text-only 模型跑它
                    # 必然 404——run_57da0af6f4d6 实例）
                    "input_modalities": _arch_modalities(m, "input_modalities"),
                    "output_modalities": _arch_modalities(m, "output_modalities"),
                }
                for m in raw
                if isinstance(m, dict) and isinstance(m.get("id"), str)
            ]
            _or_models_cache["data"] = models
            _or_models_cache["expires_at"] = now + _OR_CACHE_TTL_SECONDS
            return {"models": models, "error": None}
        except Exception as exc:
            # 抖动时回退旧缓存，避免下拉一次失败就清空。
            if cached is not None:
                logger.warning("openrouter models fetch failed, serving stale: %s", exc)
                return {"models": cached, "error": None, "stale": True}
            return {"models": [], "error": str(exc)}


def _normalize_model_for_agent(
    agent_name: str,
    model: str | None,
    settings: Any,
) -> str | None:
    """Backward-compatible import for callers that used the old API helper."""
    try:
        return _service_normalize_model_for_agent(agent_name, model, settings)
    except RunServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


# ---------- /runs body ----------------------------------------------------


class CreateRunRequest(BaseModel):
    env_name: str
    task_id: str | None = None
    prompt: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    # None → 不限时（不注入时间预算文案、不启用执行层 wait_for/deadline）。
    # 用于测「不限时」基线；设值时以秒为单位告知 agent 并强制超时。
    timeout_seconds: int | None = Field(default=1000, gt=0)
    agents: list[str] = Field(default=["blade-agent"])
    compare_mode: str = "multi-agent"
    model: str | None = None
    # same-model 模式：dict[agent, model]（per-agent 指定，必须覆盖所有 agents）；
    # multi-model 模式：list[model]（每个模型一个 blade-agent attempt）。
    # 与单值 model 同时传时 models 优先。
    models: dict[str, str] | list[str] | None = None
    # 执行方式：serial（排队，前一个 attempt 结束再起下一个）| parallel（并发）。
    # 不传时默认并行，保证同一任务的候选 attempt 同时启动。只有显式传
    # serial 的兼容调用才保留排队语义；研究实验协议会统一归一化为 parallel。
    execution: str | None = None
    blade_model: str | None = None
    blade_enable_thinking: bool | None = None
    # 通信采集档：off|metadata|parsed|full，与 server maximum 求最严格交集。
    # 默认 metadata（只记 size/timing）；只有显式 full 才落写盘前脱敏的协议原文。
    # 对 same-model fan-out 的每个 attempt 固化同一 requested policy。
    capture_policy: Literal["off", "metadata", "parsed", "full"] | None = None
    # ponytail: 保留旧字段兼容，优先用 agents
    agent: str | None = None

    @model_validator(mode="after")
    def _normalize(self) -> "CreateRunRequest":
        if self.agent and not self.agents:
            self.agents = [self.agent]
        elif self.agent and self.agent not in self.agents:
            self.agents = [self.agent]
        return self


# ---------- DB helpers ----------------------------------------------------


def _list_runs_sync(
    db_path: Path, *, limit: int = 50, offset: int = 0
) -> dict[str, Any]:
    with _open_sync(db_path) as conn:
        conn.row_factory = sqlite3.Row
        total = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        rows = conn.execute(
            "SELECT r.id AS run_id, r.task_id, r.env_name, r.status AS run_status,"
            " r.compare_mode, r.model, r.execution,"
            " r.created_at, r.started_at, r.ended_at,"
            " (SELECT COUNT(*) FROM attempts a WHERE a.run_id=r.id) AS attempt_count"
            " FROM runs r ORDER BY r.created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        items = [dict(row) for row in rows]
        attempts_by_run: dict[str, list[dict[str, Any]]] = {
            item["run_id"]: [] for item in items
        }
        if items:
            placeholders = ",".join("?" for _ in items)
            attempts = conn.execute(
                "SELECT id,run_id,agent_name,model,rubric_version,status,score_total,"
                "model_integrity_status "
                f"FROM attempts WHERE run_id IN ({placeholders}) ORDER BY created_at,id",
                tuple(item["run_id"] for item in items),
            ).fetchall()
            for attempt in attempts:
                projected = dict(attempt)
                attempts_by_run[projected.pop("run_id")].append(projected)
        for item in items:
            item["attempts"] = attempts_by_run[item["run_id"]]
    return {"items": items, "total": total,
            "limit": limit, "offset": offset}


def _get_run_sync(db_path: Path, run_id: str) -> dict[str, Any] | None:
    with _open_sync(db_path) as conn:
        conn.row_factory = sqlite3.Row
        run = conn.execute(
            "SELECT * FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        if run is None:
            return None
        attempts = conn.execute(
            "SELECT id, agent_name, model, rubric_version, status, transport_status, score_total, event_count,"
            " heartbeat_at,heartbeat_seq,current_stage,progress_message,"
            " thinking_count, tool_call_count, token_usage_json, cost_estimate,"
            " duration_ms, started_at, ended_at, external_refs_json,"
            " error_code, error_message,"
            " execution_status,execution_started_at,execution_ended_at,"
            " execution_deadline_at,execution_error_code,execution_error_message,"
            " scoring_status,scoring_queued_at,scoring_started_at,scoring_ended_at,"
            " scoring_deadline_at,scoring_error_code,scoring_error_message,"
            " execution_locus, security_event_count, security_max_severity,"
            " wire_status, wire_record_count, wire_call_count, wire_error_count,"
            " cost_usd, cost_priced, cost_model,"
            " model_integrity_status,model_integrity_observed_json,"
            " model_integrity_violation_count,model_integrity_error_code,"
            " model_integrity_error_message,model_integrity_checked_at"
            " FROM attempts WHERE run_id=? ORDER BY created_at",
            (run_id,),
        ).fetchall()
    # 资金口径：列表里的每个 attempt 都带上其临时 key 的上游
    # 实扣。展示费用只能用 `money`。
    money_by_attempt = _attempt_money_for_run(db_path, run_id)
    att_rows = []
    for a in attempts:
        row = dict(a)
        try:
            refs = json.loads(row.pop("external_refs_json") or "{}")
        except json.JSONDecodeError:
            refs = {}
        row["model_used"] = refs.get("model_used")
        row["model_integrity"] = _model_integrity_from_row(row)
        # 本地估算字段一律改名带 estimated_ 前缀：此前叫
        # `cost_usd`，与 ledger 的同名字段（上游实扣）只差一个上下文，
        # 新消费者极易照着字面取用，正是本次误读的成因。名字本身必须
        # 说明口径，不能只靠代码注释提醒。
        priced = row.pop("cost_priced", None)
        row["estimated_cost_usd"] = row.pop("cost_usd", None)
        row["estimated_cost_priced"] = None if priced is None else bool(priced)
        row["estimated_cost_model"] = row.pop("cost_model", None)
        # adapter 自报的成本同样是估算（各 CLI 自己算的），不是上游实扣。
        row["adapter_reported_cost_usd"] = row.pop("cost_estimate", None)
        # 机器可读的口径声明：消费端无需读注释就知道这是估算而非实扣。
        row["cost_source"] = "local_estimate"
        money = money_by_attempt.get(row["id"])
        if money is None:
            money = _money_unavailable(row["estimated_cost_usd"], priced)
        else:
            money["estimate"] = {
                "estimate_cost_usd": row["estimated_cost_usd"],
                "estimate_priced": row["estimated_cost_priced"],
            }
        row["money"] = money
        att_rows.append(row)
    violated = [
        row["id"] for row in att_rows
        if row["model_integrity"]["status"] == "violated"
    ]
    all_verified = bool(att_rows) and all(
        row["model_integrity"]["status"] == "verified" for row in att_rows
    )
    run_integrity = {
        "status": (
            "violated" if violated else "verified" if all_verified else "not_observed"
        ),
        "valid": False if violated else True if all_verified else None,
        "violated_attempt_ids": violated,
    }
    return {
        **dict(run),
        "attempts": att_rows,
        "model_integrity": run_integrity,
    }


def _model_integrity_from_row(row: dict[str, Any]) -> dict[str, Any]:
    """Pop storage fields and return the public attempt integrity block."""
    raw = row.pop("model_integrity_observed_json", "[]")
    try:
        observed = json.loads(raw or "[]")
        if not isinstance(observed, list):
            observed = []
    except (TypeError, json.JSONDecodeError):
        observed = []
    status = row.pop("model_integrity_status", None) or "not_observed"
    return {
        "expected_model": row.get("model"),
        "status": status,
        "observed_models": [item for item in observed if isinstance(item, str)],
        "violation_count": int(
            row.pop("model_integrity_violation_count", 0) or 0
        ),
        "error_code": row.pop("model_integrity_error_code", None),
        "error_message": row.pop("model_integrity_error_message", None),
        "checked_at": row.pop("model_integrity_checked_at", None),
        "valid": False if status == "violated" else (
            True if status == "verified" else None
        ),
    }


def _money_unavailable(
    estimate_usd: float | None, priced: Any = None
) -> dict[str, Any]:
    """没有成本账时的资金视图：金额为空，估算原样带出但不冒充金额。"""
    from .cost.money import attempt_money

    return attempt_money(
        None,
        estimate_usd=estimate_usd,
        estimate_priced=None if priced is None else bool(priced),
    )


def _attempt_money(
    db_path: Path, attempt_id: str, estimate_usd: float | None, priced: Any
) -> dict[str, Any]:
    """单个 attempt 的资金视图（上游实扣 + 估算探针）。"""
    from .cost.money import attempt_money

    est_priced = None if priced is None else bool(priced)
    try:
        from .cost import ledger as L

        row = L.get_by_scope(db_path, "attempt", attempt_id)
    except Exception:  # pragma: no cover - 成本账异常不得阻塞 attempt 详情
        logger.exception("attempt money lookup failed attempt=%s", attempt_id)
        row = None
    return attempt_money(row, estimate_usd=estimate_usd, estimate_priced=est_priced)


def _attempt_money_for_run(db_path: Path, run_id: str) -> dict[str, dict[str, Any]]:
    """run 内全部 attempt 的上游实扣，按 attempt_id 索引。

    成本账缺失不得影响 run 详情本身——查不到就返回空表，各 attempt 回落到
    `_money_unavailable`（金额为空），而不是让整个页面 500。
    """
    try:
        from .cost.money import attempt_money_for_run

        return attempt_money_for_run(db_path, run_id)
    except Exception:  # pragma: no cover - 成本账异常不得阻塞 run 详情
        logger.exception("attempt money lookup failed run=%s", run_id)
        return {}


def _upstream_tokens_for_attempt(db_path: Path, attempt_id: str) -> dict[str, Any] | None:
    """该 attempt 的上游 token 明细（来自其独占 key 的 Activity）。

    Activity 面向**已完成的 UTC 日期**，当天数据要等日结；拿不到返回 None，
    前端据此提示"尚无上游明细"，而不是显示 0（读不到 ≠ 没用 token）。
    """
    try:
        from .cost import ledger as L

        row = L.get_by_scope(db_path, "attempt", attempt_id)
        if row is None or not row.activity:
            return None
        from .cost.activity import summarize_activity

        return summarize_activity(row.activity)
    except Exception:  # pragma: no cover - 明细缺失不得影响 attempt 详情
        logger.exception("upstream tokens lookup failed attempt=%s", attempt_id)
        return None


def _list_rubric_candidate_artifacts_sync(
    data_path: Path, limit: int = 100
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    root = data_path / "rubric-evolution" / "batches"
    if not root.is_dir():
        return items
    paths = sorted(
        root.glob("*/*/evolution-result.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in paths[:max(1, min(limit, 1000))]:
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
            batch = json.loads((path.parent / "batch.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        candidate = result.get("candidate_rubric") if isinstance(result, dict) else None
        items.append({
            "batch_id": batch.get("batch_id"),
            "scope": batch.get("scope"),
            "env_name": batch.get("env_name"),
            "result": result.get("result"),
            "summary": result.get("summary"),
            "candidate_version": (
                candidate.get("proposed_version")
                if isinstance(candidate, dict) else None
            ),
            "parent_version": (
                candidate.get("parent_version")
                if isinstance(candidate, dict) else None
            ),
            "artifact_dir": str(path.parent.relative_to(data_path)),
            "updated_at": path.stat().st_mtime,
        })
    return items


def _list_directional_candidate_artifacts_sync(
    data_path: Path, limit: int = 100
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for domain, directory in (
        ("process", "process-evolution"),
        ("association", "association-evolution"),
    ):
        root = data_path / directory / "batches"
        if not root.is_dir():
            continue
        for path in root.glob("*/evolution-result.json"):
            try:
                result = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            items.append({
                "evolution_domain": domain,
                "batch_id": path.parent.name,
                "result": result.get("result"),
                "summary": result.get("summary"),
                "candidate_version": (
                    (result.get("candidate") or {}).get("proposed_version")
                    if domain == "process" else None
                ),
                "hypothesis_count": len(result.get("hypotheses") or []),
                "artifact_dir": str(path.parent.relative_to(data_path)),
                "updated_at": path.stat().st_mtime,
            })
    items.sort(key=lambda item: item["updated_at"], reverse=True)
    return items[:max(1, min(limit, 1000))]


def _get_attempt_detail_sync(db_path: Path, attempt_id: str) -> dict[str, Any] | None:
    with _open_sync(db_path) as conn:
        conn.row_factory = sqlite3.Row
        att = conn.execute(
            "SELECT id, run_id, task_id, env_name, agent_name, model, rubric_version, status, transport_status, env_session_id,"
            " external_refs_json, event_count, last_event_at, heartbeat_at,"
            " heartbeat_seq, current_stage, progress_message, thinking_count,"
            " tool_call_count, token_usage_json, cost_estimate, duration_ms,"
            " score_total, error_code, error_message, started_at, ended_at,"
            " created_at, execution_locus, permission_mode, workspace_root,"
            " execution_status,execution_started_at,execution_ended_at,"
            " execution_deadline_at,execution_error_code,execution_error_message,"
            " scoring_status,scoring_queued_at,scoring_started_at,scoring_ended_at,"
            " scoring_deadline_at,scoring_error_code,scoring_error_message,"
            " security_event_count, security_max_severity, security_hitl_json,"
            " security_reaction, security_coverage_json,"
            " cost_usd, cost_breakdown_json, cost_priced,"
            " cost_model,model_integrity_status,model_integrity_observed_json,"
            " model_integrity_violation_count,model_integrity_error_code,"
            " model_integrity_error_message,model_integrity_checked_at"
            " FROM attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()
        if att is None:
            return None
        scores = conn.execute(
            "SELECT dimension, value, detail FROM scores WHERE attempt_id=? ORDER BY id",
            (attempt_id,),
        ).fetchall()
    detail = dict(att)
    detail["external_refs"] = json.loads(detail.pop("external_refs_json") or "{}")
    detail["token_usage"] = json.loads(detail.pop("token_usage_json", None) or "{}")
    # 成本核算（token_cost_accounting）：与 token_usage 并列返回。
    # `priced` 为 0 表示部分 token 缺定价，`total_usd` 只是下界——前端必须
    # 据此标注，不能把下界当准确成本展示。
    breakdown_raw = detail.pop("cost_breakdown_json", None)
    priced = detail.pop("cost_priced", None)
    estimate_usd = detail.pop("cost_usd", None)
    detail["cost"] = {
        "total_usd": estimate_usd,
        "model": detail.pop("cost_model", None),
        "priced": None if priced is None else bool(priced),
        "breakdown": json.loads(breakdown_raw) if breakdown_raw else None,
        # 这个块是**本地估算**，不是资金口径。资金看 detail["money"]。
        # 机器可读的口径声明：不依赖消费端记得读注释。
        "is_estimate": True,
        "source": "local_estimate",
        # 同一个数字再给一份带口径的名字，供新消费者直接取用而不会误读。
        "estimated_total_usd": estimate_usd,
    }
    # adapter 自报成本同样是估算，改名带口径；旧字段名保留以免破坏既有消费端。
    detail["adapter_reported_cost_usd"] = detail.get("cost_estimate")
    # 资金口径：该 attempt 临时 key 的上游实扣。这是唯一可以
    # 作为"实际成本"展示的数字；`cost` 块仅供交叉核对。
    detail["money"] = _attempt_money(
        db_path, attempt_id, estimate_usd, priced
    )
    detail["model_integrity"] = _model_integrity_from_row(detail)
    # 上游 Activity 的三维 token。
    # OpenRouter **不提供** cache read/write，那两维只有本地采集有——
    # 这里给出的三维用于与本地交叉核对，缺失不影响金额。
    detail["upstream_tokens"] = _upstream_tokens_for_attempt(db_path, attempt_id)
    from .automation.diagnostics import read_diagnosis

    detail["diagnosis"] = read_diagnosis(db_path.parent, attempt_id)
    detail["scores"] = [dict(s) for s in scores]
    from .rubric_evolution.store import list_score_revisions_sync

    detail["rubric_score_revisions"] = list_score_revisions_sync(
        db_path, attempt_id
    )
    # 安全维度：与 score_total 并列返回（不合并）。hitl JSON 解析成对象。
    detail["security"] = {
        "execution_locus": detail.pop("execution_locus", None),
        "permission_mode": detail.pop("permission_mode", None),
        "workspace_root": detail.pop("workspace_root", None),
        "event_count": detail.pop("security_event_count", 0) or 0,
        "max_severity": detail.pop("security_max_severity", None),
        "hitl": json.loads(detail.pop("security_hitl_json", None) or "{}"),
        "reaction": detail.pop("security_reaction", None),
        # 采集覆盖：event_count=0 要配着这个读——通道为 none 说明
        # 这次压根没采到工具调用，不能当"零违规"用。
        "coverage": json.loads(detail.pop("security_coverage_json", None) or "{}"),
        # adapter 落盘的完整执行场合快照（沙盒镜像 digest / 容器 id / agent 版本 /
        # egress_policy / server_side_network / sandbox_shared …）。
        "meta": _read_security_meta(db_path.parent, attempt_id),
    }
    return detail


def _read_security_meta(data_path: Path, attempt_id: str) -> dict[str, Any]:
    try:
        return json.loads(
            (Path(data_path) / "attempts" / attempt_id / "security_meta.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, json.JSONDecodeError):
        return {}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    items: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return items


def _load_tool_calls(
    attempt_dir: Path, events: list[dict[str, Any]] | None = None
) -> tuple[list[dict[str, Any]], str]:
    """工具调用序列 + 它来自哪条通道。

    采集通道按 agent 分化：只有 blade-agent 的业务工具经 Env Attempt Server 回调，
    由 `TraceWriter` 写 trace.jsonl；五个 CLI adapter 与 dsh 的工具调用只落在
    events.jsonl 里。前端「逐步调用」原先只读 trace.jsonl，于是那六家恒显示
    「无 trace / 0 次调用」——数据一直在盘上，只是没被投影出来。

    `backend/security/classifier.py` 早已用同一手法补上了安全轴（见那里的
    `calls_channel`），这里让 API 侧跟上，复用同一个归一函数以免两处形状漂移。

    返回的 source 让「真没调用工具」与「压根没采到」在数据上可区分——没有它，
    采集缺陷会长得和「这个 agent 更省工具」一模一样。
    """
    trace = _read_jsonl(attempt_dir / "trace.jsonl")
    if trace:
        return trace, "trace.jsonl"

    if events is None:
        events = _read_jsonl(attempt_dir / "events.jsonl") or _read_jsonl(
            attempt_dir / "blade_events.jsonl"
        )
    if not events:
        return [], "none"

    from .security.toolcalls import tool_calls_from_events

    calls = tool_calls_from_events(events)
    if not calls:
        return [], "none"

    # 归一行只有 tool_name/arguments/result/tool_call_id/source_ref，而前端
    # `ToolCall` 把 is_error / duration_ms 声明为必填（会 `${duration_ms}ms` 直接
    # 渲染、并 reduce 求和算工具总耗时）。缺字段会渲染出 "undefinedms" 且让效率
    # 洞察变 NaN，所以在此补齐默认值：events 里没有逐调用耗时，给 0 而非省略。
    for row in calls:
        row.setdefault("is_error", False)
        row.setdefault("duration_ms", 0)
    return calls, "events.jsonl"


def _read_final_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _read_wire_manifest(attempt_dir: Path) -> dict[str, Any] | None:
    """读 wire-manifest.json（缺失/损坏返回 None）。"""
    path = attempt_dir / "wire-manifest.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _read_wire_records(attempt_dir: Path) -> list[dict[str, Any]]:
    """读 canonical wire.jsonl（缺失/截断 fail-open）。"""
    return _read_jsonl(attempt_dir / "wire.jsonl")


def _build_conversation_block(attempt_dir: Path) -> dict[str, Any]:
    """attempt 的 conversation 块：summary + turns + evaluation。

    - **summary**：`summarize_conversation`（缺 conversation.jsonl 的历史 attempt
      返回 legacy 单轮摘要）；
    - **turns**：逐轮明细，**不含 prompt 原文**（只 bytes/hash）；
    - **evaluation**：压缩评测 summary，从 wire manifest + canonical records
      映射（缺 wire 时状态走 incomplete，不冒充）。
    """
    from .conversation.summary import conversation_turns, summarize_conversation
    from .wire.evaluation import evaluate_compaction, inputs_from_wire

    summary = summarize_conversation(attempt_dir)
    turns = conversation_turns(attempt_dir)

    manifest = _read_wire_manifest(attempt_dir)
    records = _read_wire_records(attempt_dir)
    eval_inputs = inputs_from_wire(
        manifest=manifest,
        records=records,
        session_continuity=summary.get("session_continuity"),
    )
    evaluation = evaluate_compaction(eval_inputs)

    return {"summary": summary, "turns": turns, "evaluation": evaluation}


# 注：agent 产物根现在唯一为 attempt_dir/skill_workspace（见 _artifacts_root）。
# attempt 根目录整个属于框架，不进 artifact namespace，故不再需要按文件名/目录名
# 黑名单逐一排除框架产物（events/wire/trajectory/隔离 home 等天然被隔离在根，
# 只经权限门控的 Wire API 访问）。


def _artifacts_root(attempt_dir: Path) -> Path | None:
    """agent 产物的唯一根：attempt_dir/skill_workspace（design：agent 世界边界）。

    三家 agent 产物同构落此——blade 由 adapter 拉回，codex/CC 的 cwd 即此目录。
    attempt 根目录纯属框架（wire/trajectory/events/隔离 home），整个不进 artifact
    namespace，因此 wire capture 只能经权限门控的 Wire API 访问（防护天然成立，
    无需按文件名黑名单逐一排除）。

    skill_workspace 可能被恶意替换成指向外部目录的 symlink；这种 root 不进
    namespace。"""
    workspace = attempt_dir / "skill_workspace"
    if not workspace.is_dir() or workspace.is_symlink():
        return None
    try:
        workspace.resolve().relative_to(attempt_dir.resolve())
    except (OSError, ValueError):
        return None
    return workspace


def _safe_upload_suffix(client_name: str) -> str:
    """从客户端文件名提取一个安全的扩展名，用于服务端生成的落盘文件。

    只取 ``Path.suffix``（最后一个 ``.`` 之后的部分），剥掉任何路径成分，并限制为
    保守字符集（字母/数字，长度受限）。无法满足则返回空串——文件仍能落盘，只是没有
    扩展名。目的是保留一点可读性/可关联性，同时杜绝把客户端全名（可能含路径分隔符）
    带进落盘路径。
    """
    suffix = Path(client_name).suffix  # 已剥离目录成分，形如 ".pdf"
    if not suffix or len(suffix) > 16:
        return ""
    ext = suffix[1:]
    if not ext.isalnum() or not ext.isascii():
        return ""
    return "." + ext.lower()


def _resolve_artifact_path(attempt_dir: Path, path: str) -> Path:
    """Resolve the public artifact ref emitted by ``list_artifacts``.

    ``attempt-root`` and ``.`` are UI namespace labels, not physical child
    directories.  Resolution happens before the containment check so the same
    path contract is shared by download and preview endpoints.
    """
    # URL path 不能包含隐藏 segment、反斜杠或 dot traversal。只依赖最终
    # ``relative_to`` 不够：``x/../..`` 虽仍可能落在 root 内，隐藏文件又会绕过
    # 列表过滤被直接下载，故先按 raw parts 拦。
    raw_parts = Path(path).parts
    if (
        not raw_parts
        or "\\" in path
        or any(part == ".." or part.startswith(".") for part in raw_parts)
    ):
        raise HTTPException(status_code=404, detail=f"artifact not found: {path}")
    # 单一 namespace：产物根即 skill_workspace。``.`` 是 UI 命名空间标签
    # （list_artifacts 的 step 名），不是物理子目录，解析时剥掉。
    root = _artifacts_root(attempt_dir)
    if root is None:
        raise HTTPException(status_code=404, detail=f"artifact not found: {path}")
    parts = raw_parts[1:] if raw_parts[0] == "." else raw_parts
    if not parts:
        raise HTTPException(status_code=404, detail=f"artifact not found: {path}")
    candidate = root.joinpath(*parts)
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail=f"artifact not found: {path}")
    if resolved.is_file():
        return resolved
    raise HTTPException(status_code=404, detail=f"artifact not found: {path}")


_ARTIFACT_SCAN_MAX_FILES = 2000


def _scan_artifacts(attempt_dir: Path) -> dict[str, Any]:
    """Synchronous bounded artifact scan; API callers run it in a worker thread.

    Returns a real directory tree. Two earlier shapes both misled: listing only
    root + one level deep hid everything below `backend/app/**` (blade-agent's
    62 deliverables showed as 13), and a flat list of full directory paths made
    every row repeat its ancestors — `talent-intelligence-system/frontend/src/api`
    wrapped across lines while the indentation, derived from path depth, did not
    match the real hierarchy.

    Each node carries `file_count`/`total_size` aggregated over its whole
    subtree, so a collapsed directory still reports what it contains.

    Excluded directories (see artifact_scope) are pruned during the walk. They
    are not deliverables, and one polluted workspace held 13k of them.
    """
    empty: dict[str, Any] = {"name": "", "path": "", "dirs": [], "files": [],
                             "file_count": 0, "total_size": 0, "truncated": False}
    root = _artifacts_root(attempt_dir)
    if root is None:
        return empty
    try:
        root_resolved = root.resolve()
    except OSError:
        return empty

    # 先收集扁平的 (relative, meta)，再折成树——遍历顺序不保证父目录先出现。
    collected: list[tuple[Path, dict[str, Any]]] = []
    scanned = 0
    for relative in iter_artifact_files(root, max_files=_ARTIFACT_SCAN_MAX_FILES):
        scanned += 1
        if any(part.startswith(".") for part in relative.parts):
            continue
        absolute = root / relative
        try:
            resolved = absolute.resolve()
            resolved.relative_to(root_resolved)
            stat = resolved.stat()
        except (OSError, ValueError):
            continue
        inspection = inspect_artifact(resolved)
        collected.append((relative, {
            "name": relative.name,
            "path": relative.as_posix(),
            "size": stat.st_size,
            "type": inspection.artifact_type,
            "media_type": inspection.media_type,
        }))

    def _new_dir(name: str, path: str) -> dict[str, Any]:
        return {"name": name, "path": path, "dirs": [], "files": [],
                "file_count": 0, "total_size": 0}

    tree = _new_dir("", "")
    index: dict[str, dict[str, Any]] = {"": tree}
    for relative, meta in collected:
        node = tree
        prefix: list[str] = []
        for part in relative.parts[:-1]:
            prefix.append(part)
            key = "/".join(prefix)
            child = index.get(key)
            if child is None:
                child = _new_dir(part, key)
                index[key] = child
                node["dirs"].append(child)
            node = child
        node["files"].append(meta)

    def _finalize(node: dict[str, Any]) -> tuple[int, int]:
        """Sort children and roll subtree totals up; returns (files, bytes)."""
        count = len(node["files"])
        size = sum(f["size"] for f in node["files"])
        for child in node["dirs"]:
            child_count, child_size = _finalize(child)
            count += child_count
            size += child_size
        node["dirs"].sort(key=lambda d: d["name"])
        node["files"].sort(key=lambda f: f["name"])
        node["file_count"] = count
        node["total_size"] = size
        return count, size

    _finalize(tree)
    tree["truncated"] = scanned >= _ARTIFACT_SCAN_MAX_FILES
    return tree


def _artifact_attempt_dir(
    *, data_path: Path, db_path: Path, run_id: str, attempt_id: str
) -> Path:
    """Authorize an artifact namespace by the run→attempt relation.

    Artifact refs are scoped by both IDs in the public URL.  Looking up files by
    ``attempt_id`` alone would let a valid attempt be read through an unrelated
    run URL (and would make future per-run authorization impossible).  Return a
    uniform 404 so callers cannot use this endpoint to probe attempt ownership.
    """
    with _open_sync(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM attempts WHERE id=? AND run_id=?",
            (attempt_id, run_id),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    return data_path / "attempts" / attempt_id


_TRUSTED_PREVIEW_PAGE_BYTES = 4 * 1024 * 1024
_TRUSTED_PREVIEW_TOTAL_BYTES = 16 * 1024 * 1024
_TRUSTED_PREVIEW_MAX_PAGES = 100
_TRUSTED_PREVIEW_MAX_MANIFESTS = 256


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _trusted_presentation_pages(attempt_dir: Path, source: Path) -> list[str]:
    """Return evaluator-rendered PNG pages matching ``source`` byte-for-byte.

    The agent-writable workspace is deliberately not searched.  PPT visual
    scorers render with LibreOffice under ``attempts/_private_eval``; those
    framework-owned pages are both more faithful than the structural OOXML
    fallback and safe to display as inert PNG data.  A copied source deck in
    the preview directory must hash to the public artifact, so stale previews
    cannot be attached after an artifact is overwritten.

    共享缓存优先：LibreOffice 对同一文件的多次渲染存在字体度量抖动（实测同机
    两次渲染换行都会不同），per-attempt 各渲各的会让三个面板里同一份 draft.pptx
    长三个样。内容寻址的共享缓存（_private_eval/_shared/）保证字节相同的文件
    全局只有一张渲染图；hash 匹配契约不变，跨 attempt 复用不构成泄漏——只有
    请求方自己的产物与缓存源逐字节一致时才会命中。
    """
    if source.suffix.lower() != ".pptx":
        return []
    try:
        source_size = source.stat().st_size
        source_digest = _sha256_file(source)
    except OSError:
        return []
    private_parent = attempt_dir.parent / "_private_eval"
    shared_root = private_parent / "_shared" / _PREVIEW_RENDER_DIRNAME
    pages, _ = _pages_from_render_root(
        shared_root, attempt_dir.parent, source_size, source_digest
    )
    if pages:
        return pages
    pages, manifest_dir = _pages_from_render_root(
        private_parent / attempt_dir.name, attempt_dir.parent,
        source_size, source_digest,
    )
    if pages and manifest_dir is not None:
        # 晋升：第一份命中的 per-attempt 渲染成为该内容 hash 的全局唯一渲染图，
        # 之后其他 attempt 的同字节文件直接复用，不再各渲各的。
        _promote_render_to_shared(
            shared_root, source_digest, manifest_dir, source_size
        )
    return pages


def _promote_render_to_shared(
    shared_root: Path, source_digest: str, manifest_dir: Path, source_size: int
) -> None:
    out_dir = shared_root / source_digest[:16]
    if (out_dir / "manifest.json").is_file():
        return
    try:
        value = json.loads((manifest_dir / "manifest.json").read_text(encoding="utf-8"))
        page_names = [n for n in value.get("pages", []) if isinstance(n, str)]
        copied_source = next(
            (
                p for p in sorted(manifest_dir.glob("*.pptx"))
                if not p.is_symlink() and p.stat().st_size == source_size
                and _sha256_file(p) == source_digest
            ),
            None,
        )
        if not page_names or copied_source is None:
            return
        out_dir.parent.mkdir(parents=True, exist_ok=True)
        final_tmp = out_dir.parent / (out_dir.name + ".partial")
        if final_tmp.exists():
            shutil.rmtree(final_tmp)
        final_tmp.mkdir()
        for name in page_names:
            shutil.copyfile(manifest_dir / name, final_tmp / name)
        shutil.copyfile(copied_source, final_tmp / copied_source.name)
        (final_tmp / "manifest.json").write_text(
            json.dumps({"pages": page_names}), encoding="utf-8"
        )
        if out_dir.exists():
            shutil.rmtree(final_tmp)
        else:
            os.replace(final_tmp, out_dir)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("render promote-to-shared failed: %s", exc)


def _pages_from_render_root(
    private_root: Path, boundary: Path, source_size: int, source_digest: str
) -> tuple[list[str], Path | None]:
    if not private_root.is_dir() or private_root.is_symlink():
        return [], None
    try:
        private_resolved = private_root.resolve()
        private_resolved.relative_to(boundary.resolve())
    except (OSError, ValueError):
        return [], None

    manifests = sorted(private_root.rglob("manifest.json"))
    for manifest in manifests[:_TRUSTED_PREVIEW_MAX_MANIFESTS]:
        try:
            if manifest.is_symlink() or manifest.parent.is_symlink():
                continue
            manifest.resolve().relative_to(private_resolved)
            value = json.loads(manifest.read_text(encoding="utf-8"))
            page_names = value.get("pages")
            if not isinstance(page_names, list) or not page_names:
                continue
            copied_sources = sorted(manifest.parent.glob("*.pptx"))
            matched = False
            for copied in copied_sources:
                if copied.is_symlink() or copied.stat().st_size != source_size:
                    continue
                copied.resolve().relative_to(private_resolved)
                if _sha256_file(copied) == source_digest:
                    matched = True
                    break
            if not matched:
                continue

            pages: list[str] = []
            total = 0
            for name in page_names[:_TRUSTED_PREVIEW_MAX_PAGES]:
                if not isinstance(name, str) or Path(name).name != name:
                    raise ValueError("invalid rendered page name")
                page = manifest.parent / name
                if page.is_symlink():
                    raise ValueError("rendered page is a symlink")
                page.resolve().relative_to(private_resolved)
                size = page.stat().st_size
                if size <= 0 or size > _TRUSTED_PREVIEW_PAGE_BYTES:
                    raise ValueError("rendered page exceeds size limit")
                total += size
                if total > _TRUSTED_PREVIEW_TOTAL_BYTES:
                    raise ValueError("rendered pages exceed total size limit")
                raw = page.read_bytes()
                if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise ValueError("rendered page is not PNG")
                pages.append(
                    "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
                )
            if pages and len(pages) == len(page_names):
                return pages, manifest.parent
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return [], None


# 按需渲染（评分兜底）：timeout / 配额中断等从未进入评分流程的 attempt 没有
# 评分器产的渲染页，预览会退化成结构化 OOXML（文字定位近似、易叠字）。这里在
# 预览请求时补渲染一次，产物落 _private_eval 下并复用 _trusted_presentation_pages
# 的 manifest + 源文件 hash 契约（framework 自渲染 → 可信）。soffice / PyMuPDF
# 缺失、渲染失败都静默回退结构化预览，不影响原路径。
_PREVIEW_RENDER_DIRNAME = "octagon_preview_render"
_PREVIEW_RENDER_TIMEOUT_S = 120
_PREVIEW_RENDER_DPI = 150
_PREVIEW_RENDER_SOURCE_BYTES = 50 * 1024 * 1024
# 全局串行：RunDetail 会并发请求同 run 三个 attempt 的预览，不加锁会同时起
# 多个 soffice（每个几百 MB 内存）。首次渲染后命中磁盘缓存，不再进锁内慢路径。
_preview_render_lock = threading.Lock()


def _find_soffice() -> str | None:
    for cand in (
        shutil.which("soffice"),
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        "/usr/bin/soffice",
    ):
        if cand and Path(cand).is_file():
            return cand
    return None


def _ensure_presentation_render(attempt_dir: Path, source: Path) -> bool:
    """Render ``source`` into the trusted-preview layout if not already there.

    幂等：目标目录按源文件 sha256 前缀命名，manifest 已存在即直接返回；产物先
    写临时目录再整体 rename，读者（_trusted_presentation_pages）不会看到半成品。
    返回是否新产出了渲染页。
    """
    try:
        if source.suffix.lower() != ".pptx" or source.is_symlink():
            return False
        if source.stat().st_size > _PREVIEW_RENDER_SOURCE_BYTES:
            return False
        digest = _sha256_file(source)
    except OSError:
        return False
    # 共享内容寻址缓存：同字节文件全局一张渲染图（见 _trusted_presentation_pages）
    out_dir = (
        attempt_dir.parent / "_private_eval" / "_shared"
        / _PREVIEW_RENDER_DIRNAME / digest[:16]
    )
    if (out_dir / "manifest.json").is_file():
        return False
    soffice = _find_soffice()
    if soffice is None:
        return False
    try:
        import fitz  # PyMuPDF：pdf→png（评分链路已依赖）
    except ImportError:
        return False

    with _preview_render_lock:
        if (out_dir / "manifest.json").is_file():
            return False
        try:
            with tempfile.TemporaryDirectory(prefix="octagon-preview-") as td:
                tmp = Path(td)
                copied = tmp / source.name
                shutil.copyfile(source, copied)
                profile = tmp / "lo-profile"
                subprocess.run(
                    [
                        soffice, "--headless",
                        f"-env:UserInstallation=file://{profile}",
                        "--convert-to", "pdf", "--outdir", str(tmp), str(copied),
                    ],
                    check=True, capture_output=True,
                    timeout=_PREVIEW_RENDER_TIMEOUT_S,
                )
                pdf = copied.with_suffix(".pdf")
                if not pdf.is_file():
                    return False
                staging = tmp / "out"
                staging.mkdir()
                pages: list[str] = []
                with fitz.open(pdf) as doc:
                    for index, page in enumerate(doc):
                        if index >= _TRUSTED_PREVIEW_MAX_PAGES:
                            break
                        name = f"page_{index + 1:04d}.png"
                        page.get_pixmap(dpi=_PREVIEW_RENDER_DPI).save(staging / name)
                        pages.append(name)
                if not pages:
                    return False
                # hash 校验锚点：源文件副本必须与公开产物逐字节一致才会被采信
                shutil.copyfile(source, staging / source.name)
                (staging / "manifest.json").write_text(
                    json.dumps({"source": str(source), "pages": pages}),
                    encoding="utf-8",
                )
                out_dir.parent.mkdir(parents=True, exist_ok=True)
                final_tmp = out_dir.parent / (out_dir.name + ".partial")
                if final_tmp.exists():
                    shutil.rmtree(final_tmp)
                shutil.move(str(staging), str(final_tmp))
                if out_dir.exists():  # 竞速对手已落位，弃用本次产物
                    shutil.rmtree(final_tmp)
                else:
                    os.replace(final_tmp, out_dir)
                return True
        except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as exc:
            logger.warning("preview lazy render failed for %s: %s", source.name, exc)
            return False


def _prefer_trusted_presentation_pages(
    descriptor: dict[str, Any], pages: list[str]
) -> dict[str, Any]:
    """Replace approximate PPTX elements with evaluator-rendered full pages."""
    if not pages or descriptor.get("artifact", {}).get("type") != "presentation":
        return descriptor
    previous = descriptor.get("content")
    previous_slides = previous.get("slides", []) if isinstance(previous, dict) else []
    slides = []
    for index, data_uri in enumerate(pages):
        old = previous_slides[index] if index < len(previous_slides) else {}
        slides.append({
            "number": index + 1,
            "elements": [],
            "notes": old.get("notes") if isinstance(old, dict) else None,
            "rendered_image_data_uri": data_uri,
        })
    content = dict(previous) if isinstance(previous, dict) else {}
    content.setdefault("width", 16)
    content.setdefault("height", 9)
    content.setdefault("aspect_ratio", 16 / 9)
    content.setdefault(
        "limits",
        {"slides": _TRUSTED_PREVIEW_MAX_PAGES, "elements_per_slide": 0},
    )
    content.update({
        "kind": "presentation",
        "slides": slides,
        "render_mode": "rendered-pages",
        "truncated": False,
        "active_content_executed": False,
    })
    value = dict(descriptor)
    value.update({
        "status": "ready",
        "counts": {**descriptor.get("counts", {}), "slides": len(slides)},
        "renderer": {"name": "octagon-pptx-rendered-pages", "version": "1"},
        "error": None,
        "poll_after_ms": None,
        "content": content,
    })
    approximate_only = {
        "theme-fidelity-limited",
        "group-transform-limited",
        "bounded-static-preview",
        "background-rendering",
    }
    value["capability_gaps"] = [
        gap for gap in descriptor.get("capability_gaps", [])
        if gap not in approximate_only
    ]
    return value


def _attempt_change_signature(attempt_dir: Path) -> tuple[Any, ...]:
    watched = (
        "events.jsonl",
        "blade_events.jsonl",
        "trace.jsonl",
        "thinking.jsonl",
        "progress.json",
        "final_state.json",
        "security_events.jsonl",
        "iterations.jsonl",
        "iteration_state.json",
    )
    signature: list[Any] = []
    for name in watched:
        path = attempt_dir / name
        try:
            stat = path.stat()
            signature.append((name, stat.st_mtime_ns, stat.st_size))
        except FileNotFoundError:
            signature.append((name, 0, 0))

    artifact_count = 0
    artifact_size = 0
    artifact_mtime = 0
    root = _artifacts_root(attempt_dir)
    if root is not None and root.is_dir():
        # 产物根即 skill_workspace，不含 wire spool/blob 等框架目录，无需再按
        # framework dir 过滤（wire 变化由 manifest generation 表达）。
        # 与产物列表共用净产物边界：签名描述的是「用户看到的产物变了没有」，
        # 而 agent 跑一次 npm install 会新增上万个文件、却不改变任何交付内容。
        for relative in iter_artifact_files(root):
            if any(part.startswith(".") for part in relative.parts):
                continue
            with contextlib.suppress(OSError):
                stat = (root / relative).stat()
                artifact_count += 1
                artifact_size += stat.st_size
                artifact_mtime = max(artifact_mtime, stat.st_mtime_ns)
    signature.append(("artifacts", artifact_count, artifact_size, artifact_mtime))

    # wire finalize/rebuild 后 RunDetail 要收到 attempt update：签名加 manifest
    # generation（finalize 计数，单调递增）；读取失败才回退 mtime/size
    manifest_path = attempt_dir / "wire-manifest.json"
    try:
        generation = json.loads(manifest_path.read_text(encoding="utf-8")).get(
            "generation"
        )
        signature.append(("wire-manifest", "generation", generation))
    except (OSError, json.JSONDecodeError, AttributeError):
        try:
            stat = manifest_path.stat()
            signature.append(("wire-manifest", stat.st_mtime_ns, stat.st_size))
        except FileNotFoundError:
            signature.append(("wire-manifest", 0, 0))
    return tuple(signature)


def _sse(event: str, data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


async def _stream_run_events(
    run_id: str,
    request: Request,
    *,
    poll_interval: float = 0.75,
):
    state = runtime_state.get()
    last_run_signature = ""
    last_attempt_signatures: dict[str, tuple[Any, ...]] = {}
    last_heartbeat = time.monotonic()

    while not await request.is_disconnected():
        run = await asyncio.to_thread(_get_run_sync, state.db_path, run_id)
        if run is None:
            yield _sse("stream:error", {"message": f"run not found: {run_id}"})
            return

        run_signature = json.dumps(run, ensure_ascii=False, sort_keys=True, default=str)
        if run_signature != last_run_signature:
            last_run_signature = run_signature
            yield _sse("run:update", run)

        # Terminal runs cannot produce further attempt updates. Exit before
        # walking artifact directories or reading wire manifests; otherwise
        # each reconnect from an already-open result tab repeats expensive
        # filesystem scans for a run that can no longer change.
        if run.get("status") in {"completed", "failed"}:
            yield _sse("stream:end", {"run_id": run_id, "status": run.get("status")})
            return

        current_attempt_ids: set[str] = set()
        for attempt in run.get("attempts", []):
            attempt_id = str(attempt["id"])
            current_attempt_ids.add(attempt_id)
            file_signature = await asyncio.to_thread(
                _attempt_change_signature,
                state.data_path / "attempts" / attempt_id,
            )
            # DB 行也进签名：秒失败的 attempt（如 cli_not_found）可能从不写任何
            # 落盘文件，只有 status/error_code/score 等 DB 字段变化——不发
            # attempt:update 的话前端要等整个 run 终态才能看到错误信息
            signature = (
                json.dumps(attempt, ensure_ascii=False, sort_keys=True, default=str),
                *file_signature,
            )
            if signature != last_attempt_signatures.get(attempt_id):
                last_attempt_signatures[attempt_id] = signature
                yield _sse("attempt:update", {"attempt_id": attempt_id})
        for stale_id in set(last_attempt_signatures) - current_attempt_ids:
            last_attempt_signatures.pop(stale_id, None)

        now = time.monotonic()
        if now - last_heartbeat >= 15:
            last_heartbeat = now
            yield ": heartbeat\n\n"
        await asyncio.sleep(poll_interval)


# ---------- routes -------------------------------------------------------


def build_router() -> APIRouter:
    router = APIRouter(tags=["frontend"])

    @router.get("/agents")
    async def list_agents(request: Request) -> list[dict[str, Any]]:
        return _list_agents(request.app.state.settings)

    @router.get("/blade/config")
    async def blade_config(request: Request) -> dict[str, Any]:
        cfg = request.app.state.settings.blade
        return {
            "base_url": cfg.base_url,
            "skills_path": str(cfg.skills_path),
            "keep_blade_session": cfg.keep_blade_session,
            "api_key_set": cfg.api_key is not None,
            "health": await _blade_health(cfg.base_url),
        }

    @router.get("/blade/models")
    async def blade_models(request: Request) -> dict[str, Any]:
        cfg = request.app.state.settings.blade
        api_key = cfg.api_key.get_secret_value() if cfg.api_key else None
        return await _blade_models(cfg.base_url, api_key)

    @router.get("/models/providers")
    async def list_model_providers(request: Request) -> dict[str, Any]:
        """CC/Codex 的第三方 provider 候选（octagon.yaml 静态配置）。
        blade 的模型列表走 /api/blade/models 实时查，两者分工不重叠。
        只暴露 provider 名和建议模型名，不泄露 base_url / api_key_env。

        agent_prefix：{agent: provider前缀}，供前端 same-model 下拉列裸模型名、
        提交时按选中 agent 拼前缀（如 claude-code → or-cc/<裸名>）。来源是每个
        provider 的 agent 字段（octagon.yaml）。"""
        settings = request.app.state.settings
        agent_prefix: dict[str, str] = {}
        for name, p in (settings.model_providers or {}).items():
            agent = getattr(p, "agent", None)
            if agent:
                agent_prefix[agent] = name
        return {
            "providers": sorted(settings.model_providers.keys()),
            "suggested": settings.model_suggestions,
            "agent_prefix": agent_prefix,
        }

    @router.get("/openrouter/models")
    async def openrouter_models(request: Request) -> dict[str, Any]:
        """OpenRouter 全量模型（裸模型名）。same-model 下拉数据源；缓存见
        _openrouter_models。key 走进程环境变量 OPENROUTER_API_KEY，响应不含 key。"""
        return await _openrouter_models()

    @router.post("/runs")
    async def post_runs(
        body: CreateRunRequest,
        background_tasks: BackgroundTasks,
        request: Request,
    ) -> dict[str, Any]:
        service_request = NormalizedRunRequest(
            **body.model_dump(exclude={"agent"}),
            timeout_seconds_explicit="timeout_seconds" in body.model_fields_set,
        )
        try:
            created = await create_run_plan(
                service_request,
                settings=request.app.state.settings,
                # Preserve the existing soft catalog diagnostic at the API edge.
                # Direct/coordinator callers may inject the same dependency.
                blade_models_loader=_blade_models,
            )
        except RunServiceError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

        if created.dispatch_jobs:
            dispatcher = (
                _dispatch_serial if created.execution == "serial" else _dispatch_all
            )
            background_tasks.add_task(
                dispatcher, created.run_id, created.dispatch_jobs
            )
        return created.response()

    @router.get("/rubrics")
    async def list_rubrics() -> dict[str, Any]:
        from .rubric_evolution.store import list_rubric_registry_sync

        items = await asyncio.to_thread(
            list_rubric_registry_sync, runtime_state.get().db_path
        )
        return {"items": items}

    @router.get("/evolution/contracts")
    async def list_evolution_contracts() -> dict[str, Any]:
        from .rubric_evolution.cross_layer import list_evolution_contracts_sync

        items = await asyncio.to_thread(
            list_evolution_contracts_sync, runtime_state.get().db_path
        )
        return {"items": items}

    @router.get("/evolution/directional-candidates")
    async def list_directional_candidates(limit: int = 100) -> dict[str, Any]:
        state = runtime_state.get()
        items = await asyncio.to_thread(
            _list_directional_candidate_artifacts_sync,
            state.data_path,
            max(1, min(limit, 1000)),
        )
        return {"items": items}

    @router.get("/evolution/association-hypotheses")
    async def list_association_hypotheses() -> dict[str, Any]:
        from .rubric_evolution.cross_layer import list_association_hypotheses_sync

        items = await asyncio.to_thread(
            list_association_hypotheses_sync, runtime_state.get().db_path
        )
        return {"items": items}

    @router.get("/evolution/cross-layer-summary")
    async def get_cross_layer_summary() -> dict[str, Any]:
        from .rubric_evolution.cross_layer import list_cross_layer_counts_sync

        return await asyncio.to_thread(
            list_cross_layer_counts_sync, runtime_state.get().db_path
        )

    @router.get("/rubric-evolution/batches")
    async def list_rubric_evolution_batches(limit: int = 100) -> dict[str, Any]:
        from .rubric_evolution.store import list_batch_summaries_sync

        items = await asyncio.to_thread(
            list_batch_summaries_sync,
            runtime_state.get().db_path,
            max(1, min(limit, 1000)),
        )
        return {"items": items}

    @router.get("/rubric-evolution/candidates")
    async def list_rubric_evolution_candidates(limit: int = 100) -> dict[str, Any]:
        state = runtime_state.get()
        items = await asyncio.to_thread(
            _list_rubric_candidate_artifacts_sync,
            state.data_path,
            max(1, min(limit, 1000)),
        )
        return {"items": items}

    @router.get("/runs")
    async def list_runs(limit: int = 50, offset: int = 0) -> dict[str, Any]:
        limit = max(1, min(limit, 500))
        offset = max(0, offset)
        return _list_runs_sync(
            runtime_state.get().db_path, limit=limit, offset=offset
        )

    @router.get("/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        run = _get_run_sync(runtime_state.get().db_path, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
        return run

    @router.get("/runs/{run_id}/cost")
    async def get_run_cost(run_id: str) -> dict[str, Any]:
        """本次 run 的资金口径 + 本地估算对照。

        `upstream` 是上游对 run 专属 key 的累计实扣差值（主值）；
        `estimate` 是 token × 本地价格表（对照值）。二者定位不同，
        `auditable=false` 时 `upstream` **不得**被当作实扣归因展示。
        """
        db_path = runtime_state.get().db_path
        if _get_run_sync(db_path, run_id) is None:
            raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
        from .cost.api_view import build_run_cost_view

        view = build_run_cost_view(db_path, run_id)
        if view is None:  # pragma: no cover - build 对缺记录已有兜底
            raise HTTPException(status_code=404, detail="cost audit not found")
        return view

    @router.get("/runs/{run_id}/stream")
    async def stream_run(run_id: str, request: Request) -> StreamingResponse:
        if _get_run_sync(runtime_state.get().db_path, run_id) is None:
            raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
        return StreamingResponse(
            _stream_run_events(run_id, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @router.get("/runs/{run_id}/attempts/{attempt_id}")
    async def get_attempt(
        run_id: str,
        attempt_id: str,
        include_events: bool = True,
    ) -> dict[str, Any]:
        state = runtime_state.get()
        detail = _get_attempt_detail_sync(state.db_path, attempt_id)
        if detail is None or detail.get("run_id") != run_id:
            raise HTTPException(
                status_code=404,
                detail=f"attempt not found under run={run_id}: {attempt_id}",
            )
        attempt_dir = state.data_path / "attempts" / attempt_id
        # 兼容新旧文件名
        events = (
            _read_jsonl(attempt_dir / "events.jsonl")
            or _read_jsonl(attempt_dir / "blade_events.jsonl")
            if include_events
            else []
        )
        tool_calls, tool_calls_source = _load_tool_calls(
            attempt_dir, events if include_events else None
        )
        final_state = _read_final_state(attempt_dir / "final_state.json")
        progress = _read_final_state(attempt_dir / "progress.json")
        # 安全事件明细里的类别分布补进 security（DB 只存汇总，类别从明细文件算）
        sec_events = _read_jsonl(attempt_dir / "security_events.jsonl")
        by_cat: dict[str, int] = {}
        for e in sec_events:
            if e.get("phase") == "executed":
                c = e.get("category", "?")
                by_cat[c] = by_cat.get(c, 0) + 1
        if isinstance(detail.get("security"), dict):
            detail["security"]["by_category"] = by_cat
        # conversation 块（summary/turns/evaluation）。多轮 attempt 有
        # conversation.jsonl；历史单轮 attempt 返回 legacy summary + 空 turns。
        conversation = await asyncio.to_thread(_build_conversation_block, attempt_dir)
        from .iteration.summary import public_iteration_summary

        iteration = await asyncio.to_thread(public_iteration_summary, attempt_dir)
        return {
            **detail,
            "tool_calls": tool_calls,
            "tool_calls_source": tool_calls_source,
            "events": events,
            "final_state": final_state,
            "progress": progress,
            "conversation": conversation,
            "iteration": iteration,
        }

    @router.get("/runs/{run_id}/attempts/{attempt_id}/observations")
    async def get_attempt_observations(
        run_id: str, attempt_id: str, after: int = 0, limit: int = 200
    ) -> list[dict[str, Any]]:
        state = runtime_state.get()
        detail = _get_attempt_detail_sync(state.db_path, attempt_id)
        if detail is None or detail.get("run_id") != run_id:
            raise HTTPException(status_code=404, detail="attempt not found")
        return await asyncio.to_thread(
            list_attempt_observations, state.db_path, attempt_id,
            after=after, limit=limit,
        )

    @router.get("/runs/{run_id}/attempts/{attempt_id}/thinking")
    async def get_attempt_thinking(run_id: str, attempt_id: str) -> list[dict[str, Any]]:
        state = runtime_state.get()
        # run→attempt 归属校验：与主详情/artifact 端点共用同一 guard，否则用无关
        # run URL 也能读到有效 attempt 的思考/trace/事件（主详情已 404 却从这里泄漏）。
        attempt_dir = await asyncio.to_thread(
            _artifact_attempt_dir,
            data_path=state.data_path, db_path=state.db_path,
            run_id=run_id, attempt_id=attempt_id,
        )
        return _read_jsonl(attempt_dir / "thinking.jsonl")

    @router.get("/runs/{run_id}/attempts/{attempt_id}/trace")
    async def get_attempt_trace(run_id: str, attempt_id: str) -> list[dict[str, Any]]:
        state = runtime_state.get()
        attempt_dir = await asyncio.to_thread(
            _artifact_attempt_dir,
            data_path=state.data_path, db_path=state.db_path,
            run_id=run_id, attempt_id=attempt_id,
        )
        calls, _source = await asyncio.to_thread(_load_tool_calls, attempt_dir)
        return calls

    @router.get("/runs/{run_id}/attempts/{attempt_id}/events")
    async def get_attempt_events(run_id: str, attempt_id: str) -> list[dict[str, Any]]:
        state = runtime_state.get()
        attempt_dir = await asyncio.to_thread(
            _artifact_attempt_dir,
            data_path=state.data_path, db_path=state.db_path,
            run_id=run_id, attempt_id=attempt_id,
        )
        events = _read_jsonl(attempt_dir / "events.jsonl")
        if not events:
            events = _read_jsonl(attempt_dir / "blade_events.jsonl")
        return events

    @router.get("/runs/{run_id}/attempts/{attempt_id}/security_events")
    async def get_attempt_security_events(
        run_id: str, attempt_id: str
    ) -> list[dict[str, Any]]:
        """安全事件明细：每条含 category/severity/target/locus/hitl_status/rule_id/
        source_ref，可溯源到具体 trace 行。"""
        state = runtime_state.get()
        attempt_dir = await asyncio.to_thread(
            _artifact_attempt_dir,
            data_path=state.data_path, db_path=state.db_path,
            run_id=run_id, attempt_id=attempt_id,
        )
        return _read_jsonl(attempt_dir / "security_events.jsonl")

    @router.post("/runs/{run_id}/stop")
    async def stop_run(run_id: str) -> dict[str, Any]:
        """用户停止一个 run：执行、评分两阶段都终止，状态一次收敛。

        与 ``coordinator.stop_group`` 共用 :mod:`backend.convergence`——此前两条
        Stop 路径实现不同，这条还绕过整条投影链直接改写 ``runs.status``，
        导致停止后 attempt/run/cell 三层状态互相矛盾。
        """
        state = runtime_state.get()
        from .convergence import (
            cancel_attempts_for_runs,
            cancel_dispatch_tasks,
            kill_orphaned_agent_processes,
        )
        from .disk_guard import DISK_EXHAUSTED_CODE, is_disk_full_error
        from .scoring_queue import cancel_scoring_for_runs

        # 顺序要紧：先停产生新工作的来源（评分 job、dispatch 协程），
        # 再收敛状态。反过来会让已收敛的 attempt 又被写回 running。
        #
        # 在此之上再排一层：不需要磁盘的动作排在需要落盘的动作之前，且落盘
        # 失败不阻断取消。磁盘写满时正是最需要停止的时刻，而此前整个 stop
        # 会因落盘失败返回 500——用户得先手动腾空间才能停，可空间还在被跑着
        # 的 attempt 继续吃掉。取消协程、杀进程才是真正让 token 停止燃烧的
        # 动作，它们不该被一次写盘失败连坐（需求 7.2）。
        cancelled_tasks = cancel_dispatch_tasks([run_id])
        # 跨重启存活、已无内存协程的 Agent：cancel_dispatch_tasks 够不着
        # 它们，只改 DB 会让进程继续烧 token。按落盘 pid 直接杀。
        killed_orphans = await asyncio.to_thread(
            kill_orphaned_agent_processes, state.db_path, [run_id]
        )

        cancelled_scoring = 0
        scoring_error: str | None = None
        try:
            cancelled_scoring = cancel_scoring_for_runs([run_id])
        except Exception as exc:  # noqa: BLE001
            scoring_error = str(exc)
            logger.exception("stop %s：评分 job 取消落盘失败", run_id)

        converged = 0
        persist_error: str | None = None
        try:
            converged = await asyncio.to_thread(
                cancel_attempts_for_runs, state.db_path, [run_id]
            )
        except Exception as exc:  # noqa: BLE001
            # 状态没写成不改变「已经停了」这个事实：协程已取消、进程已杀。
            # 把失败如实报给调用方，别假装收敛成功，也别把整个停止判成失败。
            persist_error = str(exc)
            if is_disk_full_error(exc):
                # 磁盘满是这条容错路径存在的原因，明确标出来：运维看到它就
                # 知道要先腾空间再重放收敛，而不是去查 DB 损坏。
                persist_error = f"{DISK_EXHAUSTED_CODE}: {exc}"
            logger.exception("stop %s：状态收敛落盘失败，取消本身已生效", run_id)

        payload: dict[str, Any] = {
            "stopped": cancelled_tasks,
            "run_id": run_id,
            "cancelled_scoring_jobs": cancelled_scoring,
            "killed_orphan_processes": killed_orphans,
            "converged_attempts": converged,
        }
        if persist_error is not None:
            payload["convergence_persist_error"] = persist_error
        if scoring_error is not None:
            payload["scoring_cancel_persist_error"] = scoring_error
        return payload

    @router.get("/runs/{run_id}/attempts/{attempt_id}/artifacts")
    async def list_artifacts(run_id: str, attempt_id: str) -> dict[str, Any]:
        state = runtime_state.get()
        attempt_dir = await asyncio.to_thread(
            _artifact_attempt_dir,
            data_path=state.data_path, db_path=state.db_path,
            run_id=run_id, attempt_id=attempt_id,
        )
        return await asyncio.to_thread(_scan_artifacts, attempt_dir)

    @router.get("/runs/{run_id}/attempts/{attempt_id}/artifact-previews/{path:path}")
    async def get_artifact_preview(run_id: str, attempt_id: str, path: str):
        """Return the descriptor; never return untrusted Office bytes here."""
        state = runtime_state.get()
        attempt_dir = await asyncio.to_thread(
            _artifact_attempt_dir,
            data_path=state.data_path, db_path=state.db_path,
            run_id=run_id, attempt_id=attempt_id,
        )
        file_path = await asyncio.to_thread(_resolve_artifact_path, attempt_dir, path)
        descriptor = await asyncio.to_thread(
            scheduled_preview_descriptor,
            file_path,
            path,
            attempt_dir / "artifact-previews",
        )
        pages = await asyncio.to_thread(
            _trusted_presentation_pages, attempt_dir, file_path
        )
        if not pages:
            # 评分兜底：timeout/失败 attempt 没有评分器渲染页 → 按需补渲染一次
            rendered = await asyncio.to_thread(
                _ensure_presentation_render, attempt_dir, file_path
            )
            if rendered:
                pages = await asyncio.to_thread(
                    _trusted_presentation_pages, attempt_dir, file_path
                )
        return _prefer_trusted_presentation_pages(descriptor, pages)

    @router.get("/runs/{run_id}/attempts/{attempt_id}/artifacts/{path:path}")
    async def get_artifact(run_id: str, attempt_id: str, path: str):
        from fastapi.responses import FileResponse, PlainTextResponse
        state = runtime_state.get()
        attempt_dir = await asyncio.to_thread(
            _artifact_attempt_dir,
            data_path=state.data_path, db_path=state.db_path,
            run_id=run_id, attempt_id=attempt_id,
        )
        file_path = await asyncio.to_thread(_resolve_artifact_path, attempt_dir, path)
        inspection = await asyncio.to_thread(inspect_artifact, file_path)
        if inspection.artifact_type in ("text", "html"):
            content = await asyncio.to_thread(
                file_path.read_text, encoding="utf-8", errors="replace"
            )
            # HTML 产物是被测 agent 生成的**不可信内容**。这里一律以纯文本返回，
            # 绝不用 text/html：否则拿 URL 直接访问就是一个与 Octagon 同源的
            # 页面，能读 localStorage、能带着用户会话打 /api/*（删 run、读别的
            # attempt 产物）。要看渲染效果走前端的 `<iframe sandbox srcdoc>`——
            # 那里不给 allow-same-origin，脚本跑在不透明源里。
            # nosniff 防止浏览器无视 content-type 猜成 HTML 执行。
            return PlainTextResponse(
                content, headers={"X-Content-Type-Options": "nosniff"}
            )
        # Office/unknown binary is always downloaded or rendered by a dedicated
        # endpoint; it must never pass through read_text(errors="replace").
        return FileResponse(file_path, media_type=inspection.media_type)

    @router.post("/upload")
    async def upload_file(request: Request):
        # multipart 手动解析
        form = await request.form()
        state = runtime_state.get()
        upload_dir = state.data_path / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        upload_root = upload_dir.resolve()
        saved: list[dict[str, Any]] = []
        for key in form:
            item = form[key]
            if hasattr(item, "read"):
                content = await item.read()
                client_name = getattr(item, "filename", None) or key
                # 落盘文件名由服务端生成唯一 ID，绝不拼接客户端文件名——后者可含
                # 路径分隔符（../、绝对路径、Windows 反斜杠）造成目录穿越，或与既有
                # 上传重名造成任意文件覆盖。客户端原名仅作展示（name）回传前端。
                suffix = _safe_upload_suffix(client_name)
                dest = upload_dir / f"upload_{uuid.uuid4().hex}{suffix}"
                # 纵深防御：即便上面生成逻辑将来被改，也保证最终落点在 upload_dir 内。
                try:
                    dest.resolve().relative_to(upload_root)
                except ValueError:
                    raise HTTPException(status_code=400, detail="invalid upload target")
                dest.write_bytes(content)
                saved.append(
                    {
                        "name": Path(client_name).name,
                        "path": str(dest.resolve()),
                        "size": len(content),
                    }
                )
        return {"files": saved}

    return router


def register_routes(app: FastAPI, prefix: str = "") -> None:
    app.include_router(build_router(), prefix=prefix)
    from .wire.api import build_wire_router
    from .wire.proxy_api import build_proxy_router

    app.include_router(build_wire_router(), prefix=prefix)
    # reverse HTTP capture proxy：内部路由，不加 /api 前缀——
    # adapter 注入的 base URL 直接是 /internal/wire-proxy/...。
    app.include_router(build_proxy_router())
