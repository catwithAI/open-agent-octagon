"""example-tool-use 的 scorer：检查便签本 env DB 里的业务状态。

skill 类 scorer 读 attempt 专属的 env DB（`env_db` 路径）判定业务结果，而不是
读工作区文件。这与 coding 类 scorer（读 workspace 产物）形成对照。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

# 任务要求 agent 至少存入这么多条便签。
_REQUIRED_NOTES = 3


def score(
    *,
    attempt_id: str,
    task: dict[str, Any],
    env_db: Path | None = None,
    trace: list | None = None,
    **_kwargs: Any,
) -> list[dict[str, Any]]:
    note_count = _count_notes(env_db)
    passed = note_count >= _REQUIRED_NOTES
    detail = (
        f"便签本有 {note_count} 条便签（要求 ≥ {_REQUIRED_NOTES}）"
    )
    return [
        {
            "dimension": "task_completion",
            "value": 100 if passed else int(100 * note_count / _REQUIRED_NOTES),
            "detail": detail,
        }
    ]


def _count_notes(env_db: Path | None) -> int:
    if not env_db or not Path(env_db).is_file():
        return 0
    try:
        conn = sqlite3.connect(str(env_db))
        try:
            row = conn.execute("SELECT COUNT(*) FROM notes").fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    except sqlite3.Error:
        return 0
