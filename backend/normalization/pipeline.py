"""Low-risk deterministic normalization with protected-span verification."""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any

from backend.db import _open_sync
from backend.experiments.hashing import canonical_hash, hash_bytes
from backend.models import TERMINAL_ATTEMPT_STATUSES

from .models import NormalizedDocument, ProtectedSpanRecord


PIPELINE_VERSION = "octagon-normalization-pipeline-v1"
PIPELINE_HASH = canonical_hash(
    {
        "version": PIPELINE_VERSION,
        "transforms": ["unicode-nfc", "line-endings-lf", "trim-line-tail", "blank-lines-max-2"],
        "protected": ["number", "code", "command", "refusal", "tool_params"],
    }
)

_PATTERNS = (
    ("code", re.compile(r"```[\s\S]*?```|`[^`\n]+`")),
    ("command", re.compile(r"(?m)^(?:\$\s*|sudo\s+|(?:bash|sh|python|python3)\s+).+$")),
    (
        "refusal",
        re.compile(
            r"(?im)^.*(?:I (?:cannot|can't|won't)|我不能|无法协助|拒绝执行).*$"
        ),
    ),
    ("number", re.compile(r"(?<![\w])[-+]?\d+(?:[.,]\d+)*(?:%|ms|s|kg|MB|GB)?")),
)


def _pointer(parts: tuple[str, ...]) -> str:
    if not parts:
        return "/"
    return "/" + "/".join(part.replace("~", "~0").replace("/", "~1") for part in parts)


def _intervals(text: str) -> list[tuple[int, int, str]]:
    candidates: list[tuple[int, int, str, int]] = []
    for priority, (kind, pattern) in enumerate(_PATTERNS):
        for match in pattern.finditer(text):
            candidates.append((match.start(), match.end(), kind, priority))
    selected: list[tuple[int, int, str]] = []
    for start, end, kind, priority in sorted(
        candidates, key=lambda item: (item[0], item[3], -(item[1] - item[0]))
    ):
        if any(start < other_end and end > other_start for other_start, other_end, _ in selected):
            continue
        selected.append((start, end, kind))
    return sorted(selected)


def _normalize_unprotected(text: str) -> str:
    value = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t]+(?=\n)", "", value)
    return re.sub(r"\n{3,}", "\n\n", value)


def _normalize_string(
    text: str, path: tuple[str, ...]
) -> tuple[str, list[ProtectedSpanRecord], int]:
    intervals = _intervals(text)
    output: list[str] = []
    spans: list[ProtectedSpanRecord] = []
    cursor = 0
    transforms = 0
    output_size = 0
    for start, end, kind in intervals:
        before = text[cursor:start]
        normalized = _normalize_unprotected(before)
        transforms += normalized != before
        output.append(normalized)
        output_size += len(normalized)
        protected = text[start:end]
        output.append(protected)
        spans.append(
            ProtectedSpanRecord(
                field_path=_pointer(path),
                start=output_size,
                end=output_size + len(protected),
                kind=kind,
                content_hash=hash_bytes(protected.encode("utf-8")),
            )
        )
        output_size += len(protected)
        cursor = end
    tail = text[cursor:]
    normalized_tail = _normalize_unprotected(tail)
    transforms += normalized_tail != tail
    output.append(normalized_tail)
    return "".join(output), spans, transforms


def _normalize_value(
    value: Any, path: tuple[str, ...] = ()
) -> tuple[Any, list[ProtectedSpanRecord], int]:
    if isinstance(value, str):
        return _normalize_string(value, path)
    if isinstance(value, list):
        result = []
        spans: list[ProtectedSpanRecord] = []
        transforms = 0
        for index, item in enumerate(value):
            normalized, child_spans, count = _normalize_value(item, (*path, str(index)))
            result.append(normalized)
            spans.extend(child_spans)
            transforms += count
        return result, spans, transforms
    if isinstance(value, dict):
        result = {}
        spans = []
        transforms = 0
        for key in sorted(value):
            normalized, child_spans, count = _normalize_value(value[key], (*path, str(key)))
            result[key] = normalized
            spans.extend(child_spans)
            transforms += count
        return result, spans, transforms
    return value, [], 0


def _at_pointer(value: Any, pointer: str) -> Any:
    current = value
    if pointer == "/":
        return current
    for raw in pointer.lstrip("/").split("/"):
        part = raw.replace("~1", "/").replace("~0", "~")
        current = current[int(part)] if isinstance(current, list) else current[part]
    return current


def _tool_param_spans(data_path: Path, attempt_id: str) -> list[ProtectedSpanRecord]:
    path = data_path / "attempts" / attempt_id / "trace.jsonl"
    if not path.is_file():
        return []
    result: list[ProtectedSpanRecord] = []
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        for key in ("arguments", "params", "parameters", "input"):
            if key not in record:
                continue
            encoded = json.dumps(record[key], ensure_ascii=False, sort_keys=True).encode("utf-8")
            result.append(
                ProtectedSpanRecord(
                    field_path=f"trace:{index}/{key}",
                    start=0,
                    end=max(1, len(encoded)),
                    kind="tool_params",
                    content_hash=hash_bytes(encoded),
                )
            )
    return result


def normalize_attempt(*, db_path: Path, data_path: Path, attempt_id: str) -> NormalizedDocument:
    with _open_sync(Path(db_path)) as conn:
        attempt = conn.execute(
            "SELECT status FROM attempts WHERE id=?", (attempt_id,)
        ).fetchone()
    if attempt is None:
        raise ValueError("attempt not found")
    if attempt[0] not in TERMINAL_ATTEMPT_STATUSES:
        raise ValueError("normalized output source is not finalized")
    source = Path(data_path) / "attempts" / attempt_id / "final_state.json"
    if not source.is_file() or source.is_symlink():
        raise ValueError("normalized output source is unavailable")
    raw = source.read_bytes()
    try:
        parsed = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("normalized output source is invalid JSON") from exc
    normalized, spans, transform_count = _normalize_value(parsed)
    # Verify every output span against the source hash captured before transform.
    for span in spans:
        value = _at_pointer(normalized, span.field_path)
        if not isinstance(value, str):
            raise ValueError("protected span verification failed")
        protected = value[span.start : span.end]
        if hash_bytes(protected.encode("utf-8")) != span.content_hash:
            raise ValueError("protected span verification failed")
    spans.extend(_tool_param_spans(Path(data_path), attempt_id))
    source_hash = hash_bytes(raw)
    return NormalizedDocument(
        schema_version="octagon-normalized-output-v1",
        attempt_id=attempt_id,
        source_hash=source_hash,
        pipeline_hash=PIPELINE_HASH,
        content=normalized,
        transform_manifest={
            "pipeline_version": PIPELINE_VERSION,
            "transform_count": transform_count,
            "source_ref": "final_state.json",
            "raw_payload_modified": False,
        },
        protected_spans=tuple(spans),
        annotations=(
            {
                "kind": "derived-output",
                "message": "Whitespace normalization only; use raw evidence for scoring.",
            },
        ),
    )
