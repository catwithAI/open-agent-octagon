import json
from pathlib import Path

from backend.octagon_evals_client import (
    OctagonEvalsClient,
    OctagonEvalsConfig,
)


class _Response:
    def __init__(self, payload, status=200):
        self.status = status
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self._payload).encode()


def test_external_evals_bridge_starts_run_and_scores_all_dimensions(tmp_path: Path):
    root = tmp_path / "attempts" / "a1" / "skill_workspace"
    root.mkdir(parents=True)
    (root / "answer.md").write_text("candidate output", encoding="utf-8")
    (root / "binary.bin").write_bytes(b"\x00\x01")
    (root.parent / "final_state.json").write_text('{"status":"done"}', encoding="utf-8")
    calls = []

    def opener(request, timeout):
        calls.append((request.method, request.full_url, json.loads(request.data) if request.data else None, timeout))
        if request.method == "POST" and request.full_url.endswith("/runs"):
            return _Response({"plan_hash": "sha256:plan", "task_ids": ["t1", "t2"]})
        if request.method == "GET" and request.full_url.endswith("/tasks/t1"):
            if sum("/tasks/t1/score" in call[1] for call in calls) == 0:
                return _Response({"task_id": "t1", "state": "queued", "scores": []})
            return _Response({"task_id": "t1", "state": "completed", "scores": [{"value": 0.8, "resolved": 1, "reason": "good", "raw": '{"ok":true}'}]})
        if request.method == "GET" and request.full_url.endswith("/tasks/t2"):
            if sum("/tasks/t2/score" in call[1] for call in calls) == 0:
                return _Response({"task_id": "t2", "state": "queued", "scores": []})
            return _Response({"task_id": "t2", "state": "completed", "scores": [{"value": 0.4, "resolved": 1, "reason": "partial", "raw": '{"ok":false}'}]})
        if request.method == "POST" and request.full_url.endswith("/tasks/t1/score"):
            return _Response({"task_id": "t1", "value": 0.8, "source": "agent_judge"})
        if request.method == "POST" and request.full_url.endswith("/tasks/t2/score"):
            return _Response({"task_id": "t2", "value": 0.4, "source": "agent_judge"})
        raise AssertionError((request.method, request.full_url))

    client = OctagonEvalsClient(
        OctagonEvalsConfig("http://eval.test", request_timeout_seconds=7, max_evidence_bytes=1000),
        opener=opener,
    )
    result = client.score(
        experiment_id="exp1",
        run_id="run1",
        scenario={"id": "demo", "version": 1},
        task={"id": "task1", "prompt": "do it"},
        attempt={"env_name": "demo", "agent_name": "codex", "model": "m"},
        attempt_root=root.parent,
        snapshot_ref="scoring-snapshots/hash",
        input_hash="sha256:input",
        env_meta={
            "dimensions": [
                {"name": "correctness", "weight": 60, "description": "Is it correct?"},
                {"name": "quality", "weight": 40, "description": "Is it clear?"},
            ]
        },
    )

    assert [(row["dimension"], row["value"]) for row in result.scores] == [
        ("correctness", 80),
        ("quality", 40),
    ]
    start_payload = calls[0][2]
    assert all(d["method"] == "agent_judge" for d in start_payload["dimensions"])
    assert start_payload["artifact"]["content_hash"] == "sha256:input"
    score_payload = next(call[2] for call in calls if call[1].endswith("/tasks/t1/score"))
    assert score_payload["evidence"]["artifact"]["files"][0]["content"] == "candidate output"
    assert result.metadata["backend"] == "octagon-evals"


def test_external_evals_sends_top_level_upstream_completed_for_failed_attempt(tmp_path: Path):
    root = tmp_path / "attempts" / "a1" / "skill_workspace"
    root.mkdir(parents=True)
    calls = []

    def opener(request, timeout):
        calls.append((request.method, request.full_url, json.loads(request.data) if request.data else None, timeout))
        if request.method == "POST" and request.full_url.endswith("/runs"):
            return _Response({"plan_hash": "sha256:plan", "task_ids": ["t1"]})
        if request.method == "GET" and request.full_url.endswith("/tasks/t1"):
            return _Response({"task_id": "t1", "state": "completed", "scores": [{"value": 0.0, "resolved": 1}]})
        raise AssertionError((request.method, request.full_url))

    client = OctagonEvalsClient(
        OctagonEvalsConfig("http://eval.test", request_timeout_seconds=7, max_evidence_bytes=1000),
        opener=opener,
    )
    client.score(
        experiment_id="exp1",
        run_id="run1",
        scenario={"id": "demo", "version": 1},
        task={"id": "task1", "prompt": "do it"},
        attempt={"env_name": "demo", "agent_name": "codex", "model": "m", "execution_status": "timeout"},
        attempt_root=root.parent,
        snapshot_ref="scoring-snapshots/hash",
        input_hash="sha256:input",
        env_meta={"dimensions": [{"name": "correctness", "weight": 100, "description": "Is it correct?"}]},
    )
    payload = calls[0][2]
    assert payload["upstream_completed"] is False
    assert "run_status" not in payload
