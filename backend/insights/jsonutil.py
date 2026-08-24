"""Extract a JSON object from model text that may include fences or preamble."""

from __future__ import annotations

import json
import re
from typing import Any

_TRAILING_CLAIM_ID = re.compile(
    r"\]\s*\}\s*\]\s*,\s*\"id\"\s*:\s*(\"[^\"]+\")\s*\}"
)


def repair_trailing_claim_id(text: str) -> str:
    """Move a claim id that was emitted after the object and array closed.

    Observed shape: ``...limitations:[...]} ], "id":"claim.id"}, {``
    Intended shape: ``...limitations:[...], "id":"claim.id"}, {``
    """
    return _TRAILING_CLAIM_ID.sub(r'], "id": \1}', text, count=8)


def parse_json_object(text: str) -> dict[str, Any]:
    value = (text or "").strip()
    if value.startswith("{") or value.startswith("["):
        pass
    elif "```" in value:
        start = value.find("```")
        rest = value[start + 3:]
        if rest.lstrip().lower().startswith("json"):
            rest = rest.lstrip()[4:]
            if rest.startswith("\n"):
                rest = rest[1:]
        end = rest.rfind("```")
        value = rest[:end].strip() if end >= 0 else rest.strip()
    if not value.startswith("{"):
        begin = value.find("{")
        end = value.rfind("}")
        if begin >= 0 and end > begin:
            value = value[begin:end + 1]
    if not value.startswith("{"):
        raise ValueError(
            "model output is not a JSON object; prose refusals are invalid"
        )
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = json.loads(repair_trailing_claim_id(value))
    if not isinstance(parsed, dict):
        raise ValueError("expected a JSON object")
    return parsed


def content_as_workspace_tool(text: str) -> tuple[str, dict[str, Any]] | None:
    """Treat a JSON body like {\"tool\":\"list_evidence_files\"} as a real tool call."""
    try:
        parsed = parse_json_object(text)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    name = parsed.get("tool") or parsed.get("name")
    function = parsed.get("function")
    if isinstance(function, dict) and not name:
        name = function.get("name")
    if name not in {"list_evidence_files", "read_evidence_file"}:
        return None
    arguments = parsed.get("arguments") or parsed.get("parameters") or {}
    if isinstance(function, dict) and not arguments:
        raw = function.get("arguments") or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = {}
        arguments = raw
    if not isinstance(arguments, dict):
        arguments = {}
    if "path" in parsed and "path" not in arguments:
        arguments = {**arguments, "path": parsed["path"]}
    return str(name), arguments
