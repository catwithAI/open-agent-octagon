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
        OctagonEvalsConfig("http://eval.test", request_timeout_seconds=7, max_evidence_bytes=4096),
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
    assert len(json.dumps(score_payload, ensure_ascii=False).encode()) <= 4096
    assert result.metadata["backend"] == "octagon-evals"


def test_external_run_ids_are_attempt_specific_for_shared_local_run():
    starts = []

    def opener(request, timeout):
        payload = json.loads(request.data) if request.data else None
        if request.method == "POST" and request.full_url.endswith("/runs"):
            starts.append(payload)
            return _Response({"plan_hash": "sha256:plan", "task_ids": [f"task-{len(starts)}"]})
        if request.method == "GET" and "/tasks/task-" in request.full_url:
            return _Response({
                "state": "completed",
                "scores": [{"value": 1.0, "resolved": 1, "reason": "ok"}],
            })
        raise AssertionError((request.method, request.full_url))

    client = OctagonEvalsClient(
        OctagonEvalsConfig("http://eval.test"),
        opener=opener,
    )
    common = {
        "run_id": "local-comparison-run",
        "agent_name": "codex",
        "model": "model-a",
    }
    for attempt_id in ("attempt-a", "attempt-b"):
        client.score(
            experiment_id="comparison-experiment",
            run_id="local-comparison-run",
            scenario={"id": "demo", "version": 1},
            task={"id": attempt_id, "prompt": "do it"},
            attempt={**common, "id": attempt_id},
            attempt_root=Path("/tmp/nonexistent-attempt"),
            snapshot_ref=f"scoring-snapshots/{attempt_id}",
            input_hash=f"sha256:{attempt_id}",
            env_meta={"dimensions": [{"name": "quality", "weight": 1}]},
        )

    assert [payload["run_id"] for payload in starts] == ["attempt-a", "attempt-b"]
    assert [payload["producer"]["local_run_id"] for payload in starts] == [
        "local-comparison-run",
        "local-comparison-run",
    ]


def test_evidence_budget_truncates_large_sections_with_markers(tmp_path: Path):
    root = tmp_path / "attempts" / "a1"
    workspace = root / "skill_workspace"
    workspace.mkdir(parents=True)
    (root / "final_state.json").write_text(json.dumps({"output": "x" * 20000}), encoding="utf-8")
    (workspace / "answer.md").write_text("y" * 20000, encoding="utf-8")

    client = OctagonEvalsClient(OctagonEvalsConfig("http://eval.test", max_evidence_bytes=2048))
    evidence = {
        "task": {"prompt": "z" * 20000},
        "final_state": {"output": "x" * 20000},
        "trace": [{"content": "t" * 20000}],
        "events": [{"content": "e" * 20000}],
        "artifact": {"files": [{"path": "answer.md", "content": "y" * 20000}]},
        "artifact_ref": {"snapshot_ref": "snap"},
        "history_refs": {"trace_ref": "trace"},
    }
    fitted = client._fit_evidence(evidence, 2048)

    assert len(json.dumps(fitted, ensure_ascii=False).encode()) <= 2048
    assert any(isinstance(value, dict) and value.get("_truncated") for value in fitted.values())
