"""Small deterministic scorer shared by text-answer benchmark sample envs."""

from __future__ import annotations

import json
import re
import string
from pathlib import Path
from typing import Any


def score_answer_file(
    *,
    env_dir: Path,
    attempt_id: str,
    task: dict[str, Any],
    env_db: Path | None,
) -> list[dict[str, Any]]:
    """Score ``answer.txt`` using the env-private answer contract.

    Supported modes are ``exact`` (case/spacing/punctuation insensitive) and
    ``phrase_set`` (comma/semicolon/newline separated, order insensitive).
    """

    task_id = str(task.get("id") or task.get("task_id") or "")
    answers = json.loads((env_dir / "private" / "answers.json").read_text(encoding="utf-8"))
    spec = answers.get(task_id)
    if not isinstance(spec, dict) or not isinstance(spec.get("answer"), str):
        raise KeyError(f"no private answer contract for task {task_id!r}")

    attempt_dir = env_db.parent if env_db and env_db.parent.name == attempt_id else Path("data/attempts") / attempt_id
    answer_path = attempt_dir / "skill_workspace" / "answer.txt"
    if not answer_path.is_file():
        answer_path = attempt_dir / "answer.txt"
    try:
        actual = answer_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        actual = ""

    expected = spec["answer"]
    mode = spec.get("mode", "exact")
    if mode == "exact":
        passed = _normalize(actual) == _normalize(expected)
    elif mode == "phrase_set":
        passed = _phrase_set(actual) == _phrase_set(expected)
    else:
        raise ValueError(f"unsupported answer mode: {mode!r}")

    return [{
        "dimension": "answer_correctness",
        "value": 100 if passed else 0,
        "detail": "answer.txt matched the private reference" if passed else "answer.txt missing or did not match the private reference",
    }]


def _normalize(value: str) -> str:
    return re.sub(r"\s+", "", value).lower().translate(str.maketrans("", "", string.punctuation))


def _phrase_set(value: str) -> set[str]:
    return {_normalize(item) for item in re.split(r"[,;\n]", value) if _normalize(item)}
