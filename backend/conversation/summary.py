"""conversation.jsonl reader 与 summary 投影。

fail-open：截断尾行按 partial 处理并继续；文件缺失合成 legacy 单轮摘要，
历史 attempt 无需迁移即可被 API/UI 消费。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import (
    EVENT_INTERACTION_ANSWERED,
    EVENT_TURN_COMPLETED,
    EVENT_TURN_FAILED,
    EVENT_TURN_STARTED,
)
from .writer import CONVERSATION_FILENAME


def read_conversation_events(path: Path) -> tuple[list[dict[str, Any]], bool]:
    """读取事件行，返回 (events, partial)。

    JSON 解析失败的行计 partial 并跳过（崩溃截断的尾行是主要来源；中间行
    损坏同样 fail-open——宁可少读一行，不让整个 summary 不可用）。
    """
    events: list[dict[str, Any]] = []
    partial = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            partial = True
            continue
        if isinstance(record, dict):
            events.append(record)
        else:
            partial = True
    return events, partial


def _legacy_summary() -> dict[str, Any]:
    """历史 attempt（无 conversation.jsonl）：视为一个 legacy turn。

    completed 计数不猜测——attempt 终态由 attempts 表权威记录，这里只给
    结构占位；消费方要判断完成情况应看 attempt status，不看本字段。
    """
    return {
        "is_legacy": True,
        "turn_count": 1,
        "completed_turn_count": None,
        "last_completed_turn_index": None,
        "producer_session_id": None,
        "session_continuity": "unknown",
        "score_turn_id": None,
        "partial": False,
    }


def summarize_conversation(attempt_dir: Path) -> dict[str, Any]:
    """attempt 目录 → conversation summary。"""
    path = Path(attempt_dir) / CONVERSATION_FILENAME
    if not path.is_file():
        return _legacy_summary()

    events, partial = read_conversation_events(path)

    started: dict[str, dict[str, Any]] = {}
    completed: dict[str, dict[str, Any]] = {}
    failed: dict[str, dict[str, Any]] = {}
    session_ids: list[str] = []
    turn_count_declared: int | None = None
    score_turn_id: str | None = None
    is_legacy = False

    for record in events:
        event = record.get("event")
        turn_id = record.get("turn_id")
        if event == "conversation.started":
            declared = record.get("turn_count")
            if isinstance(declared, int):
                turn_count_declared = declared
            declared_score = record.get("score_turn_id")
            if isinstance(declared_score, str) and declared_score:
                score_turn_id = declared_score
            is_legacy = bool(record.get("is_legacy", False))
        elif event == EVENT_TURN_STARTED and isinstance(turn_id, str):
            started[turn_id] = record
        elif event == EVENT_TURN_COMPLETED and isinstance(turn_id, str):
            completed[turn_id] = record
        elif event == EVENT_INTERACTION_ANSWERED and isinstance(turn_id, str):
            # 被成功应答的 answer_interaction turn 是**已完成的一轮**：
            # `turn_count` 来自 plan（含 interaction turns），只数
            # turn.completed 会让 setup+interaction+probe 的成功场景显示
            # 2/3，看起来像没跑完；interaction 若是最后一个逻辑 turn，
            # last_completed_turn_index 也会偏小。
            #
            # 它不占 send_message 序号（driver 在事件流中匹配消费，不主动
            # 发送），所以只有这一条事件、没有配对的 turn.completed。
            completed[turn_id] = record
        elif event == EVENT_TURN_FAILED and isinstance(turn_id, str):
            failed[turn_id] = record
        sid = record.get("producer_session_id")
        if isinstance(sid, str) and sid:
            session_ids.append(sid)

    unique_sessions = list(dict.fromkeys(session_ids))
    if not unique_sessions:
        session_continuity = "unknown"
    elif len(unique_sessions) == 1:
        session_continuity = "continuous"
    else:
        # 出现多个 session ID 即视为连续性破裂；分段判定在 wire 层，
        # 这里只做 summary 展示。
        session_continuity = "broken"

    completed_indexes = [
        r.get("turn_index") for r in completed.values()
        if isinstance(r.get("turn_index"), int)
    ]

    return {
        "is_legacy": is_legacy,
        "turn_count": (
            turn_count_declared
            if turn_count_declared is not None
            else len(started)
        ),
        "completed_turn_count": len(completed),
        "failed_turn_count": len(failed),
        "last_completed_turn_index": (
            max(completed_indexes) if completed_indexes else None
        ),
        "producer_session_id": unique_sessions[0] if unique_sessions else None,
        "session_continuity": session_continuity,
        # 来自 conversation.started（plan 声明）。plan 校验保证它是最后一轮：
        # 当前执行模型只在 conversation 终态后评分一次，无中间评分点。
        "score_turn_id": score_turn_id,
        "partial": partial,
    }


def conversation_turns(attempt_dir: Path) -> list[dict[str, Any]]:
    """按 turn 聚合的逐轮明细。

    每轮：turn_id/turn_index/purpose/action/producer_session_id/status/时间/
    非敏感 prompt 投影（prompt_bytes、prompt_hash）/非敏感 error 摘要。

    **绝不含 prompt 原文**——只透传 writer 已投影的 bytes/hash。缺
    conversation.jsonl 的历史 attempt 返回空列表（legacy 由 summary 表达单轮）。
    截断尾行 fail-open 跳过（partial 状态由 summary.partial 表达）。
    """
    path = Path(attempt_dir) / CONVERSATION_FILENAME
    if not path.is_file():
        return []
    events, _partial = read_conversation_events(path)

    turns: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    def _slot(turn_id: str, record: dict[str, Any]) -> dict[str, Any]:
        if turn_id not in turns:
            turns[turn_id] = {
                "turn_id": turn_id,
                "turn_index": record.get("turn_index"),
                "purpose": record.get("purpose"),
                "action": record.get("action"),
                "producer_session_id": record.get("producer_session_id"),
                "status": "started",
                "started_at": None,
                "ended_at": None,
                "prompt_bytes": None,
                "prompt_hash": None,
                "error_code": None,
                "error_summary": None,
            }
            order.append(turn_id)
        return turns[turn_id]

    for record in events:
        event = record.get("event")
        turn_id = record.get("turn_id")
        if not isinstance(turn_id, str) or not turn_id:
            continue
        slot = _slot(turn_id, record)
        ts = record.get("timestamp")
        if event == EVENT_TURN_STARTED:
            slot["started_at"] = ts
            # 只透传非敏感投影，绝不带 prompt 原文。
            if record.get("prompt_bytes") is not None:
                slot["prompt_bytes"] = record.get("prompt_bytes")
            if record.get("prompt_hash") is not None:
                slot["prompt_hash"] = record.get("prompt_hash")
        elif event == EVENT_TURN_COMPLETED:
            slot["status"] = "completed"
            slot["ended_at"] = ts
        elif event == EVENT_INTERACTION_ANSWERED:
            # 已应答的交互轮也算完成的一轮（与 summary 的口径一致）。
            slot["status"] = "interaction_answered"
            slot["ended_at"] = ts
        elif event == EVENT_TURN_FAILED:
            slot["status"] = "failed"
            slot["ended_at"] = ts
            slot["error_code"] = record.get("error_code")
            slot["error_summary"] = record.get("error_summary")

    # 按 turn_index 排序（缺失的排后，保持首次出现顺序作次级键）。
    def _sort_key(tid: str) -> tuple[int, int]:
        idx = turns[tid].get("turn_index")
        return (0, idx) if isinstance(idx, int) else (1, order.index(tid))

    return [turns[tid] for tid in sorted(order, key=_sort_key)]
