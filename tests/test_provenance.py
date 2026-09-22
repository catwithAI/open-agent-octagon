"""attempt_provenance 数据治理锚的单元测试。

覆盖:
- hash_env_dir:确定性、改动判分文件即变、缺目录返回 None、排除非判分文件
- hash_task:内容字段稳定、不含 created_at 等元数据
- canonical_model:剥 provider 前缀别名 / 合成前缀 / 传输别名
- write_attempt_provenance:幂等、跳过 None、缺锚不报错
- record_attempt_creation:创建阶段写 4 锚
- finalize_attempt_provenance:评分阶段补 judge 锚 + manifest_ref + cli_version
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

from backend.db import _init_db_sync, _now_iso
from backend.model_providers import canonical_model
from backend.provenance import (
    finalize_attempt_provenance,
    hash_env_dir,
    hash_task,
    record_attempt_creation,
    read_judge_anchors,
    record_judge_run,
    write_attempt_provenance,
)


def _db_path() -> Path:
    db = Path(tempfile.mkdtemp()) / "octagon.db"
    _init_db_sync(db)
    return db


def _insert_attempt(db: Path, attempt_id: str, model: str | None = None) -> None:
    now = _now_iso()
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO tasks(id, env_name, prompt, created_at) VALUES(?,?,?,?)",
            ("t1", "demo", "do it", now),
        )
        conn.execute(
            "INSERT INTO runs(id, task_id, env_name, status, model, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (f"run_{attempt_id}", "t1", "demo", "queued", model, now),
        )
        conn.execute(
            "INSERT INTO attempts(id, run_id, task_id, env_name, agent_name, model,"
            " status, env_session_id, env_token_hash, event_count, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,0,?)",
            (
                attempt_id,
                f"run_{attempt_id}",
                "t1",
                "demo",
                "fake",
                model,
                "queued",
                f"env_{attempt_id}",
                "token",
                now,
            ),
        )
        conn.commit()


def _demo_env() -> Path:
    env = Path(tempfile.mkdtemp()) / "demo-env"
    env.mkdir()
    (env / "meta.yaml").write_text("name: demo\npass_threshold: 70\n")
    (env / "scorer.py").write_text("def score(): return 1\n")
    (env / "tasks").mkdir()
    (env / "tasks" / "t1.json").write_text('{"id": "t1"}')
    (env / "private").mkdir()
    (env / "private" / "rubric.json").write_text("{}")
    (env / "README.md").write_text("docs excluded")  # 不应进入契约 hash
    return env


# ---------- hash_env_dir ----------------------------------------------------


def test_hash_env_dir_deterministic_and_sensitive() -> None:
    env = _demo_env()
    h1 = hash_env_dir(env)
    h2 = hash_env_dir(env)
    assert h1 == h2
    assert h1.startswith("sha256:")
    (env / "scorer.py").write_text("def score(): return 2\n")
    assert hash_env_dir(env) != h1


def test_hash_env_dir_excludes_docs_and_missing_dir() -> None:
    env = _demo_env()
    h1 = hash_env_dir(env)
    (env / "README.md").write_text("changed docs")
    assert hash_env_dir(env) == h1  # README 不进契约 hash
    assert hash_env_dir(env / "missing") is None


# ---------- hash_task -------------------------------------------------------


def test_hash_task_stable_and_sensitive() -> None:
    task = {
        "id": "t1",
        "env_name": "demo",
        "prompt": "do it",
        "context": {},
        "constraints": {},
        "timeout_seconds": 600,
    }
    assert hash_task(task) == hash_task(dict(task))
    assert hash_task(task) != hash_task({**task, "prompt": "other"})


# ---------- canonical_model -------------------------------------------------


def test_canonical_model_normalizes_prefixes() -> None:
    providers = {"or-cc": object(), "or-codex": object(), "blade": object()}
    assert canonical_model("or-cc/deepseek/deepseek-v4-flash-0731", providers) == (
        "deepseek/deepseek-v4-flash-0731"
    )
    assert canonical_model("or-codex/deepseek/deepseek-v4-flash-0731", providers) == (
        "deepseek/deepseek-v4-flash-0731"
    )
    assert canonical_model("upstream/gpt-5.6-luna") == "gpt-5.6-luna"
    assert canonical_model("provider-305b3f4b50::deepseek-v4-flash") == (
        "deepseek-v4-flash"
    )
    # 未知前缀不剥:deepseek 可能是双段模型 id 的 org 段
    assert canonical_model("deepseek/deepseek-v4-flash") == "deepseek/deepseek-v4-flash"
    assert canonical_model(None) is None


# ---------- write_attempt_provenance ---------------------------------------


def test_write_is_idempotent_and_skips_none() -> None:
    db = _db_path()
    _insert_attempt(db, "att_a")
    write_attempt_provenance(db, attempt_id="att_a", env_dir_hash="sha256:abc")
    write_attempt_provenance(
        db, attempt_id="att_a", env_dir_hash="sha256:abc", provenance_complete=1
    )
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT env_dir_hash, provenance_complete, agent_cli_version"
            " FROM attempt_provenance WHERE attempt_id='att_a'"
        ).fetchall()
    assert rows == [("sha256:abc", 1, None)]


def test_write_with_no_anchors_is_noop() -> None:
    db = _db_path()
    _insert_attempt(db, "att_b")
    write_attempt_provenance(db, attempt_id="att_b")
    with sqlite3.connect(db) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM attempt_provenance WHERE attempt_id='att_b'"
        ).fetchone()[0]
    assert n == 0


# ---------- record_attempt_creation ----------------------------------------


def test_record_creation_writes_creation_anchors() -> None:
    db = _db_path()
    _insert_attempt(db, "att_c", model="or-cc/deepseek/ds-1")
    env = _demo_env()
    task = {
        "id": "t1",
        "env_name": "demo",
        "prompt": "do it",
        "context": {},
        "constraints": {},
        "timeout_seconds": 600,
    }
    record_attempt_creation(
        db,
        attempt_id="att_c",
        env_name="demo",
        task=task,
        agent_name="fake",
        model="or-cc/deepseek/ds-1",
        input_snapshot_ref="sha256:snap",
        env_dir=env,
    )
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT env_dir_hash, task_content_hash, model_canonical,"
            " input_snapshot_ref, provenance_complete FROM attempt_provenance"
            " WHERE attempt_id='att_c'"
        ).fetchone()
    assert row[0].startswith("sha256:")
    assert row[1] == hash_task(task)
    assert row[2] == "or-cc/deepseek/ds-1"  # providers=None → 不剥 or-cc
    assert row[3] == "sha256:snap"
    assert row[4] == 0  # 创建阶段 complete=0


# ---------- read_judge_anchors / finalize ----------------------------------


def test_read_judge_anchors_from_scoring_work() -> None:
    data_root = Path(tempfile.mkdtemp())
    job = data_root / "scoring-work" / "scj_x" / "attempts" / "att_x" / "private_eval" / "product_judge"
    job.mkdir(parents=True)
    (job / "judge_result.json").write_text(
        json.dumps({"model": "blade/minimax-m3", "prompt_version": "judge_v2"})
    )
    (job / ".." / "process_judge").mkdir(parents=True, exist_ok=True)
    (job.parent / "process_judge" / "judge_result.json").write_text(
        json.dumps({"model": "blade/minimax-m3", "prompt_version": "judge_v1"})
    )
    anchors = read_judge_anchors(data_root, "scj_x")
    assert anchors["judge_model"] == "blade/minimax-m3"
    assert anchors["judge_prompt_version"] == "judge_v1|judge_v2"


def test_finalize_completes_judge_manifest_cli() -> None:
    db = _db_path()
    _insert_attempt(db, "att_x")
    data_root = Path(tempfile.mkdtemp())
    job = data_root / "scoring-work" / "scj_x" / "attempts" / "att_x" / "private_eval" / "product_judge"
    job.mkdir(parents=True)
    (job / "judge_result.json").write_text(
        json.dumps({"model": "blade/minimax-m3", "prompt_version": "judge_v2"})
    )
    now = _now_iso()
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE attempts SET external_refs_json=? WHERE id='att_x'",
            ('{"cli_version": "claude 2.3.4"}',),
        )
        conn.execute(
            "INSERT INTO scores(attempt_id, dimension, value, scored_at,"
            " evaluation_manifest_ref) VALUES('att_x','d',80,?,'evaluation-manifests/h.json')",
            (now,),
        )
        conn.commit()
    finalize_attempt_provenance(db, attempt_id="att_x", data_path=data_root, job_id="scj_x")
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT judge_model, judge_prompt_version, manifest_ref,"
            " agent_cli_version, provenance_complete FROM attempt_provenance"
            " WHERE attempt_id='att_x'"
        ).fetchone()
    assert row == ("blade/minimax-m3", "judge_v2", "evaluation-manifests/h.json", "claude 2.3.4", 1)


def _scored_attempt(db: Path, attempt_id: str, *, score_total: int = 82) -> None:
    """插入一个已评分的 attempt(带 score_total + 两个维度分)。"""
    _insert_attempt(db, attempt_id)
    now = _now_iso()
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE attempts SET score_total=? WHERE id=?",
            (score_total, attempt_id),
        )
        conn.execute(
            "INSERT INTO scores(attempt_id, dimension, value, detail, scored_at,"
            " evaluation_manifest_ref) VALUES(?,?,?,?,?,'evaluation-manifests/h.json')",
            (attempt_id, "content", 80, "c detail", now),
        )
        conn.execute(
            "INSERT INTO scores(attempt_id, dimension, value, detail, scored_at,"
            " evaluation_manifest_ref) VALUES(?,?,?,?,?,'evaluation-manifests/h.json')",
            (attempt_id, "style", 90, "s detail", now),
        )
        conn.commit()


def _judge_result(data_root: Path, job_id: str, attempt_id: str) -> None:
    job = data_root / "scoring-work" / job_id / "attempts" / attempt_id / "private_eval" / "product_judge"
    job.mkdir(parents=True)
    (job / "judge_result.json").write_text(
        json.dumps({"model": "blade/minimax-m3", "prompt_version": "judge_v2"})
    )


def test_finalize_appends_judge_run_row() -> None:
    db = _db_path()
    _scored_attempt(db, "att_x")
    data_root = Path(tempfile.mkdtemp())
    _judge_result(data_root, "scj_x", "att_x")
    finalize_attempt_provenance(db, attempt_id="att_x", data_path=data_root, job_id="scj_x")
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT score_revision, score_total, judge_model, judge_prompt_version,"
            " manifest_ref, scoring_job_id, dimensions_json, status"
            " FROM attempt_judge_runs WHERE attempt_id='att_x'"
        ).fetchone()
    assert row is not None
    assert row[0] == 1
    assert row[1] == 82
    assert row[2] == "blade/minimax-m3"
    assert row[3] == "judge_v2"
    assert row[4] == "evaluation-manifests/h.json"
    assert row[5] == "scj_x"
    dims = json.loads(row[6])
    assert {d["dimension"]: d["value"] for d in dims} == {"content": 80, "style": 90}
    assert {d["detail"] for d in dims} == {"c detail", "s detail"}
    assert row[7] == "completed"


def test_record_judge_run_appends_revisions() -> None:
    db = _db_path()
    _scored_attempt(db, "att_x")
    data_root = Path(tempfile.mkdtemp())
    _judge_result(data_root, "scj_x", "att_x")
    record_judge_run(db, attempt_id="att_x", data_path=data_root, job_id="scj_x")
    record_judge_run(db, attempt_id="att_x", data_path=data_root, job_id="scj_x")
    with sqlite3.connect(db) as conn:
        revs = [
            r[0]
            for r in conn.execute(
                "SELECT score_revision FROM attempt_judge_runs WHERE attempt_id='att_x'"
                " ORDER BY score_revision"
            ).fetchall()
        ]
    assert revs == [1, 2]  # append-only, 不覆盖历史


def test_record_judge_run_rejects_duplicate_revision() -> None:
    db = _db_path()
    _scored_attempt(db, "att_x")
    data_root = Path(tempfile.mkdtemp())
    _judge_result(data_root, "scj_x", "att_x")
    record_judge_run(db, attempt_id="att_x", data_path=data_root, job_id="scj_x")
    # record_judge_run 已写 revision 1 → 再插 revision 1 应触发 UNIQUE 约束
    with sqlite3.connect(db) as conn:
        try:
            conn.execute(
                "INSERT INTO attempt_judge_runs(id, attempt_id, score_revision, score_total,"
                " status, created_at) VALUES('jgr_dup','att_x',1,82,'completed',?)",
                (_now_iso(),),
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("expected UNIQUE(attempt_id, score_revision) violation")

