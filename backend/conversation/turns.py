"""三个 adapter 共用的轮次 helper。

**只提取真正同构的部分**，不强行统一不同 transport：blade 走 SDK 的
socket/REST、CC 与 Codex 各自 spawn CLI 子进程，它们的 session 建立、
事件消费、失败语义都不一样，硬套一个 Driver 基类只会让每个 adapter 都塞满
特例分支。这里放的是三家逐字节相同的纯函数。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..adapters.base import AdapterRunInput

RECOVERY_FILENAME = "recovery.json"

# events.jsonl 的 turn 归属扩展键。namespaced 前缀避免与 producer
# 原生字段冲突；写在**行级**而不是 raw 里，producer 字段原义不变。
TURN_ID_KEY = "x-octagon.turn-id"
TURN_INDEX_KEY = "x-octagon.turn-index"


def with_turn_ext(
    row: dict[str, Any], turn_id: str | None, turn_index: int | None
) -> dict[str, Any]:
    """给 events.jsonl 行附加 turn 归属。

    `turn_id=None`（单轮 legacy attempt）时原样返回——历史文件形状零变化，
    旧 reader 不受影响。原地修改并返回同一 dict，调用点可直接内联。
    """
    if turn_id is None:
        return row
    row[TURN_ID_KEY] = turn_id
    if turn_index is not None:
        row[TURN_INDEX_KEY] = turn_index
    return row


def write_checkpoint(attempt_dir: Path, data: dict[str, Any]) -> None:
    """原子写 recovery.json。

    进程崩溃时必须有无敏感信息的检查点，启动恢复才能找回 producer session。
    写临时文件再 rename——半截 JSON 会让恢复路径读到损坏数据。
    """
    path = Path(attempt_dir) / RECOVERY_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8",
    )
    tmp.replace(path)


def render_turn_prompt(
    task: AdapterRunInput, turn: Any, *, base_prompt: str
) -> str:
    """某一轮实际发给 producer 的 prompt（CC / Codex 共用）。

    首轮用 adapter 已渲染好的完整 prompt（含时间预算与 task context），
    后续轮只发该轮 prompt 原文——context 已在同一 session/thread 里，重复
    发送是噪音，重复宣称"本任务限时 X"还会误导 agent。legacy 单轮因此与
    多轮改造前逐字节一致。

    blade adapter 不用这个函数：它的 prompt 渲染在自己的 `_render_prompt`
    里完成，首轮替换逻辑也不同（见 `blade_service._render_turn_prompt`）。
    """
    if turn.turn_index == 0:
        if turn.prompt is None or turn.prompt == task.task_prompt:
            return base_prompt
        # 多轮首轮自带 prompt：替换任务消息主体，其余渲染方式不变。
        return base_prompt.replace(task.task_prompt, turn.prompt, 1)
    return turn.prompt or ""
