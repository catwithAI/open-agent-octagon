"""Explicit mutator registry; no adapter/runner/scorer coupling."""

from __future__ import annotations

from dataclasses import dataclass, field

from .base import TaskMutator


@dataclass
class MutatorRegistry:
    _items: dict[tuple[str, str], TaskMutator] = field(default_factory=dict)

    def register(self, mutator: TaskMutator) -> None:
        if not isinstance(mutator, TaskMutator):
            raise TypeError("mutator does not implement TaskMutator")
        key = (mutator.id, mutator.version)
        if key in self._items:
            raise ValueError(f"duplicate mutator registration: {key[0]}@{key[1]}")
        self._items[key] = mutator

    def get(self, mutator_id: str, version: str) -> TaskMutator:
        try:
            return self._items[(mutator_id, version)]
        except KeyError:
            raise KeyError(f"unknown mutator: {mutator_id}@{version}") from None

    def versions(self, mutator_id: str) -> tuple[str, ...]:
        return tuple(sorted(version for name, version in self._items if name == mutator_id))

    def all(self) -> tuple[TaskMutator, ...]:
        return tuple(self._items[key] for key in sorted(self._items))


def builtin_registry() -> MutatorRegistry:
    from .builtins import (
        BaselineMutator,
        InstructionPositionMutator,
        LetterCaseMutator,
        SpacingMutator,
        UnicodeHomoglyphMutator,
    )

    registry = MutatorRegistry()
    for mutator in (
        BaselineMutator(),
        UnicodeHomoglyphMutator(),
        SpacingMutator(),
        LetterCaseMutator(),
        InstructionPositionMutator(),
    ):
        registry.register(mutator)
    return registry
