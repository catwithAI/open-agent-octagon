from __future__ import annotations

import random

from ..hashing import variant_content_hash
from ..models import Applicability, MutationInput, VariantResult, VariantSpec
from .common import (
    has_prompt_boundary,
    normalized_prompt,
    prompt_spans,
    transform_unprotected,
    verify_protected_values,
)

_HOMOGLYPHS = str.maketrans(
    {
        "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К",
        "M": "М", "O": "О", "P": "Р", "T": "Т", "X": "Х", "a": "а",
        "c": "с", "e": "е", "o": "о", "p": "р", "x": "х", "y": "у",
    }
)


class _SurfaceMutator:
    version = "1"
    invariants = ("protected_spans", "context_identity", "constraints_identity")
    modalities = ("text",)

    def _candidate(self, char: str) -> bool:
        raise NotImplementedError

    def check(self, source: MutationInput, spec: VariantSpec) -> Applicability:
        if "text" not in source.modalities:
            return Applicability(status="unsupported", reason="text modality required")
        if not has_prompt_boundary(source):
            return Applicability(
                status="unsupported", reason="explicit /prompt mutable region required"
            )
        try:
            spans = prompt_spans(source)
        except ValueError as exc:
            return Applicability(status="unsupported", reason=str(exc))
        text = normalized_prompt(source)
        protected = {index for span in spans for index in range(span.start, span.end)}
        if not any(self._candidate(char) and index not in protected for index, char in enumerate(text)):
            return Applicability(status="unsupported", reason="no applicable characters")
        return Applicability(status="supported")

    def _rate(self, spec: VariantSpec) -> float:
        defaults = {"low": 0.1, "medium": 0.25, "high": 0.5, "default": 0.25}
        rate = float(spec.params.get("rate", defaults.get(spec.intensity, 0.25)))
        if not 0 < rate <= 1:
            raise ValueError("mutation rate must be in (0, 1]")
        return rate

    def _apply(self, text: str, rng: random.Random, rate: float) -> str:
        raise NotImplementedError

    def mutate(self, source: MutationInput, spec: VariantSpec) -> VariantResult:
        applicability = self.check(source, spec)
        if applicability.status == "unsupported":
            raise ValueError(applicability.reason or "mutation unsupported")
        rng = random.Random(spec.seed)
        prompt = transform_unprotected(
            source, lambda chunk: self._apply(chunk, rng, self._rate(spec))
        )
        if prompt == normalized_prompt(source):
            raise ValueError("mutation produced no change")
        verify_protected_values(source, prompt)
        return VariantResult(
            prompt=prompt,
            content_hash=variant_content_hash(
                source, spec, prompt=prompt, context_delta={}
            ),
            summary={
                "mutator": self.id,
                "changed_characters": sum(
                    left != right
                    for left, right in zip(normalized_prompt(source), prompt, strict=False)
                ),
            },
        )


class UnicodeHomoglyphMutator(_SurfaceMutator):
    id = "unicode-homoglyph"

    def _candidate(self, char: str) -> bool:
        return ord(char) in _HOMOGLYPHS

    def _apply(self, text: str, rng: random.Random, rate: float) -> str:
        indexes = [index for index, char in enumerate(text) if self._candidate(char)]
        if not indexes:
            return text
        selected = set(rng.sample(indexes, k=max(1, round(len(indexes) * rate))))
        return "".join(
            _HOMOGLYPHS[ord(char)] if index in selected else char
            for index, char in enumerate(text)
        )


class LetterCaseMutator(_SurfaceMutator):
    id = "letter-case"

    def _candidate(self, char: str) -> bool:
        return char.isascii() and char.isalpha()

    def _apply(self, text: str, rng: random.Random, rate: float) -> str:
        indexes = [index for index, char in enumerate(text) if self._candidate(char)]
        if not indexes:
            return text
        selected = set(rng.sample(indexes, k=max(1, round(len(indexes) * rate))))
        return "".join(
            (char.lower() if char.isupper() else char.upper())
            if index in selected
            else char
            for index, char in enumerate(text)
        )


class SpacingMutator(_SurfaceMutator):
    id = "spacing"

    def _candidate(self, char: str) -> bool:
        return char.isspace()

    def _apply(self, text: str, rng: random.Random, rate: float) -> str:
        indexes = [index for index, char in enumerate(text) if char in " \t"]
        if not indexes:
            return text
        selected = set(rng.sample(indexes, k=max(1, round(len(indexes) * rate))))
        return "".join(
            char + " " if index in selected else char
            for index, char in enumerate(text)
        )
