"""证据锚点：JSONL 记录 ID 的取值与摘要计算次数。

`_legacy_id` 原本每处理一行就把整个文件重读一遍做 sha256，复杂度是
O(行数 × 文件大小)。实测一个 11.47MB / 38632 行的 events.jsonl 单独就需要
哈希 443GB，一次研究洞察生成（35 个 attempt）累计 4.73TB，同步阻塞在事件
循环上十分钟仍未跑完。

摘要只取决于文件内容、与行号无关，因此每个文件只需算一次。这里锁住两件事：
取值不能变（历史记录 ID 必须稳定），以及每个文件最多算一次摘要。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from backend.insights.anchors import _jsonl_records, source_record_ids


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
        encoding="utf-8",
    )


def test_legacy_ids_keep_the_documented_file_hash_line_projection(tmp_path: Path) -> None:
    """无稳定 ID 的历史记录，仍是「文件哈希前 20 位 + 零基行号」。"""
    path = tmp_path / "events.jsonl"
    _write_jsonl(path, [{"a": 1}, {"b": 2}, {"c": 3}])

    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:20]
    ids = [record_id for record_id, _rec, _line in _jsonl_records(path)]

    assert ids == [f"legacy:{digest}:{i}" for i in range(3)]


def test_stable_ids_win_over_the_legacy_projection(tmp_path: Path) -> None:
    """record_id / id / canonical_id 三个别名都优先于文件哈希投影。"""
    path = tmp_path / "wire.jsonl"
    _write_jsonl(
        path,
        [
            {"record_id": "r-1"},
            {"id": "r-2"},
            {"canonical_id": "r-3"},
            {"no_stable_key": True},
        ],
    )

    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:20]
    ids = [record_id for record_id, _rec, _line in _jsonl_records(path)]

    assert ids == ["r-1", "r-2", "r-3", f"legacy:{digest}:3"]


def test_digest_is_computed_at_most_once_per_file(tmp_path: Path, monkeypatch) -> None:
    """摘要每个文件最多算一次——这正是 O(行数 × 文件大小) 的防回归点。"""
    path = tmp_path / "events.jsonl"
    _write_jsonl(path, [{"n": i} for i in range(200)])

    calls = 0
    real_sha256 = hashlib.sha256

    def counting_sha256(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_sha256(*args, **kwargs)

    monkeypatch.setattr(hashlib, "sha256", counting_sha256)
    ids = [record_id for record_id, _rec, _line in _jsonl_records(path)]

    assert len(ids) == 200
    assert calls == 1, f"200 行触发了 {calls} 次 sha256，摘要没有被复用"


def test_files_with_only_stable_ids_are_never_hashed(tmp_path: Path, monkeypatch) -> None:
    """整份都带稳定 ID 时（如 canonical wire），一次都不该哈希。"""
    path = tmp_path / "wire.jsonl"
    _write_jsonl(path, [{"record_id": f"r-{i}"} for i in range(50)])

    calls = 0
    real_sha256 = hashlib.sha256

    def counting_sha256(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_sha256(*args, **kwargs)

    monkeypatch.setattr(hashlib, "sha256", counting_sha256)
    ids = [record_id for record_id, _rec, _line in _jsonl_records(path)]

    assert ids == [f"r-{i}" for i in range(50)]
    assert calls == 0, f"全部带稳定 ID 却仍哈希了 {calls} 次"


def test_malformed_and_non_dict_lines_do_not_shift_line_numbers(tmp_path: Path) -> None:
    """跳过的坏行仍占用行号——行号是文件内的零基位置，不是结果序号。"""
    path = tmp_path / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"a": 1}\nnot json\n[1, 2]\n{"b": 2}\n',
        encoding="utf-8",
    )

    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:20]
    records = _jsonl_records(path)

    assert [record_id for record_id, _rec, _line in records] == [
        f"legacy:{digest}:0",
        f"legacy:{digest}:3",
    ]
    assert [line for _id, _rec, line in records] == [0, 3]


def test_source_record_ids_falls_back_across_event_filenames(tmp_path: Path) -> None:
    """events 源有两个候选文件名，缺第一个时回落到 blade_events.jsonl。"""
    data_path = tmp_path / "data"
    attempt_dir = data_path / "attempts" / "att_x"
    _write_jsonl(attempt_dir / "blade_events.jsonl", [{"record_id": "b-1"}])

    assert source_record_ids(data_path, "att_x", "events") == ["b-1"]
    assert source_record_ids(data_path, "att_x", "trace") == []
