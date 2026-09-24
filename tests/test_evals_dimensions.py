"""env meta.yaml 维度 → evals Dimension 的映射（method / role / comparison 透传）。

核心不变式：
- 不写 method 的维度维持历史行为（agent_judge、role=scored、权重原样）；
- 比较式维度默认 diagnostic 且 **weight 强制 0** —— 这是挡住它进 score_total
  的结构性闸，不依赖调用顺序；
- 比较式维度不进 /evaluate（那是 attempt 级入口，会整体 409），由 run 级
  比较链单独消费。
"""

from __future__ import annotations

from pathlib import Path

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
    # question 被包成明确的评判指令：env 的 description 常写的是口径而非判据，
    # 原样送过去 judge 会去「核验这句话是否属实」（2026-09-24 实测有一个
    # 候选正是跑去读 env 的 judge_local.py 确认实现与描述相符，给了 100 分）。
    assert "核心功能正确" in first["question"]
    assert "functional_correctness" in first["question"]
    assert "候选" in first["question"]


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


def test_method_override_forces_pointwise_method():
    """绑定 env 私有资产的维度需要整体切 agentic，而不改共享 env 仓库。"""
    dims = _dimensions_from_env(MixedEnv, method_override="agent_judge_agentic")
    assert all(d["method"] == "agent_judge_agentic" for d in dims)
    # 比较式维度走另一条链，不受影响
    assert [d["id"] for d in dims] == ["functional_correctness", "repository_discipline"]
    assert comparison_dimensions_from_env(MixedEnv)[0]["method"] == "pairwise_judge_agentic"


def _capture(monkeypatch):
    captured: list = []

    def fake_post(url, *, json=None, timeout=None):
        captured.append(json)
        return httpx.Response(200, json={"status": "completed", "results": []},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr("backend.evals_scorer.httpx.post", fake_post)
    return captured


def _env_with_dir(tmp_path):
    class EnvWithDir:
        name = "gdpval-like"
        env_dir = tmp_path / "envs" / "gdpval-like"
        meta = {"dimensions": [{"name": "official_rubric_judge", "weight": 100}]}

    return EnvWithDir


def test_agentic_evidence_is_pointers_not_inlined_bulk(tmp_path, monkeypatch):
    """agentic judge 有 read/bash —— 内联大块材料只会撑爆 prompt。"""
    captured = _capture(monkeypatch)
    monkeypatch.setattr(
        "backend.atif_evidence.materialize_atif",
        lambda dp, aid, agent_name=None: tmp_path / "attempts" / aid / "atif" / "trajectory.json",
    )
    scorer = make_evals_scorer(
        env=_env_with_dir(tmp_path), base_url="http://x", timeout=1.0,
        data_path=tmp_path, job_id="scj_z", method_override="agent_judge_agentic",
    )
    scorer(attempt_id="att1", task={}, env_db=None,
           trace=[{"big": "x" * 5000}], final_state={"done": True},
           events=[{"big": "y" * 5000}])
    evidence = captured[0]["evidence"]
    # 指针齐全
    assert "attempts/att1" in evidence["attempt_dir"]
    assert evidence["env_dir"].endswith("gdpval-like")
    assert evidence["atif_trajectory_path"].endswith("atif/trajectory.json")
    assert "ATIF" in evidence["evidence_guide"]
    # 大块材料不内联
    assert "trace" not in evidence
    assert "events" not in evidence


def test_agentic_evidence_declares_missing_atif(tmp_path, monkeypatch):
    """blade-agent 无转换器 —— 缺口要说明，不能默默少一块。"""
    captured = _capture(monkeypatch)
    monkeypatch.setattr(
        "backend.atif_evidence.materialize_atif",
        lambda dp, aid, agent_name=None: None,
    )
    scorer = make_evals_scorer(
        env=_env_with_dir(tmp_path), base_url="http://x", timeout=1.0,
        data_path=tmp_path, job_id="scj_z", method_override="agent_judge_agentic",
    )
    scorer(attempt_id="att1", task={}, env_db=None, trace=[], final_state={})
    evidence = captured[0]["evidence"]
    assert evidence["atif_trajectory_path"] is None
    assert "格式" in evidence["atif_unavailable"]


def test_non_agentic_evidence_stays_inlined(tmp_path, monkeypatch):
    """非 agentic judge 只能看 prompt —— 材料必须内联。"""
    captured = _capture(monkeypatch)
    scorer = make_evals_scorer(
        env=_env_with_dir(tmp_path), base_url="http://x", timeout=1.0,
        data_path=tmp_path, job_id="scj_z",
    )
    scorer(attempt_id="att1", task={}, env_db=None,
           trace=[{"k": 1}], final_state={}, events=[{"e": 2}])
    evidence = captured[0]["evidence"]
    assert evidence["trace"] == [{"k": 1}]
    assert evidence["events"] == [{"e": 2}]


def test_inline_evidence_truncation_is_visible(tmp_path, monkeypatch):
    """超预算必须显式标注截断 —— 静默少给几条等于让 judge 以为看全了。"""
    captured = _capture(monkeypatch)
    scorer = make_evals_scorer(
        env=_env_with_dir(tmp_path), base_url="http://x", timeout=1.0,
        data_path=tmp_path, job_id="scj_z",
    )
    huge = [{"i": i, "pad": "z" * 2000} for i in range(200)]  # ~400KB
    scorer(attempt_id="att1", task={}, env_db=None,
           trace=huge, final_state={}, events=[])
    trace = captured[0]["evidence"]["trace"]
    assert "_truncated" in trace
    assert len(trace["records"]) < len(huge)
    assert "只给出前" in trace["_truncated"]


def test_env_without_dir_omits_the_key(tmp_path, monkeypatch):
    """没有 env_dir 的 env（测试桩/历史对象）不应凭空造一个路径出来。"""
    captured = _capture(monkeypatch)
    scorer = make_evals_scorer(
        env=MixedEnv, base_url="http://x", timeout=1.0,
        data_path=tmp_path, job_id="scj_w",
    )
    scorer(attempt_id="att1", task={}, env_db=None, trace=[], final_state={})
    assert "env_dir" not in captured[0]["evidence"]


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


def test_judge_visible_paths_are_absolute(tmp_path, monkeypatch):
    """跨进程 judge 的 cwd 是它自己的临时工作区 —— 相对路径它找不到，而且
    不会报错，只会开始满盘 grep 乱找（2026-09-24 实测）。"""
    import os

    from backend.evals_scorer import build_evidence

    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "attempts" / "att1").mkdir(parents=True)

    class RelEnv:
        name = "rel"
        env_dir = Path("envs/rel")
        meta = {"dimensions": [{"name": "d", "weight": 1}]}

    monkeypatch.setattr(
        "backend.atif_evidence.materialize_atif",
        lambda dp, aid, agent_name=None: Path("data/attempts") / aid / "atif" / "trajectory.json",
    )
    evidence = build_evidence(
        data_path=Path("data"), attempt_id="att1", env=RelEnv,
        trace=[], final_state={}, events=[], agentic=True,
    )
    for key in ("attempt_dir", "env_dir", "atif_trajectory_path"):
        assert os.path.isabs(evidence[key]), f"{key} 必须是绝对路径: {evidence[key]}"


def test_attempt_dir_points_at_frozen_snapshot(tmp_path, monkeypatch):
    """被评的是冻结快照，不是实时 attempt 目录 —— 交付物只存在于快照里，
    而且 input_hash/snapshot_ref 整套可重放机制的前提就是评那一份。"""
    from backend.evals_scorer import build_evidence

    monkeypatch.setattr(
        "backend.atif_evidence.materialize_atif",
        lambda dp, aid, agent_name=None: Path(dp) / "attempts" / aid / "atif" / "trajectory.json",
    )
    live = tmp_path / "data"
    frozen = tmp_path / "data" / "scoring-work" / "scj_1"
    evidence = build_evidence(
        data_path=live, attempt_id="att1", env=_env_with_dir(tmp_path),
        trace=[], final_state={}, events=[], agentic=True, input_path=frozen,
    )
    assert evidence["attempt_dir"] == str((frozen / "attempts" / "att1").resolve())
    # ATIF 仍取自实时目录：冻结快照里没有 sandbox_home，各家 CLI 的会话转录
    # 在那儿，轨迹是归因辅助而非被评对象。
    assert str(live.resolve()) in evidence["atif_trajectory_path"]
    assert "scoring-work" not in evidence["atif_trajectory_path"]


def test_question_disambiguates_protocol_from_criterion():
    """env 的 description 常是协议说明 —— 必须指明评的是候选交付物。"""
    class ProtocolEnv:
        name = "gdpval"
        meta = {"dimensions": [{
            "name": "official_rubric_judge", "weight": 100,
            "description": "BladeAgent LLM judge 对官方 59 条 rubric 严格二元评分后归一化为 100 分",
        }]}

    q = _dimensions_from_env(ProtocolEnv)[0]["question"]
    assert "attempt_dir" in q                    # 指明评判对象
    assert "不要去核验这段话本身" in q           # 挡住「核验描述是否属实」的误读
    assert "59 条 rubric" in q                   # 原口径完整保留
