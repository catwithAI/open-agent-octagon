"""评分快照：硬链接复制、回落记账、只读冻结与哈希稳定性。

背景见 docs/specs/260921-eval-storage-and-artifact-recovery。330 个 attempt 的
快照曾占 32G——因为 `copytree` 把每份产物在磁盘上又存了一遍。改硬链接后内容
不变、哈希不变，但不再占第二份空间。
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from backend.scoring_snapshot import (
    _snapshot_hash,
    create_scoring_snapshot,
    materialize_scoring_input,
    verify_scoring_snapshot,
)


def _build_attempt(data_path: Path, attempt_id: str = "att_test0001") -> Path:
    attempt = data_path / "attempts" / attempt_id
    (attempt / "skill_workspace" / "src").mkdir(parents=True)
    (attempt / "skill_workspace" / "src" / "main.py").write_text("print('hi')\n")
    (attempt / "skill_workspace" / "README.md").write_text("# demo\n")
    (attempt / "events.jsonl").write_text('{"type":"start"}\n')
    (attempt / "trajectory.json").write_text('{"steps":[]}\n')
    return attempt


def test_snapshot_uses_hardlinks_and_records_link_mode(tmp_path: Path) -> None:
    """默认路径下同一文件系统内必须走硬链接，且 manifest 记 link_mode。"""
    data_path = tmp_path / "data"
    attempt = _build_attempt(data_path)

    snapshot = create_scoring_snapshot(data_path=data_path, attempt_id="att_test0001")

    assert snapshot.link_mode == "link"
    source_file = attempt / "skill_workspace" / "src" / "main.py"
    snap_file = data_path / snapshot.relative_path / "attempt" / "skill_workspace" / "src" / "main.py"
    # 共享 inode == 没有占第二份磁盘。这是本次改动的全部收益所在。
    assert os.stat(source_file).st_ino == os.stat(snap_file).st_ino
    assert os.stat(snap_file).st_nlink >= 2


def test_snapshot_hash_identical_between_link_and_copy(tmp_path: Path, monkeypatch) -> None:
    """需求 2.4：硬链接不改变内容，哈希必须与整树复制时一致。"""
    data_path = tmp_path / "data"
    _build_attempt(data_path)
    linked = create_scoring_snapshot(data_path=data_path, attempt_id="att_test0001")

    # 同一份 attempt，强制回落到 copy 模式再建一次。
    copy_data = tmp_path / "data_copy"
    _build_attempt(copy_data)
    real_link = os.link

    def _refuse_link(src, dst, **kwargs):
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(os, "link", _refuse_link)
    copied = create_scoring_snapshot(data_path=copy_data, attempt_id="att_test0001")
    monkeypatch.setattr(os, "link", real_link)

    assert copied.link_mode == "copy"
    assert copied.input_hash == linked.input_hash


def test_link_failure_falls_back_to_copy_without_silence(tmp_path: Path, monkeypatch) -> None:
    """需求 2.2：跨设备时回落复制，且必须在 manifest 里留痕，不得静默。"""
    data_path = tmp_path / "data"
    _build_attempt(data_path)

    def _refuse_link(src, dst, **kwargs):
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(os, "link", _refuse_link)
    snapshot = create_scoring_snapshot(data_path=data_path, attempt_id="att_test0001")

    assert snapshot.link_mode == "copy"
    manifest = json.loads(
        (data_path / snapshot.relative_path / "snapshot.json").read_text()
    )
    assert manifest["link_mode"] == "copy"


def test_unexpected_link_errno_propagates(tmp_path: Path, monkeypatch) -> None:
    """ENOSPC 之类不是环境限制而是真故障，不能被当成回落吞掉。"""
    data_path = tmp_path / "data"
    _build_attempt(data_path)

    def _no_space(src, dst, **kwargs):
        raise OSError(errno.ENOSPC, "no space left on device")

    monkeypatch.setattr(os, "link", _no_space)
    # copytree 把逐文件的错误聚合成 shutil.Error；关键是它**抛出来**了，
    # 而不是被 _link_or_copy 当成环境限制悄悄回落成复制。
    with pytest.raises((OSError, shutil.Error)) as excinfo:
        create_scoring_snapshot(data_path=data_path, attempt_id="att_test0001")
    assert "no space left on device" in str(excinfo.value)


def test_snapshot_stays_read_only(tmp_path: Path) -> None:
    """需求 2.3：_make_read_only 仍然生效，快照内容不可被改写。"""
    data_path = tmp_path / "data"
    create_attempt = _build_attempt(data_path)
    snapshot = create_scoring_snapshot(data_path=data_path, attempt_id="att_test0001")

    snap_file = (
        data_path / snapshot.relative_path / "attempt" / "skill_workspace" / "src" / "main.py"
    )
    assert not os.access(snap_file, os.W_OK)
    with pytest.raises(PermissionError):
        snap_file.write_text("tampered")
    assert create_attempt is not None


def test_materialize_does_not_unfreeze_the_snapshot(tmp_path: Path) -> None:
    """materialize 必须真复制。

    它随后把工作副本 chmod 成可写；若走硬链接，那次 chmod 会写穿到共享
    inode，快照就不再冻结了——需求 2.3 会被悄悄破坏。
    """
    data_path = tmp_path / "data"
    _build_attempt(data_path)
    snapshot = create_scoring_snapshot(data_path=data_path, attempt_id="att_test0001")

    work_root = materialize_scoring_input(
        data_path=data_path,
        snapshot_ref=snapshot.relative_path,
        attempt_id="att_test0001",
        expected_hash=snapshot.input_hash,
        job_id="scj_test",
    )

    work_file = work_root / "attempts" / "att_test0001" / "skill_workspace" / "src" / "main.py"
    snap_file = (
        data_path / snapshot.relative_path / "attempt" / "skill_workspace" / "src" / "main.py"
    )
    assert os.stat(work_file).st_ino != os.stat(snap_file).st_ino

    # 工作副本可改；改完快照仍是原内容、仍只读。
    work_file.write_text("scorer touched this\n")
    assert snap_file.read_text() == "print('hi')\n"
    assert not os.access(snap_file, os.W_OK)

    # 快照哈希仍与注册值一致。
    actual, _ = _snapshot_hash(data_path / snapshot.relative_path / "attempt")
    assert actual == snapshot.input_hash


def test_source_deletion_leaves_snapshot_readable(tmp_path: Path) -> None:
    """归档删 attempt 目录后快照仍可读——这是第 3 块归档能安全执行的前提。"""
    data_path = tmp_path / "data"
    attempt = _build_attempt(data_path)
    snapshot = create_scoring_snapshot(data_path=data_path, attempt_id="att_test0001")

    target = attempt / "skill_workspace" / "src" / "main.py"
    target.unlink()

    snap_file = (
        data_path / snapshot.relative_path / "attempt" / "skill_workspace" / "src" / "main.py"
    )
    assert snap_file.read_text() == "print('hi')\n"
    actual, _ = _snapshot_hash(data_path / snapshot.relative_path / "attempt")
    assert actual == snapshot.input_hash


def test_sandbox_runtime_dirs_are_excluded_from_snapshots(tmp_path: Path) -> None:
    """attempt 根下的沙盒运行时目录不进快照。

    改硬链接后这条从「省一次复制」升级成**归档能否回收空间**的前提：
    快照一旦链住 sandbox_home 的 inode，归档删掉 attempt 侧的目录项也不会
    释放任何字节。
    """
    data_path = tmp_path / "data"
    attempt = _build_attempt(data_path)
    (attempt / "sandbox_home" / "lo").mkdir(parents=True)
    (attempt / "sandbox_home" / "lo" / "rt.bin").write_bytes(b"z" * 100_000)
    (attempt / "sandbox_ro").mkdir()
    (attempt / "sandbox_ro" / "seed.txt").write_text("seed")
    # agent 在 workspace 深处自建的同名目录是**产物**，必须保留。
    (attempt / "skill_workspace" / "sandbox_home").mkdir()
    (attempt / "skill_workspace" / "sandbox_home" / "mine.py").write_text("agent\n")

    snapshot = create_scoring_snapshot(data_path=data_path, attempt_id="att_test0001")
    snap = data_path / snapshot.relative_path / "attempt"

    assert not (snap / "sandbox_home").exists()
    assert not (snap / "sandbox_ro").exists()
    assert (snap / "skill_workspace" / "sandbox_home" / "mine.py").read_text() == "agent\n"


def test_archiving_after_a_snapshot_actually_frees_space(tmp_path: Path) -> None:
    """归档必须真的把字节还给文件系统，而不只是删掉目录项。"""
    import subprocess

    from backend.tools.archive_attempts import apply_archive, plan_archive

    data_path = tmp_path / "data"
    attempt = _build_attempt(data_path)
    (attempt / "sandbox_home" / "lo").mkdir(parents=True)
    (attempt / "sandbox_home" / "lo" / "rt.bin").write_bytes(b"z" * 3_000_000)
    db_path = data_path / "octagon.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE attempts (id TEXT PRIMARY KEY, run_id TEXT, status TEXT,"
            " archived_at TEXT, archived_kinds TEXT)"
        )
        conn.execute(
            "INSERT INTO attempts (id, run_id, status) VALUES"
            " ('att_test0001','run_1','completed')"
        )
        conn.commit()

    create_scoring_snapshot(data_path=data_path, attempt_id="att_test0001")

    def _kb() -> int:
        out = subprocess.run(
            ["du", "-sk", str(data_path)], capture_output=True, text=True
        ).stdout
        return int(out.split()[0])

    before = _kb()
    plans = plan_archive(data_path=data_path, db_path=db_path)
    apply_archive(data_path=data_path, db_path=db_path, plans=plans)
    freed_mb = (before - _kb()) / 1024

    assert freed_mb > 2.0, f"归档只释放了 {freed_mb:.2f} MB —— 快照大概率还链着这些 inode"


def test_file_still_being_written_does_not_corrupt_the_snapshot(tmp_path: Path) -> None:
    """建快照后仍被写入的文件，不得让快照哈希漂移。

    2026-09-21 生产事故：wire capture 在 attempt 结束后异步 finalize，
    `wire-sources/capture-events.jsonl.partial` 在建完快照后继续增长
    （2712 → 3608 字节）。硬链接让这些写入**穿透进快照**，判分前的
    verify 报 scoring_input_mismatch，**7 个 attempt 全部判分失败**。
    """
    data_path = tmp_path / "data"
    attempt = _build_attempt(data_path)
    partial = attempt / "wire-sources" / "capture-events.jsonl.partial"
    partial.parent.mkdir(parents=True)
    partial.write_text('{"e":1}\n')

    snapshot = create_scoring_snapshot(data_path=data_path, attempt_id="att_test0001")

    # 快照建好之后，平台后处理继续往源文件追加。
    with partial.open("a") as stream:
        stream.write('{"e":2}\n{"e":3}\n')

    # 快照必须纹丝不动 —— 这正是 verify_scoring_snapshot 的前提。
    verify_scoring_snapshot(
        data_path=data_path,
        snapshot_ref=snapshot.relative_path,
        attempt_id="att_test0001",
        expected_hash=snapshot.input_hash,
    )
    snap_partial = (
        data_path / snapshot.relative_path / "attempt"
        / "wire-sources" / "capture-events.jsonl.partial"
    )
    assert snap_partial.read_text() == '{"e":1}\n'
    # 不共享 inode = 源怎么写都透不进来
    assert os.stat(snap_partial).st_ino != os.stat(partial).st_ino


def test_normal_files_still_use_hardlinks(tmp_path: Path) -> None:
    """解链只针对仍在变动的文件，普通产物必须仍走硬链接（否则省不下 32G）。"""
    data_path = tmp_path / "data"
    attempt = _build_attempt(data_path)
    snapshot = create_scoring_snapshot(data_path=data_path, attempt_id="att_test0001")

    src = attempt / "skill_workspace" / "src" / "main.py"
    snap = (
        data_path / snapshot.relative_path / "attempt"
        / "skill_workspace" / "src" / "main.py"
    )
    assert os.stat(src).st_ino == os.stat(snap).st_ino
