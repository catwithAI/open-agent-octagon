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
from backend.scoring_queue import RejudgeError, _prepare_rejudge


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
