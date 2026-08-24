"""Application service shared by Experiment preview/create HTTP mappings."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from backend import runtime_state
from backend.config import Settings
from backend.db import _now_iso, _open_sync
from backend.mutations.preview import (
    PreviewPayloadStore,
    VariantPreview,
    build_preview,
    mutation_input_from_task,
    validate_preview_token,
)
from backend.mutations.registry import builtin_registry

from .hashing import canonical_hash, content_id
from .models import ExperimentProtocol, MatrixCell, RunGroupPlan, VariantSpec
from .protocol import ProtocolNormalizer
from .repository import (
    CreateExperimentBundle,
    CreatedExperimentBundle,
    ExperimentRepository,
    FrozenVariant,
)


class ExperimentServiceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PreparedPreview:
    env: Any
    task: Any
    protocol: ExperimentProtocol
    preview: VariantPreview


@dataclass(frozen=True)
class AdhocTask:
    id: str
    env_name: str
    prompt: str
    context: dict[str, Any]
    constraints: dict[str, Any]
    timeout_seconds: int


def prepare_preview(
    *,
    settings: Settings,
    env_name: str,
    task_id: str | None,
    prompt: str | None = None,
    context: dict[str, Any] | None = None,
    constraints: dict[str, Any] | None = None,
    timeout_seconds: int | None = None,
) -> tuple[Any, Any]:
    state = runtime_state.get()
    env = state.envs.get(env_name)
    if env is None:
        raise ExperimentServiceError("env_not_found", f"environment not found: {env_name}")
    if task_id is not None:
        task = getattr(env, "tasks_by_id", {}).get(task_id)
        if task is None:
            raise ExperimentServiceError("task_not_found", f"task not found: {task_id}")
        # timeout_seconds is None -> caller didn't ask to override, keep the
        # task's own default. A non-None value overrides it — return a copy,
        # never mutate the shared Task cached on env.tasks_by_id (that would
        # leak into every other request in this process).
        if timeout_seconds is not None and timeout_seconds != task.timeout_seconds:
            task = replace(task, timeout_seconds=timeout_seconds)
    else:
        source = {
            "env_name": env_name,
            "prompt": prompt or "",
            "context": context or {},
            "constraints": constraints or {},
        }
        task = AdhocTask(
            id=f"adhoc_{canonical_hash(source).removeprefix('sha256:')[:12]}",
            env_name=env_name,
            prompt=prompt or "",
            context=context or {},
            constraints=constraints or {},
            # Free-prompt tasks have no file-based default to fall back on.
            timeout_seconds=timeout_seconds if timeout_seconds is not None else 600,
        )
    return env, task


def preview_experiment(
    *,
    settings: Settings,
    env_name: str,
    task_id: str | None,
    protocol: ExperimentProtocol,
    prompt: str | None = None,
    context: dict[str, Any] | None = None,
    constraints: dict[str, Any] | None = None,
    timeout_seconds: int | None = None,
    persist_execution_payloads: bool = False,
) -> PreparedPreview:
    env, task = prepare_preview(
        settings=settings,
        env_name=env_name,
        task_id=task_id,
        prompt=prompt,
        context=context,
        constraints=constraints,
        timeout_seconds=timeout_seconds,
    )
    # The request-level timeout_seconds only reached `task` above (preview
    # display / free-prompt AdhocTask). Execution actually reads
    # protocol.timeout_seconds (coordinator._provision_cell:
    # `protocol.timeout_seconds or source[2]`) — a distinct field on
    # ExperimentProtocol that historically had no way to be set from the
    # request body's plain timeout_seconds, so callers who set only the
    # top-level field (batch scripts, and the ExperimentBuilder form before
    # this fix) silently fell back to the task file's hardcoded default.
    # Fold it in here, before normalize()/hashing, so preview_token and the
    # persisted protocol agree on the same value — mutating protocol after
    # normalize() would desync the preview hash from what create() persists.
    if timeout_seconds is not None and protocol.timeout_seconds is None:
        protocol = protocol.model_copy(update={"timeout_seconds": timeout_seconds})
    try:
        normalized = ProtocolNormalizer(settings).normalize(protocol)
    except ValueError as exc:
        raise ExperimentServiceError("protocol_invalid", str(exc)) from exc
    preview = build_preview(
        protocol=normalized.protocol,
        source=mutation_input_from_task(task),
        mutation_contract=dict(getattr(env, "meta", {}).get("mutations") or {}),
        registry=builtin_registry(),
        payload_store=PreviewPayloadStore(runtime_state.get().data_path),
        persist_execution_payloads=persist_execution_payloads,
    )
    preview = preview.model_copy(
        update={
            "advisory_warnings": (
                *normalized.advisory_warnings,
                *preview.advisory_warnings,
            )
        }
    )
    return PreparedPreview(
        env=env,
        task=task,
        protocol=normalized.protocol,
        preview=preview,
    )


def _hydrate_task(db_path: Path, task: Any) -> None:
    source_kind = "adhoc" if isinstance(task, AdhocTask) else "file"
    with _open_sync(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO tasks(id,env_name,prompt,context_json,constraints_json,"
            "timeout_seconds,source,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                task.id,
                task.env_name,
                task.prompt,
                json.dumps(task.context, ensure_ascii=False),
                json.dumps(task.constraints, ensure_ascii=False),
                task.timeout_seconds,
                source_kind,
                _now_iso(),
            ),
        )
        conn.commit()


def create_experiment(
    *,
    settings: Settings,
    title: str,
    question: str,
    env_name: str,
    task_id: str | None,
    protocol: ExperimentProtocol,
    preview_token: str,
    idempotency_key: str,
    prompt: str | None = None,
    context: dict[str, Any] | None = None,
    constraints: dict[str, Any] | None = None,
    timeout_seconds: int | None = None,
) -> CreatedExperimentBundle:
    prepared = preview_experiment(
        settings=settings,
        env_name=env_name,
        task_id=task_id,
        protocol=protocol,
        prompt=prompt,
        context=context,
        constraints=constraints,
        timeout_seconds=timeout_seconds,
        persist_execution_payloads=True,
    )
    try:
        validate_preview_token(preview_token, prepared.preview)
    except ValueError as exc:
        raise ExperimentServiceError("preview_tampered", "preview token mismatch") from exc
    if prepared.preview.blocking_warnings:
        raise ExperimentServiceError(
            "preview_blocked", "; ".join(prepared.preview.blocking_warnings)
        )

    frozen: list[FrozenVariant] = []
    for spec, item in zip(
        prepared.protocol.variant_specs, prepared.preview.variants, strict=True
    ):
        if item.status != "ready" or item.content_hash is None:
            raise ExperimentServiceError(
                "preview_blocked", item.error_message or "variant is not ready"
            )
        frozen.append(
            FrozenVariant(
                # Preview IDs are content identities. Persisted variants need
                # aggregate-local identities so the same protocol can create
                # two independent Experiments under different idempotency keys.
                id=content_id(
                    "variant",
                    {
                        "idempotency_key": idempotency_key,
                        "preview_variant_id": item.id,
                    },
                ),
                kind="baseline" if spec.mutator_id == "baseline" else "mutation",
                spec=spec,
                source_hash=prepared.preview.source_hash,
                content_hash=item.content_hash,
                prompt_ref=item.prompt_ref,
                context_delta=item.context_delta,
                summary=item.summary,
            )
        )

    group_id = content_id("run_group", {"idempotency_key": idempotency_key})
    cells = tuple(
        MatrixCell(
            schema_version="octagon-matrix-cell-v1",
            id=content_id(
                "cell",
                {
                    "group_id": group_id,
                    "variant_id": variant.id,
                    "repeat_index": repeat_index,
                },
            ),
            variant_id=variant.id,
            repeat_index=repeat_index,
        )
        for variant in frozen
        for repeat_index in range(prepared.protocol.repeats)
    )
    bundle = CreateExperimentBundle(
        title=title,
        env_name=env_name,
        source_task_id=prepared.task.id,
        question=question,
        protocol=prepared.protocol,
        variants=tuple(frozen),
        group_id=group_id,
        group_plan=RunGroupPlan(
            schema_version="octagon-run-group-plan-v1",
            strategy="full-matrix",
            cells=cells,
        ),
    )
    state = runtime_state.get()
    _hydrate_task(state.db_path, prepared.task)
    return ExperimentRepository(state.db_path, state.data_path).create_bundle(
        bundle, idempotency_key=idempotency_key
    )


def clone_experiment(
    *,
    experiment_id: str,
    idempotency_key: str,
    title: str | None = None,
    question: str | None = None,
) -> CreatedExperimentBundle:
    """Clone the frozen snapshot without consulting mutable catalogs/profiles."""
    state = runtime_state.get()
    repository = ExperimentRepository(state.db_path, state.data_path)
    original = repository.get_bundle(experiment_id)
    if original is None:
        raise ExperimentServiceError(
            "experiment_not_found", f"experiment not found: {experiment_id}"
        )
    experiment = original["experiment"]
    protocol = ExperimentProtocol.model_validate_json(experiment["protocol_json"])
    specs: dict[tuple[str, str, int, str], VariantSpec] = {
        (
            spec.mutator_id,
            spec.mutator_version,
            spec.seed,
            canonical_hash(spec.params),
        ): spec
        for spec in protocol.variant_specs
    }
    frozen: list[FrozenVariant] = []
    for row in original["variants"]:
        params = json.loads(row["params_json"])
        spec = specs[
            (
                row["mutator_id"],
                row["mutator_version"],
                row["seed"],
                canonical_hash(params),
            )
        ]
        frozen.append(
            FrozenVariant(
                id=content_id(
                    "variant",
                    {
                        "idempotency_key": idempotency_key,
                        "source_variant_id": row["id"],
                    },
                ),
                kind=row["kind"],
                spec=spec,
                source_hash=row["source_hash"],
                content_hash=row["content_hash"],
                prompt_ref=row["prompt_ref"],
                context_delta=json.loads(row["context_delta_json"]),
                summary=json.loads(row["summary_json"]),
                status=row["status"],
                error_code=row["error_code"],
            )
        )
    group_id = content_id("run_group", {"idempotency_key": idempotency_key})
    cells = tuple(
        MatrixCell(
            schema_version="octagon-matrix-cell-v1",
            id=content_id(
                "cell",
                {
                    "group_id": group_id,
                    "variant_id": variant.id,
                    "repeat_index": repeat_index,
                },
            ),
            variant_id=variant.id,
            repeat_index=repeat_index,
        )
        for variant in frozen
        for repeat_index in range(protocol.repeats)
    )
    bundle = CreateExperimentBundle(
        parent_experiment_id=experiment_id,
        title=title or f"{experiment['title']} (clone)",
        env_name=experiment["env_name"],
        source_task_id=experiment["source_task_id"],
        question=question or experiment["question"],
        protocol=protocol,
        variants=tuple(frozen),
        group_id=group_id,
        group_plan=RunGroupPlan(
            schema_version="octagon-run-group-plan-v1",
            strategy="full-matrix",
            cells=cells,
        ),
    )
    return repository.create_bundle(bundle, idempotency_key=idempotency_key)
