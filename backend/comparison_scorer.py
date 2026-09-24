"""run 级比较式评分（pairwise / listwise），经 octagon-evals 的流程式 API。

与 pointwise 的根本差别在**粒度**：pointwise 是 attempt 级，一个 attempt 独立
成分；比较式是 run 级，必须把同一个 run 下的多个 attempt 聚成一组候选才能裁决。
evals 的 ``/evaluate`` 是单次 attempt 入口，按方法白名单会 409 拒收比较维度——
这是对的，所以这条链走流程式 API：

    ① POST /experiments/{run_id}/runs   ×N   每个 attempt 一个 evals run，
                                             共享同一份**完整** EvalPlan
    ② POST /experiments/{run_id}/compare     每个比较维度一次，返回每-attempt 标量
    ③ 回写 scores 表（role=diagnostic，weight=0，不进 score_total）

层级映射：octagon ``run`` ↔ evals ``experiment``，octagon ``attempt`` ↔ evals ``run``。

**结果的可比性依赖候选集。** win_count 是「胜场 ÷ 该候选实际参赛场次」，同一个
attempt 在 3 人组和 7 人组里的分不可比；换了 conversion 算法的结果也不能与旧结果
混排。这三项都落进 ``run_comparison_jobs``，不落就无从判断两个分数能不能放一起看。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import httpx

from .db import _now_iso, _open_sync
from .evals_scorer import COMPARISON_METHODS

logger = logging.getLogger(__name__)

COMPARISON_SCORER_VERSION = "octagon-comparison-v1"

#: 候选数下限。少于两个无从比较——不是失败，是这个 run 不具备比较条件。
MIN_CANDIDATES = 2


class ComparisonError(RuntimeError):
    """evals 流程式 API 调用或响应失败。

    与 pointwise 的 ``EvalsJudgeError`` 不同：比较失败**不**影响任何 attempt 的
    权威分数——比较维度是 diagnostic，从不参与 score_total。上层记 failed 即可。
    """


# ---------- 候选与作业记录 --------------------------------------------------


def list_candidates(db_path: Path, run_id: str) -> list[dict[str, Any]]:
    """run 下可参与比较的 attempt。

    只取 ``completed``：超时/崩溃的 attempt 没有可比的交付物，硬塞进候选集会让
    judge 在「没做完」和「做得差」之间做一个它无从判断的区分。
    """
    with _open_sync(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, agent_name, model, env_name, status FROM attempts "
            "WHERE run_id=? AND status='completed' ORDER BY id",
            (run_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def _record_job(
    db_path: Path,
    *,
    run_id: str,
    dimension: str,
    method: str,
    plan_hash: str,
    conversion: str,
    conversion_version: str,
    candidate_ids: list[str],
    status: str,
    result: dict[str, Any] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> str:
    job_id = f"cmp_{uuid.uuid4().hex[:12]}"
    now = _now_iso()
    with _open_sync(db_path) as conn:
        conn.execute(
            "INSERT INTO run_comparison_jobs "
            "(id, run_id, dimension, method, plan_hash, conversion, conversion_version,"
            " candidate_ids_json, status, result_json, error_code, error_message,"
            " created_at, ended_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(run_id, dimension, plan_hash) DO UPDATE SET "
            " status=excluded.status, result_json=excluded.result_json,"
            " candidate_ids_json=excluded.candidate_ids_json,"
            " error_code=excluded.error_code, error_message=excluded.error_message,"
            " ended_at=excluded.ended_at",
            (
                job_id, run_id, dimension, method, plan_hash, conversion,
                conversion_version, json.dumps(sorted(candidate_ids)), status,
                json.dumps(result or {}, ensure_ascii=False),
                error_code, error_message, now, now,
            ),
        )
        conn.commit()
    return job_id


def list_comparison_jobs(db_path: Path, run_id: str) -> list[dict[str, Any]]:
    with _open_sync(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM run_comparison_jobs WHERE run_id=? ORDER BY dimension",
            (run_id,),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["candidate_ids"] = json.loads(item.pop("candidate_ids_json") or "[]")
        item["result"] = json.loads(item.pop("result_json") or "{}")
        out.append(item)
    return out


def _write_scores(
    db_path: Path, *, dimension: str, values: dict[str, int], detail: str
) -> None:
    """把比较派生的标量写进 scores 表，同维度先删后插。

    先删是因为重评（换 conversion 或换候选集）必须**替换**旧值而不是并存——
    同一个 attempt 的同一维度出现两行，任何读取方都只能瞎猜该信哪个。
    """
    now = _now_iso()
    with _open_sync(db_path) as conn:
        for attempt_id, value in values.items():
            conn.execute(
                "DELETE FROM scores WHERE attempt_id=? AND dimension=?",
                (attempt_id, dimension),
            )
            conn.execute(
                "INSERT INTO scores (attempt_id, dimension, value, detail, scored_at) "
                "VALUES (?,?,?,?,?)",
                (attempt_id, dimension, int(value), detail, now),
            )
        conn.commit()


# ---------- evals 流程式 API ------------------------------------------------


def _post(endpoint: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    try:
        resp = httpx.post(endpoint, json=payload, timeout=timeout)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise ComparisonError(f"octagon-evals 调用失败 {endpoint}: {exc}") from exc
    try:
        return resp.json()
    except ValueError as exc:
        raise ComparisonError(f"octagon-evals 响应非 JSON {endpoint}: {exc}") from exc


def _start_candidate(
    *, base_url: str, timeout: float, run_id: str, attempt: dict[str, Any],
    env: Any, task: dict[str, Any], dimensions: list[dict[str, Any]],
) -> str:
    """把一个 attempt 注册成 evals experiment 下的一个 run，返回 plan_hash。

    EvaluationInput 要求 scenario/task/artifact/history 四个信封字段都非空，
    且 artifact 的 snapshot_ref / content_hash 是非空字符串（evals models.py 的
    __post_init__）。这里给的是本地引用——真正的证据靠 evidence 里的
    ``attempt_dir`` + evals 侧 pi 的 bash 去读。
    """
    attempt_id = str(attempt["id"])
    payload = {
        "run_id": attempt_id,
        "scenario": {
            "id": getattr(env, "name", None) or str(attempt.get("env_name") or "unknown"),
            "version": (getattr(env, "meta", {}) or {}).get("schema_version") or 1,
        },
        "task": task or {"id": run_id},
        "artifact": {
            "snapshot_ref": f"local:attempts/{attempt_id}",
            "content_hash": f"attempt:{attempt_id}",
        },
        "history": {"events_ref": f"local:attempts/{attempt_id}"},
        "producer": {
            "variant_id": str(attempt.get("agent_name") or ""),
            "model_id": str(attempt.get("model") or ""),
        },
        "upstream_completed": True,
        "dimensions": dimensions,
    }
    body = _post(f"{base_url}/experiments/{run_id}/runs", payload, timeout)
    plan_hash = body.get("plan_hash")
    if not plan_hash:
        raise ComparisonError(f"start_run 未返回 plan_hash: {body}")
    return str(plan_hash)


def _compare_dimension(
    *, base_url: str, timeout: float, run_id: str, dimension_id: str,
    evidence_by_attempt: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    body = _post(
        f"{base_url}/experiments/{run_id}/compare",
        {"dimension_id": dimension_id, "evidence": evidence_by_attempt},
        timeout,
    )
    scores = body.get("scores")
    if not isinstance(scores, dict):
        raise ComparisonError(f"compare 响应缺 scores: {body}")
    return scores


# ---------- 编排 ------------------------------------------------------------


def run_comparisons(
    *,
    db_path: Path,
    data_path: Path,
    run_id: str,
    env: Any,
    task: dict[str, Any],
    dimensions: list[dict[str, Any]],
    base_url: str,
    timeout: float,
) -> dict[str, Any]:
    """对一个 run 跑完全部比较维度，逐维记账并回写 scores。

    ``dimensions`` 是**完整的 plan 维度集**（pointwise + 比较式），不是只有
    比较式那几个：evals 的 ``validate_plan`` 要求 plan 里至少有一个
    ``role=scored`` 且权重 > 0 的维度，而比较维度恒为 diagnostic/weight=0——
    只送比较维度会被 ``PlanError: scored weights must sum to > 0`` 拒掉。
    这条规则本身是对的（一个什么都不评的 plan 没有意义），而且完整 plan 也更
    贴合 ``plan_hash`` 的语义：它冻结的是这个实验的全部维度。

    实际只对其中的比较式维度调 ``/compare``；pointwise 维度在这条链上只是
    plan 的一部分，不在这里评。

    单维失败不打断其余维度。返回 ``{"run_id", "candidates", "results": [...]}``，
    每个 result 是 ``{dimension, status, values?, error?}``。
    """
    plan_dimensions = list(dimensions or [])
    comparison_dims = [
        d for d in plan_dimensions
        if str(d.get("method", "")) in COMPARISON_METHODS
    ]
    if not comparison_dims:
        return {"run_id": run_id, "candidates": [], "results": [],
                "reason": "no_comparison_dimensions"}
    dimensions = comparison_dims

    base_url = str(base_url).rstrip("/")
    candidates = list_candidates(db_path, run_id)
    candidate_ids = [str(c["id"]) for c in candidates]

    if len(candidates) < MIN_CANDIDATES:
        # 不是失败：这个 run 就是不具备比较条件。逐维记 skipped，让读取方
        # 知道「比过了但没结果」和「压根没比」的区别。
        for dim in dimensions:
            _record_job(
                db_path, run_id=run_id, dimension=str(dim["id"]),
                method=str(dim["method"]), plan_hash="",
                conversion=str((dim.get("comparison") or {}).get("conversion") or "win_count"),
                conversion_version=str(
                    (dim.get("comparison") or {}).get("conversion_version") or "1"
                ),
                candidate_ids=candidate_ids, status="skipped",
                error_code="insufficient_candidates",
                error_message=f"需要至少 {MIN_CANDIDATES} 个 completed attempt，实得 {len(candidates)}",
            )
        return {"run_id": run_id, "candidates": candidate_ids, "results": [],
                "reason": "insufficient_candidates"}

    # 比较式判定同样要给归一化轨迹：raw events.jsonl 是各家 adapter 的原始格式，
    # 六个 agent 体积差 74 倍（见 atif_evidence 模块注释），拿它排序会被证据形态
    # 本身带偏，而不是被交付物质量带偏。路径必须绝对——judge 在自己的临时工作区
    # 里执行，相对路径它找不到，也不会报错，只会开始满盘乱找。
    from .atif_evidence import materialize_atif
    from .evals_scorer import judge_visible_path

    evidence_by_attempt: dict[str, dict[str, Any]] = {}
    for aid in candidate_ids:
        item: dict[str, Any] = {
            "attempt_dir": judge_visible_path(data_path, "attempts", aid),
        }
        atif = materialize_atif(Path(data_path), aid)
        item["atif_trajectory_path"] = (
            judge_visible_path(atif) if atif is not None else None
        )
        evidence_by_attempt[aid] = item
    missing_atif = sorted(
        aid for aid, item in evidence_by_attempt.items()
        if item["atif_trajectory_path"] is None
    )
    if missing_atif:
        # 证据形态不对等会直接影响排序公平性，必须说出来而不是埋掉。
        logger.warning(
            "比较式评分 run=%s：%d/%d 个候选没有 ATIF 轨迹（%s），"
            "它们与其余候选的证据形态不对等",
            run_id, len(missing_atif), len(candidate_ids), ", ".join(missing_atif),
        )

    # ① 注册候选。所有 attempt 必须送完全相同的 dimensions——evals 用 plan_hash
    #    锁住「一个 experiment 的所有 run 共享同一份冻结计划」，不一致会 409。
    plan_hash = ""
    try:
        for attempt in candidates:
            plan_hash = _start_candidate(
                base_url=base_url, timeout=timeout, run_id=run_id, attempt=attempt,
                env=env, task=task, dimensions=plan_dimensions,
            )
    except ComparisonError as exc:
        for dim in dimensions:
            _record_job(
                db_path, run_id=run_id, dimension=str(dim["id"]),
                method=str(dim["method"]), plan_hash=plan_hash or "",
                conversion=str((dim.get("comparison") or {}).get("conversion") or "win_count"),
                conversion_version=str(
                    (dim.get("comparison") or {}).get("conversion_version") or "1"
                ),
                candidate_ids=candidate_ids, status="failed",
                error_code="start_run_failed", error_message=str(exc),
            )
        raise

    # ② 逐维裁决 + ③ 回写
    results: list[dict[str, Any]] = []
    for dim in dimensions:
        name = str(dim["id"])
        comparison_cfg = dim.get("comparison") or {}
        conversion = str(comparison_cfg.get("conversion") or "win_count")
        conversion_version = str(comparison_cfg.get("conversion_version") or "1")
        try:
            scores = _compare_dimension(
                base_url=base_url, timeout=timeout, run_id=run_id,
                dimension_id=name, evidence_by_attempt=evidence_by_attempt,
            )
        except ComparisonError as exc:
            logger.warning("比较维度失败 run=%s dimension=%s: %s", run_id, name, exc)
            _record_job(
                db_path, run_id=run_id, dimension=name, method=str(dim["method"]),
                plan_hash=plan_hash, conversion=conversion,
                conversion_version=conversion_version, candidate_ids=candidate_ids,
                status="failed", error_code="compare_failed", error_message=str(exc),
            )
            results.append({"dimension": name, "status": "failed", "error": str(exc)})
            continue

        # evals 的标量是 [0,1]；未被采样到的候选返回 None → 该维度无分，不写行。
        values: dict[str, int] = {}
        for attempt_id, item in scores.items():
            raw = item.get("value") if isinstance(item, dict) else item
            if raw is None:
                continue
            try:
                values[str(attempt_id)] = int(round(float(raw) * 100))
            except (TypeError, ValueError):
                logger.warning(
                    "比较分非法 run=%s dimension=%s attempt=%s value=%r",
                    run_id, name, attempt_id, raw,
                )
        detail = (
            f"comparison:{dim['method']} conversion={conversion}"
            f" v{conversion_version} candidates={len(candidate_ids)}"
        )
        _write_scores(db_path, dimension=name, values=values, detail=detail)
        _record_job(
            db_path, run_id=run_id, dimension=name, method=str(dim["method"]),
            plan_hash=plan_hash, conversion=conversion,
            conversion_version=conversion_version, candidate_ids=candidate_ids,
            status="completed", result=values,
        )
        results.append({"dimension": name, "status": "completed", "values": values})

    return {"run_id": run_id, "candidates": candidate_ids,
            "plan_hash": plan_hash, "results": results}
