"""Lineage hashes shared by mutation implementations."""

from __future__ import annotations

from typing import Any

from backend.experiments.hashing import canonical_hash, source_task_hash

from .models import MutationInput, VariantSpec


def mutation_source_hash(
    *,
    prompt: str,
    context: dict[str, Any],
    constraints: dict[str, Any],
    material_hashes: dict[str, str] | tuple[str, ...] = (),
) -> str:
    return source_task_hash(
        prompt=prompt,
        context=context,
        constraints=constraints,
        material_hashes=material_hashes,
    )


def variant_spec_hash(source_hash: str, spec: VariantSpec) -> str:
    return canonical_hash(
        {
            "source_hash": source_hash,
            "mutator_id": spec.mutator_id,
            "mutator_version": spec.mutator_version,
            "seed": spec.seed,
            "intensity": spec.intensity,
            "params": spec.params,
        }
    )


def variant_content_hash(
    source: MutationInput,
    spec: VariantSpec,
    *,
    prompt: str,
    context_delta: dict[str, Any],
) -> str:
    return canonical_hash(
        {
            "source_hash": source.source_hash,
            "spec_hash": variant_spec_hash(source.source_hash, spec),
            "prompt": prompt,
            "context_delta": context_delta,
        }
    )
