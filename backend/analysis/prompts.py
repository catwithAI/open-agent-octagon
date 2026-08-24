from __future__ import annotations

import json
from typing import Any

ATTEMPT_PROMPT_VERSION = "octagon-blackbox-attempt-prompt-v4"
COMPARISON_PROMPT_VERSION = "octagon-blackbox-comparison-prompt-v4"
CRITIC_PROMPT_VERSION = "octagon-blackbox-critic-prompt-v3"


def _encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


_ATTEMPT_EXAMPLE = {
    "schema_version": "octagon-attempt-analysis-v1",
    "attempt_id": "att_not_this_run",
    "summary": "Agent 先读 README，再运行仓库已有测试确认改动。",
    "claims": [{
        "id": "att_not_this_run.validation",
        "text": "Agent 用仓库测试做完成依据，而不是只看文件是否存在。",
        "confidence": 0.8,
        "evidence_refs": ["octagon://attempt/att_not_this_run/trace/1"],
        "contradicting_refs": [],
        "alternative_explanations": ["测试也可能是顺手跑的"],
        "limitations": ["未看到测试输出全文"],
    }],
}

_COMPARISON_EXAMPLE = {
    "schema_version": "octagon-cross-agent-analysis-v1",
    "run_id": "run_example",
    "summary": "两个 Agent 都完成了计数，但验证方式不同。",
    "claims": [{
        "id": "comparison.validation",
        "text": "两者都尝试验证结果，但执行路径不同。",
        "confidence": 0.75,
        "evidence_refs": [
            "octagon://attempt/att_a/trace/1",
            "octagon://attempt/att_b/trace/1",
        ],
        "contradicting_refs": [],
        "alternative_explanations": [],
        "limitations": ["样本只有一次运行"],
    }],
    "recommended_next_steps": ["增加同协议重复运行"],
}

_CRITIC_EXAMPLE = {
    "schema_version": "octagon-evidence-critique-v1",
    "accepted_claim_ids": ["att_example.task_understanding"],
    "rejected_claims": [{
        "claim_id": "comparison.validation",
        "reason": "引用的 anchor 不能支持该结论",
        "evidence_refs": ["octagon://attempt/att_a/trace/1"],
    }],
    "limitations": ["未读文件不得当成已知事实"],
}


def attempt_prompt(
    snapshot: dict[str, Any], attempt: dict[str, Any],
    process_contract: dict[str, Any] | None = None,
    evidence_catalog: dict[str, Any] | None = None,
) -> str:
    del snapshot, process_contract
    attempt_id = (attempt.get("metadata") or {}).get("id")
    return (
        "返回根对象，不要返回 claims 数组里的某一项。\n"
        "根对象必须是：\n"
        '{"schema_version":"octagon-attempt-analysis-v1",'
        '"attempt_id":"<下面给出的 Attempt id>",'
        '"summary":"<基于已读文件的摘要>","claims":[]}\n'
        "你是 Process Judge，不是 Product Judge。不要打产品分，不要改官方分数。"
        "只解释这个 Attempt 为什么做成或没做成。一个动作可以服务多个意图。\n"
        "冻结证据只在工作区文件里。先 list_evidence_files，再 read_evidence_file。"
        "未读文件不得当成事实。下面的示例属于另一个无关任务，禁止照抄其 id、"
        "anchor 或摘要。attempt_id 必须等于下面给出的 Attempt id。\n"
        "claims[] 每项含 id,text,confidence,evidence_refs,contradicting_refs,"
        "alternative_explanations,limitations。id 必须是该对象的第一个键，"
        "形如 <attempt_id>.<slug>，不要把 id 写在对象末尾，不要使用中文键名。"
        "evidence_refs 只能复制 anchors.json 里已有的 octagon://attempt/... 值。"
        "只返回一个 JSON 对象，不要 Markdown。\n"
        f"形状参考（禁止照抄）：{_encoded(_ATTEMPT_EXAMPLE)}\n"
        f"Prompt version: {ATTEMPT_PROMPT_VERSION}\n"
        f"Attempt id: {_encoded(attempt_id)}\n"
        f"Evidence catalog: {_encoded(evidence_catalog or {})}"
    )


def comparison_prompt(
    snapshot: dict[str, Any], analyses: list[dict[str, Any]],
    process_contract: dict[str, Any] | None = None,
    evidence_catalog: dict[str, Any] | None = None,
) -> str:
    del analyses, process_contract
    run_id = (snapshot.get("run") or {}).get("id")
    attempt_ids = [
        (item.get("metadata") or {}).get("id")
        for item in snapshot.get("attempts") or []
    ]
    return (
        "你是跨 Agent 的 Process 对比员，不是 Product Judge。不要打产品分。\n"
        "比较语义上的决策分叉、恢复、验证和完成依据，不要比较工具名。"
        "时间先后不是因果。冻结证据和工作区中的 attempt 分析先读再判。\n"
        "顶层必须恰好是这 5 个键：schema_version, run_id, summary, claims,"
        "recommended_next_steps。即使只有一个 Attempt，也必须返回这个信封："
        "写一条 comparison.single_attempt_scope claim，说明无法做跨 Agent 对比，"
        "然后基于这一条轨迹写过程 claim。禁止用中文散文拒绝，禁止空对象。"
        "claims 的字段与 Attempt 分析相同。"
        "引用只能复制 anchors.json 里的 octagon://attempt/... 值。"
        "只返回 JSON，不要 Markdown。\n"
        f"完整示例：{_encoded(_COMPARISON_EXAMPLE)}\n"
        f"Prompt version: {COMPARISON_PROMPT_VERSION}\n"
        f"Run id: {_encoded(run_id)}\n"
        f"Attempt ids: {_encoded(attempt_ids)}\n"
        f"Evidence catalog: {_encoded(evidence_catalog or {})}"
    )


def critic_prompt(
    snapshot: dict[str, Any], attempt_analyses: list[dict[str, Any]],
    comparison: dict[str, Any], process_contract: dict[str, Any] | None = None,
    evidence_catalog: dict[str, Any] | None = None,
) -> str:
    del process_contract
    claim_ids = []
    for analysis in attempt_analyses:
        for claim in analysis.get("claims") or []:
            if isinstance(claim, dict) and claim.get("id"):
                claim_ids.append(str(claim["id"]))
    for claim in (comparison.get("claims") or []):
        if isinstance(claim, dict) and claim.get("id"):
            claim_ids.append(str(claim["id"]))
    return (
        "你是 Evidence Critic，不是 Product Judge，也不要重写分析。"
        "只检查已有 claim：引用是否真实存在、是否支持结论、是否过度推断、"
        "是否把基础设施问题归给候选。不要发明新的 claim id。\n"
        "顶层必须恰好是这 4 个键：schema_version, accepted_claim_ids,"
        "rejected_claims, limitations。rejected_claims 每项含 claim_id,reason,"
        "evidence_refs。只返回 JSON，不要 Markdown。\n"
        f"完整示例：{_encoded(_CRITIC_EXAMPLE)}\n"
        f"Prompt version: {CRITIC_PROMPT_VERSION}\n"
        f"Known claim ids: {_encoded(claim_ids)}\n"
        f"Available anchors: {_encoded(snapshot.get('anchors') or [])}\n"
        f"Evidence catalog: {_encoded(evidence_catalog or {})}"
    )
