"""Canonical JSON hashing and stable research-domain identifiers."""

from __future__ import annotations

import hashlib
import unicodedata
import uuid
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any, Literal

import rfc8785
from pydantic import BaseModel

EntityKind = Literal[
    "experiment",
    "variant",
    "run_group",
    "cell",
    "leader_event",
    "robustness_snapshot",
    "insight_report",
    "feedback",
    "recommendation_state",
    "normalized_output",
    "input_snapshot",
]

ID_PREFIXES: dict[EntityKind, str] = {
    "experiment": "exp",
    "variant": "var",
    "run_group": "grp",
    "cell": "cell",
    "leader_event": "ldr",
    "robustness_snapshot": "rob",
    "insight_report": "ins",
    "feedback": "fb",
    "recommendation_state": "rec",
    "normalized_output": "norm",
    "input_snapshot": "snap",
}


def _json_value(value: Any) -> Any:
    """Convert supported objects to JSON and NFC-normalize all text.

    RFC 8785 intentionally does not normalize Unicode.  Octagon does so before
    canonicalization because protected-span offsets and source lineage are
    defined over NFC text.  Normalized mapping-key collisions are rejected
    rather than silently dropping one value.
    """
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", by_alias=True, exclude_none=False)
    elif is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    elif isinstance(value, Enum):
        value = value.value

    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"canonical JSON object key must be str, got {type(key).__name__}")
            normalized_key = unicodedata.normalize("NFC", key)
            if normalized_key in normalized:
                raise ValueError(
                    f"duplicate object key after NFC normalization: {normalized_key!r}"
                )
            normalized[normalized_key] = _json_value(item)
        return normalized
    raise TypeError(f"value is not canonical-JSON compatible: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    """Return RFC 8785 bytes after Octagon's NFC normalization pass."""
    return rfc8785.dumps(_json_value(value))


def hash_bytes(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def canonical_hash(value: Any) -> str:
    return hash_bytes(canonical_json_bytes(value))


def source_task_hash(
    *,
    prompt: str,
    context: dict[str, Any],
    constraints: dict[str, Any],
    material_hashes: dict[str, str] | list[str] | tuple[str, ...] = (),
) -> str:
    """Hash every input that may affect a variant's source semantics."""
    return canonical_hash(
        {
            "prompt": prompt,
            "context": context,
            "constraints": constraints,
            "material_hashes": material_hashes,
        }
    )


def new_id(kind: EntityKind) -> str:
    """Create an opaque identifier with a stable, type-specific prefix."""
    return f"{ID_PREFIXES[kind]}_{uuid.uuid4().hex[:20]}"


def content_id(kind: EntityKind, value: Any) -> str:
    """Create a deterministic identifier for content-addressed derived data."""
    digest = canonical_hash(value).removeprefix("sha256:")
    return f"{ID_PREFIXES[kind]}_{digest[:20]}"
