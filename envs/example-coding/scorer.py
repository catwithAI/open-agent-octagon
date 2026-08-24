"""example-coding 的确定性 scorer：精确匹配 answer.txt 与参考答案。

scorer 契约：导出 `score(*, attempt_id, task, env_db, ...)`，返回
`[{dimension, value, detail}]`，dimension 必须与 meta.yaml 对齐。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# 每个任务 id → 参考答案。示例场景把答案直接放这里；正式场景通常放在
# agent 看不到的 private/ 目录下（见 gaia 样例）。
_REFERENCE_ANSWERS: dict[str, str] = {
    "sum_of_squares": "385",
}


def score(
    *,
    attempt_id: str,
    task: dict[str, Any],
    env_db: Path | None = None,
    **_kwargs: Any,
) -> list[dict[str, Any]]:
    task_id = str(task.get("id") or task.get("task_id") or "")
    reference = _REFERENCE_ANSWERS.get(task_id)
    if reference is None:
        raise KeyError(f"no reference answer for task {task_id!r}")

    answer_path = _attempt_dir(attempt_id, env_db) / "skill_workspace" / "answer.txt"
    try:
        model_answer = answer_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        model_answer = ""

    passed = model_answer == reference
    detail = (
        "answer.txt matched the reference answer"
        if passed
        else f"answer.txt = {model_answer!r}, expected {reference!r}"
    )
    return [
        {
            "dimension": "answer_correctness",
            "value": 100 if passed else 0,
            "detail": detail,
        }
    ]


def _attempt_dir(attempt_id: str, env_db: Path | None) -> Path:
    if env_db and env_db.parent.name == attempt_id:
        return env_db.parent
    return Path("data/attempts") / attempt_id
