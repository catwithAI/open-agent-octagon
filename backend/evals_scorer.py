"""octagon-evals 外部 judge 适配器（settings.judge.backend = "evals"）。

把 env 的每个维度打包成 EvaluateRequest 送 evals 的 ``/evaluate``，返回的
[0,1] 标量转回 0-100 维度分——与内置 scorer 的输出形状一致，commit /
outbox / leader / provenance 全链路复用。

judge 血统（model / prompt_version）写进 ``scoring-work/<job_id>/judge_result.json``，
供 ``provenance.read_judge_anchors`` 回读 judge 锚——否则外部 judge 的版本锚会
变 NULL，provenance_complete=1 但 judge 一段是空的。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class EvalsJudgeError(RuntimeError):
    """evals 服务调用/响应失败。上层映射为 scoring_failed（judge_unavailable），
    绝不落业务 0 分——与 scoring_queue 的 judge_infrastructure_error 同一语义。
    """


#: evals 的比较式方法（models.COMPARISON_METHODS）。这些维度是 **run 级**的——
#: 要把同一个 run 下的多个 attempt 聚成一组候选才能裁决，而 /evaluate 是
#: attempt 级单次入口，送进去会被整体 409（api.py 的 method 白名单）。故
#: pointwise 打包时必须先滤掉，否则一个比较维度会让同 env 的所有维度都评不成。
COMPARISON_METHODS = frozenset({
    "pairwise_judge", "pairwise_judge_agentic",
    "listwise_judge", "listwise_judge_agentic",
})

#: /evaluate 接受的 pointwise 方法白名单。env 写了白名单外的方法 → 按缺省
#: agent_judge 处理并告警，不让一个拼错的方法名把整批评分带崩。
_POINTWISE_METHODS = frozenset({
    "deterministic", "agent_judge", "agent_judge_agentic", "jev_judge",
})

DEFAULT_METHOD = "agent_judge"


def _dimension_from_meta(item: dict[str, Any]) -> dict[str, Any]:
    """meta.yaml 的一个维度块 → evals Dimension dict。

    维度 ID 直接取 dimension.name：evals 返回的 ``dimension_id`` 即维度名，
    agent-octagon 的 scores.dimension 用它落库，两侧 ID 天然对齐。

    ``method`` / ``role`` / ``anchors`` / ``comparison`` 原样透传给 evals，
    由那边的 ``Dimension`` / ``ComparisonConfig`` 负责校验——这里不重复建模，
    免得两侧的合法取值各自漂移。
    """
    name = str(item["name"])
    method = str(item.get("method") or DEFAULT_METHOD)
    dim: dict[str, Any] = {
        "id": name,
        "version": int(item.get("version", 1)),
        "weight": int(item.get("weight", 1)),
        "method": method,
        "question": str(item.get("description") or name),
    }
    # 比较维度默认 diagnostic：它的分依赖同组其他 attempt，进了 score_total
    # 就让单个 attempt 的总分随同伴变化，也就没法单独重放。要进总分必须在
    # meta.yaml 里显式写 role: scored。
    default_role = "diagnostic" if method in COMPARISON_METHODS else "scored"
    dim["role"] = str(item.get("role") or default_role)
    if dim["role"] == "diagnostic":
        # 权重强制归零，而不只是靠「不进 scores 列表」。_aggregate_total 是按
        # `weights.get(dimension)` 查权重、`if w > 0` 才计入的——权重为 0 就
        # 保证了即便将来有人改成从 DB 的 scores 表重算总分，diagnostic 维度
        # 也不会被算进去。这是一道结构性的闸，不依赖调用顺序。
        dim["weight"] = 0
    if item.get("anchors"):
        dim["anchors"] = item["anchors"]
    if item.get("comparison"):
        dim["comparison"] = item["comparison"]
    return dim


def _iter_meta_dimensions(env: Any):
    meta = getattr(env, "meta", {}) or {}
    for item in meta.get("dimensions") or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        yield item


def comparison_dimensions_from_env(env: Any) -> list[dict[str, Any]]:
    """env 里的比较式维度（run 级，由 run_comparison 那条链消费）。"""
    return [
        _dimension_from_meta(item)
        for item in _iter_meta_dimensions(env)
        if str(item.get("method") or DEFAULT_METHOD) in COMPARISON_METHODS
    ]


def _dimensions_from_env(env: Any) -> list[dict[str, Any]]:
    """env meta.yaml 的 dimensions → evals EvalPlan dimensions（仅 pointwise）。"""
    dims: list[dict[str, Any]] = []
    for item in _iter_meta_dimensions(env):
        method = str(item.get("method") or DEFAULT_METHOD)
        if method in COMPARISON_METHODS:
            continue
        if method not in _POINTWISE_METHODS:
            logger.warning(
                "env %s 维度 %s 的 method=%r 不被 /evaluate 支持，按 %s 处理",
                getattr(env, "name", "?"), item["name"], method, DEFAULT_METHOD,
            )
            item = {**item, "method": DEFAULT_METHOD}
        dims.append(_dimension_from_meta(item))
    return dims


def _write_judge_result(
    data_path: Path, job_id: str, models: set[str], prompts: set[str]
) -> None:
    """把 evals 回传的 judge 血统落盘，供 provenance 回读 judge 锚。

    多维度各自带 lineage，合并取并集 `|` 连接——与 read_judge_anchors 的
    合并口径一致。best-effort：写失败不阻塞评分。
    """
    if not models and not prompts:
        return
    target = Path(data_path) / "scoring-work" / job_id / "judge_result.json"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "model": "|".join(sorted(models)) or None,
                    "prompt_version": "|".join(sorted(prompts)) or None,
                    "provider": "octagon-evals",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        logger.warning("judge_result 写入失败(不影响评分) job=%s", job_id)


def make_evals_scorer(
    *,
    env: Any,
    base_url: str,
    timeout: float,
    data_path: Path,
    job_id: str,
):
    """返回兼容 scorer 签名的闭包：调 evals ``/evaluate`` 评单个 attempt 全维度。

    签名对齐 ``evaluate()`` 的 scorer 约定
    ``score(*, attempt_id, task, env_db, trace, final_state)``；events 经 **kwargs
    接收（evaluate() 按签名检测是否注入）。
    """
    dimensions = _dimensions_from_env(env)
    endpoint = f"{str(base_url).rstrip('/')}/evaluate"

    def score(
        *,
        attempt_id: str,
        task: dict[str, Any],
        env_db,
        trace,
        final_state,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        evidence = {
            "final_state": final_state,
            "trace": trace,
            "events": kwargs.get("events") or [],
            "attempt_dir": str(Path(data_path) / "attempts" / attempt_id),
        }
        if not dimensions:
            # env 只配了比较式维度：pointwise 无可评，直接返回空而不是发一个
            # dimensions=[] 的请求（evals 侧 min_length=1 会 422）。这些维度的
            # 分由 run 级比较链在全部 attempt 评完后回填。
            logger.info(
                "env %s 无 pointwise 维度，跳过 /evaluate attempt=%s",
                getattr(env, "name", "?"), attempt_id,
            )
            return []
        payload = {
            "evaluation_id": f"{job_id}:{attempt_id}",
            "run_id": attempt_id,
            "scenario": {
                "id": getattr(env, "name", None) or "unknown",
                "version": (getattr(env, "meta", {}) or {}).get("schema_version"),
            },
            "task": task,
            "artifact": {"snapshot_ref": "local", "content_hash": ""},
            "history": {"events_ref": "local"},
            "evidence": evidence,
            "dimensions": dimensions,
            "deadline_seconds": timeout,
            "judge_config": {},
        }
        try:
            resp = httpx.post(endpoint, json=payload, timeout=timeout)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise EvalsJudgeError(f"octagon-evals 调用失败: {exc}") from exc
        try:
            body = resp.json()
        except ValueError as exc:
            raise EvalsJudgeError(f"octagon-evals 响应非 JSON: {exc}") from exc
        if body.get("status") != "completed":
            raise EvalsJudgeError(
                f"octagon-evals 评分失败: {body.get('error') or body.get('status')}"
            )
        results: list[dict[str, Any]] = []
        models: set[str] = set()
        prompts: set[str] = set()
        for item in body.get("results") or []:
            try:
                value = int(round(float(item["value"]) * 100))
            except (KeyError, TypeError, ValueError) as exc:
                raise EvalsJudgeError(f"evals 维度分非法: {item}") from exc
            results.append(
                {
                    "dimension": str(item.get("dimension_id", "")),
                    "value": value,
                    "detail": str(item.get("reason") or ""),
                }
            )
            lineage = item.get("lineage") or {}
            if lineage.get("model"):
                models.add(str(lineage["model"]))
            if lineage.get("prompt_version"):
                prompts.add(str(lineage["prompt_version"]))
        _write_judge_result(data_path, job_id, models, prompts)
        return results

    return score
