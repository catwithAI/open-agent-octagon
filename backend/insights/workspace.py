"""Frozen evidence workspace for Agent-style judges.

Product / Process / Association judges inspect files; they do not receive a
serialized runtrace inside the prompt.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.experiments.hashing import canonical_hash

DEFAULT_READ_LIMIT = 20_000
WORKSPACE_TOOL_NAMES = ("list_evidence_files", "read_evidence_file")

OPENAI_WORKSPACE_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_evidence_files",
            "description": (
                "List frozen evidence files in this workspace. "
                "Use this before reading; do not assume unread files."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_evidence_file",
            "description": (
                "Read one frozen evidence file by relative path. "
                "Use offset/limit for large files; never invent unread content."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Character offset into the UTF-8 text.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 80_000,
                        "description": "Maximum characters to return.",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
]


def _norm(relative: str) -> str:
    value = str(relative or "").replace("\\", "/").lstrip("/")
    parts = [part for part in value.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"invalid evidence path: {relative}")
    return "/".join(parts)


@dataclass(frozen=True)
class EvidenceWorkspace:
    """Read-only map of relative evidence paths to local files."""

    files: dict[str, Path]
    descriptions: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized = {_norm(key): Path(value) for key, value in self.files.items()}
        object.__setattr__(self, "files", normalized)
        object.__setattr__(
            self,
            "descriptions",
            {_norm(key): str(value) for key, value in self.descriptions.items()},
        )

    @classmethod
    def from_dir(cls, root: Path, *, prefix: str = "evidence") -> EvidenceWorkspace:
        files: dict[str, Path] = {}
        if root.is_dir():
            for path in sorted(root.rglob("*")):
                if path.is_file() and not path.name.startswith("."):
                    relative = path.relative_to(root).as_posix()
                    files[f"{prefix.rstrip('/')}/{relative}"] = path
        return cls(files)

    def catalog(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for relative, path in sorted(self.files.items()):
            data = path.read_bytes()
            items.append({
                "path": relative,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest()[:16],
                "description": self.descriptions.get(relative, ""),
            })
        return items

    def digest(self) -> str:
        payload = {
            relative: hashlib.sha256(path.read_bytes()).hexdigest()
            for relative, path in sorted(self.files.items())
        }
        return canonical_hash(payload)

    def resolve(self, relative: str) -> Path:
        key = _norm(relative)
        path = self.files.get(key)
        if path is None:
            raise FileNotFoundError(f"evidence file is not in workspace: {relative}")
        return path

    def read(
        self,
        relative: str,
        *,
        offset: int | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        text = self.resolve(relative).read_text(encoding="utf-8")
        start = max(int(offset or 0), 0)
        width = int(limit or DEFAULT_READ_LIMIT)
        chunk = text[start:start + width]
        return {
            "path": _norm(relative),
            "offset": start,
            "limit": width,
            "total_chars": len(text),
            "truncated": start + width < len(text),
            "content": chunk,
        }

    def dispatch(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        args = arguments or {}
        if name == "list_evidence_files":
            return json.dumps({"files": self.catalog()}, ensure_ascii=False)
        if name == "read_evidence_file":
            try:
                payload = self.read(
                    str(args.get("path") or ""),
                    offset=args.get("offset"),
                    limit=args.get("limit"),
                )
            except (OSError, ValueError, FileNotFoundError) as exc:
                return json.dumps({"error": str(exc)}, ensure_ascii=False)
            return json.dumps(payload, ensure_ascii=False)
        return json.dumps({"error": f"unknown tool: {name}"}, ensure_ascii=False)

    def prompt_catalog(self) -> str:
        return json.dumps({"files": self.catalog()}, ensure_ascii=False, sort_keys=True)
