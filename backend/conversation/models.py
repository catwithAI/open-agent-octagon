"""conversation.jsonl 事件模型。

原则：
- 默认不落 prompt 原文，只落 bytes/hash；hash 复用 wire 公共
  hashing（不自建弱化规则）；
- 事件是 append-only 的控制面边界记录，producer 原生事件另在 events.jsonl，
  两者不互相替代。
"""

from __future__ import annotations

from typing import Any

from ..adapters.base import ConversationTurn
from ..wire.hashing import raw_bytes_hash

SCHEMA_VERSION = "octagon-conversation-v1"

# 最小事件集。turn.interaction_answered 是已声明交互被应答的
# 记录点（起写入）；unexpected_interaction 走 turn.failed + error_code。
EVENT_CONVERSATION_STARTED = "conversation.started"
EVENT_CONVERSATION_COMPLETED = "conversation.completed"
EVENT_CONVERSATION_FAILED = "conversation.failed"
EVENT_TURN_STARTED = "turn.started"
EVENT_TURN_COMPLETED = "turn.completed"
EVENT_TURN_FAILED = "turn.failed"
EVENT_INTERACTION_ANSWERED = "turn.interaction_answered"

KNOWN_EVENTS = frozenset({
    EVENT_CONVERSATION_STARTED,
    EVENT_CONVERSATION_COMPLETED,
    EVENT_CONVERSATION_FAILED,
    EVENT_TURN_STARTED,
    EVENT_TURN_COMPLETED,
    EVENT_TURN_FAILED,
    EVENT_INTERACTION_ANSWERED,
})


def prompt_digest(prompt: str | None) -> dict[str, Any]:
    """prompt 的非敏感投影：只有 bytes 与 sha256，绝不含原文。"""
    if prompt is None:
        return {}
    data = prompt.encode("utf-8")
    return {
        "prompt_bytes": len(data),
        "prompt_hash": f"sha256:{raw_bytes_hash(data)}",
    }


def turn_fields(turn: ConversationTurn) -> dict[str, Any]:
    """turn record 的公共字段。"""
    return {
        "turn_id": turn.turn_id,
        "turn_index": turn.turn_index,
        "purpose": turn.purpose,
        "action": turn.action,
    }
