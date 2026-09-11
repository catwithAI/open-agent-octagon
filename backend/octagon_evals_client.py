"""HTTP bridge to the standalone ``octagon-evals`` scoring service.

The execution service owns attempts and immutable scoring snapshots.  This module
only translates one frozen attempt into octagon-evals' EvaluationInput envelope,
submits the dimension evidence, and converts the normalized [0, 1] scores back to
Octagon's legacy [0, 100] score rows.

The bridge intentionally uses the standard library HTTP client so installing the
evaluator remains optional for the runtime process.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .evaluator import ScorerUnavailableError


class OctagonEvalsUnavailableError(ScorerUnavailableError):
    """The external evaluation service could not produce a score."""


@dataclass(frozen=True)
class OctagonEvalsConfig:
    base_url: str
    request_timeout_seconds: float = 30.0
    max_evidence_bytes: int = 262_144
    max_file_bytes: int = 32_768


@dataclass
class OctagonEvalsResult:
    scores: list[dict[str, Any]]
    plan_hash: str | None
    task_ids: dict[str, str]
    metadata: dict[str, Any] = field(default_factory=dict)


class OctagonEvalsClient:
    def __init__(
        self,
        config: OctagonEvalsConfig,
        *,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        self.config = config
        self.opener = opener

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = Request(
            self.config.base_url.rstrip("/") + path,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with self.opener(req, timeout=self.config.request_timeout_seconds) as response:
                raw = response.read()
                status = int(getattr(response, "status", 200))
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise OctagonEvalsUnavailableError(f"octagon-evals request failed: {exc}") from exc
        try:
            result = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise OctagonEvalsUnavailableError(
                f"octagon-evals returned invalid JSON (HTTP {status})"
            ) from exc
        if status >= 400:
            detail = result.get("detail") if isinstance(result, dict) else result
            raise OctagonEvalsUnavailableError(f"octagon-evals HTTP {status}: {detail}")
        if not isinstance(result, dict):
            raise OctagonEvalsUnavailableError("octagon-evals response must be an object")
        return result

    @staticmethod
    def _dimensions(meta: dict[str, Any]) -> list[dict[str, Any]]:
        dimensions = meta.get("dimensions") or []
        if not isinstance(dimensions, list):
            raise OctagonEvalsUnavailableError("env dimensions must be a list")
        result: list[dict[str, Any]] = []
        for raw in dimensions:
            if not isinstance(raw, dict):
                continue
            dimension_id = str(raw.get("id") or raw.get("name") or "").strip()
            if not dimension_id:
                continue
            # The standalone service is the LLM-as-judge backend in this mode.
            # Keep the env's rubric text and optional anchors, but do not ask the
            # service to discover an env-local deterministic scorer that it cannot
            # import from the runtime repository.
            result.append(
                {
                    "id": dimension_id,
                    "version": int(raw.get("version", 1)),
                    "role": "scored",
                    "weight": float(raw.get("weight", 1.0)),
                    "method": "agent_judge",
                    "evidence": list(raw.get("evidence") or []),
                    "scorer_version": str(raw.get("scorer_version", "octagon-evals-agent-judge-v1")),
                    "question": str(raw.get("question") or raw.get("description") or dimension_id),
                    "anchors": raw.get("anchors") or [],
                    "output_schema": raw.get("output_schema") or {
                        "type": "object",
                        "required": ["value", "reason", "raw"],
                        "value_range": [0, 1],
                    },
                }
            )
        if not result:
            raise OctagonEvalsUnavailableError("env has no scoreable dimensions")
        return result

    def _artifact_evidence(self, attempt_root: Path) -> dict[str, Any]:
        """Return bounded, text-first evidence for a remote judge."""
        workspace = attempt_root / "skill_workspace"
        root = workspace if workspace.is_dir() else attempt_root
        total = 0
        files: list[dict[str, Any]] = []
        for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(root).as_posix()
            try:
                size = path.stat().st_size
            except OSError:
                continue
            item: dict[str, Any] = {"path": relative, "size": size}
            # Binary/media artifacts remain referenced but are not guessed as text.
            if size <= self.config.max_file_bytes and total < self.config.max_evidence_bytes:
                try:
                    data = path.read_bytes()
                except OSError:
                    data = b""
                if b"\x00" not in data:
                    remaining = self.config.max_evidence_bytes - total
                    text = data[: min(len(data), remaining)].decode("utf-8", errors="replace")
                    item["content"] = text
                    total += len(text.encode("utf-8"))
            files.append(item)
        return {"root": "skill_workspace" if workspace.is_dir() else ".", "files": files}

    @staticmethod
    def _json_size(value: Any) -> int:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    @classmethod
    def _fit_evidence(cls, evidence: dict[str, Any], max_bytes: int) -> dict[str, Any]:
        """Enforce one serialized-size budget for the complete judge payload.

        Component-level caps keep normal requests small, but they do not account
        for JSON structure, task text, final_state, or the sum of all evidence
        fields.  If the complete payload is still too large, replace the largest
        top-level evidence section with an explicit marker until the serialized
        request fits the configured limit.
        """
        if max_bytes <= 0:
            return {"_truncated": True, "reason": "invalid_evidence_budget"}
        result = dict(evidence)
        encoded_size = cls._json_size(result)
        if encoded_size <= max_bytes:
            return result

        original_size = encoded_size
        while cls._json_size(result) > max_bytes:
            candidates = [
                key for key in result
                if key not in {"_evidence_truncation", "artifact_ref", "history_refs"}
            ]
            if not candidates:
                return {
                    "_truncated": True,
                    "reason": "max_evidence_bytes",
                    "original_bytes": original_size,
                }
            key = max(candidates, key=lambda item: cls._json_size(result[item]))
            result[key] = {
                "_truncated": True,
                "reason": "max_evidence_bytes",
                "original_bytes": cls._json_size(evidence[key]),
            }
        result["_evidence_truncation"] = {
            "truncated": True,
            "original_bytes": original_size,
            "max_bytes": max_bytes,
        }
        # Adding the summary marker can itself cross a very small budget.
        if cls._json_size(result) > max_bytes:
            result.pop("_evidence_truncation", None)
        if cls._json_size(result) > max_bytes:
            return {
                "_truncated": True,
                "reason": "max_evidence_bytes",
                "original_bytes": original_size,
            }
        return result

    @staticmethod
    def _score_from_detail(dimension_id: str, detail: dict[str, Any]) -> dict[str, Any]:
        rows = detail.get("scores") or []
        resolved = [row for row in rows if isinstance(row, dict) and row.get("resolved")]
        row = resolved[-1] if resolved else (rows[-1] if rows else {})
        try:
            value = float(row.get("value", detail.get("value")))
        except (TypeError, ValueError) as exc:
            raise OctagonEvalsUnavailableError(
                f"octagon-evals returned no numeric score for {dimension_id}"
            ) from exc
        if not 0 <= value <= 1:
            raise OctagonEvalsUnavailableError(f"invalid normalized score for {dimension_id}: {value}")
        raw = row.get("raw")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                pass
        reason = row.get("reason") or detail.get("reason") or ""
        return {
            "dimension": dimension_id,
            "value": round(value * 100),
            "detail": str(reason) if reason else f"octagon-evals agent_judge value={value:.4f}",
            "normalized_value": value,
            "raw": raw,
            "source": row.get("source") or detail.get("source") or "agent_judge",
        }

    def score(
        self,
        *,
        experiment_id: str,
        run_id: str,
        scenario: dict[str, Any],
        task: dict[str, Any],
        attempt: dict[str, Any],
        attempt_root: Path,
        snapshot_ref: str,
        input_hash: str,
        env_meta: dict[str, Any],
    ) -> OctagonEvalsResult:
        dimensions = self._dimensions(env_meta)
        artifact = {
            "snapshot_ref": snapshot_ref,
            "content_hash": input_hash,
        }
        history = {
            "trajectory_ref": f"{snapshot_ref}/attempt/trace.jsonl",
            "actions_ref": f"{snapshot_ref}/attempt/events.jsonl",
            "trace_ref": f"{snapshot_ref}/attempt/trace.jsonl",
        }
        # octagon-evals models one AgentRun per attempt.  Keep the local run ID
        # in producer metadata for comparison/group lineage, but use the
        # attempt-specific ID as the external run identity.
        external_run_id = str(attempt.get("id") or attempt.get("attempt_id") or run_id)
        local_run_id = str(attempt.get("run_id") or run_id) or None
        evaluation_input = {
            "experiment_id": experiment_id,
            "run_id": external_run_id,
            "scenario": scenario or {"id": attempt.get("env_name", "unknown"), "version": 1},
            "task": task,
            "artifact": artifact,
            "history": history,
            "run_status": {"upstream_completed": True},
            "producer": {
                "agent_name": attempt.get("agent_name"),
                "model": attempt.get("model"),
                "local_run_id": local_run_id,
            },
            "dimensions": dimensions,
            "plan_version": 1,
        }
        started = self._request(
            "POST",
            f"/experiments/{experiment_id}/runs",
            evaluation_input,
        )
        raw_task_ids = started.get("task_ids") or []
        if len(raw_task_ids) != len(dimensions):
            raise OctagonEvalsUnavailableError(
                f"octagon-evals returned {len(raw_task_ids)} tasks for {len(dimensions)} dimensions"
            )
        task_ids = {dimension["id"]: str(task_id) for dimension, task_id in zip(dimensions, raw_task_ids)}
        scores: list[dict[str, Any]] = []
        evidence = {
            "task": task,
            "final_state": _read_json(
                attempt_root / "final_state.json",
                max_bytes=max(1_024, self.config.max_evidence_bytes // 8),
            ),
            "trace": _read_jsonl(
                attempt_root / "trace.jsonl",
                max_bytes=max(1_024, self.config.max_evidence_bytes // 8),
            ),
            "events": _read_jsonl(
                attempt_root / "events.jsonl",
                max_bytes=max(1_024, self.config.max_evidence_bytes // 8),
            ),
            "artifact": self._artifact_evidence(attempt_root),
            "artifact_ref": artifact,
            "history_refs": history,
        }
        # The score endpoint wraps the object in {"evidence": ...}; reserve
        # that small envelope so max_evidence_bytes bounds the full JSON body,
        # not only the nested evidence value.
        envelope_bytes = self._json_size({"evidence": {}})
        evidence = self._fit_evidence(
            evidence,
            max(1, self.config.max_evidence_bytes - envelope_bytes),
        )
        for dimension in dimensions:
            dimension_id = dimension["id"]
            task_id = task_ids[dimension_id]
            detail = self._request("GET", f"/tasks/{task_id}")
            state = detail.get("state")
            if state != "completed":
                self._request("POST", f"/tasks/{task_id}/score", {"evidence": evidence})
                detail = self._request("GET", f"/tasks/{task_id}")
            if detail.get("state") != "completed":
                raise OctagonEvalsUnavailableError(
                    f"octagon-evals task {task_id} did not complete (state={detail.get('state')})"
                )
            scores.append(self._score_from_detail(dimension_id, detail))
        return OctagonEvalsResult(
            scores=scores,
            plan_hash=str(started.get("plan_hash")) if started.get("plan_hash") else None,
            task_ids=task_ids,
            metadata={
                "backend": "octagon-evals",
                "endpoint": self.config.base_url.rstrip("/"),
                "plan_hash": started.get("plan_hash"),
                "task_ids": task_ids,
                "dimensions": [d["id"] for d in dimensions],
                "external_run_id": external_run_id,
                "local_run_id": local_run_id,
            },
        )


def _read_json(path: Path, *, max_bytes: int | None = None) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError:
        return {}
    if max_bytes is not None and len(raw) > max_bytes:
        return {
            "_truncated": True,
            "reason": "component_byte_budget",
            "path": path.name,
            "original_bytes": len(raw),
        }
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_jsonl(path: Path, *, max_bytes: int | None = None) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    result: list[dict[str, Any]] = []
    used = 0
    truncated = False
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        encoded_size = len(line.encode("utf-8"))
        if max_bytes is not None and used + encoded_size > max_bytes:
            truncated = True
            break
        result.append(value)
        used += encoded_size
    if truncated:
        result.append({
            "_truncated": True,
            "reason": "component_byte_budget",
            "path": path.name,
        })
    return result


def make_scorer(
    client: OctagonEvalsClient,
    *,
    experiment_id: str,
    run_id: str,
    scenario: dict[str, Any],
    attempt: dict[str, Any],
    attempt_root: Path,
    snapshot_ref: str,
    input_hash: str,
    env_meta: dict[str, Any],
) -> Callable[..., list[dict[str, Any]]]:
    metadata: dict[str, Any] = {}

    def score(*, task: dict[str, Any], **_kwargs: Any) -> list[dict[str, Any]]:
        result = client.score(
            experiment_id=experiment_id,
            run_id=run_id,
            scenario=scenario,
            task=task,
            attempt=attempt,
            attempt_root=attempt_root,
            snapshot_ref=snapshot_ref,
            input_hash=input_hash,
            env_meta=env_meta,
        )
        metadata.update(result.metadata)
        return result.scores

    def manifest() -> dict[str, Any]:
        return {
            "backend": "octagon-evals",
            "endpoint": client.config.base_url.rstrip("/"),
            "plan_hash": metadata.get("plan_hash"),
            "task_ids": metadata.get("task_ids", {}),
            "scorer_version": "octagon-evals-agent-judge-v1",
        }

    score.__octagon_evaluation_manifest__ = manifest  # type: ignore[attr-defined]
    score.__octagon_scorer_version__ = "octagon-evals-agent-judge-v1"  # type: ignore[attr-defined]
    return score
