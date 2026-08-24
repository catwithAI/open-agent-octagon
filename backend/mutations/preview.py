"""Side-effect-free Run preview with policy-aware variant payload refs."""

from __future__ import annotations

import difflib
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from backend.experiments.hashing import canonical_hash, content_id
from backend.experiments.models import ExperimentProtocol

from .hashing import mutation_source_hash
from .models import MutationInput, ProtectedSpan, VariantSpec
from .registry import MutatorRegistry


class PreviewVariant(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    mutator_id: str
    mutator_version: str
    status: str
    content_hash: str | None = None
    prompt_ref: str | None = Field(default=None, repr=False)
    context_delta: dict[str, Any] = Field(default_factory=dict, repr=False)
    diff: str | None = Field(default=None, repr=False)
    summary: dict[str, Any] = Field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    error_code: str | None = None
    error_message: str | None = None


class VariantPreview(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = "octagon-variant-preview-v1"
    source_hash: str
    protocol_hash: str
    preview_token: str
    cells: int
    attempts: int
    variants: tuple[PreviewVariant, ...]
    blocking_warnings: tuple[str, ...] = ()
    advisory_warnings: tuple[str, ...] = ()


@dataclass
class PreviewPayloadStore:
    data_path: Path

    def put_prompt(
        self, content_hash: str, prompt: str, *, namespace: str = "preview-payloads"
    ) -> str:
        if namespace not in {"preview-payloads", "variant-payloads"}:
            raise ValueError("unsupported payload namespace")
        relative = Path(namespace) / f"{content_hash.removeprefix('sha256:')}.txt"
        destination = Path(self.data_path) / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(prompt, encoding="utf-8")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return relative.as_posix()


def mutation_input_from_task(task: Any) -> MutationInput:
    def value(name: str, default: Any) -> Any:
        if hasattr(task, name):
            return getattr(task, name)
        if isinstance(task, dict):
            return task.get(name, default)
        return default

    context = dict(value("context", {}))
    constraints = dict(value("constraints", {}))
    prompt = str(value("prompt", ""))
    task_id = value("id", None)
    mutation_meta = context.get("_mutation") or {}
    protected = tuple(
        ProtectedSpan.model_validate(item)
        for item in mutation_meta.get("protected_spans", [])
    )
    material_hashes = mutation_meta.get("material_hashes", {})
    source_hash = mutation_source_hash(
        prompt=prompt,
        context=context,
        constraints=constraints,
        material_hashes=material_hashes,
    )
    return MutationInput(
        source_task_id=task_id,
        prompt=prompt,
        context=context,
        constraints=constraints,
        modalities=tuple(mutation_meta.get("modalities", ["text"])),
        source_hash=source_hash,
        protected_spans=protected,
        mutable_regions=tuple(mutation_meta.get("mutable_regions", [])),
    )


def _allowed(mutator_id: str, contract: dict[str, Any], source: MutationInput) -> bool:
    if mutator_id in set(contract.get("allowed") or ["baseline"]):
        return True
    rule = (contract.get("conditional") or {}).get(mutator_id)
    if not isinstance(rule, dict):
        return False
    capabilities = set((source.context.get("_mutation") or {}).get("capabilities", []))
    return set(rule.get("requires") or []).issubset(capabilities)


def build_preview(
    *,
    protocol: ExperimentProtocol,
    source: MutationInput,
    mutation_contract: dict[str, Any],
    registry: MutatorRegistry,
    payload_store: PreviewPayloadStore,
    persist_execution_payloads: bool = False,
) -> VariantPreview:
    variants: list[PreviewVariant] = []
    blocking: list[str] = []
    advisory: list[str] = []
    for experiment_spec in protocol.variant_specs:
        spec = VariantSpec(
            mutator_id=experiment_spec.mutator_id,
            mutator_version=experiment_spec.mutator_version,
            seed=experiment_spec.seed,
            intensity=experiment_spec.intensity,
            params=experiment_spec.params,
        )
        identity = {
            "source_hash": source.source_hash,
            "mutator": spec.model_dump(),
        }
        variant_id = content_id("variant", identity)
        if not _allowed(spec.mutator_id, mutation_contract, source):
            message = f"mutator forbidden by env contract: {spec.mutator_id}"
            variants.append(
                PreviewVariant(
                    id=variant_id,
                    mutator_id=spec.mutator_id,
                    mutator_version=spec.mutator_version,
                    status="unsupported",
                    error_code="mutator_forbidden",
                    error_message=message,
                )
            )
            blocking.append(message)
            continue
        try:
            mutator = registry.get(spec.mutator_id, spec.mutator_version)
            applicability = mutator.check(source, spec)
            if applicability.status == "unsupported":
                raise ValueError(applicability.reason or "mutator unsupported")
            advisory.extend(
                f"{spec.mutator_id}: {warning}"
                for warning in applicability.warnings
            )
            result = mutator.mutate(source, spec)
            prompt_ref = None
            diff = None
            if persist_execution_payloads:
                prompt_ref = payload_store.put_prompt(
                    result.content_hash,
                    result.prompt,
                    namespace="variant-payloads",
                )
            elif protocol.capture_policy == "full":
                prompt_ref = payload_store.put_prompt(result.content_hash, result.prompt)
            if protocol.capture_policy == "full":
                diff = "".join(
                    difflib.unified_diff(
                        source.prompt.splitlines(keepends=True),
                        result.prompt.splitlines(keepends=True),
                        fromfile="source",
                        tofile="variant",
                    )
                )
            variants.append(
                PreviewVariant(
                    id=variant_id,
                    mutator_id=spec.mutator_id,
                    mutator_version=spec.mutator_version,
                    status="ready",
                    content_hash=result.content_hash,
                    prompt_ref=prompt_ref,
                    diff=diff,
                    context_delta=result.context_delta,
                    summary=result.summary,
                    warnings=result.warnings,
                )
            )
        except Exception as exc:
            message = str(exc)
            variants.append(
                PreviewVariant(
                    id=variant_id,
                    mutator_id=spec.mutator_id,
                    mutator_version=spec.mutator_version,
                    status="unsupported",
                    error_code=(
                        "variant_unsupported"
                        if isinstance(exc, (KeyError, ValueError))
                        else "variant_generation_failed"
                    ),
                    error_message=message,
                )
            )
            blocking.append(f"{spec.mutator_id}: {message}")

    protocol_hash = canonical_hash(protocol)
    return VariantPreview(
        source_hash=source.source_hash,
        protocol_hash=protocol_hash,
        # The canonical protocol hash is the stateless preview
        # token.  Create regenerates the normalized protocol and compares it.
        preview_token=protocol_hash,
        cells=protocol.cell_count,
        attempts=protocol.attempt_count,
        variants=tuple(variants),
        blocking_warnings=tuple(blocking),
        advisory_warnings=tuple(advisory),
    )


def validate_preview_token(expected_token: str, regenerated: VariantPreview) -> None:
    if expected_token != regenerated.preview_token:
        raise ValueError("preview_tampered")
