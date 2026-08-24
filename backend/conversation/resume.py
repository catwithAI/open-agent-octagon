"""多轮 attempt 的恢复决策。

纯函数：输入 checkpoint + 当前 plan，输出"能不能恢复、从哪一轮续"。不碰
网络/DB，便于对每条分支单测。

核心原则——**turn.completed 是唯一权威**：只有它证明某轮已发送且完成，才
可以在恢复时跳过。崩溃时正在飞的那一轮（active_turn_index 非空）状态未知，
默认不重发（可能已在服务端产生外部副作用），交由调用方查询 producer 判定。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..adapters.base import ConversationTurn
from .plan import plan_hash


@dataclass(frozen=True)
class ResumeDecision:
    """恢复判定结果。

    - `can_resume=False`：拒绝恢复，`reason` 说明原因，调用方按不可恢复处理
      （标 interrupted，不重放）；
    - `can_resume=True`：从 `resume_after_turn_index` 之后的轮次继续；
      该值为 None 表示一轮都没完成，从头开始。
    - `active_turn_unknown`：崩溃时有轮在飞，其是否产生副作用无从判断——
      调用方必须先向 producer 查证，不得直接重发。
    """

    can_resume: bool
    resume_after_turn_index: int | None = None
    active_turn_unknown: bool = False
    reason: str | None = None


def decide_resume(
    *,
    checkpoint: dict[str, Any],
    turns: tuple[ConversationTurn, ...],
) -> ResumeDecision:
    """按 checkpoint 判断多轮 attempt 能否恢复。

    单轮/legacy attempt（checkpoint 无 conversation_plan_hash）返回
    `can_resume=False, reason="legacy_single_turn"`——它们走原有的
    `recover_existing` 重新附着路径，不适用轮次级恢复。
    """
    stored_hash = checkpoint.get("conversation_plan_hash")
    if not stored_hash:
        return ResumeDecision(
            can_resume=False, reason="legacy_single_turn",
        )

    if not checkpoint.get("blade_session_id"):
        return ResumeDecision(can_resume=False, reason="session_id_missing")

    # plan 变了就拒绝：把新计划的轮次接到旧 session 的历史上会产出无法解释
    # 的混合结果。
    if stored_hash != plan_hash(turns):
        return ResumeDecision(can_resume=False, reason="plan_hash_mismatch")

    stored_count = checkpoint.get("conversation_turn_count")
    if isinstance(stored_count, int) and stored_count != len(turns):
        return ResumeDecision(can_resume=False, reason="turn_count_mismatch")

    last_completed = checkpoint.get("last_completed_turn_index")
    if last_completed is not None and not isinstance(last_completed, int):
        return ResumeDecision(can_resume=False, reason="checkpoint_corrupt")
    if isinstance(last_completed, int) and not (
        0 <= last_completed < len(turns)
    ):
        return ResumeDecision(can_resume=False, reason="checkpoint_out_of_range")

    active = checkpoint.get("active_turn_index")
    if active is not None and not isinstance(active, int):
        return ResumeDecision(can_resume=False, reason="checkpoint_corrupt")

    return ResumeDecision(
        can_resume=True,
        resume_after_turn_index=last_completed,
        # active 非空 = 崩溃时该轮在飞，完成与否未知（只有 producer
        # 能证明它没执行才允许重发）。
        active_turn_unknown=active is not None,
    )
