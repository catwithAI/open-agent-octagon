"""attempt 归档：dry-run 默认、活跃 attempt 拒绝、状态回写。

背景见 docs/specs/260921-eval-storage-and-artifact-recovery 需求 3。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from backend.tools.archive_attempts import (
    apply_archive,
    main,
    parse_duration,
    plan_archive,
)


def _make_db(db_path: Path, rows: list[tuple[str, str, str]]) -> None:
    """rows: (attempt_id, run_id, status)"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE attempts (id TEXT PRIMARY KEY, run_id TEXT, status TEXT,"
            " archived_at TEXT, archived_kinds TEXT)"
        )
        conn.executemany(
            "INSERT INTO attempts (id, run_id, status) VALUES (?,?,?)", rows
        )
        conn.commit()


def _make_attempt(data_path: Path, attempt_id: str) -> Path:
    attempt = data_path / "attempts" / attempt_id
    # 可重建的运行时：归档要删掉的东西。
    home = attempt / "sandbox_home" / "lo"
    home.mkdir(parents=True)
    (home / "runtime.bin").write_bytes(b"x" * 4096)
    (attempt / "sandbox_ro").mkdir()
    (attempt / "sandbox_ro" / "seed.txt").write_text("seed")
    # 证据：归档必须留下的东西。
    (attempt / "skill_workspace").mkdir()
    (attempt / "skill_workspace" / "main.py").write_text("print(1)\n")
    (attempt / "events.jsonl").write_text('{"t":1}\n')
    (attempt / "trajectory.json").write_text("{}")
    return attempt


def test_parse_duration() -> None:
    assert parse_duration("7d") == 7 * 86400
    assert parse_duration("12h") == 12 * 3600
    assert parse_duration("30m") == 1800
    assert parse_duration("3") == 3 * 86400


def test_plan_lists_reclaimable_dirs_only(tmp_path: Path) -> None:
    data_path = tmp_path / "data"
    _make_attempt(data_path, "att_done")
    db_path = data_path / "octagon.db"
    _make_db(db_path, [("att_done", "run_1", "completed")])

    plans = plan_archive(data_path=data_path, db_path=db_path)

    assert len(plans) == 1
    kinds = {t["kind"] for t in plans[0]["targets"]}
    assert kinds == {"sandbox_home", "sandbox_ro"}
    assert plans[0]["reclaim_bytes"] > 4000


def test_active_attempts_are_refused(tmp_path: Path) -> None:
    """需求 3.3：running/queued/scoring 的目录仍在被写，不得归档。"""
    data_path = tmp_path / "data"
    for attempt_id in ("att_run", "att_queue", "att_score", "att_done"):
        _make_attempt(data_path, attempt_id)
    db_path = data_path / "octagon.db"
    _make_db(
        db_path,
        [
            ("att_run", "run_1", "running"),
            ("att_queue", "run_1", "queued"),
            ("att_score", "run_1", "scoring"),
            ("att_done", "run_1", "completed"),
        ],
    )

    plans = plan_archive(data_path=data_path, db_path=db_path)

    assert [p["attempt_id"] for p in plans] == ["att_done"]


def test_run_id_filter(tmp_path: Path) -> None:
    data_path = tmp_path / "data"
    _make_attempt(data_path, "att_a")
    _make_attempt(data_path, "att_b")
    db_path = data_path / "octagon.db"
    _make_db(db_path, [("att_a", "run_1", "completed"), ("att_b", "run_2", "completed")])

    plans = plan_archive(data_path=data_path, db_path=db_path, run_id="run_2")

    assert [p["attempt_id"] for p in plans] == ["att_b"]


def test_dry_run_deletes_nothing(tmp_path: Path, capsys) -> None:
    """需求 3.2：不给 --yes 就只打印，不能动文件。"""
    data_path = tmp_path / "data"
    attempt = _make_attempt(data_path, "att_done")
    db_path = data_path / "octagon.db"
    _make_db(db_path, [("att_done", "run_1", "completed")])

    rc = main(["--data-path", str(data_path), "--db-path", str(db_path)])

    assert rc == 0
    assert (attempt / "sandbox_home").is_dir()
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "att_done" in out


def test_apply_removes_runtime_keeps_evidence(tmp_path: Path) -> None:
    """需求 3.1：只删可重建目录，证据必须原样保留。"""
    data_path = tmp_path / "data"
    attempt = _make_attempt(data_path, "att_done")
    db_path = data_path / "octagon.db"
    _make_db(db_path, [("att_done", "run_1", "completed")])

    plans = plan_archive(data_path=data_path, db_path=db_path)
    summary = apply_archive(data_path=data_path, db_path=db_path, plans=plans)

    assert summary["archived_attempts"] == 1
    assert summary["failures"] == []
    assert not (attempt / "sandbox_home").exists()
    assert not (attempt / "sandbox_ro").exists()
    # 证据毫发无损
    assert (attempt / "skill_workspace" / "main.py").read_text() == "print(1)\n"
    assert (attempt / "events.jsonl").is_file()
    assert (attempt / "trajectory.json").is_file()


def test_apply_writes_archive_markers(tmp_path: Path) -> None:
    """需求 3.4：元数据要能区分「已归档」与「没采集」。"""
    data_path = tmp_path / "data"
    _make_attempt(data_path, "att_done")
    db_path = data_path / "octagon.db"
    _make_db(db_path, [("att_done", "run_1", "completed")])

    plans = plan_archive(data_path=data_path, db_path=db_path)
    apply_archive(data_path=data_path, db_path=db_path, plans=plans)

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT archived_at, archived_kinds FROM attempts WHERE id='att_done'"
        ).fetchone()
    assert row[0]
    assert json.loads(row[1]) == ["sandbox_home", "sandbox_ro"]


def test_readonly_files_are_still_removable(tmp_path: Path) -> None:
    """快照硬链接把文件置成 0444；归档仍要能删掉运行时目录。"""
    data_path = tmp_path / "data"
    attempt = _make_attempt(data_path, "att_done")
    for path in (attempt / "sandbox_home").rglob("*"):
        if path.is_file():
            path.chmod(0o444)
    (attempt / "sandbox_home" / "lo").chmod(0o555)
    db_path = data_path / "octagon.db"
    _make_db(db_path, [("att_done", "run_1", "completed")])

    plans = plan_archive(data_path=data_path, db_path=db_path)
    summary = apply_archive(data_path=data_path, db_path=db_path, plans=plans)

    assert summary["failures"] == []
    assert not (attempt / "sandbox_home").exists()


def test_attempt_without_db_row_is_left_alone(tmp_path: Path) -> None:
    """DB 里没有的目录可能属于别的部署，宁可不动。"""
    data_path = tmp_path / "data"
    _make_attempt(data_path, "att_orphan")
    db_path = data_path / "octagon.db"
    _make_db(db_path, [])

    plans = plan_archive(data_path=data_path, db_path=db_path)

    assert plans == []


def test_older_than_filter(tmp_path: Path) -> None:
    data_path = tmp_path / "data"
    attempt = _make_attempt(data_path, "att_fresh")
    db_path = data_path / "octagon.db"
    _make_db(db_path, [("att_fresh", "run_1", "completed")])

    # 刚建的目录，要求「早于 7 天」应当筛掉。
    assert plan_archive(data_path=data_path, db_path=db_path, older_than=7 * 86400) == []
    # 门槛放到 0 秒则命中。
    plans = plan_archive(data_path=data_path, db_path=db_path, older_than=0)
    assert [p["attempt_id"] for p in plans] == ["att_fresh"]
    assert attempt.is_dir()
