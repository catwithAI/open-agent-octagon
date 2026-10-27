import asyncio
import sqlite3

import pytest

from .fakes import Gate, FakeBladeRuntime


async def test_controlled_crash_and_call_order(runtime, clock, db_path, tmp_path):
    gate = runtime.gates["create"] = Gate()
    task = asyncio.create_task(runtime.invoke("create", model="test"))
    try:
        await gate.entered.wait()
        clock.advance(600)
        assert runtime.count("create") == 1
        assert runtime.sessions == {}
        gate.proceed.set()
        assert (await task)["session_id"] == "session-1"
        runtime.failures["upload"] = RuntimeError("upload failed")
        with pytest.raises(RuntimeError, match="upload failed"):
            await runtime.invoke("upload", session_id="session-1")
        assert [op for op, _ in runtime.calls] == ["create", "upload"]
        assert FakeBladeRuntime().calls == []
        with sqlite3.connect(db_path) as conn:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def test_external_network_fails_before_contact():
    import socket
    with pytest.raises(AssertionError, match="network"):
        socket.create_connection(("127.0.0.1", 1))
