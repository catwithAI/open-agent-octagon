"""octagon-evals 归因适配器（``POST /attribute``）。

把一个已评分维度 + 行为证据送去 evals，换回候选归因（现象 / 根因 / 修改建议）。

三条硬规矩，与 ``automation/diagnostics.py`` 同一套语义：

- **派生投影**：归因产物落 ``data/attempts/<id>/attribution/<dimension>.json``，
  不进 DB、不碰 ``scores``、不碰 ``attempts.score_total``。归因失败绝不改写
  权威评分事实。
- **永远是候选**：evals 侧强制 ``status="candidate"``（role.py 的校验），这里
  再复核一次。任何调用方都不得自动应用它的 suggestions。
- **手动触发**：不挂 runner 的终态钩子。成本是 per-维度一次 agentic pi 会话，
  自动化之前先让人按需要触发（API: ``POST /api/attempts/{id}/attribute``）。

证据与 pointwise agentic judge 共用同一结构（``attempt_dir`` 是**路径字符串**，
靠 evals 侧 pi 的 bash 去读）——因此同样要求 evals 与本进程同机同盘，见
``config.JudgeSection.evals_base_url`` 的说明。
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

ATTRIBUTION_CLIENT_VERSION = "octagon-attribution-client-v1"


class AttributionError(RuntimeError):
    """evals /attribute 调用或响应失败。

    与 ``EvalsJudgeError`` 的区别：归因失败**没有**业务后果——它不产生分数，
    上层只需把错误回给调用方，不改 attempt 任何状态。
    """


def attribution_dir(data_path: Path, attempt_id: str) -> Path:
    return Path(data_path) / "attempts" / attempt_id / "attribution"


def attribution_path(data_path: Path, attempt_id: str, dimension: str) -> Path:
    return attribution_dir(data_path, attempt_id) / f"{_safe_name(dimension)}.json"


def _safe_name(dimension: str) -> str:
    """维度名 → 安全文件名。维度名来自 env meta.yaml，不是用户输入，但它会
    进路径，仍然要挡住 ``/`` 和 ``..``。"""
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in str(dimension))
    return cleaned.strip(".-") or "dimension"


def select_dimensions(
    scores: list[dict[str, Any]],
    *,
    threshold: int,
    max_dimensions: int,
) -> list[dict[str, Any]]:
    """挑出值得归因的维度：低于阈值的，按分数升序，最多 N 个。

    分数最低的最可能有说法，先归因它们；成本上限由 ``max_dimensions`` 兜住。
    """
    candidates = [
        s for s in scores
        if s.get("dimension") and _as_int(s.get("value")) is not None
        and _as_int(s.get("value")) < threshold
    ]
    candidates.sort(key=lambda s: (_as_int(s["value"]), str(s["dimension"])))
    return candidates[:max_dimensions]


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def attribute_dimension(
    *,
    base_url: str,
    timeout: float,
    attribution_id: str,
    dimension: dict[str, Any],
    score: dict[str, Any],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """调一次 evals ``/attribute``，返回归因产物（不落盘）。

    ``score.value`` 以 **0-100** 传入（agent-octagon 的标度），这里归一化成
    evals 要求的 [0,1]。两侧标度不同是既有事实（``evals_scorer`` 回来时 ×100），
    归一化必须在边界上做一次且只做一次。
    """
    value = _as_int(score.get("value"))
    if value is None:
        raise AttributionError(f"维度 {dimension.get('id')!r} 没有可归因的分数")
    payload = {
        "attribution_id": attribution_id,
        "dimension": dimension,
        "score": {
            "value": max(0.0, min(1.0, value / 100.0)),
            "reason": str(score.get("detail") or ""),
            "source": str(score.get("source") or "agent-octagon"),
            "lineage": dict(score.get("lineage") or {}),
        },
        "evidence": evidence,
    }
    endpoint = f"{str(base_url).rstrip('/')}/attribute"
    try:
        resp = httpx.post(endpoint, json=payload, timeout=timeout)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise AttributionError(f"octagon-evals /attribute 调用失败: {exc}") from exc
    try:
        body = resp.json()
    except ValueError as exc:
        raise AttributionError(f"octagon-evals /attribute 响应非 JSON: {exc}") from exc
    attribution = body.get("attribution")
    if not isinstance(attribution, dict):
        raise AttributionError(f"octagon-evals /attribute 响应缺 attribution: {body}")
    # 复核候选态：evals 侧已强制，这里再挡一次——归因一旦被当成定论使用，
    # 就变成了没有校准过的自动裁决。
    if attribution.get("status") != "candidate":
        raise AttributionError(
            f"归因产物不是候选态（status={attribution.get('status')!r}），拒绝采纳"
        )
    return attribution


def attribute_attempt(
    *,
    data_path: Path,
    attempt_id: str,
    job_id: str,
    dimensions: list[dict[str, Any]],
    scores: list[dict[str, Any]],
    evidence: dict[str, Any],
    base_url: str,
    timeout: float,
    threshold: int,
    max_dimensions: int,
) -> dict[str, Any]:
    """对一个 attempt 的低分维度批量归因，逐维落盘。

    单维失败不打断其余维度——归因是尽力而为的分析，部分结果仍然有用。返回
    ``{"attempt_id", "attributed": [...], "errors": [...], "skipped": [...]}``。
    """
    by_id = {str(d.get("id")): d for d in dimensions if d.get("id")}
    selected = select_dimensions(scores, threshold=threshold, max_dimensions=max_dimensions)
    attributed: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for score in selected:
        name = str(score["dimension"])
        dimension = by_id.get(name)
        if dimension is None:
            # 分数在库里、维度已从 env meta.yaml 移除：没有 rubric 就没有归因
            # 的依据（anchors/question 都来自它），跳过而不是拿空维度硬问。
            skipped.append({"dimension": name, "reason": "dimension_not_in_env"})
            continue
        try:
            attribution = attribute_dimension(
                base_url=base_url,
                timeout=timeout,
                attribution_id=f"{job_id}:{attempt_id}:{name}",
                dimension=dimension,
                score=score,
                evidence=evidence,
            )
        except AttributionError as exc:
            logger.warning("归因失败 attempt=%s dimension=%s: %s", attempt_id, name, exc)
            errors.append({"dimension": name, "error": str(exc)})
            continue
        record = {
            "schema_version": ATTRIBUTION_CLIENT_VERSION,
            "attempt_id": attempt_id,
            "dimension": name,
            "score_value": _as_int(score.get("value")),
            "attribution": attribution,
        }
        _atomic_json(attribution_path(data_path, attempt_id, name), record)
        attributed.append(record)

    return {
        "attempt_id": attempt_id,
        "attributed": attributed,
        "errors": errors,
        "skipped": skipped,
    }


def read_attributions(data_path: Path, attempt_id: str) -> list[dict[str, Any]]:
    """回读一个 attempt 已落盘的全部归因。缺失/损坏 → 跳过，不抛。"""
    root = attribution_dir(data_path, attempt_id)
    if not root.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            records.append(value)
    return records
