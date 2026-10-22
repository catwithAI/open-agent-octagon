"""Use the installed SDK's request and event codecs, not FakeBladeRuntime."""
import json
from importlib.metadata import version

import httpx
import pytest
import respx
from blade_agent_kit import BladeAgentClient, RawEvent
from blade_agent_kit.events import ChatEnd


class SocketBoundary:
    def __init__(self):
        self.sent = []

    async def chat(self, session_id, payload):
        self.sent.append((session_id, payload))
        for raw in [
            {"type": "tool:call", "tool_call_id": "ask-1", "payload": {"name": "AskUserQuestion", "prompt_id": "p1", "question": "Continue?"}},
            {"type": "chat:end", "chat_run_id": "chat-1", "status": "completed"},
        ]:
            event = ChatEnd.from_payload(raw) if raw["type"] == "chat:end" else RawEvent.from_wire(session_id, raw)
            if event is not None:
                yield event

    def subscribe(self, session_id):
        return ("subscription", session_id)

    async def stop(self, session_id, *, chat_run_id=None):
        self.sent.append((session_id, {"stop": chat_run_id}))
        return {"accepted": True}

    async def close(self):
        pass


async def test_real_sdk_http_and_socket_codecs(tmp_path):
    assert version("blade-agent-kit") in {"2612.0.0b41", "2612.0.0-beta.41"}
    binary = tmp_path / "input.bin"
    binary.write_bytes(b"\x00\xff")
    with respx.mock as router:
        create = router.post("https://blade.test/api/sessions").respond(200, json={"session_id": "s1"})
        router.get("https://blade.test/api/sessions/s1").respond(200, json={"id": "s1"})
        upload = router.post("https://blade.test/api/sessions/s1/skills:upload").respond(200, json={"name": "eval/sample", "file_count": 2})
        attachment = router.post("https://blade.test/api/sessions/s1/upload/%2E").respond(200, json={"files": []})
        router.get("https://blade.test/api/sessions/s1/history").respond(200, json={"nodes": [{"id": "n1", "tool_call_id": "ask-1", "role": "tool", "content": "yes"}]})
        async with BladeAgentClient("https://blade.test", token="fixture-secret") as client:
            socket = SocketBoundary()
            client._socket = socket
            session = await client.create_session(intent="", model="fixture-model", initiator="sdk")
            await client.upload_session_skill(session.id, name="eval/sample", files=[
                {"path": "SKILL.md", "content": "---\nname: sample\n---\nDemo"},
                {"path": "assets/input.bin", "content": "AP8=", "encoding": "base64"},
            ])
            await client.upload_file(session.id, binary, remote_path="inputs/input.bin")
            events = [event async for event in client.chat(session.id, "", model="fixture-model", askuser_answer={"prompt_id": "p1", "answer": "yes"})]
            assert client.subscribe(session.id) == ("subscription", "s1")
            await client.stop(session.id, chat_run_id="chat-1")
            history = await client.get_history(session.id)
    assert json.loads(create.calls[0].request.content) == {"intent": "", "model": "fixture-model", "initiator": "sdk"}
    assert json.loads(upload.calls[0].request.content)["files"][1]["encoding"] == "base64"
    assert b"\x00\xff" in attachment.calls[0].request.content
    assert b"inputs/input.bin" in attachment.calls[0].request.content
    assert socket.sent[0][1]["askuser_answer"] == {"prompt_id": "p1", "answer": "yes"}
    assert socket.sent[-1][1] == {"stop": "chat-1"}
    assert history.nodes[0]["tool_call_id"] == "ask-1"
    assert events[-1].raw["chat_run_id"] == "chat-1"
    assert not any(word in json.dumps(socket.sent) for word in ("env_token", "rubric", "score"))


async def test_sdk_upload_error_does_not_become_success():
    with respx.mock as router:
        router.post("https://blade.test/api/sessions/s1/skills:upload").respond(403, json={"error": "denied"})
        async with BladeAgentClient("https://blade.test", token="fixture") as client:
            with pytest.raises(Exception) as error:
                await client.upload_session_skill("s1", name="eval/sample", files=[])
            assert "403" in str(error.value)
