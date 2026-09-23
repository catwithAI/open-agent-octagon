"""rejudge（judge 重评）的单元测试。

覆盖:
- _prepare_rejudge:404/409 校验拒绝路径 + 清理旧评分投影 + 入参重建
- commit_scoring_result revision-aware:第二次 commit 追加 outbox revision 2 并更新分数
- _next_score_revision:跳过被占用的 revision（陈旧半提交）
- leader _candidates_through / final_leader_state:同一 attempt 多 revision 只取最新
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace

from backend.db import _init_db_sync, _now_iso
from backend.experiments.leader import _candidates_through, final_leader_state
from backend.experiments.scoring import _next_score_revision, commit_scoring_result
from backend.provenance import record_judge_run
from backend.scoring_queue import (
    RejudgeError,
    _finish_failure,
    _prepare_rejudge,
)


def _db_path() -> Path:
    db = Path(tempfile.mkdtemp()) / "octagon.db"
    _init_db_sync(db)
    return db


def _insert_attempt(
    db: Path,
    attempt_id: str,
    run_id: str | None = None,
    *,
    execution_status: str = "completed",
    scoring_status: str = "not_ready",
) -> None:
    now = _now_iso()
    run_id = run_id or f"run_{attempt_id}"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO tasks(id, env_name, prompt, created_at) VALUES(?,?,?,?)",
            ("t1", "demo", "do it", now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO runs(id, task_id, env_name, status, created_at)"
            " VALUES(?,?,?,?,?)",
            (run_id, "t1", "demo", "completed", now),
        )
        conn.execute(
            "INSERT INTO attempts(id, run_id, task_id, env_name, agent_name, model,"
            " status, env_session_id, env_token_hash, execution_status, scoring_status,"
            " external_refs_json, event_count, thinking_count, tool_call_count,"
            " token_usage_json, duration_ms, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                attempt_id,
                run_id,
                "t1",
                "demo",
                "fake",
                "fake-model",
                "completed" if execution_status == "completed" else "failed",
                f"env_{attempt_id}",
                "token",
                execution_status,
                scoring_status,
                json.dumps({"variant_id": "var1", "repeat_index": 0}),
                0,
                0,
                0,
                "{}",
                0,
                now,
            ),
        )
        conn.commit()


def _attempt_dir(data: Path, attempt_id: str) -> Path:
    root = data / "attempts" / attempt_id
    root.mkdir(parents=True, exist_ok=True)
    (root / "security_meta.json").write_text(
        json.dumps({"agent_name": "fake", "execution_locus": "docker"})
    )
    return root


def _state(db: Path, data: Path, *, envs: dict | None = None) -> SimpleNamespace:
    loaded = {"demo": object()} if envs is None else envs
    return SimpleNamespace(db_path=db, data_path=data, envs=loaded)


# ---------- _prepare_rejudge 校验 ------------------------------------------


def test_prepare_rejudge_rejects_unknown_attempt() -> None:
    db, data = _db_path(), Path(tempfile.mkdtemp())
    try:
        _prepare_rejudge(_state(db, data), "att_missing", "run_x", 2)
        assert False, "should raise"
    except RejudgeError as exc:
        assert (exc.status_code, exc.code) == (404, "attempt_not_found")


def test_prepare_rejudge_rejects_wrong_run() -> None:
    db, data = _db_path(), Path(tempfile.mkdtemp())
    _insert_attempt(db, "att1")
    try:
        _prepare_rejudge(_state(db, data), "att1", "run_other", 2)
        assert False, "should raise"
    except RejudgeError as exc:
        assert (exc.status_code, exc.code) == (404, "attempt_not_found")


def test_prepare_rejudge_rejects_running_execution() -> None:
    db, data = _db_path(), Path(tempfile.mkdtemp())
    _insert_attempt(db, "att1", execution_status="running")
    try:
        _prepare_rejudge(_state(db, data), "att1", "run_att1", 2)
        assert False, "should raise"
    except RejudgeError as exc:
        assert (exc.status_code, exc.code) == (409, "attempt_not_terminal")


def test_prepare_rejudge_rejects_inflight_scoring() -> None:
    db, data = _db_path(), Path(tempfile.mkdtemp())
    _insert_attempt(db, "att1", scoring_status="queued")
    try:
        _prepare_rejudge(_state(db, data), "att1", "run_att1", 2)
        assert False, "should raise"
    except RejudgeError as exc:
        assert (exc.status_code, exc.code) == (409, "scoring_in_flight")


def test_prepare_rejudge_rejects_missing_attempt_dir() -> None:
    db, data = _db_path(), Path(tempfile.mkdtemp())
    _insert_attempt(db, "att1")
    # data_path 目录存在但 attempts/att1 不在盘上
    try:
        _prepare_rejudge(_state(db, data), "att1", "run_att1", 2)
        assert False, "should raise"
    except RejudgeError as exc:
        assert (exc.status_code, exc.code) == (409, "attempt_dir_missing")


def test_prepare_rejudge_rejects_unloaded_env() -> None:
    db, data = _db_path(), Path(tempfile.mkdtemp())
    _insert_attempt(db, "att1")
    _attempt_dir(data, "att1")
    try:
        _prepare_rejudge(_state(db, data, envs={}), "att1", "run_att1", 2)
        assert False, "should raise"
    except RejudgeError as exc:
        assert (exc.status_code, exc.code) == (409, "env_unavailable")


# ---------- _prepare_rejudge 清理 + 重建 -------------------------------------


def test_prepare_rejudge_cleans_old_projections_and_rebuilds_inputs() -> None:
    db, data = _db_path(), Path(tempfile.mkdtemp())
    _insert_attempt(db, "att1", execution_status="timeout")
    _attempt_dir(data, "att1")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO scoring_jobs(id,attempt_id,status,scorer_version,created_at)"
            " VALUES('scj_old','att1','completed','v',?)",
            (_now_iso(),),
        )
        conn.execute(
            "INSERT INTO scores(attempt_id,dimension,value,detail)"
            " VALUES('att1','quality',50,'old')"
        )
        conn.commit()

    prepared = _prepare_rejudge(_state(db, data), "att1", "run_att1", 2)

    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM scoring_jobs WHERE attempt_id='att1'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM scores WHERE attempt_id='att1'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM attempt_judge_runs WHERE attempt_id='att1'"
        ).fetchone()[0] == 0  # append-only 历史绝不动

    assert prepared["attempt_id"] == "att1"
    assert prepared["adapter_status"] == "timeout"
    assert prepared["execution_ended_at"] is None
    assert prepared["stats"]["external_refs"]["variant_id"] == "var1"
    assert prepared["security_meta"]["agent_name"] == "fake"
    assert prepared["capacity"] == 2


# ---------- commit_scoring_result revision-aware ---------------------------


def _minimal_manifest() -> dict:
    return {"schema_version": "test", "scorer": "fake", "checks": []}


def _first_commit(db: Path, data: Path, attempt_id: str, score_total: int) -> str:
    return commit_scoring_result(
        db_path=db,
        data_path=data,
        attempt_id=attempt_id,
        scores=[{"dimension": "quality", "value": score_total, "detail": "x"}],
        manifest=_minimal_manifest(),
        status="completed",
        score_total=score_total,
        ended_at=_now_iso(),
        external_refs={"scoring_input_hash": "h"},
        event_count=0,
        last_event_at=None,
    )


def test_commit_scoring_result_appends_revision_on_rejudge() -> None:
    db, data = _db_path(), Path(tempfile.mkdtemp())
    _insert_attempt(db, "att1")

    first_id = _first_commit(db, data, "att1", 60)
    record_judge_run(db, attempt_id="att1")

    second_id = _first_commit(db, data, "att1", 90)

    assert first_id != second_id
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT score_revision FROM score_transition_outbox WHERE attempt_id='att1'"
            " ORDER BY score_revision"
        ).fetchall()
        assert [r[0] for r in rows] == [1, 2]
        score = conn.execute(
            "SELECT score_total FROM attempts WHERE id='att1'"
        ).fetchone()[0]
        assert score == 90


def test_next_score_revision_skips_occupied_revision() -> None:
    db = _db_path()
    _insert_attempt(db, "att1")
    _insert_attempt(db, "att2")
    with sqlite3.connect(db) as conn:
        # 陈旧半提交：outbox rev1 存在，但 attempt_judge_runs 没有任何行
        # （runner 同步评分路径就是这样：只 commit 不 record_judge_run）。
        conn.execute(
            "INSERT INTO score_transition_outbox(id,attempt_id,score_revision,scope_key,"
            "score,scorer_fingerprint,manifest_ref,seq,created_at,scope_terminal)"
            " VALUES('score_old','att1',1,NULL,60,'fp','m',1,?,0)",
            (_now_iso(),),
        )
        conn.commit()
    with sqlite3.connect(db) as conn:
        assert _next_score_revision(conn, "att1") == 2
        assert _next_score_revision(conn, "att2") == 1


# ---------- leader 最新 revision 去重 ---------------------------------------


def _experiment_scaffold(db: Path, attempt_id: str) -> str:
    now = _now_iso()
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO experiments(id,title,env_name,question,protocol_json,"
            " protocol_hash,schema_version,status,created_at,updated_at)"
            " VALUES('exp1','x','demo','?','{}','h','v1','ready',?,?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO task_variants(id,experiment_id,source_task_id,kind,mutator_id,"
            " mutator_version,seed,params_json,source_hash,content_hash,status,created_at)"
            " VALUES('var1','exp1','t1','m','m','v',1,'{}','h','h','ready',?)",
            (now,),
        )
        conn.execute(
            "INSERT INTO run_groups(id,experiment_id,strategy,status,plan_json,"
            " plan_hash,stop_policy_json,total_cells,created_at)"
            " VALUES('grp1','exp1','test','running','{}','h','{}',1,?)",
            (now,),
        )
        conn.execute(
            "INSERT INTO run_group_cells(id,run_group_id,variant_id,repeat_index,run_id,"
            " status,created_at,updated_at)"
            " VALUES('cell1','grp1','var1',0,'run_att1','completed',?,?)",
            (now, now),
        )
        cur = conn.execute(
            "SELECT external_refs_json FROM attempts WHERE id=?", (attempt_id,)
        )
        refs = json.loads(cur.fetchone()[0])
        scope_key = f"variant:{refs['variant_id']}/repeat:{refs['repeat_index']}"
        conn.commit()
    return scope_key


def _push_outbox(
    db: Path, attempt_id: str, scope_key: str, score: int, revision: int, seq: int
) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO score_transition_outbox(id,attempt_id,score_revision,scope_key,"
            "score,scorer_fingerprint,manifest_ref,seq,created_at,scope_terminal)"
            " VALUES(?,?,?,?,?,?,?,?,?,1)",
            (f"score_{revision}_{attempt_id}", attempt_id, revision, scope_key, score,
             "fp", "m", seq, _now_iso()),
        )
        conn.commit()


def test_candidates_through_keeps_latest_revision_per_attempt() -> None:
    db = _db_path()
    _insert_attempt(db, "att1")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE attempts SET status='completed' WHERE id='att1'"
        )
        conn.commit()
    scope_key = _experiment_scaffold(db, "att1")
    # 同 attempt 两个 revision：先 60 后 90
    _push_outbox(db, "att1", scope_key, 60, 1, 1)
    _push_outbox(db, "att1", scope_key, 90, 2, 2)

    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        rows = _candidates_through(conn, "grp1", scope_key)
        assert len(rows) == 1
        assert rows[0]["score"] == 90


def test_final_leader_state_reflects_latest_revision_even_if_lower() -> None:
    db = _db_path()
    _insert_attempt(db, "att1")
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE attempts SET status='completed' WHERE id='att1'")
        conn.commit()
    scope_key = _experiment_scaffold(db, "att1")
    # 重评把分打低：先 90 后 60 —— leader 必须反映最新 60，而不是取最高 90
    _push_outbox(db, "att1", scope_key, 90, 1, 1)
    _push_outbox(db, "att1", scope_key, 60, 2, 2)

    state = final_leader_state(db, "grp1", scope_key)
    assert state["status"] == "final"
    assert state["leader"]["attempt_id"] == "att1"
    assert state["leader"]["score"] == 60


# ---------- 审查修复（2026-09-22 review）------------------------------------


def test_prepare_rejudge_rejects_cancelled_execution() -> None:
    """审查 #2：cancelled（用户手动 stop）不能重评，否则被 _execution_status
    的 catch-all 转成 failed、中止操作从记录里被抹掉。"""
    db, data = _db_path(), Path(tempfile.mkdtemp())
    attempt_id = "att_cancelled"
    _insert_attempt(db, attempt_id, execution_status="cancelled")
    _attempt_dir(data, attempt_id)
    try:
        _prepare_rejudge(_state(db, data), attempt_id, f"run_{attempt_id}", 2)
        assert False, "should raise"
    except RejudgeError as exc:
        assert exc.status_code == 409 and exc.code == "attempt_not_terminal"


def test_prepare_rejudge_captures_prev_score() -> None:
    """审查 #1：rejudge 前保存原分/状态，失败路径据此恢复。"""
    db, data = _db_path(), Path(tempfile.mkdtemp())
    attempt_id = "att_prev"
    _insert_attempt(db, attempt_id)
    _attempt_dir(data, attempt_id)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE attempts SET score_total=90, status='completed' WHERE id=?",
            (attempt_id,),
        )
        conn.commit()
    prepared = _prepare_rejudge(_state(db, data), attempt_id, f"run_{attempt_id}", 2)
    assert prepared["rejudge_prev"] == {"score_total": 90, "status": "completed"}


def test_finish_failure_restores_prev_score_on_rejudge_failure() -> None:
    """审查 #1：rejudge 失败（judge_unavailable/deadline/…）时 score_total 恢复
    到最后一次成功评分，绝不让 attempt 永久 NULL 而 leaderboard 仍显示旧分。"""
    db = _db_path()
    attempt_id = "att_restore"
    _insert_attempt(db, attempt_id)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE attempts SET score_total=90, status='completed',"
            " scoring_status='completed', execution_status='completed' WHERE id=?",
            (attempt_id,),
        )
        conn.execute(
            "INSERT INTO scoring_jobs(id,attempt_id,status,scorer_version,"
            "scorer_config_json,created_at) VALUES('scj_r',?,'queued','v',?,?)",
            (
                attempt_id,
                json.dumps({"rejudge": {"score_total": 90, "status": "completed"}}),
                _now_iso(),
            ),
        )
        conn.commit()
    _finish_failure(
        db, "scj_r", status="scoring_failed", code="judge_unavailable", message="boom"
    )
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT score_total, scoring_status, status FROM attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()
    assert row[0] == 90  # 原分恢复
    assert row[1] == "scoring_failed"  # scoring 轴如实标失败
    assert row[2] == "completed"  # 顶层保留执行结论


def test_commit_scoring_result_idempotent_by_job_id() -> None:
    """审查 #3：幂等按 scoring job 身份——commit 成功但进程在标记 completed 前
    崩溃，启动恢复重跑同一 job 不再 append 虚假 rejudge。"""
    db, data = _db_path(), Path(tempfile.mkdtemp())
    attempt_id = "att_idem"
    _insert_attempt(db, attempt_id)
    _attempt_dir(data, attempt_id)
    manifest = _minimal_manifest()
    kwargs = dict(
        db_path=db, data_path=data, attempt_id=attempt_id,
        scores=[{"dimension": "d", "value": 90}], manifest=manifest,
        status="completed", score_total=90, ended_at=_now_iso(),
        external_refs={}, event_count=0, last_event_at=None,
    )
    out1 = commit_scoring_result(**kwargs, scoring_job_id="scj_idem")
    out2 = commit_scoring_result(**kwargs, scoring_job_id="scj_idem")
    assert out1 == out2  # 同 job 重跑：返回既有 outbox，不重复写
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT score_revision, scoring_job_id FROM score_transition_outbox "
            "WHERE attempt_id=? ORDER BY score_revision",
            (attempt_id,),
        ).fetchall()
    assert [(r[0], r[1]) for r in rows] == [(1, "scj_idem")]
    # 不同 job（真实 rejudge）→ 新 revision
    out3 = commit_scoring_result(
        **{**kwargs, "score_total": 95}, scoring_job_id="scj_idem2"
    )
    assert out3 != out1
    with sqlite3.connect(db) as conn:
        revs = [r[0] for r in conn.execute(
            "SELECT score_revision FROM score_transition_outbox WHERE attempt_id=? "
            "ORDER BY score_revision",
            (attempt_id,),
        )]
    assert revs == [1, 2]


def test_record_judge_run_aligns_with_outbox_revision() -> None:
    """审查 #4：同步评分路径（outbox 已有评分但 judge_runs 空）首次 rejudge 时
    outbox 是 rev1（同步首评）+ rev2（rejudge commit，_next_score_revision 跳过
    rev1），record_judge_run 应写 rev2 对齐，而不是 MAX(judge_runs)+1=rev1。"""
    db = _db_path()
    attempt_id = "att_sync"
    _insert_attempt(db, attempt_id)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO score_transition_outbox(id,attempt_id,score_revision,"
            "scope_key,score,scorer_fingerprint,seq,created_at) VALUES('o1',?,1,"
            "'s',90,'f',1,?)",
            (attempt_id, _now_iso()),
        )
        conn.execute(
            "INSERT INTO score_transition_outbox(id,attempt_id,score_revision,"
            "scope_key,score,scorer_fingerprint,seq,created_at) VALUES('o2',?,2,"
            "'s',80,'f',2,?)",
            (attempt_id, _now_iso()),
        )
        conn.commit()
    record_judge_run(db, attempt_id=attempt_id)
    with sqlite3.connect(db) as conn:
        rev = conn.execute(
            "SELECT score_revision FROM attempt_judge_runs WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()[0]
    assert rev == 2


def test_prepare_rejudge_updates_rubric_version() -> None:
    """审查 #6：rejudge 用当前 env rubric，attempts.rubric_version 更新到最新，
    否则 record_judge_run 把新分归到旧 rubric 名下。"""
    db, data = _db_path(), Path(tempfile.mkdtemp())
    attempt_id = "att_rubric"
    _insert_attempt(db, attempt_id)
    _attempt_dir(data, attempt_id)
    now = _now_iso()
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO rubric_versions(id, env_name, version, parent_version,"
            " scope, status, rubric_json, rubric_hash, created_at)"
            " VALUES('rv1','demo','v2','v1','product','active','{}','hash',?)",
            (now,),
        )
        conn.execute(
            "INSERT OR IGNORE INTO active_rubrics(env_name, rubric_version, rubric_id,"
            " updated_at) VALUES('demo','v2','rv1',?)",
            (now,),
        )
        conn.commit()
    _prepare_rejudge(_state(db, data), attempt_id, f"run_{attempt_id}", 2)
    with sqlite3.connect(db) as conn:
        rv = conn.execute(
            "SELECT rubric_version FROM attempts WHERE id=?", (attempt_id,)
        ).fetchone()[0]
    assert rv == "v2"
