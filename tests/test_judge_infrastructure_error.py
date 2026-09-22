"""判分设施故障不得被记成 agent 的 0 分。

2026-09-18 横评：109 个 attempt 因 "Blade judge 未配置" 拿到
score=0 / failure_kind=NULL / scoring_status=completed，跨全部 7 个 agent
（opencode 42 / kimi 41 / dsh 41 / codex 41 / cc 35 / BA 35 / mimo 24）。
env scorer 在 judge 起不来时仍返回一行 value=0 的维度，平台照单全收，
于是一次配置事故被写成了能力结论——这正是 octagon 要防的那类误读。
"""

from __future__ import annotations

from backend.evaluator import judge_infrastructure_error


def test_detects_unconfigured_judge() -> None:
    """现场实际写入 scores.detail 的那句话必须被认出来。"""
    scores = [
        {
            "dimension": "official_rubric_judge",
            "value": 0,
            "detail": "Blade judge 未配置；请设置 octagon.yaml 的 llm_judge 或 "
            "LLM_JUDGE_* 环境变量",
        }
    ]
    assert judge_infrastructure_error(scores) is not None


def test_detects_the_other_env_wordings() -> None:
    """各 env 的 judge_local.py 措辞不同，都要认。"""
    for detail in (
        "Blade LLM judge 未配置；请设置 octagon.yaml 的 blade/llm_judge 或 "
        "DOCUMENT_REVIEW_FORMATTING_PRODUCT_JUDGE_* 环境变量",
        "Blade judge 未配置；请设置 octagon.yaml 的 presentbench.judge 或 "
        "PRESENTBENCH_JUDGE_* 环境变量",
        "judge 调用失败: connection refused",
    ):
        assert judge_infrastructure_error([
            {"dimension": "d", "value": 0, "detail": detail}
        ]) is not None, detail


def test_detects_judge_that_returned_an_invalid_verdict() -> None:
    """judge 起来了但没给出合法裁决，同样是设施故障。

    2026-09-21 实测：`Blade judge response parse/validation failed:
    missing rubric item: <uuid>` —— judge 自己没产出有效结果，却被记成
    agent 得 0 分（3 个 attempt，failure_kind 全是 NULL）。
    「judge 崩了」与「agent 做得差」必须分开。
    """
    for detail in (
        "Blade judge response parse/validation failed: missing rubric item: 52dc40eb-af9f",
        "judge response parse error",
        "missing rubric item: abc123",
    ):
        assert judge_infrastructure_error([
            {"dimension": "official_rubric_judge", "value": 0, "detail": detail}
        ]) is not None, detail


def test_real_zero_is_not_an_infrastructure_error() -> None:
    """agent 真的没交付就是真的 0 分，不能被这条逻辑洗白。

    这是本检测最重要的边界：判太松会把真实的差表现藏起来，
    那比原来的问题更糟——原来只是冤枉 agent，这样会替 agent 掩盖。
    """
    for detail in (
        "no source file changed",
        "产物未回收",
        "交付物缺失：未找到 xlsx",
        "rubric judge 给出 0 分：完全没有完成任务要求",
        "",
    ):
        assert judge_infrastructure_error([
            {"dimension": "d", "value": 0, "detail": detail}
        ]) is None, detail


def test_nonzero_score_is_never_infrastructure() -> None:
    """judge 跑起来了并给了分，就不是设施问题——哪怕 detail 里提到 judge。"""
    scores = [
        {"dimension": "d", "value": 72, "detail": "judge 未配置时的兜底说明文本"}
    ]
    assert judge_infrastructure_error(scores) is None


def test_scans_all_dimensions() -> None:
    """多维度时任一维命中即算——judge 通常只喂其中一维。"""
    scores = [
        {"dimension": "format", "value": 80, "detail": "ok"},
        {"dimension": "official_rubric_judge", "value": 0, "detail": "Blade judge 未配置"},
    ]
    assert judge_infrastructure_error(scores) is not None


def test_empty_and_malformed_input() -> None:
    assert judge_infrastructure_error([]) is None
    assert judge_infrastructure_error([{"dimension": "d"}]) is None
    assert judge_infrastructure_error([{"value": "x", "detail": None}]) is None
    # detail 不是字符串也不能炸
    assert judge_infrastructure_error([{"value": 0, "detail": {"a": 1}}]) is None


def test_judge_failure_never_writes_a_business_zero(tmp_path) -> None:
    """落库效果：judge 挂掉的 attempt 分数保持 NULL，不是 0。

    这是整条链路的要害——只要写进去一个 0，它就再也无法与「agent 真的
    得了 0 分」区分，聚合、矩阵、BA 点评全被污染。
    """
    import asyncio
    import sqlite3

    from backend.db import init_db
    from backend.scoring_queue import _finish_failure

    db = tmp_path / "octagon.db"
    asyncio.run(init_db(db))
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO tasks (id,env_name,prompt,created_at) VALUES ('t','e','p','x')"
        )
        conn.execute(
            "INSERT INTO runs (id,task_id,env_name,status,created_at)"
            " VALUES ('r','t','e','running','x')"
        )
        conn.execute(
            "INSERT INTO attempts (id,run_id,task_id,env_name,status,execution_status,"
            "scoring_status,env_session_id,env_token_hash,created_at,score_total)"
            " VALUES ('a','r','t','e','scoring','completed','running','s','h','x',NULL)"
        )
        conn.execute(
            "INSERT INTO scoring_jobs (id,attempt_id,status,scorer_version,"
            "scorer_config_json,input_hash,created_at)"
            " VALUES ('j','a','running','v','{}','h','x')"
        )
        conn.commit()

    _finish_failure(
        db,
        "j",
        status="scoring_failed",
        code="judge_unavailable",
        message="Blade judge 未配置；请设置 octagon.yaml 的 llm_judge",
    )

    with sqlite3.connect(db) as conn:
        status, score, kind, scoring_status, code, execution = conn.execute(
            "SELECT status,score_total,failure_kind,scoring_status,scoring_error_code,"
            "execution_status FROM attempts WHERE id='a'"
        ).fetchone()

    assert score is None, "判分设施故障被写成了业务 0 分"
    assert kind == "scoring"
    assert scoring_status == "scoring_failed"
    assert code == "judge_unavailable"
    # agent 干完了活、产出可评，只是 judge 没跑成——顶层状态保留执行结论。
    assert status == "completed"
    assert execution == "completed"
