"""env meta.yaml 维度 → evals Dimension 的映射（method / role / comparison 透传）。

核心不变式：
- 不写 method 的维度维持历史行为（agent_judge、role=scored、权重原样）；
- 比较式维度默认 diagnostic 且 **weight 强制 0** —— 这是挡住它进 score_total
  的结构性闸，不依赖调用顺序；
- 比较式维度不进 /evaluate（那是 attempt 级入口，会整体 409），由 run 级
  比较链单独消费。
"""

from __future__ import annotations

import httpx

from backend.evals_scorer import (
    COMPARISON_METHODS,
    _dimensions_from_env,
    comparison_dimensions_from_env,
    make_evals_scorer,
)
from backend.evaluator import _aggregate_total


class MixedEnv:
    name = "mixed-env"
    meta = {
        "schema_version": "1.0",
        "dimensions": [
            # ① 老写法：不带 method
            {"name": "functional_correctness", "weight": 60, "description": "核心功能正确"},
            # ② pointwise agentic
            {"name": "repository_discipline", "weight": 30,
             "method": "agent_judge_agentic", "description": "改动集中",
             "anchors": [{"score": 0.0, "label": "绕过", "description": "硬编码绕测试"}]},
            # ③ 比较式
            {"name": "solution_elegance", "weight": 10,
             "method": "pairwise_judge_agentic", "description": "实现是否贴合既有抽象",
             "comparison": {"strategy": "round_robin", "conversion": "win_count"}},
        ],
    }


class OnlyComparisonEnv:
    name = "cmp-only"
    meta = {"dimensions": [
        {"name": "elegance", "method": "listwise_judge", "weight": 10},
    ]}


class BadMethodEnv:
    name = "typo-env"
    meta = {"dimensions": [
        {"name": "d1", "weight": 10, "method": "agentic_judge"},  # 拼错
    ]}


def test_legacy_dimension_keeps_historical_shape():
    dims = _dimensions_from_env(MixedEnv)
    first = next(d for d in dims if d["id"] == "functional_correctness")
    assert first["method"] == "agent_judge"
    assert first["role"] == "scored"
    assert first["weight"] == 60
    assert first["question"] == "核心功能正确"


def test_method_and_anchors_pass_through():
    dims = _dimensions_from_env(MixedEnv)
    second = next(d for d in dims if d["id"] == "repository_discipline")
    assert second["method"] == "agent_judge_agentic"
    assert second["anchors"][0]["label"] == "绕过"


def test_comparison_dimension_excluded_from_evaluate():
    """比较维度不能混进 /evaluate 的 dimensions —— 否则整批 409。"""
    ids = [d["id"] for d in _dimensions_from_env(MixedEnv)]
    assert ids == ["functional_correctness", "repository_discipline"]


def test_comparison_dimension_is_diagnostic_with_zero_weight():
    cmp_dims = comparison_dimensions_from_env(MixedEnv)
    assert [d["id"] for d in cmp_dims] == ["solution_elegance"]
    dim = cmp_dims[0]
    assert dim["method"] in COMPARISON_METHODS
    assert dim["role"] == "diagnostic"
    # meta.yaml 里写的是 10，透传时被强制归零
    assert dim["weight"] == 0
    assert dim["comparison"]["conversion"] == "win_count"


def test_diagnostic_weight_zero_keeps_it_out_of_score_total():
    """即便比较分混进了 scores 列表，weight=0 也让它不参与加权平均。"""
    weights = {"functional_correctness": 60, "solution_elegance": 0}
    scores = [
        {"dimension": "functional_correctness", "value": 80},
        {"dimension": "solution_elegance", "value": 0},
    ]
    assert _aggregate_total(scores, weights) == 80


def test_explicit_scored_role_survives():
    """显式写 role: scored 时不被降级——代价由配置者承担。"""
    class ExplicitEnv:
        name = "explicit"
        meta = {"dimensions": [
            {"name": "elegance", "method": "pairwise_judge", "weight": 20, "role": "scored"},
        ]}

    dim = comparison_dimensions_from_env(ExplicitEnv)[0]
    assert dim["role"] == "scored"
    assert dim["weight"] == 20


def test_unknown_method_falls_back_to_agent_judge():
    dims = _dimensions_from_env(BadMethodEnv)
    assert dims[0]["method"] == "agent_judge"


def test_only_comparison_env_skips_evaluate_call(tmp_path, monkeypatch):
    """全是比较维度时不发 /evaluate（dimensions=[] 会被 evals 422）。"""
    calls: list = []

    def fake_post(url, *, json=None, timeout=None):
        calls.append(url)
        return httpx.Response(200, json={"status": "completed", "results": []},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr("backend.evals_scorer.httpx.post", fake_post)
    scorer = make_evals_scorer(
        env=OnlyComparisonEnv, base_url="http://127.0.0.1:8000",
        timeout=10.0, data_path=tmp_path, job_id="scj_y",
    )
    out = scorer(attempt_id="att1", task={}, env_db=None, trace=[], final_state={})
    assert out == []
    assert calls == []
