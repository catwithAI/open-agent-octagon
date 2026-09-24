"""run 级比较式评分：候选集、流程式 API 编排、回写与记账。

核心不变式：
- 结果的可比性依赖候选集与转换算法 —— ``candidate_ids`` / ``conversion`` /
  ``conversion_version`` 必须随结果一起落账，否则两个分数能不能放一起看就
  无从判断；
- 候选 < 2 记 ``skipped`` 而不是 failed（这个 run 不具备比较条件，不是出错）；
- 同一维度重评是**替换**而非并存；
- 单维失败不打断其余维度。
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import httpx
import pytest

from backend.comparison_scorer import (
    ComparisonError,
    list_candidates,
    list_comparison_jobs,
    run_comparisons,
)
from backend.db import _init_db_sync, _now_iso


class FakeEnv:
    name = "demo-env"
    meta = {"schema_version": "1.0"}


POINTWISE = {"id": "official_rubric_judge", "method": "agent_judge_agentic",
             "weight": 100, "role": "scored", "question": "q"}

# 送给 start_run 的是**完整 plan**：evals 的 validate_plan 要求至少有一个
# role=scored 且权重 > 0 的维度，而比较维度恒为 diagnostic/weight=0。
DIMENSIONS = [
    POINTWISE,
    {"id": "solution_elegance", "method": "pairwise_judge_agentic",
     "weight": 0, "role": "diagnostic", "question": "哪个更贴合既有抽象",
     "comparison": {"strategy": "round_robin", "conversion": "win_count",
                    "conversion_version": "1"}},
]


def _db(attempts: list[tuple[str, str]]) -> Path:
    """attempts: [(attempt_id, status)]，全部挂在 run1 下。"""
    db = Path(tempfile.mkdtemp()) / "octagon.db"
    _init_db_sync(db)
    now = _now_iso()
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO tasks(id, env_name, prompt, created_at) VALUES(?,?,?,?)",
            ("t1", "demo-env", "do it", now),
        )
        conn.execute(
            "INSERT INTO runs(id, task_id, env_name, status, created_at)"
            " VALUES(?,?,?,?,?)",
            ("run1", "t1", "demo-env", "completed", now),
        )
        for attempt_id, status in attempts:
            conn.execute(
                "INSERT INTO attempts(id, run_id, task_id, env_name, agent_name,"
                " status, env_session_id, env_token_hash, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (attempt_id, "run1", "t1", "demo-env", f"agent-{attempt_id}",
                 status, f"env_{attempt_id}", "token", now),
            )
        conn.commit()
    return db


def _patch(monkeypatch, compare_handler):
    """桩掉 httpx.post：start_run 恒成功，compare 交给 handler。"""
    calls: list = []

    def fake_post(url, *, json=None, timeout=None):
        calls.append((url, json))
        if url.endswith("/runs"):
            return httpx.Response(
                200, json={"experiment_id": "run1", "run_id": json["run_id"],
                           "plan_hash": "ph_abc", "task_ids": ["t"]},
                request=httpx.Request("POST", url),
            )
        return compare_handler(url, json)

    monkeypatch.setattr("backend.comparison_scorer.httpx.post", fake_post)
    return calls


def _compare_ok(url, payload):
    return httpx.Response(
        200,
        json={"experiment_id": "run1", "dimension_id": payload["dimension_id"],
              "scores": {"att_a": {"value": 1.0, "source": "pairwise_judge_agentic"},
                         "att_b": {"value": 0.0, "source": "pairwise_judge_agentic"}}},
        request=httpx.Request("POST", url),
    )


def _run(db, monkeypatch, handler=_compare_ok, dimensions=None):
    calls = _patch(monkeypatch, handler)
    out = run_comparisons(
        db_path=db, data_path=db.parent, run_id="run1", env=FakeEnv,
        task={"id": "t1"}, dimensions=dimensions if dimensions is not None else DIMENSIONS,
        base_url="http://127.0.0.1:8000/", timeout=60.0,
    )
    return out, calls


# ---------- 候选集 ----------


def test_only_completed_attempts_are_candidates():
    db = _db([("att_a", "completed"), ("att_b", "completed"), ("att_c", "timeout")])
    assert [c["id"] for c in list_candidates(db, "run1")] == ["att_a", "att_b"]


def test_insufficient_candidates_is_skipped_not_failed(monkeypatch):
    db = _db([("att_a", "completed"), ("att_b", "gave_up")])
    out, calls = _run(db, monkeypatch)
    assert out["reason"] == "insufficient_candidates"
    assert calls == []  # 没发任何请求
    job = list_comparison_jobs(db, "run1")[0]
    assert job["status"] == "skipped"
    assert job["error_code"] == "insufficient_candidates"
    assert job["candidate_ids"] == ["att_a"]


def test_no_comparison_dimensions_short_circuits(monkeypatch):
    db = _db([("att_a", "completed"), ("att_b", "completed")])
    out, calls = _run(db, monkeypatch, dimensions=[])
    assert out["reason"] == "no_comparison_dimensions"
    assert calls == []


# ---------- 编排 ----------


def test_registers_every_candidate_with_identical_plan(monkeypatch):
    db = _db([("att_a", "completed"), ("att_b", "completed")])
    _, calls = _run(db, monkeypatch)
    starts = [c for c in calls if c[0].endswith("/runs")]
    assert len(starts) == 2
    # 所有候选必须送完全相同的 dimensions —— 不一致 evals 会 409
    assert starts[0][1]["dimensions"] == starts[1][1]["dimensions"] == DIMENSIONS
    assert {s[1]["run_id"] for s in starts} == {"att_a", "att_b"}
    # EvaluationInput 要求 artifact 两个字段非空
    assert starts[0][1]["artifact"]["snapshot_ref"]
    assert starts[0][1]["artifact"]["content_hash"]


def test_compare_gets_evidence_per_candidate(monkeypatch):
    db = _db([("att_a", "completed"), ("att_b", "completed")])
    _, calls = _run(db, monkeypatch)
    compare = next(c for c in calls if c[0].endswith("/compare"))
    assert compare[0] == "http://127.0.0.1:8000/experiments/run1/compare"
    assert set(compare[1]["evidence"]) == {"att_a", "att_b"}
    assert "attempts/att_a" in compare[1]["evidence"]["att_a"]["attempt_dir"]


# ---------- 回写与记账 ----------


def test_writes_scores_and_records_comparability_facts(monkeypatch):
    db = _db([("att_a", "completed"), ("att_b", "completed")])
    out, _ = _run(db, monkeypatch)
    assert out["results"][0]["status"] == "completed"
    assert out["results"][0]["values"] == {"att_a": 100, "att_b": 0}

    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT attempt_id, value, detail FROM scores "
            "WHERE dimension='solution_elegance' ORDER BY attempt_id"
        ).fetchall()
    assert [(r[0], r[1]) for r in rows] == [("att_a", 100), ("att_b", 0)]
    assert "conversion=win_count" in rows[0][2]
    assert "candidates=2" in rows[0][2]

    job = list_comparison_jobs(db, "run1")[0]
    assert job["status"] == "completed"
    assert job["candidate_ids"] == ["att_a", "att_b"]
    assert job["conversion"] == "win_count"
    assert job["conversion_version"] == "1"
    assert job["plan_hash"] == "ph_abc"
    assert job["result"] == {"att_a": 100, "att_b": 0}


def test_rerun_replaces_scores_instead_of_duplicating(monkeypatch):
    db = _db([("att_a", "completed"), ("att_b", "completed")])
    _run(db, monkeypatch)

    def flipped(url, payload):
        return httpx.Response(
            200, json={"scores": {"att_a": {"value": 0.0}, "att_b": {"value": 1.0}}},
            request=httpx.Request("POST", url),
        )

    _run(db, monkeypatch, flipped)
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT attempt_id, value FROM scores WHERE dimension='solution_elegance'"
            " ORDER BY attempt_id"
        ).fetchall()
    assert rows == [("att_a", 0), ("att_b", 100)]  # 替换，不是四行
    assert len(list_comparison_jobs(db, "run1")) == 1  # UNIQUE 约束生效


def test_unsampled_candidate_gets_no_score_row(monkeypatch):
    """convert_* 对没被采样到的候选返回 None —— 无分就不写行，不写 0。"""
    db = _db([("att_a", "completed"), ("att_b", "completed")])

    def partial(url, payload):
        return httpx.Response(
            200, json={"scores": {"att_a": {"value": 1.0}, "att_b": {"value": None}}},
            request=httpx.Request("POST", url),
        )

    _run(db, monkeypatch, partial)
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT attempt_id FROM scores WHERE dimension='solution_elegance'"
        ).fetchall()
    assert [r[0] for r in rows] == ["att_a"]


def test_one_dimension_failure_does_not_stop_others(monkeypatch):
    db = _db([("att_a", "completed"), ("att_b", "completed")])
    dims = DIMENSIONS + [
        {"id": "readability", "method": "listwise_judge", "weight": 0,
         "role": "diagnostic",
         "comparison": {"conversion": "rank_interpolation", "conversion_version": "1"}},
    ]

    def handler(url, payload):
        if payload["dimension_id"] == "solution_elegance":
            raise httpx.ConnectError("refused")
        return _compare_ok(url, payload)

    out, _ = _run(db, monkeypatch, handler, dimensions=dims)
    statuses = {r["dimension"]: r["status"] for r in out["results"]}
    assert statuses == {"solution_elegance": "failed", "readability": "completed"}
    jobs = {j["dimension"]: j for j in list_comparison_jobs(db, "run1")}
    assert jobs["solution_elegance"]["error_code"] == "compare_failed"
    assert jobs["readability"]["conversion"] == "rank_interpolation"


def test_start_run_failure_records_all_dimensions_failed(monkeypatch):
    db = _db([("att_a", "completed"), ("att_b", "completed")])

    def fake_post(url, *, json=None, timeout=None):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr("backend.comparison_scorer.httpx.post", fake_post)
    with pytest.raises(ComparisonError):
        run_comparisons(
            db_path=db, data_path=db.parent, run_id="run1", env=FakeEnv,
            task={}, dimensions=DIMENSIONS,
            base_url="http://x", timeout=1.0,
        )
    job = list_comparison_jobs(db, "run1")[0]
    assert job["status"] == "failed"
    assert job["error_code"] == "start_run_failed"


def test_plan_carries_scored_dimensions_not_just_comparison(monkeypatch):
    """evals 的 validate_plan 要求至少一个 role=scored 且权重 > 0 的维度。
    只送比较维度（恒为 diagnostic/weight=0）会被 500 PlanError 拒掉。"""
    db = _db([("att_a", "completed"), ("att_b", "completed")])
    _, calls = _run(db, monkeypatch)
    sent = next(c for c in calls if c[0].endswith("/runs"))[1]["dimensions"]
    assert any(d.get("role") == "scored" and d.get("weight", 0) > 0 for d in sent)
    # 但只有比较维度会去调 /compare
    compares = [c for c in calls if c[0].endswith("/compare")]
    assert {c[1]["dimension_id"] for c in compares} == {"solution_elegance"}


def test_pointwise_only_plan_short_circuits(monkeypatch):
    """plan 里没有比较维度 —— 不发任何请求。"""
    db = _db([("att_a", "completed"), ("att_b", "completed")])
    out, calls = _run(db, monkeypatch, dimensions=[POINTWISE])
    assert out["reason"] == "no_comparison_dimensions"
    assert calls == []
