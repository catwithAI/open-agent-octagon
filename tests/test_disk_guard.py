"""磁盘护栏：低空间时不调度新 attempt，且 stop 仍然可用。

背景见 docs/specs/260921-eval-storage-and-artifact-recovery 需求 7。
2026-09-18 的横评把盘写到 0 字节，之后连 stop 都返回 500。
"""

from __future__ import annotations

import errno
import shutil
from pathlib import Path

import pytest

from backend.disk_guard import (
    DISK_EXHAUSTED_CODE,
    GIB,
    LowDiskSpace,
    check_free_disk,
    ensure_free_disk,
    is_disk_full_error,
)


def _usage(free_gb: float):
    total = int(500 * GIB)
    return shutil._ntuple_diskusage(total, total - int(free_gb * GIB), int(free_gb * GIB))


def test_free_disk_above_threshold_passes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(shutil, "disk_usage", lambda p: _usage(40))
    status = ensure_free_disk(tmp_path, min_free_gb=15)
    assert status.ok
    assert status.free_gb == pytest.approx(40, abs=0.1)


def test_free_disk_below_threshold_blocks(tmp_path: Path, monkeypatch) -> None:
    """需求 7.1：低于阈值不再调度新 attempt。"""
    monkeypatch.setattr(shutil, "disk_usage", lambda p: _usage(3))
    with pytest.raises(LowDiskSpace) as excinfo:
        ensure_free_disk(tmp_path, min_free_gb=15)
    assert "低于阈值" in str(excinfo.value)
    assert excinfo.value.status.free_gb == pytest.approx(3, abs=0.1)


def test_threshold_zero_disables_the_guard(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(shutil, "disk_usage", lambda p: _usage(0))
    assert ensure_free_disk(tmp_path, min_free_gb=0).ok


def test_missing_path_walks_up_to_an_existing_ancestor(tmp_path: Path) -> None:
    """data 目录可能还没建出来，不能因此报错。"""
    status = check_free_disk(tmp_path / "not" / "yet" / "there", min_free_gb=0)
    assert status.total_bytes > 0


def test_unreadable_usage_does_not_block_the_pipeline(tmp_path: Path, monkeypatch) -> None:
    """查不到用量时放行——误判成「盘满」会卡死整条流水线。"""
    def _boom(path):
        raise OSError(errno.EIO, "io error")

    monkeypatch.setattr(shutil, "disk_usage", _boom)
    assert check_free_disk(tmp_path, min_free_gb=15).ok


def test_is_disk_full_error_detects_enospc_through_chains() -> None:
    """盘满按 errno 判，且要能穿透包装层。"""
    assert is_disk_full_error(OSError(errno.ENOSPC, "no space left"))
    assert not is_disk_full_error(OSError(errno.EACCES, "denied"))
    assert not is_disk_full_error(ValueError("unrelated"))

    outer = RuntimeError("write failed")
    outer.__cause__ = OSError(errno.ENOSPC, "no space left")
    assert is_disk_full_error(outer)


def test_is_disk_full_error_survives_reference_cycles() -> None:
    a = RuntimeError("a")
    b = RuntimeError("b")
    a.__cause__ = b
    b.__cause__ = a
    assert is_disk_full_error(a) is False


def test_disk_exhausted_is_infrastructure_not_agent_failure(tmp_path: Path) -> None:
    """需求 7.3：磁盘失败不能混进 agent 的成绩里。

    横评矩阵按 failure_kind 分流：判成 agent 就等于说「这个 agent 做得差」，
    而实际上是评测机没空间了。
    """
    import asyncio
    import sqlite3

    from backend.db import init_db
    from backend.runner import _finalize_no_score

    db_path = tmp_path / "octagon.db"
    asyncio.run(init_db(db_path))
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO tasks (id, env_name, prompt, created_at) VALUES"
            " ('task_1','env','do it','2026-09-21T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO runs (id, task_id, env_name, status, created_at) VALUES"
            " ('run_1','task_1','env','running','2026-09-21T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO attempts (id,run_id,task_id,env_name,status,env_session_id,"
            "env_token_hash,created_at) VALUES"
            " ('att_1','run_1','task_1','env','running','s','h','2026-09-21T00:00:00Z')"
        )
        conn.commit()

    _finalize_no_score(
        db_path=db_path,
        attempt_id="att_1",
        status=DISK_EXHAUSTED_CODE,
        error_code=DISK_EXHAUSTED_CODE,
        error_message="可用磁盘 3.0G 低于阈值 15.0G",
        pass_threshold=60,
    )

    with sqlite3.connect(db_path) as conn:
        kind, code, score = conn.execute(
            "SELECT failure_kind, error_code, score_total FROM attempts WHERE id='att_1'"
        ).fetchone()
    assert kind == "infrastructure"
    assert code == DISK_EXHAUSTED_CODE
    # 未知分数保持 NULL，绝不写业务 0 分。
    assert score is None
