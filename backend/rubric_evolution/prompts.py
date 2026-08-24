from __future__ import annotations

import json
from typing import Any

from .models import EvolutionBatch

RUBRIC_EVOLUTION_PROMPT_VERSION = "octagon-rubric-evolution-prompt-v2"


def _encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def rubric_evolution_prompt(
    *,
    batch: EvolutionBatch,
    current_rubric: dict[str, Any],
    evidence_catalog: dict[str, Any] | None = None,
) -> str:
    return (
        "你在执行生产 Rubric Evolution Loop。核心分析对象是当前 Rubric，而不是为某个"
        "Agent 的动作模式定义奖励。Action Analysis 只解释 Agent 做了什么；你要分析现有"
        "评分标准是否覆盖不足、边界不清、重复或失真。你可以按需读取工作区中的冻结"
        "Runtrace，但不能把某次行为模式直接升级为奖励标准。Judge 不知道本 Loop 的存在，"
        "也不参与进化。\n"
        "冻结证据已写入工作区文件。先列出文件，再读取需要的片段；不要把未读文件"
        "当成已知事实，也不要把整份轨迹塞进结论。本批次已离线冻结；不要请求新运行、"
        "不要修改正式 Rubric、不要发布版本。允许正常返回 no_change 或 "
        "insufficient_evidence，不得为了进化而强制改动。\n"
        "若生成候选，必须输出一份完整的规范化 candidate_rubric；每个 check 的 criteria"
        " 必须同时定义 pass、partial、fail、not_applicable、unknown。Unknown 不是 Fail。"
        "只返回 JSON。\n"
        "输出骨架："
        '{"schema_version":"octagon-rubric-evolution-result-v1",'
        '"result":"candidate_generated|no_change|insufficient_evidence",'
        '"summary":"...","diagnoses":[],"changes":[],"candidate_rubric":null}\n'
        "candidate_rubric 骨架："
        '{"schema_version":"octagon-evolved-rubric-v1","rubric_id":"...",'
        '"parent_version":"...","proposed_version":"...",'
        '"scope":"environment|cross_environment","env_name":null,"checks":['
        '{"check_id":"...","title":"...","description":"...","weight":1,'
        '"evidence_requirements":[],"criteria":{"pass":"...","partial":"...",'
        '"fail":"...","not_applicable":"...","unknown":"..."}}]}\n'
        f"Prompt version: {RUBRIC_EVOLUTION_PROMPT_VERSION}\n"
        f"Frozen batch: {_encoded(batch.model_dump(mode='json'))}\n"
        f"Current rubric: {_encoded(current_rubric)}\n"
        f"Evidence catalog: {_encoded(evidence_catalog or {})}"
    )
