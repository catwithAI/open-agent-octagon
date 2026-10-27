"""Exercise the pinned SDK at HTTP/event boundaries used by Octagon."""

import json
from pathlib import Path

import httpx
import pytest
import respx
from blade_agent_kit import RawEvent

from backend.adapters.blade_service import BladeAdapterConfig, BladeServiceAdapter
from backend.wire.normalizers.blade import BladeNormalizer


@pytest.mark.asyncio
async def test_sdk_client_uploads_skill_and_reads_correlated_history(tmp_path: Path) -> None:
    config = BladeAdapterConfig(
        base_url="https://blade.test", skills_path=tmp_path, api_key="test-token",
        reconnect_timeout_seconds=7,
    )
    files = [
        {"path": "SKILL.md", "content": "---\nname: local/demo\n---\nWrite a report."},
        {"path": "assets/input.bin", "content": "AP8=", "encoding": "base64"},
    ]
    with respx.mock as router:
        create = router.post("https://blade.test/api/sessions").mock(
            return_value=httpx.Response(200, json={"session_id": "s1"}),
        )
        router.get("https://blade.test/api/sessions/s1").mock(
            return_value=httpx.Response(200, json={"id": "s1"}),
        )
        upload = router.post("https://blade.test/api/sessions/s1/skills:upload").mock(
            return_value=httpx.Response(200, json={
                "name": "local/demo", "skill_dir": ".agents/skills/local/demo", "file_count": 2,
            }),
        )
        history = router.get("https://blade.test/api/sessions/s1/history").mock(
            return_value=httpx.Response(200, json={"nodes": [
                {"id": "tool-1", "role": "tool", "tool_call_id": "call-1", "content": "done"},
            ]}),
        )
        async with BladeServiceAdapter(config)._new_client() as client:
            session = await client.create_session(intent="Write a report", model="test-model")
            uploaded = await client.upload_session_skill(session.id, name="local/demo", files=files)
            result = await client.get_history(session.id)

    assert json.loads(create.calls[0].request.content)["model"] == "test-model"
    assert json.loads(upload.calls[0].request.content) == {"name": "local/demo", "files": files}
    assert upload.calls[0].request.headers["Authorization"] == "Bearer test-token"
    assert uploaded.file_count == 2
    assert history.called
    assert result.nodes[0]["tool_call_id"] == "call-1"


def test_pinned_sdk_raw_event_round_trips_into_normalizer(tmp_path: Path) -> None:
    wire = {
        "type": "llm:response:done", "stream_sequence": 5,
        "payload": {
            "loop_name": "root", "content": "Read the input first",
            "tool_calls": [{"id": "call-1", "function": {"name": "read_file", "arguments": "{}"}}],
        },
    }
    event = RawEvent.from_wire("session-1", wire)
    assert event is not None
    row = {"source": "socket", "timestamp": "2026-10-21T10:00:00Z", "raw": event.raw}
    (tmp_path / "events.jsonl").write_text(json.dumps(row) + "\n")
    result = BladeNormalizer().normalize(attempt_id="attempt-1", attempt_dir=tmp_path)
    assert result.parse_errors == 0
    assert any(s.get("tool_call_id") == "call-1" for s in result.trajectory["steps"])
