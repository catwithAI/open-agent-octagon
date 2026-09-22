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


def _dimensions_from_env(env: Any) -> list[dict[str, Any]]:
    """env meta.yaml 的 dimensions → evals EvalPlan dimensions。

    维度 ID 直接取 dimension.name：evals 返回的 ``dimension_id`` 即维度名，
    agent-octagon 的 scores.dimension 用它落库，两侧 ID 天然对齐。
    """
    meta = getattr(env, "meta", {}) or {}
    dims: list[dict[str, Any]] = []
    for item in meta.get("dimensions") or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        name = str(item["name"])
        dims.append({
            "id": name,
            "version": 1,
            "weight": int(item.get("weight", 1)),
            "method": "agent_judge",
            "question": str(item.get("description") or name),
        })
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
