"""Size-aware storage for derived JSON payloads."""

from __future__ import annotations

import os
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .experiments.hashing import canonical_hash, canonical_json_bytes


@dataclass(frozen=True)
class StoredPayload:
    content_hash: str
    inline_json: str | None
    object_ref: str | None


@dataclass
class DerivedPayloadStore:
    data_path: Path
    inline_threshold_bytes: int = 64 * 1024

    def put(self, kind: str, payload: Any) -> StoredPayload:
        encoded = canonical_json_bytes(payload)
        digest = canonical_hash(payload)
        if len(encoded) <= self.inline_threshold_bytes:
            return StoredPayload(digest, encoded.decode("utf-8"), None)
        safe_kind = kind.replace("/", "_").replace("..", "_")
        relative = Path("derived") / safe_kind / f"{digest.removeprefix('sha256:')}.json"
        destination = Path(self.data_path) / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_bytes(encoded)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return StoredPayload(digest, None, relative.as_posix())

    def get(self, index: dict[str, Any]) -> Any:
        inline = index.get("inline")
        object_ref = index.get("object_ref")
        if (inline is None) == (object_ref is None):
            raise ValueError("derived payload index must select exactly one storage form")
        if inline is not None:
            payload = json.loads(inline)
        else:
            relative = Path(str(object_ref))
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("invalid derived payload reference")
            root = Path(self.data_path).resolve()
            path = (root / relative).resolve()
            try:
                path.relative_to(root)
            except ValueError as exc:
                raise ValueError("derived payload reference escapes data root") from exc
            if path.is_symlink() or not path.is_file():
                raise ValueError("derived payload object is missing")
            payload = json.loads(path.read_text(encoding="utf-8"))
        if canonical_hash(payload) != index.get("content_hash"):
            raise ValueError("derived payload content hash mismatch")
        return payload
