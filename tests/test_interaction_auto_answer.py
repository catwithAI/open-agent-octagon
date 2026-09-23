"""未声明提问的自动应答：SDK 通道的应答构造与配置默认值。

端到端（桩 blade server 的 SDK run）不在单测范围；这里锁定两件事——
1) `build_generic_askuser_answer` 构造的应答形状与既有
   `build_askuser_answer` 一致（tool_call_id + selections + custom），
   每个 question index 都答同一句拒绝话术、不解任何选项；
2) 配置开关默认打开、能从 Settings 一路传到 adapter config。
"""

from __future__ import annotations

from pathlib import Path

from backend.adapters.blade_service import (
    UNEXPECTED_INTERACTION_AUTO_ANSWER,
    BladeAdapterConfig,
    build_generic_askuser_answer,
)

_DUMMY_PAUSE = {
    "name": "builtin:AskUserQuestion",
    "arguments": {
        "description": "确认需求",
        "questions": [
            {"question": "未定位文章怎么处理？", "options": [{"label": "显示在未定位列表"}]},
            {"question": "要不要过滤国内新闻？", "options": [{"label": "要"}, {"label": "不要"}]},
        ],
    },
    "tool_call_id": "call_auto_1",
}


def test_generic_answer_answers_every_question_with_refusal() -> None:
    payload, text = build_generic_askuser_answer(_DUMMY_PAUSE)

    assert text == UNEXPECTED_INTERACTION_AUTO_ANSWER
    # 形状与 build_askuser_answer 一致：tool_call_id 取自运行时 pause 数据。
    assert payload["tool_call_id"] == "call_auto_1"
    assert payload["selections"] == {}
    # 每个 question index 都答同一句拒绝话术，不挑选项。
    assert payload["custom"] == {
        "0": UNEXPECTED_INTERACTION_AUTO_ANSWER,
        "1": UNEXPECTED_INTERACTION_AUTO_ANSWER,
    }


def test_generic_answer_degrades_without_question_list() -> None:
    payload, text = build_generic_askuser_answer({"tool_call_id": "x"})
    assert text == UNEXPECTED_INTERACTION_AUTO_ANSWER
    assert payload["custom"] == {"0": UNEXPECTED_INTERACTION_AUTO_ANSWER}


def test_config_defaults_to_on_and_reaches_adapter() -> None:
    """BladeAdapterConfig 默认开；Settings 同名开关经 _build_blade_config 透传。"""
    assert BladeAdapterConfig(
        base_url="http://blade.test", skills_path=Path("/tmp/s")
    ).answer_unexpected_interaction is True

    from backend.config import Settings
    from backend.run_dispatch import _build_blade_config

    # 默认 Settings → 开关默认开。
    settings = Settings()
    config = _build_blade_config(settings, model="ba-pro")
    assert config.answer_unexpected_interaction is True

    # 关掉后也能透传。
    settings.blade.answer_unexpected_interaction = False
    config = _build_blade_config(settings, model="ba-pro")
    assert config.answer_unexpected_interaction is False
