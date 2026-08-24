"""ConversationPlan：多轮输入的校验、legacy 映射与执行视图。

职责边界：
- 只做结构与静态校验，不做 adapter 渲染——time budget notice / task context
  的注入仍由各 adapter 在首轮（turn_index==0 的 send_message）完成；
- `answer_interaction` 轮不参与顺序发送，作为 pending 集合供 driver 在事件流
  中按 wait_for 匹配消费；
- 任务定义层的拒绝在 parse 时抛 ConversationPlanError，API 入口转 400，
  dispatch 侧同一异常兜底为 attempt 失败。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, get_args

from ..adapters.base import (
    AdapterRunInput,
    ConversationTurn,
    InteractionWaitFor,
    TurnAction,
    TurnPurpose,
)

# task_context 里承载 conversation 数组的保留 key。下划线前缀已有既定语义：
# prompt_context() 不会把它渲染进给 agent 看的上下文（不改请求 schema）。
CONVERSATION_CONTEXT_KEY = "_conversation"

_VALID_PURPOSES = frozenset(get_args(TurnPurpose))
_VALID_ACTIONS = frozenset(get_args(TurnAction))


class ConversationPlanError(ValueError):
    """任务定义非法：空数组、重复/断裂 ID、评分轮冲突、字段互斥违规。"""


def default_turn_id(task_id: str, turn_index: int) -> str:
    """缺省 turn_id：确定性派生（恢复/重建可复算），不用随机 UUID。"""
    return f"{task_id}::t{turn_index}"


def legacy_turn(task_id: str, task_prompt: str) -> ConversationTurn:
    """历史单轮映射：task_prompt 原样为一轮 task，评分在其后。"""
    return ConversationTurn(
        turn_id=default_turn_id(task_id, 0),
        turn_index=0,
        action="send_message",
        purpose="task",
        prompt=task_prompt,
        score_after=True,
    )


def _parse_wait_for(raw: Any, *, where: str) -> InteractionWaitFor:
    if not isinstance(raw, dict):
        raise ConversationPlanError(f"{where}: wait_for 必须是对象")
    tool_name = raw.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name.strip():
        raise ConversationPlanError(f"{where}: wait_for.tool_name 必须是非空字符串")
    question_key = raw.get("question_key")
    if question_key is not None and not isinstance(question_key, str):
        raise ConversationPlanError(f"{where}: wait_for.question_key 必须是字符串")
    unknown = set(raw) - {"tool_name", "question_key"}
    if unknown:
        raise ConversationPlanError(f"{where}: wait_for 含未知字段 {sorted(unknown)}")
    return InteractionWaitFor(tool_name=tool_name.strip(), question_key=question_key)


def parse_conversation(
    raw: Any, *, task_id: str
) -> tuple[ConversationTurn, ...]:
    """把 API/task context 的 conversation 数组解析为 ConversationTurn 序列。

    entry 支持 `id`（场景作者视角）或 `turn_id`；缺省按
    位置确定性生成。turn_index 由位置派生；entry 显式给出时必须与位置一致
    （等价于"不连续 turn_index"拒绝）。
    """
    if not isinstance(raw, list):
        raise ConversationPlanError("conversation 必须是数组")
    if not raw:
        raise ConversationPlanError("conversation 数组不能为空")

    turns: list[ConversationTurn] = []
    for index, entry in enumerate(raw):
        where = f"conversation[{index}]"
        if not isinstance(entry, dict):
            raise ConversationPlanError(f"{where}: 每轮必须是对象")

        turn_id = entry.get("turn_id") or entry.get("id")
        if turn_id is None:
            turn_id = default_turn_id(task_id, index)
        if not isinstance(turn_id, str) or not turn_id.strip():
            raise ConversationPlanError(f"{where}: id/turn_id 必须是非空字符串")

        explicit_index = entry.get("turn_index")
        if explicit_index is not None and explicit_index != index:
            raise ConversationPlanError(
                f"{where}: turn_index={explicit_index!r} 与位置 {index} 不一致"
            )

        action = entry.get("action", "send_message")
        purpose = entry.get("purpose")
        if purpose is None:
            purpose = "interaction" if action == "answer_interaction" else "task"

        wait_for_raw = entry.get("wait_for")
        turns.append(ConversationTurn(
            turn_id=turn_id.strip(),
            turn_index=index,
            action=action,
            purpose=purpose,
            score_after=bool(entry.get("score_after", False)),
            prompt=entry.get("prompt"),
            wait_for=(
                _parse_wait_for(wait_for_raw, where=where)
                if wait_for_raw is not None
                else None
            ),
            answer=entry.get("answer"),
        ))

    validate_turns(tuple(turns))
    return tuple(turns)


def validate_turns(turns: tuple[ConversationTurn, ...]) -> None:
    """全量静态校验。dataclass 层不做校验，统一收口在这里。"""
    if not turns:
        raise ConversationPlanError("conversation 不能为空")

    seen_ids: set[str] = set()
    score_turns: list[str] = []
    for expected_index, turn in enumerate(turns):
        where = f"turn {turn.turn_id!r}"

        if turn.turn_index != expected_index:
            raise ConversationPlanError(
                f"{where}: turn_index={turn.turn_index} 断裂，期望 {expected_index}"
            )
        if turn.turn_id in seen_ids:
            raise ConversationPlanError(f"重复的 turn_id: {turn.turn_id!r}")
        seen_ids.add(turn.turn_id)

        if turn.action not in _VALID_ACTIONS:
            raise ConversationPlanError(
                f"{where}: 未知 action {turn.action!r}，可选 {sorted(_VALID_ACTIONS)}"
            )
        if turn.purpose not in _VALID_PURPOSES:
            raise ConversationPlanError(
                f"{where}: 未知 purpose {turn.purpose!r}，可选 {sorted(_VALID_PURPOSES)}"
            )

        if turn.action == "send_message":
            if not isinstance(turn.prompt, str) or not turn.prompt.strip():
                raise ConversationPlanError(f"{where}: send_message 轮必须有非空 prompt")
            if turn.wait_for is not None or turn.answer is not None:
                raise ConversationPlanError(
                    f"{where}: send_message 轮不得携带 wait_for/answer"
                )
        else:  # answer_interaction
            if turn.turn_index == 0:
                raise ConversationPlanError(
                    f"{where}: 首轮不能是 answer_interaction——此时 session 内"
                    "还不存在任何可应答的交互请求"
                )
            if turn.prompt is not None:
                raise ConversationPlanError(
                    f"{where}: answer_interaction 轮不得携带 prompt"
                )
            if turn.wait_for is None:
                raise ConversationPlanError(f"{where}: answer_interaction 轮缺少 wait_for")
            if not isinstance(turn.answer, dict) or not turn.answer:
                raise ConversationPlanError(
                    f"{where}: answer_interaction 轮的 answer 必须是非空对象"
                )

        if turn.score_after:
            score_turns.append(turn.turn_id)

    if len(score_turns) > 1:
        raise ConversationPlanError(
            f"多个互相冲突的最终评分轮次: {score_turns}（score_after=true 至多一轮）"
        )

    # score_after 必须落在**最后一个可执行 turn** 上。
    #
    # 当前执行模型没有"中间评分点"：adapter 跑完全部 send_message turns，
    # runner 在 conversation 终态后统一评分一次。若允许
    # `t0(score_after) → t1`，实际评分发生在 t1 之后而不是 t0——声明与行为
    # 不符，且 scorer 会看到本不该看到的后续轮产物。
    #
    # 要支持中间评分点，需要 driver 在该边界停下或触发一次评分，并重新定义
    # scorer 契约（现有契约建立在"评分时产物已最终"上）。在那之前，校验期
    # 拒绝比运行期静默偏移安全。
    if score_turns:
        last_executable = turns[-1]
        if score_turns[0] != last_executable.turn_id:
            raise ConversationPlanError(
                f"score_after 必须位于最后一轮（{last_executable.turn_id!r}），"
                f"实际在 {score_turns[0]!r}——当前执行模型只在 conversation "
                "终态后评分一次，中间评分点尚不支持"
            )


def plan_hash(turns: tuple[ConversationTurn, ...]) -> str:
    """conversation plan 的确定性指纹。

    恢复时用它确认"要恢复的计划"与"当初执行的计划"是同一个：任务定义被改过
    （轮次增删、prompt 改写、answer 改写）就拒绝恢复，避免把新计划的轮次接到
    旧 session 的历史上。只覆盖影响执行的字段，不含运行时值。
    """
    payload = [
        {
            "turn_id": t.turn_id,
            "turn_index": t.turn_index,
            "action": t.action,
            "purpose": t.purpose,
            "score_after": t.score_after,
            "prompt": t.prompt,
            "wait_for": (
                None if t.wait_for is None
                else {
                    "tool_name": t.wait_for.tool_name,
                    "question_key": t.wait_for.question_key,
                }
            ),
            "answer": t.answer,
        }
        for t in turns
    ]
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ConversationPlan:
    """校验通过的执行视图。turns 顺序即声明顺序。"""

    turns: tuple[ConversationTurn, ...]
    is_legacy: bool

    @property
    def plan_hash(self) -> str:
        return plan_hash(self.turns)

    @property
    def send_message_turns(self) -> tuple[ConversationTurn, ...]:
        """主循环顺序发送的轮次。"""
        return tuple(t for t in self.turns if t.action == "send_message")

    @property
    def interaction_turns(self) -> tuple[ConversationTurn, ...]:
        """pending 集合：driver 在事件流中按 wait_for 匹配消费，不主动发送。"""
        return tuple(t for t in self.turns if t.action == "answer_interaction")

    @property
    def requires_interaction_answer(self) -> bool:
        """capability gate 的判定输入。"""
        return any(t.action == "answer_interaction" for t in self.turns)

    @property
    def score_turn(self) -> ConversationTurn:
        """最终评分轮：显式 score_after=true 的一轮，否则最后一轮。"""
        for turn in self.turns:
            if turn.score_after:
                return turn
        return self.turns[-1]


def effective_conversation(task: AdapterRunInput) -> ConversationPlan:
    """三个 adapter 共用入口。

    有 conversation_turns → 校验后返回；无 → legacy 单轮（与现状等价：
    adapter 对首轮仍做自己的 prompt 渲染，notice/context 只注入首轮）。
    """
    if task.conversation_turns:
        validate_turns(task.conversation_turns)
        return ConversationPlan(turns=task.conversation_turns, is_legacy=False)
    return ConversationPlan(
        turns=(legacy_turn(task.task_id, task.task_prompt),),
        is_legacy=True,
    )


def conversation_turns_from_context(
    *, task_id: str, task_context: dict[str, Any]
) -> tuple[ConversationTurn, ...]:
    """dispatch 消费点：task_context 里有 `_conversation` 时解析，否则空 tuple。"""
    raw = (task_context or {}).get(CONVERSATION_CONTEXT_KEY)
    if raw is None:
        return ()
    return parse_conversation(raw, task_id=task_id)
