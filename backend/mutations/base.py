"""TaskMutator structural protocol."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import Applicability, MutationInput, VariantResult, VariantSpec


@runtime_checkable
class TaskMutator(Protocol):
    id: str
    version: str
    invariants: tuple[str, ...]
    modalities: tuple[str, ...]

    def check(self, source: MutationInput, spec: VariantSpec) -> Applicability: ...

    def mutate(self, source: MutationInput, spec: VariantSpec) -> VariantResult: ...
