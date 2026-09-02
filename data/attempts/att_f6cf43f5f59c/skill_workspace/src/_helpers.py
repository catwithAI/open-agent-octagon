"""Tiny shared helpers (kept import-light so every module can use them)."""

from __future__ import annotations

from typing import List


def chat_messages(rows: List[dict]) -> List[dict]:
    """Turn (role, content) rows into OpenAI-style messages, dropping blanks."""
    messages: List[dict] = []
    for row in rows:
        content = (row.get("content") or "").strip()
        role = row.get("role")
        if content and role in {"user", "assistant"}:
            messages.append({"role": role, "content": content})
    return messages