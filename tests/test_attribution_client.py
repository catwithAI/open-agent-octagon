"""归因客户端：维度挑选、标度归一、候选态复核、落盘与回读。

核心不变式：
- 归因是派生投影 —— 不写 scores、不动 score_total；单维失败不打断其余维度；
- agent-octagon 的 0-100 标度在边界上归一成 evals 要求的 [0,1]，只做一次；
- 产物必须自称 ``candidate``；不是候选态就拒绝采纳（归因不是裁决）。
"""

from __future__ import annotations

import json

import httpx
import pytest

from backend.attribution_client import (
    AttributionError,
    attribute_attempt,
    attribute_dimension,
    read_attributions,
    select_dimensions,
)


def _attribution(status: str = "candidate") -> dict:
    return {
        "schema_version": "octagon_evals.attribution.v1",
        "status": status,
        "phenomenon": "未跑测试就交付",
        "root_causes": [{"category": "agent_behavior", "description": "跳过验证",
                         "confidence": "high"}],
        "suggestions": [{"target": "agent", "action": "先跑测试", "rationale": "挡回归"}],
        "notes": [],
    }


def _ok_body(dimension_id: str = "validation", status: str = "candidate") -> dict:
    return {
        "attribution_id": f"job:att:{dimension_id}",
        "dimension_id": dimension_id,
        "attribution": _attribution(status),
    }


DIMENSIONS = [
    {"id": "functional_correctness", "weight": 60, "method": "agent_judge"},
    {"id": "validation", "weight": 5, "method": "agent_judge_agentic"},
    {"id": "regression_safety", "weight": 25, "method": "agent_judge"},
]

SCORES = [
    {"dimension": "functional_correctness", "value": 90, "detail": "ok"},
    {"dimension": "validation", "value": 20, "detail": "未见测试"},
    {"dimension": "regression_safety", "value": 45, "detail": "部分回归"},
]


# ---------- 维度挑选 ----------


def test_selects_lowest_scores_under_threshold():
    picked = select_dimensions(SCORES, threshold=60, max_dimensions=3)
    assert [s["dimension"] for s in picked] == ["validation", "regression_safety"]


def test_respects_max_dimensions_cost_cap():
    picked = select_dimensions(SCORES, threshold=100, max_dimensions=2)
    assert [s["dimension"] for s in picked] == ["validation", "regression_safety"]


def test_threshold_zero_selects_nothing():
    assert select_dimensions(SCORES, threshold=0, max_dimensions=5) == []


def test_non_numeric_value_is_skipped_not_crashed():
    picked = select_dimensions(
        [{"dimension": "d", "value": None}, {"dimension": "e", "value": 10}],
        threshold=60, max_dimensions=5,
    )
    assert [s["dimension"] for s in picked] == ["e"]


# ---------- 单维调用 ----------


def _patch_post(monkeypatch, handler):
    calls: list = []

    def fake_post(url, *, json=None, timeout=None):
        calls.append((url, json, timeout))
        return handler(url, json)

    monkeypatch.setattr("backend.attribution_client.httpx.post", fake_post)
    return calls


def test_normalizes_score_scale_to_unit_interval(monkeypatch):
    calls = _patch_post(
        monkeypatch,
        lambda url, payload: httpx.Response(
            200, json=_ok_body(), request=httpx.Request("POST", url)
        ),
    )
    attribute_dimension(
        base_url="http://127.0.0.1:8000/", timeout=30.0,
        attribution_id="job:att:validation",
        dimension=DIMENSIONS[1],
        score={"value": 20, "detail": "未见测试"},
        evidence={"attempt_dir": "/d/attempts/att"},
    )
    url, payload, timeout = calls[0]
    assert url == "http://127.0.0.1:8000/attribute"
    assert timeout == 30.0
    assert payload["score"]["value"] == pytest.approx(0.2)
    assert payload["score"]["reason"] == "未见测试"
    assert payload["dimension"]["id"] == "validation"


def test_rejects_non_candidate_status(monkeypatch):
    _patch_post(
        monkeypatch,
        lambda url, payload: httpx.Response(
            200, json=_ok_body(status="final"), request=httpx.Request("POST", url)
        ),
    )
    with pytest.raises(AttributionError, match="候选态"):
        attribute_dimension(
            base_url="http://x", timeout=1.0, attribution_id="a",
            dimension=DIMENSIONS[1], score={"value": 20}, evidence={},
        )


def test_http_error_becomes_attribution_error(monkeypatch):
    def boom(url, payload):
        raise httpx.ConnectError("refused")

    _patch_post(monkeypatch, boom)
    with pytest.raises(AttributionError, match="调用失败"):
        attribute_dimension(
            base_url="http://x", timeout=1.0, attribution_id="a",
            dimension=DIMENSIONS[1], score={"value": 20}, evidence={},
        )


def test_missing_score_value_raises(monkeypatch):
    _patch_post(monkeypatch, lambda url, payload: httpx.Response(
        200, json=_ok_body(), request=httpx.Request("POST", url)))
    with pytest.raises(AttributionError, match="没有可归因的分数"):
        attribute_dimension(
            base_url="http://x", timeout=1.0, attribution_id="a",
            dimension=DIMENSIONS[1], score={"detail": "无分"}, evidence={},
        )


# ---------- 批量 + 落盘 ----------


def _run_attempt(tmp_path, monkeypatch, handler):
    _patch_post(monkeypatch, handler)
    return attribute_attempt(
        data_path=tmp_path, attempt_id="att1", job_id="run1",
        dimensions=DIMENSIONS, scores=SCORES,
        evidence={"attempt_dir": str(tmp_path / "attempts" / "att1")},
        base_url="http://127.0.0.1:8000", timeout=30.0,
        threshold=60, max_dimensions=3,
    )


def test_attributes_and_persists_per_dimension(tmp_path, monkeypatch):
    out = _run_attempt(
        tmp_path, monkeypatch,
        lambda url, payload: httpx.Response(
            200, json=_ok_body(payload["dimension"]["id"]),
            request=httpx.Request("POST", url),
        ),
    )
    assert [r["dimension"] for r in out["attributed"]] == ["validation", "regression_safety"]
    assert out["errors"] == [] and out["skipped"] == []

    path = tmp_path / "attempts" / "att1" / "attribution" / "validation.json"
    record = json.loads(path.read_text())
    assert record["attempt_id"] == "att1"
    assert record["score_value"] == 20
    assert record["attribution"]["status"] == "candidate"

    # 回读拿到两份，按维度名排序
    assert [r["dimension"] for r in read_attributions(tmp_path, "att1")] == [
        "regression_safety", "validation",
    ]


def test_single_dimension_failure_does_not_stop_the_rest(tmp_path, monkeypatch):
    def handler(url, payload):
        if payload["dimension"]["id"] == "validation":
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json=_ok_body(payload["dimension"]["id"]),
                              request=httpx.Request("POST", url))

    out = _run_attempt(tmp_path, monkeypatch, handler)
    assert [r["dimension"] for r in out["attributed"]] == ["regression_safety"]
    assert [e["dimension"] for e in out["errors"]] == ["validation"]


def test_score_without_matching_env_dimension_is_skipped(tmp_path, monkeypatch):
    _patch_post(monkeypatch, lambda url, payload: httpx.Response(
        200, json=_ok_body(), request=httpx.Request("POST", url)))
    out = attribute_attempt(
        data_path=tmp_path, attempt_id="att1", job_id="run1",
        dimensions=[DIMENSIONS[0]],  # env 里已没有 validation 这一维
        scores=[{"dimension": "validation", "value": 20}],
        evidence={}, base_url="http://x", timeout=1.0,
        threshold=60, max_dimensions=3,
    )
    assert out["attributed"] == []
    assert out["skipped"] == [{"dimension": "validation", "reason": "dimension_not_in_env"}]


def test_read_attributions_missing_dir_is_empty(tmp_path):
    assert read_attributions(tmp_path, "nope") == []
