"""Strict LLM rubric executor used by replay/shadow runs.

The prompt contains no Rubric Evolution concepts. The model is an ordinary
judge that executes the supplied checklist and may not author new criteria.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field

from backend.insights.jsonutil import parse_json_object
from backend.insights.providers import InsightProvider

from .models import CandidateRubric, JudgeCheckRecord
from .replay import RubricReplayCase

JUDGE_EXECUTION_PROMPT_VERSION = "octagon-rubric-executor-prompt-v1"


class RubricJudgeExecution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(pattern=r"^octagon-rubric-judge-execution-v1$")
    checks: list[JudgeCheckRecord]


class ProviderRubricExecutor:
    def __init__(self, provider: InsightProvider) -> None:
        self.provider = provider

    async def __call__(
        self, case: RubricReplayCase, rubric: CandidateRubric
    ) -> list[JudgeCheckRecord]:
        prompt = (
            "你是 Rubric 执行器。只逐项执行输入 Rubric 中已经定义的检查，不增加新标准、"
            "不修改权重、不根据整体印象调分。每个 check 必须返回一次且只能返回一次。"
            "证据不足、冲突或无法可靠判断时返回 unknown；Unknown 不是 Fail。只返回 JSON。\n"
            "结果骨架："
            '{"schema_version":"octagon-rubric-judge-execution-v1","checks":['
            '{"check_id":"...","result":"pass|partial|fail|not_applicable|unknown",'
            '"awarded":null,"maximum":1,"evidence_refs":[],"unknown_reason":null,'
            '"detail":"..."}]}\n'
            f"Prompt version: {JUDGE_EXECUTION_PROMPT_VERSION}\n"
            f"Rubric: {json.dumps(rubric.model_dump(mode='json', by_alias=True), ensure_ascii=False)}\n"
            f"Evidence snapshot: {json.dumps(case.evidence_snapshot, ensure_ascii=False, default=str)}"
        )
        generated = await self.provider.generate(prompt)
        execution = RubricJudgeExecution.model_validate(parse_json_object(generated.text))
        expected = {item.check_id: item for item in rubric.checks}
        actual_ids = [item.check_id for item in execution.checks]
        if len(actual_ids) != len(set(actual_ids)):
            raise ValueError("rubric judge returned duplicate check_id")
        if set(actual_ids) != set(expected):
            raise ValueError("rubric judge must return exactly the configured checks")
        for result in execution.checks:
            configured = expected[result.check_id]
            if abs(result.maximum - configured.weight) > 1e-9:
                raise ValueError(
                    f"rubric judge changed check weight: {result.check_id}"
                )
        return execution.checks
