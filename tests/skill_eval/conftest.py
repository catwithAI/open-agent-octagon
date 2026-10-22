from __future__ import annotations

import socket
import sqlite3
from pathlib import Path

import pytest

from .fakes import Clock, FakeBladeRuntime


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("Skill Eval local tests must not open network connections")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def runtime():
    return FakeBladeRuntime()


@pytest.fixture
def db_path(tmp_path: Path):
    path = tmp_path / "octagon.db"
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
    return path


@pytest.fixture
def skill_files():
    return {
        "SKILL.md": (b"---\nname: sample\nmetadata:\n  version: 0.0.1\n---\nRead inputs.\n", 0o644),
        "scripts/main.py": (b"print('ok')\n", 0o644),
        "scripts/run.sh": (b"#!/bin/sh\necho ok\n", 0o755),
        "assets/data.bin": (b"\x00\xff\x00", 0o644),
    }
