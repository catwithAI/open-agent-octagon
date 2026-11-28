"""Interaction matching and AskUserQuestion answer builders for BladeServiceAdapter."""

from __future__ import annotations

from typing import Any

#: BA 提出未预声明的提问时自动回的话术。刻意无信息量：不给 agent 任何评测
#: 线索，只把会话从 WAITING_FOR_INPUT 解出来，防止 attempt 报废/挂死。
#: blade_cli adapter 复用同一常量，保证两条通道口径一致。
UNEXPECTED_INTERACTION_AUTO_ANSWER = "你自己看着办不要问我"

#: 单次 attempt 内未声明交互自动应答的安全上限，防止 timeout_seconds 为空或很大时
#: agent 反复 AskUserQuestion 陷入无限循环。
_MAX_UNEXPECTED_AUTO_ANSWERS = 20


def match_interaction_turn(
    pending: list[Any], pause_tool_data: dict[str, Any], pause_tool: str | None
) -> Any | None:
    """按 wait_for 在待处理 answer_interaction turns 里找匹配项。

    - ``tool_name`` 对 ``pause_tool``（值带 provider 前缀，如
      ``builtin:AskUserQuestion``），大小写敏感全等；
    - ``question_key`` 非空时还需命中 ``arguments.questions[].question``
      （子串匹配，允许场景作者只写关键词）；
    匹配不到返回 None → 调用方按 unexpected interaction 处理，绝不猜答案。
    """
    if not pending:
        return None
    questions = []
    args = pause_tool_data.get("arguments")
    if isinstance(args, dict) and isinstance(args.get("questions"), list):
        questions = [
            str(q.get("question", ""))
            for q in args["questions"]
            if isinstance(q, dict)
        ]
    for turn in pending:
        wait_for = turn.wait_for
        if wait_for is None or wait_for.tool_name != pause_tool:
            continue
        key = wait_for.question_key
        if key and not any(key in q for q in questions):
            continue
        return turn
    return None


def _interaction_question_summary(
    pause_tool_data: dict[str, Any], *, limit: int = 3
) -> list[str]:
    """unexpected interaction 的问题摘要（供错误消息/排查用，非敏感）。"""
    args = pause_tool_data.get("arguments")
    if not isinstance(args, dict) or not isinstance(args.get("questions"), list):
        return []
    out: list[str] = []
    for question in args["questions"][:limit]:
        if isinstance(question, dict):
            text = str(question.get("question", "")).strip()
            if text:
                out.append(text[:200])
    return out


def build_askuser_answer(
    turn: Any, pause_tool_data: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    """把静态 answer 翻译成 blade 的 askuser_answer + 展示用答案文本。

    支持两种写法（spike 结论第 4 点）：
    - blade 原生形状 ``{"selections": {...}}`` / ``{"custom": {...}}`` 直接透传；
    - ``{"option_label": "方案A"}`` 由本函数按 label 在 pause_tool_data 里解析成
      question/option index。
    ``tool_call_id`` 永远取自运行时 pause_tool_data，不从静态 answer 读。
    """
    answer = dict(turn.answer or {})
    tool_call_id = str(pause_tool_data.get("tool_call_id") or "")

    selections = answer.get("selections")
    custom = answer.get("custom")
    text_parts: list[str] = []

    if selections is None and custom is None:
        label = answer.get("option_label")
        args = pause_tool_data.get("arguments")
        questions = (
            args.get("questions")
            if isinstance(args, dict) and isinstance(args.get("questions"), list)
            else []
        )
        resolved: dict[str, list[int]] = {}
        if isinstance(label, str) and label:
            for q_index, question in enumerate(questions):
                if not isinstance(question, dict):
                    continue
                for o_index, option in enumerate(question.get("options") or []):
                    if isinstance(option, dict) and option.get("label") == label:
                        resolved[str(q_index)] = [o_index]
                        text_parts.append(label)
                        break
                if resolved:
                    break
        if not resolved:
            # 没有可解析的选项：退化为自由文本（answer.text 或 option_label 原文）
            free_text = str(answer.get("text") or label or "")
            if not free_text:
                raise ValueError(
                    f"answer_interaction turn {turn.turn_id!r} 的 answer 无法翻译成"
                    " blade 应答：需要 selections/custom/option_label/text 之一"
                )
            custom = {"0": free_text}
            text_parts.append(free_text)
        else:
            selections = resolved

    payload: dict[str, Any] = {"tool_call_id": tool_call_id}
    payload["selections"] = selections if isinstance(selections, dict) else {}
    payload["custom"] = custom if isinstance(custom, dict) else {}

    if not text_parts:
        # 从 selections 反查 label 作为展示文本；失败则用 custom 文本
        args = pause_tool_data.get("arguments")
        questions = (
            args.get("questions")
            if isinstance(args, dict) and isinstance(args.get("questions"), list)
            else []
        )
        for q_key, o_indexes in payload["selections"].items():
            try:
                question = questions[int(q_key)]
                options = question.get("options") or []
            except (ValueError, IndexError, AttributeError, TypeError):
                continue
            for o_index in o_indexes or []:
                try:
                    text_parts.append(str(options[int(o_index)].get("label", "")))
                except (ValueError, IndexError, AttributeError, TypeError):
                    continue
        text_parts.extend(str(v) for v in payload["custom"].values() if v)

    answer_text = "；".join(p for p in text_parts if p) or "（已选择）"
    return payload, answer_text


def build_generic_askuser_answer(
    pause_tool_data: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    """未声明的 AskUserQuestion：回一句无信息的拒绝话术，不解任何选项。

    一次提问可能带多个 question，每个都答同一句拒绝文案（custom 按 question
    index 填）。不替 agent 猜答案、不泄漏任务/评测信息，只把会话从
    WAITING_FOR_INPUT 解出来。payload 形状与 build_askuser_answer 一致
    （tool_call_id 恒取运行时 pause_tool_data），server 侧同样按 custom 应答。
    """
    tool_call_id = str(pause_tool_data.get("tool_call_id") or "")
    args = pause_tool_data.get("arguments")
    questions = (
        args.get("questions")
        if isinstance(args, dict) and isinstance(args.get("questions"), list)
        else []
    )
    custom = {
        str(q_index): UNEXPECTED_INTERACTION_AUTO_ANSWER
        for q_index in range(len(questions))
    }
    if not custom:
        custom = {"0": UNEXPECTED_INTERACTION_AUTO_ANSWER}
    payload = {
        "tool_call_id": tool_call_id,
        "selections": {},
        "custom": custom,
    }
    return payload, UNEXPECTED_INTERACTION_AUTO_ANSWER
