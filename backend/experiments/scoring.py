"""Atomic scoring commit, immutable manifests and leader candidate facts."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from backend.cost.estimate import apply_cost_columns
from backend.db import _now_iso, _open_sync
from backend.models import TERMINAL_ATTEMPT_STATUSES

from .hashing import canonical_hash, canonical_json_bytes

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Candidate:
    scope_key: str
    attempt_id: str
    score_total: float
    scorer_fingerprint: str
    duration_ms: int
    terminal_at: str
    outbox_id: str


@dataclass(frozen=True)
class CandidateSet:
    status: Literal["ready", "unavailable", "incomparable"]
    candidates: tuple[Candidate, ...]
    fingerprints: tuple[str, ...]


def _write_manifest(data_path: Path, manifest: dict[str, Any]) -> tuple[str, str]:
    payload = canonical_json_bytes(manifest)
    fingerprint = canonical_hash(manifest)
    relative = Path("evaluation-manifests") / (
        f"{fingerprint.removeprefix('sha256:')}.json"
    )
    destination = Path(data_path) / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.is_file():
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_bytes(payload)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    return relative.as_posix(), fingerprint


DEFAULT_PRICING_PATH = Path("data/pricing.json")

# 定价表按路径 + mtime 缓存：每个 attempt 重读一次 342 个模型的 JSON 是无谓
# 开销，而评分是高频路径。mtime 进 key，`sync_pricing.py` 更新后自动失效。
_PRICING_CACHE: dict[tuple[str, float], Any] = {}


#: 缺定价表的告警只发一次：评分是高频路径，每个 attempt 刷一条会淹没日志。
_PRICING_MISSING_WARNED: set[str] = set()


def _warn_pricing_missing(target: Path) -> None:
    """定价表缺失时告警一次。

    **为什么必须显式告警**：`PricingTable({})` 会让 `compute_cost` 对每个模型
    都返回 None，于是 `cost_usd` 全库 NULL——看起来像"成本功能没启用"，实际是
    "定价表文件不存在"。2026-07-28 的实测就撞上这个：仓库里根本没有
    `data/pricing.json`，而链路一声不吭。静默降级比报错危险。
    """
    key = str(target)
    if key in _PRICING_MISSING_WARNED:
        return
    _PRICING_MISSING_WARNED.add(key)
    logger.warning(
        "定价表不存在或不可读：%s —— 所有 attempt 的 cost_usd 将为 NULL。"
        "运行 `uv run python scripts/sync_pricing.py` 生成。",
        target,
    )


def _load_pricing(path: Path | None) -> Any:
    from backend.pricing import PricingTable

    target = Path(path) if path is not None else DEFAULT_PRICING_PATH
    try:
        mtime = target.stat().st_mtime
    except OSError:
        _warn_pricing_missing(target)
        return PricingTable({})
    key = (str(target.resolve()), mtime)
    cached = _PRICING_CACHE.get(key)
    if cached is None:
        cached = PricingTable.load(target)
        # 只保留最新一份：定价表切换是低频事件，无需长期多版本共存。
        _PRICING_CACHE.clear()
        _PRICING_CACHE[key] = cached
    return cached


def _compute_attempt_cost(
    token_usage: dict[str, Any] | None,
    model: str | None,
    pricing_path: Path | None,
) -> Any:
    """五维用量 + 定价表 → CostBreakdown。任何一环缺失都返回 None。

    实现已移至 `backend/cost/estimate.compute_breakdown`，与 runner 的两条
    finalize 路径、wire backfill 共用同一口径。此处保留薄封装是因为
    既有测试直接 import 它。
    """
    from backend.cost.estimate import compute_breakdown

    return compute_breakdown(token_usage, model, pricing_path)


def commit_scoring_result(
    *,
    db_path: Path,
    data_path: Path,
    attempt_id: str,
    scores: list[dict[str, Any]],
    manifest: dict[str, Any],
    status: str,
    score_total: int,
    ended_at: str,
    external_refs: dict[str, Any],
    event_count: int,
    last_event_at: str | None,
    thinking_count: int = 0,
    tool_call_count: int = 0,
    token_usage: dict[str, int] | None = None,
    cost_estimate: float | None = None,
    duration_ms: int = 0,
    transport_status: str = "unknown",
    model: str | None = None,
    pricing_path: Path | None = None,
) -> str:
    """Commit scores + attempt terminal + outbox in one SQLite transaction.

    成本（token_cost_accounting）与分数在**同一事务**里写入：否则会出现
    「有分数没成本」的中间态，聚合/排名读到就得区分"还没算"与"算不出"。
    定价表缺失或模型未收录时四个 cost_* 列保持 NULL——不猜、不填 0。
    """
    manifest_ref, fingerprint = _write_manifest(data_path, manifest)
    scored_at = _now_iso()
    with _open_sync(db_path) as conn:
        existing = conn.execute(
            "SELECT id FROM score_transition_outbox WHERE attempt_id=? "
            "AND score_revision=1",
            (attempt_id,),
        ).fetchone()
        if existing is not None:
            return str(existing[0])
        attempt = conn.execute(
            "SELECT external_refs_json FROM attempts WHERE id=?", (attempt_id,)
        ).fetchone()
        if attempt is None:
            raise ValueError(f"attempt not found: {attempt_id}")
        stored_refs = json.loads(attempt[0] or "{}")
        merged_refs = {**stored_refs, **external_refs}
        for score in scores:
            conn.execute(
                "INSERT INTO scores(attempt_id,dimension,value,detail,scored_at,"
                "evaluation_manifest_ref) VALUES(?,?,?,?,?,?)",
                (
                    attempt_id,
                    str(score.get("dimension", "")),
                    int(score.get("value", 0)),
                    str(score.get("detail", "")),
                    scored_at,
                    manifest_ref,
                ),
            )
        conn.execute(
            "UPDATE attempts SET status=?,score_total=?,external_refs_json=?,event_count=?,"
            "last_event_at=?,thinking_count=?,tool_call_count=?,token_usage_json=?,"
            "cost_estimate=?,duration_ms=?,transport_status=?,ended_at=?,"
            "execution_status=CASE WHEN execution_status IN ('queued','running') "
            "THEN 'completed' "
            "ELSE execution_status END,"
            "execution_ended_at=COALESCE(execution_ended_at,?),"
            "scoring_status='completed',scoring_ended_at=?,"
            "scoring_error_code=NULL,scoring_error_message=NULL WHERE id=?",
            (
                status,
                score_total,
                json.dumps(merged_refs, ensure_ascii=False, sort_keys=True),
                event_count,
                last_event_at,
                thinking_count,
                tool_call_count,
                json.dumps(token_usage or {}, ensure_ascii=False, sort_keys=True),
                cost_estimate,
                duration_ms,
                transport_status,
                ended_at,
                ended_at,
                ended_at,
                attempt_id,
            ),
        )
        # 成本估算：与 runner 两条 finalize 路径、wire backfill 共用同一 helper
        # ，保证四处口径一致。仍在同一事务里——否则会出现"有分数没成本"
        # 的中间态，聚合方读到就得区分"还没算"与"算不出"。
        apply_cost_columns(
            conn,
            attempt_id,
            token_usage=token_usage,
            model=model,
            pricing_path=pricing_path,
        )
        scope_key = None
        variant_id = merged_refs.get("variant_id")
        repeat_index = merged_refs.get("repeat_index")
        if variant_id is not None and repeat_index is not None:
            scope_key = f"variant:{variant_id}/repeat:{repeat_index}"
        related_statuses = [
            row[0]
            for row in conn.execute(
                "SELECT peer.status FROM attempts peer JOIN run_group_cells c "
                "ON c.run_id=peer.run_id WHERE c.run_id=(SELECT run_id FROM attempts "
                "WHERE id=?)",
                (attempt_id,),
            ).fetchall()
        ]
        scope_terminal = bool(related_statuses) and all(
            item in TERMINAL_ATTEMPT_STATUSES for item in related_statuses
        )
        sequence = conn.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM score_transition_outbox"
        ).fetchone()[0]
        outbox_id = f"score_{canonical_hash({'attempt_id': attempt_id, 'revision': 1}).removeprefix('sha256:')[:20]}"
        conn.execute(
            "INSERT INTO score_transition_outbox(id,attempt_id,score_revision,scope_key,"
            "score,scorer_fingerprint,manifest_ref,seq,created_at,scope_terminal) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                outbox_id,
                attempt_id,
                1,
                scope_key,
                score_total,
                fingerprint,
                manifest_ref,
                sequence,
                scored_at,
                int(scope_terminal),
            ),
        )
        conn.commit()
    return outbox_id


def extract_candidates(db_path: Path, group_id: str, scope_key: str) -> CandidateSet:
    with _open_sync(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT o.id,o.scope_key,o.attempt_id,o.score,o.scorer_fingerprint,"
            "o.created_at,a.duration_ms,a.status FROM score_transition_outbox o "
            "JOIN attempts a ON a.id=o.attempt_id "
            "JOIN run_group_cells c ON c.run_id=a.run_id "
            "WHERE c.run_group_id=? AND o.scope_key=? "
            "AND a.status IN ('completed','gave_up') ORDER BY o.seq,o.id",
            (group_id, scope_key),
        ).fetchall()
    candidates = tuple(
        Candidate(
            scope_key=row["scope_key"],
            attempt_id=row["attempt_id"],
            score_total=row["score"],
            scorer_fingerprint=row["scorer_fingerprint"],
            duration_ms=row["duration_ms"],
            terminal_at=row["created_at"],
            outbox_id=row["id"],
        )
        for row in rows
    )
    fingerprints = tuple(sorted({item.scorer_fingerprint for item in candidates}))
    if not candidates:
        status: Literal["ready", "unavailable", "incomparable"] = "unavailable"
    elif len(fingerprints) > 1:
        status = "incomparable"
    else:
        status = "ready"
    return CandidateSet(status, candidates, fingerprints)
