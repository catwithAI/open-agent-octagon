"""conversation.jsonl writer：append-only、逐行 flush。

崩溃语义：进程在写一行中途死掉，尾行可能截断——reader（summary.py）按
partial fail-open 处理，不影响之前已完整落盘的行。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from ..adapters.base import ConversationTurn
from .models import (
    EVENT_CONVERSATION_COMPLETED,
    EVENT_CONVERSATION_FAILED,
    EVENT_CONVERSATION_STARTED,
    EVENT_INTERACTION_ANSWERED,
    EVENT_TURN_COMPLETED,
    EVENT_TURN_FAILED,
    EVENT_TURN_STARTED,
    SCHEMA_VERSION,
    prompt_digest,
    turn_fields,
)

CONVERSATION_FILENAME = "conversation.jsonl"


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class ConversationTraceWriter:
    """一个 attempt 一个 writer。所有事件带 schema_version/attempt_id/时间。

    prompt 只经 prompt_digest 投影；error_summary 由调用方保证非敏感
    （复用各 adapter 现有错误摘要通道，credential 不经过这里）。
    """

    def __init__(self, path: Path, *, attempt_id: str) -> None:
        self._path = Path(path)
        self._attempt_id = attempt_id
        self._fp: TextIO | None = None

    def _emit(self, event: str, fields: dict[str, Any]) -> None:
        record = {
            "schema_version": SCHEMA_VERSION,
            "event": event,
            "attempt_id": self._attempt_id,
            "timestamp": _now_iso(),
            **fields,
        }
        if self._fp is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._fp = self._path.open("a", encoding="utf-8")
        self._fp.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._fp.flush()

    # ---- conversation 级 ---------------------------------------------------

    def conversation_started(
        self, *, turn_count: int, is_legacy: bool, score_turn_id: str | None = None,
    ) -> None:
        """conversation 开始。

        `score_turn_id` 来自 plan（校验保证它是最后一轮）——写进 trace 后
        summary 才能投影出来；trace 自身不做评分决策，只如实记录计划。
        """
        self._emit(EVENT_CONVERSATION_STARTED, {
            "turn_count": turn_count,
            "is_legacy": is_legacy,
            "score_turn_id": score_turn_id,
        })

    def conversation_completed(self) -> None:
        self._emit(EVENT_CONVERSATION_COMPLETED, {"status": "completed"})

    def conversation_failed(
        self, *, error_code: str | None, error_summary: str | None
    ) -> None:
        self._emit(EVENT_CONVERSATION_FAILED, {
            "status": "failed",
            "error_code": error_code,
            "error_summary": error_summary,
        })

    # ---- turn 级 -----------------------------------------------------------

    def turn_started(
        self,
        turn: ConversationTurn,
        *,
        producer_session_id: str | None,
    ) -> None:
        self._emit(EVENT_TURN_STARTED, {
            **turn_fields(turn),
            "producer_session_id": producer_session_id,
            **prompt_digest(turn.prompt),
        })

    def turn_completed(
        self,
        turn: ConversationTurn,
        *,
        producer_session_id: str | None,
    ) -> None:
        self._emit(EVENT_TURN_COMPLETED, {
            **turn_fields(turn),
            "producer_session_id": producer_session_id,
            "status": "completed",
        })

    def turn_failed(
        self,
        turn: ConversationTurn,
        *,
        producer_session_id: str | None,
        error_code: str | None,
        error_summary: str | None,
    ) -> None:
        self._emit(EVENT_TURN_FAILED, {
            **turn_fields(turn),
            "producer_session_id": producer_session_id,
            "status": "failed",
            "error_code": error_code,
            "error_summary": error_summary,
        })

    def interaction_answered(
        self,
        turn: ConversationTurn,
        *,
        producer_session_id: str | None,
        tool_name: str,
    ) -> None:
        """已声明交互被应答是预期内的一步，正常记录不算失败。
        只记 tool_name，不记 answer 内容（answer 属任务定义，不属运行敏感面，
        但保持 trace 瘦身原则——需要时从 task 定义反查）。"""
        self._emit(EVENT_INTERACTION_ANSWERED, {
            **turn_fields(turn),
            "producer_session_id": producer_session_id,
            "tool_name": tool_name,
        })

    def close(self) -> None:
        if self._fp is not None:
            self._fp.close()
            self._fp = None

    def __enter__(self) -> "ConversationTraceWriter":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
