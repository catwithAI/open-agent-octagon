from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


class HarborTaskSpecError(ValueError):
    """A task is not safe to execute in Harbor compatibility mode."""


def _require_string(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HarborTaskSpecError(f"_harbor.{key} must be a non-empty string")
    return value


def _optional_string(data: Mapping[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise HarborTaskSpecError(f"_harbor.{key} must be a non-empty string or null")
    return value


def _pinned_image(value: str, field: str) -> str:
    if "@sha256:" not in value:
        raise HarborTaskSpecError(f"_harbor.{field} must be digest-pinned: {value!r}")
    return value


def _positive_int(value: Any, field: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise HarborTaskSpecError(f"_harbor.{field} must be a positive integer") from exc
    if result <= 0:
        raise HarborTaskSpecError(f"_harbor.{field} must be a positive integer")
    return result


@dataclass(frozen=True)
class HarborTaskSpec:
    """Execution metadata copied from a frozen Harbor task manifest.

    The object contains no prompt text and no verifier files. The agent receives
    only the task instruction; this spec is consumed by the backend runtime.
    """

    release: str
    source_commit: str
    task_name: str
    task_digest: str
    task_toml_sha256: str
    instruction_sha256: str
    agent_image: str
    verifier_image: str
    agent_workdir: str
    agent_timeout_seconds: int
    verifier_timeout_seconds: int
    agent_network_mode: str | None
    verifier_network_mode: str | None
    agent_resources: dict[str, Any]
    verifier_resources: dict[str, Any]
    environment_mode: str
    artifacts: tuple[str, ...]
    has_collect_hooks: bool
    collect_hook_count: int
    execution_mode: str
    image_pull_policy: str
    auto_prepare: bool

    @classmethod
    def from_context(cls, context: Mapping[str, Any]) -> "HarborTaskSpec":
        raw = context.get("_harbor")
        if not isinstance(raw, Mapping):
            raise HarborTaskSpecError("task context is missing _harbor metadata")
        artifacts = raw.get("artifacts") or []
        if not isinstance(artifacts, list) or not all(isinstance(x, str) for x in artifacts):
            raise HarborTaskSpecError("_harbor.artifacts must be a string array")
        agent_resources = raw.get("agent_resources") or {}
        verifier_resources = raw.get("verifier_resources") or {}
        if not isinstance(agent_resources, Mapping) or not isinstance(verifier_resources, Mapping):
            raise HarborTaskSpecError("_harbor resource fields must be objects")
        workdir = _optional_string(raw, "agent_workdir") or "/workspace"
        if not workdir.startswith("/"):
            raise HarborTaskSpecError("_harbor.agent_workdir must be absolute")
        execution_mode = _optional_string(raw, "execution_mode") or "single_step"
        if execution_mode not in {"single_step", "multi_step"}:
            raise HarborTaskSpecError(f"unsupported Harbor execution_mode: {execution_mode!r}")
        image_pull_policy = _optional_string(raw, "image_pull_policy") or "selected-task-only"
        if image_pull_policy not in {"never", "selected-task-only"}:
            raise HarborTaskSpecError(
                f"unsupported Harbor image_pull_policy: {image_pull_policy!r}"
            )
        auto_prepare = raw.get("auto_prepare", image_pull_policy == "selected-task-only")
        if not isinstance(auto_prepare, bool):
            raise HarborTaskSpecError("_harbor.auto_prepare must be a boolean")
        collect_count = raw.get("collect_hook_count", 0)
        try:
            collect_count = int(collect_count)
        except (TypeError, ValueError) as exc:
            raise HarborTaskSpecError("_harbor.collect_hook_count must be an integer") from exc
        if collect_count < 0:
            raise HarborTaskSpecError("_harbor.collect_hook_count cannot be negative")
        return cls(
            release=_require_string(raw, "release"),
            source_commit=_require_string(raw, "source_commit"),
            task_name=_require_string(raw, "task_name"),
            task_digest=_require_string(raw, "task_digest"),
            task_toml_sha256=_require_string(raw, "task_toml_sha256"),
            instruction_sha256=_require_string(raw, "instruction_sha256"),
            agent_image=_pinned_image(_require_string(raw, "agent_image"), "agent_image"),
            verifier_image=_pinned_image(_require_string(raw, "verifier_image"), "verifier_image"),
            agent_workdir=workdir.rstrip("/") or "/",
            agent_timeout_seconds=_positive_int(raw.get("agent_timeout_seconds", 600), "agent_timeout_seconds"),
            verifier_timeout_seconds=_positive_int(raw.get("verifier_timeout_seconds", 600), "verifier_timeout_seconds"),
            agent_network_mode=_optional_string(raw, "agent_network_mode"),
            verifier_network_mode=_optional_string(raw, "verifier_network_mode"),
            agent_resources={str(k): v for k, v in agent_resources.items()},
            verifier_resources={str(k): v for k, v in verifier_resources.items()},
            environment_mode=_optional_string(raw, "environment_mode") or "separate",
            artifacts=tuple(artifacts),
            has_collect_hooks=bool(raw.get("has_collect_hooks", False)),
            collect_hook_count=collect_count,
            execution_mode=execution_mode,
            image_pull_policy=image_pull_policy,
            auto_prepare=auto_prepare,
        )
