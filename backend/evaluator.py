"""Evaluator——读 trace + final_state + env DB,调 scorer,写 scores 表。

scorer 签名(`envs/<env>/scorer.py:score`):

    score(*, attempt_id, task, env_db, trace, final_state) -> list[dict]
        每项含 dimension / value / detail。

总分计算:meta.yaml 里 `dimensions[*].weight` 加权求平均;若 dimensions 缺失
权重,默认每个维度等权。`pass_threshold` 取 meta.yaml 顶层字段;不存在按 60。
"""

from __future__ import annotations

import json
import inspect
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


class ScorerUnavailableError(RuntimeError):
    """The scorer's own judge/runtime is unavailable, not a task-quality zero."""


@dataclass
class EvaluationOutcome:
    score_total: int
    pass_threshold: int
    scores: list[dict[str, Any]]
    passed: bool
    evaluation_manifest: dict[str, Any]
    # 安全轴：与 score_total 并列，不合并（防「危险手段换高分」被掩盖）。
    security: dict[str, Any] | None = None


def _read_jsonl(p: Path) -> list[dict[str, Any]]:
    if not p.exists():
        return []
    items: list[dict[str, Any]] = []
    with p.open("r", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return items


def load_trace(data_path: Path, attempt_id: str) -> list[dict[str, Any]]:
    """评分用的工具调用序列，trace 缺失时从 events.jsonl 归一。

    只有 blade-agent 的业务工具经 Env Attempt Server 回调写 trace.jsonl；五个 CLI
    adapter 与 dsh 的调用只落在 events.jsonl。原先这里文件缺失就返回 []，于是所有
    吃 trace 的 scorer 维度对那六家恒判零——**不是它们没做，是没人看见**。

    49 实测（2026-08-17，swebench 全部 env 的 validation 维度均分）：

        blade-agent 91.7 ／ 其余六家全部 0.0（n=178）

    这不是能力差异：181 个被判 0 的 attempt 里，有 18 个的 events.jsonl 明确记录了
    成功的测试命令（如 `python3 -m pytest testing/test_mark.py -q`）。跨 agent 评分
    因此不可比，而这正是本项目要比的东西。

    API 侧（`api.py::_load_tool_calls`）与安全轴（`security/classifier.py`）已各自
    用同一手法补上，这里让评分路径跟上，三处共用 `tool_calls_from_events`。
    """
    trace = _read_jsonl(data_path / "attempts" / attempt_id / "trace.jsonl")
    if trace:
        return trace

    attempt_dir = data_path / "attempts" / attempt_id
    events = _read_jsonl(attempt_dir / "events.jsonl") or _read_jsonl(
        attempt_dir / "blade_events.jsonl"
    )
    if not events:
        return []

    from .security.toolcalls import tool_calls_from_events

    return tool_calls_from_events(events)


def load_final_state(data_path: Path, attempt_id: str) -> dict[str, Any]:
    p = data_path / "attempts" / attempt_id / "final_state.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _load_jsonl(p: Path) -> list[dict[str, Any]]:
    if not p.exists():
        return []
    items: list[dict[str, Any]] = []
    with p.open("r", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return items


def load_events(data_path: Path, attempt_id: str) -> list[dict[str, Any]]:
    return _load_jsonl(data_path / "attempts" / attempt_id / "events.jsonl")


def load_thinking(data_path: Path, attempt_id: str) -> list[dict[str, Any]]:
    return _load_jsonl(data_path / "attempts" / attempt_id / "thinking.jsonl")


def env_db_path(data_path: Path, attempt_id: str) -> Path:
    return data_path / "attempts" / attempt_id / "env.db"


def _extract_meta(env: Any) -> tuple[int, dict[str, int]]:
    """返回 (pass_threshold, {dim: weight})。weight 缺失时按 0,后续 normalize。"""
    meta: dict[str, Any] = getattr(env, "meta", {}) or {}
    pass_threshold = int(meta.get("pass_threshold", 60))
    weights: dict[str, int] = {}
    for dim in meta.get("dimensions", []) or []:
        if not isinstance(dim, dict):
            continue
        name = dim.get("name")
        if not name:
            continue
        try:
            weights[name] = int(dim.get("weight", 0))
        except (TypeError, ValueError):
            weights[name] = 0
    return pass_threshold, weights


def _aggregate_total(scores: list[dict[str, Any]], weights: dict[str, int]) -> int:
    """加权平均(weight 为 0 的维度不参与;全 0 时简单平均)。

    单维度满分 100,score_total 也按 100 满分;返回 int。
    """
    weighted_sum = 0
    weight_total = 0
    fallback_values: list[int] = []
    for s in scores:
        try:
            value = int(s.get("value", 0))
        except (TypeError, ValueError):
            value = 0
        fallback_values.append(value)
        w = weights.get(s.get("dimension", ""), 0)
        if w > 0:
            weighted_sum += value * w
            weight_total += w
    if weight_total > 0:
        return round(weighted_sum / weight_total)
    if fallback_values:
        return round(sum(fallback_values) / len(fallback_values))
    return 0


def _scorer_manifest(
    env: Any, scorer: Callable[..., list[dict[str, Any]]]
) -> dict[str, Any]:
    """Capture score-affecting configuration at evaluation time."""
    from .experiments.hashing import hash_bytes

    meta = getattr(env, "meta", {}) or {}
    scorer_path = inspect.getsourcefile(scorer)
    scorer_hash = None
    if scorer_path:
        try:
            scorer_hash = hash_bytes(Path(scorer_path).read_bytes())
        except OSError:
            scorer_hash = None
    declared = getattr(scorer, "__octagon_evaluation_manifest__", None)
    if callable(declared):
        declared = declared()
    if declared is None:
        declared = {}
    if not isinstance(declared, dict):
        raise TypeError("scorer evaluation manifest hook must return a mapping")
    forbidden = {"api_key", "token", "secret", "password", "authorization"}

    def sanitize(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): ("<redacted>" if str(key).lower() in forbidden else sanitize(item))
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [sanitize(item) for item in value]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return repr(value)

    return {
        "schema_version": "octagon-evaluation-manifest-v1",
        "env_name": getattr(env, "name", meta.get("name", "unknown")),
        "env_schema_version": meta.get("schema_version"),
        "pass_threshold": int(meta.get("pass_threshold", 60)),
        "dimensions": [
            {
                "name": item.get("name"),
                "weight": item.get("weight", 0),
            }
            for item in (meta.get("dimensions") or [])
            if isinstance(item, dict)
        ],
        "scorer": {
            "module": getattr(scorer, "__module__", None),
            "qualname": getattr(scorer, "__qualname__", None),
            "source_hash": scorer_hash,
            "version": getattr(scorer, "__version__", None),
        },
        # Judge provider/model/settings, rubric/material hashes and dependency
        # versions are declared by complex scorers through this hook.
        "declared": sanitize(declared),
    }


def _write_security_events(
    data_path: Path, attempt_id: str, events: list[dict[str, Any]]
) -> None:
    p = data_path / "attempts" / attempt_id / "security_events.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fp:
        for e in events:
            fp.write(json.dumps(e, ensure_ascii=False, default=str) + "\n")


def run_security_scan(
    *,
    attempt_id: str,
    env: Any,
    data_path: Path,
    trace: list[dict[str, Any]],
    security_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """离线安全扫描：不改变 agent 执行，只读已落盘数据。

    security_meta（② 执行场合快照）可选：缺失时 locus 记 unknown、
    workspace_root 回落 attempt 目录（对 CC/Codex 是其 cwd）。danger_tools 来自
    env meta.yaml。返回汇总 dict，明细写 security_events.jsonl。
    """
    # 延迟 import：security 是可选子系统，缺失不应拖垮评分主链路。
    from .security import SecurityContext, scan

    meta = security_meta or {}
    attempt_dir = str((data_path / "attempts" / attempt_id).resolve())
    env_meta: dict[str, Any] = getattr(env, "meta", {}) or {}
    danger_tools = env_meta.get("danger_tools", {}) or {}

    ctx = SecurityContext(
        agent_name=meta.get("agent_name", ""),
        execution_locus=meta.get("execution_locus", "unknown"),
        workspace_root=meta.get("workspace_root") or attempt_dir,
        danger_tools=danger_tools,
    )
    result = scan(
        trace=trace,
        events=load_events(data_path, attempt_id),
        thinking=load_thinking(data_path, attempt_id),
        ctx=ctx,
    )
    _write_security_events(
        data_path, attempt_id, [e.to_dict() for e in result.events]
    )
    return result.summary.to_dict()


def evaluate(
    *,
    attempt_id: str,
    task: dict[str, Any],
    env: Any,
    data_path: Path,
    scorer: Callable[..., list[dict[str, Any]]],
    security_meta: dict[str, Any] | None = None,
) -> EvaluationOutcome:
    """跑一次评估。如果 scorer 抛错,直接抛上去,由 runner 捕获并设
    `scoring_failed` status。安全扫描独立于 scorer，异常不影响任务分。"""
    trace = load_trace(data_path, attempt_id)
    final_state = load_final_state(data_path, attempt_id)
    db_path = env_db_path(data_path, attempt_id)
    scorer_kwargs = {
        "attempt_id": attempt_id,
        "task": task,
        "env_db": db_path,
        "trace": trace,
        "final_state": final_state,
    }
    signature = inspect.signature(scorer)
    if "events" in signature.parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    ):
        scorer_kwargs["events"] = load_events(data_path, attempt_id)
    raw_scores = scorer(**scorer_kwargs)
    if not isinstance(raw_scores, list):
        raise TypeError(f"scorer must return list, got {type(raw_scores).__name__}")
    pass_threshold, weights = _extract_meta(env)
    score_total = _aggregate_total(raw_scores, weights)

    security: dict[str, Any] | None = None
    try:
        security = run_security_scan(
            attempt_id=attempt_id,
            env=env,
            data_path=data_path,
            trace=trace,
            security_meta=security_meta,
        )
    except Exception:  # 安全轴不得拖垮任务评分
        logger.exception("security scan 失败 attempt=%s（不影响任务分）", attempt_id)

    return EvaluationOutcome(
        score_total=score_total,
        pass_threshold=pass_threshold,
        scores=raw_scores,
        passed=score_total >= pass_threshold,
        evaluation_manifest=_scorer_manifest(env, scorer),
        security=security,
    )


def write_scores_sync(
    db_path: Path,
    attempt_id: str,
    scores: list[dict[str, Any]],
    evaluation_manifest_ref: str | None = None,
) -> None:
    """把维度分写 scores 表。重复调用时先清掉旧行(M1 attempt 不重评,但保险)。"""
    from .db import _now_iso

    scored_at = _now_iso()
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM scores WHERE attempt_id=?", (attempt_id,))
        for s in scores:
            conn.execute(
                "INSERT INTO scores(attempt_id, dimension, value, detail, scored_at, "
                "evaluation_manifest_ref) VALUES(?, ?, ?, ?, ?, ?)",
                (
                    attempt_id,
                    str(s.get("dimension", "")),
                    int(s.get("value", 0)),
                    str(s.get("detail", "")),
                    scored_at,
                    evaluation_manifest_ref,
                ),
            )
        conn.commit()


def write_attempt_score_sync(db_path: Path, attempt_id: str, score_total: int) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE attempts SET score_total=? WHERE id=?",
            (score_total, attempt_id),
        )
        conn.commit()


def write_security_summary_sync(
    db_path: Path, attempt_id: str, security: dict[str, Any] | None
) -> None:
    """把安全汇总写 attempts 的 security_* 列。与 score_total 独立。"""
    if not security:
        return
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE attempts SET security_event_count=?, security_max_severity=?, "
            "security_hitl_json=?, security_reaction=?, security_coverage_json=? "
            "WHERE id=?",
            (
                int(security.get("event_count", 0)),
                security.get("max_severity"),
                json.dumps(security.get("hitl", {}), ensure_ascii=False),
                security.get("reaction"),
                json.dumps(security.get("coverage", {}), ensure_ascii=False),
                attempt_id,
            ),
        )
        conn.commit()
