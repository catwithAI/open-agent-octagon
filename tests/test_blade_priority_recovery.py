"""blade 优先回收：下载要校验内容真的变了，记账要能区分两种低分。

背景见 docs/specs/260921-eval-storage-and-artifact-recovery 需求 5/6。
「download_file 没抛异常」曾被当作回收成功，但实测落地文件仍是基线内容——
远端前缀猜错时请求到的是另一个位置的同名文件。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.adapters.blade_service import (
    BladeServiceAdapter,
    _candidate_remote_paths,
    _digest_bytes,
    _digest_of,
)


class _FakeClient:
    """按路径返回内容；未登记的路径抛异常（远端 404）。"""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.requested: list[str] = []

    async def download_file(self, session_id: str, path: str) -> bytes:
        self.requested.append(path)
        if path not in self.files:
            raise RuntimeError(f"404 {path}")
        return self.files[path]


def _adapter() -> BladeServiceAdapter:
    return BladeServiceAdapter.__new__(BladeServiceAdapter)


def _run(coro):
    return asyncio.run(coro)


def test_changed_content_counts_as_downloaded(tmp_path: Path) -> None:
    baseline = b"original\n"
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_bytes(baseline)
    client = _FakeClient({"src/main.py": b"agent edited this\n"})
    errors: list[str] = []

    got, missing = _run(
        _adapter()._download_priority_paths(
            client, "sess", ["src/main.py"], download_root=tmp_path,
            baseline_root=tmp_path, errors=errors
        )
    )

    assert got == ["src/main.py"]
    assert missing == []
    assert (tmp_path / "src" / "main.py").read_bytes() == b"agent edited this\n"
    assert errors == []


def test_baseline_identical_content_is_not_a_hit(tmp_path: Path) -> None:
    """需求 5.2/5.3：内容与基线一致 = 没真正拿到，不得记进 priority_downloaded。"""
    baseline = b"original\n"
    (tmp_path / "main.py").write_bytes(baseline)
    # 所有候选前缀都返回基线内容——正是横评里观察到的情形。
    client = _FakeClient(dict.fromkeys(_candidate_remote_paths("main.py"), baseline))
    errors: list[str] = []

    got, missing = _run(
        _adapter()._download_priority_paths(
            client, "sess", ["main.py"], download_root=tmp_path,
            baseline_root=tmp_path, errors=errors
        )
    )

    assert got == []
    assert missing == ["main.py"]
    # 试过了每一个候选前缀才放弃
    assert len(client.requested) == len(_candidate_remote_paths("main.py"))


def test_falls_through_to_a_later_candidate_prefix(tmp_path: Path) -> None:
    """前缀猜错时要接着试下一个候选，而不是就此判定未命中。"""
    (tmp_path / "out.xlsx").write_bytes(b"seed")
    client = _FakeClient(
        {
            "out.xlsx": b"seed",  # 同名但没变——不算命中
            "workspace/out.xlsx": b"real deliverable",
        }
    )
    errors: list[str] = []

    got, missing = _run(
        _adapter()._download_priority_paths(
            client, "sess", ["out.xlsx"], download_root=tmp_path,
            baseline_root=tmp_path, errors=errors
        )
    )

    assert got == ["out.xlsx"]
    assert missing == []
    assert (tmp_path / "out.xlsx").read_bytes() == b"real deliverable"


def test_new_file_always_counts_as_a_hit(tmp_path: Path) -> None:
    """本地没有基线时，拿到任何内容都是新的。"""
    client = _FakeClient({"report.md": b"# result\n"})
    errors: list[str] = []

    got, missing = _run(
        _adapter()._download_priority_paths(
            client, "sess", ["report.md"], download_root=tmp_path,
            baseline_root=tmp_path, errors=errors
        )
    )

    assert got == ["report.md"]
    assert (tmp_path / "report.md").read_text() == "# result\n"


def test_probe_misses_do_not_pollute_errors(tmp_path: Path) -> None:
    """候选前缀猜错会 404，那是预期内的试探。

    errors 非空会让迭代评审路径把 attempt 判成 chat_failed——把试探噪声记
    进去，等于用一个更隐蔽的方式重演「基础设施问题冒充 agent 失败」。
    """
    client = _FakeClient({})
    errors: list[str] = []

    got, missing = _run(
        _adapter()._download_priority_paths(
            client, "sess", ["nowhere.py"], download_root=tmp_path,
            baseline_root=tmp_path, errors=errors
        )
    )

    assert got == []
    assert missing == ["nowhere.py"]
    assert errors == []


def test_candidate_prefixes_cover_the_known_layouts() -> None:
    candidates = _candidate_remote_paths("src/main.py")
    assert candidates[0] == "src/main.py"
    assert "workspace/src/main.py" in candidates
    assert "main.py" in candidates  # 直接写在工作区根上的情形
    # 不重复
    assert len(candidates) == len(set(candidates))


def test_digest_helpers(tmp_path: Path) -> None:
    assert _digest_of(tmp_path / "missing.txt") is None
    target = tmp_path / "a.txt"
    target.write_bytes(b"hello")
    assert _digest_of(target) == _digest_bytes(b"hello")


def test_recovery_failure_is_flagged_as_infrastructure() -> None:
    """需求 6.3：优先路径全空 = 分数衡量的是空工作区，不能算 agent 的锅。"""
    from backend.adapters.blade_service import artifact_recovery_failed

    # 有依据、全部落空 —— 正是 2026-09-18 横评里 BA 的形状。
    assert artifact_recovery_failed(
        {"artifact_sync": {"priority_missing": ["a.py"], "priority_downloaded": []}}
    )
    # 至少拿到一个产物：回收链路是通的，低分是真实的。
    assert not artifact_recovery_failed(
        {"artifact_sync": {"priority_missing": ["a.py"], "priority_downloaded": ["b.py"]}}
    )
    # 压根没提取到路径是另一种情况（需求 4.4），不在这里判。
    assert not artifact_recovery_failed(
        {"artifact_sync": {"priority_missing": [], "priority_source": "none"}}
    )
    assert not artifact_recovery_failed({})
    assert not artifact_recovery_failed({"artifact_sync": {"error": "boom"}})


def test_baseline_is_read_from_the_live_workspace_not_staging(tmp_path: Path) -> None:
    """续聊/恢复走 staging 目录，基线必须取自活的 workspace。

    取 download_root 的话，staging 是新建的空目录、摘要恒为 None，
    「内容没变就算未命中」这条校验会完全失效——而那恰恰是它要守的路径。
    """
    workspace = tmp_path / "skill_workspace"
    workspace.mkdir()
    (workspace / "main.py").write_bytes(b"original\n")
    staging = tmp_path / ".skill_workspace.sync-x"
    staging.mkdir()

    # 远端所有候选都只返回基线内容 —— 没有真正的产物。
    client = _FakeClient(dict.fromkeys(_candidate_remote_paths("main.py"), b"original\n"))
    errors: list[str] = []

    got, missing = _run(
        _adapter()._download_priority_paths(
            client, "sess", ["main.py"],
            download_root=staging, baseline_root=workspace, errors=errors,
        )
    )

    assert got == []
    assert missing == ["main.py"]


def test_dot_prefixed_paths_get_usable_candidates() -> None:
    """`lstrip('./')` 会把 `.hidden/x.md` 削成 `hidden/x.md`，候选全指错位置。"""
    candidates = _candidate_remote_paths(".hidden/report.md")
    assert ".hidden/report.md" in candidates
    assert not any(c.startswith("hidden/") for c in candidates)
    # `./` 前缀仍应被正常剥掉
    assert _candidate_remote_paths("./a/b.py")[0] == "a/b.py"
