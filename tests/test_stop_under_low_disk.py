"""磁盘写满时 stop 仍须可用（需求 7.2）。

2026-09-18 的横评把盘写到 0 字节后，stop API 返回 500：停止流程里的落盘动作
失败，整个请求被判失败。于是必须先手动腾空间才能停，而空间正被还在跑的
attempt 继续吃掉。取消协程、杀进程这些真正止损的动作不该被一次写盘失败连坐。
"""

from __future__ import annotations

import errno
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient


def _stop_app(monkeypatch, *, converge_raises: bool, scoring_raises: bool) -> TestClient:
    """装一个只挂 stop 路由的 app，把它依赖的四个动作全部替换成可控桩。"""
    import backend.api as api
    import backend.convergence as convergence
    import backend.scoring_queue as scoring_queue

    calls: dict[str, bool] = {}

    def _cancel_tasks(run_ids: list[str]) -> int:
        calls["cancel_tasks"] = True
        return 2

    def _kill_orphans(db_path: Any, run_ids: list[str]) -> int:
        calls["kill_orphans"] = True
        return 1

    def _cancel_scoring(run_ids: list[str]) -> int:
        calls["cancel_scoring"] = True
        if scoring_raises:
            raise OSError(errno.ENOSPC, "no space left on device")
        return 3

    def _cancel_attempts(db_path: Any, run_ids: list[str]) -> int:
        calls["cancel_attempts"] = True
        if converge_raises:
            raise OSError(errno.ENOSPC, "no space left on device")
        return 4

    monkeypatch.setattr(convergence, "cancel_dispatch_tasks", _cancel_tasks)
    monkeypatch.setattr(convergence, "kill_orphaned_agent_processes", _kill_orphans)
    monkeypatch.setattr(convergence, "cancel_attempts_for_runs", _cancel_attempts)
    monkeypatch.setattr(scoring_queue, "cancel_scoring_for_runs", _cancel_scoring)

    class _State:
        db_path = "/nonexistent/octagon.db"

    monkeypatch.setattr(api.runtime_state, "get", lambda: _State())

    # 只取 stop 这一条路由，避免把整个 app 的启动依赖拖进单测。
    full = api.build_router()
    router = APIRouter()
    for route in full.routes:
        if getattr(route, "path", "").endswith("/stop") and "runs" in route.path:
            router.routes.append(route)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    client.calls = calls  # type: ignore[attr-defined]
    return client


def test_stop_succeeds_when_everything_works(monkeypatch) -> None:
    client = _stop_app(monkeypatch, converge_raises=False, scoring_raises=False)
    response = client.post("/runs/run_1/stop")
    assert response.status_code == 200
    body = response.json()
    assert body["stopped"] == 2
    assert body["cancelled_scoring_jobs"] == 3
    assert body["converged_attempts"] == 4
    assert "convergence_persist_error" not in body


def test_stop_returns_200_when_convergence_cannot_persist(monkeypatch) -> None:
    """状态没写成不改变「已经停了」这个事实。"""
    client = _stop_app(monkeypatch, converge_raises=True, scoring_raises=False)
    response = client.post("/runs/run_1/stop")

    assert response.status_code == 200
    body = response.json()
    # 止损动作照常完成
    assert body["stopped"] == 2
    assert body["killed_orphan_processes"] == 1
    # 落盘失败如实上报，不假装收敛成功
    assert body["converged_attempts"] == 0
    assert "no space left on device" in body["convergence_persist_error"]


def test_stop_kills_processes_even_if_scoring_cancel_cannot_persist(monkeypatch) -> None:
    """评分取消要落盘，它失败时后面的杀进程不能被跳过。"""
    client = _stop_app(monkeypatch, converge_raises=False, scoring_raises=True)
    response = client.post("/runs/run_1/stop")

    assert response.status_code == 200
    body = response.json()
    assert body["stopped"] == 2
    assert body["killed_orphan_processes"] == 1
    assert body["cancelled_scoring_jobs"] == 0
    assert "no space left on device" in body["scoring_cancel_persist_error"]


def test_stop_survives_a_completely_full_disk(monkeypatch) -> None:
    """两条落盘路径同时失败——盘真的满了的样子。"""
    client = _stop_app(monkeypatch, converge_raises=True, scoring_raises=True)
    response = client.post("/runs/run_1/stop")

    assert response.status_code == 200
    body = response.json()
    assert body["stopped"] == 2
    assert body["killed_orphan_processes"] == 1
    assert "convergence_persist_error" in body
    assert "scoring_cancel_persist_error" in body
    # 真正止损的两个动作都执行到了
    assert client.calls["cancel_tasks"] is True  # type: ignore[attr-defined]
    assert client.calls["kill_orphans"] is True  # type: ignore[attr-defined]
