"""evals 外部 judge 适配器测试 + settings.judge 配置解析。

核心不变式：external 模式下 scorer 形状与内置一致（维度分 0-100 int）；
judge 血统写 judge_result.json 供 provenance 回读（judge 锚不丢）；
evals 服务不可达 / 响应非法 → EvalsJudgeError，上层映射 judge_unavailable，
绝不落业务 0 分。
"""

from __future__ import annotations

import json

import httpx

from backend.config import load_settings
from backend.evals_scorer import EvalsJudgeError, make_evals_scorer


class FakeEnv:
    name = "demo-env"
    meta = {
        "schema_version": "1.0",
        "dimensions": [
            {"name": "functional_correctness", "weight": 55, "description": "核心功能正确"},
            {"name": "regression_safety", "weight": 25},
        ],
    }


def _scorer(tmp_path, monkeypatch, *, responses=None, raise_error=None):
    calls: list[tuple[str, dict, float]] = []

    def fake_post(url, *, json=None, timeout=None):
        calls.append((url, json, timeout))
        if raise_error is not None:
            raise raise_error
        payload = responses.pop(0) if responses else _ok_response()
        return httpx.Response(
            200, json=payload, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr("backend.evals_scorer.httpx.post", fake_post)
    scorer = make_evals_scorer(
        env=FakeEnv,
        base_url="http://127.0.0.1:8000",
        timeout=42.0,
        data_path=tmp_path,
        job_id="scj_x",
    )
    return scorer, calls


def _ok_response():
    return {
        "status": "completed",
        "results": [
            {"dimension_id": "functional_correctness", "method": "agent_judge",
             "value": 0.9, "reason": "solid", "lineage": {"model": "m1", "prompt_version": "v1"}},
            {"dimension_id": "regression_safety", "method": "agent_judge",
             "value": 2 / 3, "reason": "partial", "lineage": {"model": "m1", "prompt_version": "v1"}},
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        "error": None,
    }


def test_packs_dimensions_and_converts_scores(tmp_path, monkeypatch):
    scorer, calls = _scorer(tmp_path, monkeypatch)
    out = scorer(
        attempt_id="att1",
        task={"id": "t1"},
        env_db=None,
        trace=[{"kind": "tool", "tool": "bash"}],
        final_state={"done": True},
        events=[{"kind": "agent:end"}],
    )
    url, payload, timeout = calls[0]
    assert url == "http://127.0.0.1:8000/evaluate"
    assert timeout == 42.0
    assert payload["evaluation_id"] == "scj_x:att1"
    assert payload["run_id"] == "att1"
    # 维度来自 env meta.yaml，ID=维度名，method=agent_judge（LLM-as-judge）
    dims = payload["dimensions"]
    assert [d["id"] for d in dims] == ["functional_correctness", "regression_safety"]
    assert all(d["method"] == "agent_judge" for d in dims)
    assert dims[0]["weight"] == 55
    # evidence 带 trace/final_state/events/attempt_dir
    assert payload["evidence"]["trace"] == [{"kind": "tool", "tool": "bash"}]
    assert payload["evidence"]["final_state"] == {"done": True}
    assert payload["evidence"]["events"] == [{"kind": "agent:end"}]
    assert "attempts/att1" in payload["evidence"]["attempt_dir"]
    # [0,1] 标量 → 0-100 int 维度分
    assert out == [
        {"dimension": "functional_correctness", "value": 90, "detail": "solid"},
        {"dimension": "regression_safety", "value": 67, "detail": "partial"},
    ]


def test_writes_judge_result_for_provenance(tmp_path, monkeypatch):
    scorer, _ = _scorer(tmp_path, monkeypatch)
    scorer(
        attempt_id="att1",
        task={},
        env_db=None,
        trace=[],
        final_state={},
    )
    target = tmp_path / "scoring-work" / "scj_x" / "judge_result.json"
    assert target.is_file()
    data = json.loads(target.read_text())
    assert data["model"] == "m1"
    assert data["prompt_version"] == "v1"
    assert data["provider"] == "octagon-evals"


def test_http_error_raises_evals_judge_error(tmp_path, monkeypatch):
    scorer, _ = _scorer(
        tmp_path, monkeypatch, raise_error=httpx.ConnectError("connection refused")
    )
    try:
        scorer(attempt_id="att1", task={}, env_db=None, trace=[], final_state={})
        raise AssertionError("expected EvalsJudgeError")
    except EvalsJudgeError as exc:
        assert "调用失败" in str(exc)


def test_failed_status_raises(tmp_path, monkeypatch):
    scorer, _ = _scorer(
        tmp_path, monkeypatch,
        responses=[{"status": "failed", "results": [], "error": "judge boom", "usage": None}],
    )
    try:
        scorer(attempt_id="att1", task={}, env_db=None, trace=[], final_state={})
        raise AssertionError("expected EvalsJudgeError")
    except EvalsJudgeError as exc:
        assert "judge boom" in str(exc)


def test_invalid_value_raises_not_zero(tmp_path, monkeypatch):
    """非法 value 必须失败而不是转成 0 分（不猜分）。"""
    scorer, _ = _scorer(
        tmp_path, monkeypatch,
        responses=[{"status": "completed", "results": [
            {"dimension_id": "d", "value": "not-a-number", "reason": "x", "lineage": {}},
        ], "usage": None, "error": None}],
    )
    try:
        scorer(attempt_id="att1", task={}, env_db=None, trace=[], final_state={})
        raise AssertionError("expected EvalsJudgeError")
    except EvalsJudgeError as exc:
        assert "非法" in str(exc)


def test_no_lineage_skips_judge_result(tmp_path, monkeypatch):
    scorer, _ = _scorer(
        tmp_path, monkeypatch,
        responses=[{"status": "completed", "results": [
            {"dimension_id": "d", "value": 0.5, "reason": "x", "lineage": {}},
        ], "usage": None, "error": None}],
    )
    scorer(attempt_id="att1", task={}, env_db=None, trace=[], final_state={})
    assert not (tmp_path / "scoring-work" / "scj_x" / "judge_result.json").exists()


# ---------- settings.judge 配置解析 ----------


def test_judge_section_defaults_to_internal(tmp_path):
    cfg = load_settings(tmp_path / "absent.yaml")
    assert cfg.judge.backend == "internal"
    assert cfg.judge.evals_base_url == "http://127.0.0.1:8000"
    assert cfg.judge.evals_timeout == 300.0


def test_judge_section_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("OCTAGON_JUDGE_BACKEND", "evals")
    monkeypatch.setenv("OCTAGON_EVALS_BASE_URL", "http://127.0.0.1:9999")
    monkeypatch.setenv("OCTAGON_EVALS_TIMEOUT", "120")
    cfg = load_settings(tmp_path / "absent.yaml")
    assert cfg.judge.backend == "evals"
    assert cfg.judge.evals_base_url == "http://127.0.0.1:9999"
    assert cfg.judge.evals_timeout == 120.0
