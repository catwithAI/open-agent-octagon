"""Protected-span validation and deterministic text transformation helpers."""

from __future__ import annotations

import unicodedata
from collections.abc import Callable

from backend.experiments.hashing import hash_bytes

from ..models import MutationInput, ProtectedSpan


def normalized_prompt(source: MutationInput) -> str:
    return unicodedata.normalize("NFC", source.prompt)


def prompt_spans(source: MutationInput) -> tuple[ProtectedSpan, ...]:
    text = normalized_prompt(source)
    spans = sorted(
        (span for span in source.protected_spans if span.field_path == "/prompt"),
        key=lambda span: (span.start, span.end),
    )
    previous_end = 0
    for span in spans:
        if span.end > len(text):
            raise ValueError(f"protected span out of bounds: {span.start}:{span.end}")
        actual = hash_bytes(text[span.start : span.end].encode("utf-8"))
        if actual != span.content_hash:
            raise ValueError(f"protected span content hash mismatch: {span.kind}")
        if span.start < previous_end:
            # Overlap is allowed by contract; transformation uses their union.
            previous_end = max(previous_end, span.end)
        else:
            previous_end = span.end
    return tuple(spans)


def has_prompt_boundary(source: MutationInput) -> bool:
    return "/prompt" in source.mutable_regions


def transform_unprotected(
    source: MutationInput, transform: Callable[[str], str]
) -> str:
    text = normalized_prompt(source)
    spans = prompt_spans(source)
    result: list[str] = []
    cursor = 0
    for span in spans:
        if span.start > cursor:
            result.append(transform(text[cursor : span.start]))
        if span.end > cursor:
            result.append(text[max(cursor, span.start) : span.end])
            cursor = span.end
    result.append(transform(text[cursor:]))
    return "".join(result)


def protected_values(source: MutationInput) -> tuple[str, ...]:
    text = normalized_prompt(source)
    return tuple(text[span.start : span.end] for span in prompt_spans(source))


def verify_protected_values(source: MutationInput, output: str) -> None:
    for value in protected_values(source):
        if value not in output:
            raise ValueError("mutation changed or removed a protected span")
