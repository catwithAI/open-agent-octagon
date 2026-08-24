"""Versioned prompt rendering for Consortium Insights."""

from __future__ import annotations

from backend.experiments.hashing import canonical_json_bytes


PROMPT_VERSION = "octagon-insight-prompt-v3"

# v2：给出显式顶层骨架。v1 只列了 "Required sections"，deepseek 等模型会把五个
# section 平铺到顶层导致 ValidationError（sections missing + extra_forbidden）。
# v3：强制内容字段使用简体中文（Octagon 是中文工作台）；JSON 键、kind 枚举、
# schema_version 与 anchor 必须保持英文/原样，否则 schema 校验会失败。
_OUTPUT_SKELETON = (
    '{"schema_version": "octagon-insight-v1", '
    '"sections": {"consensus": [], "divergence": [], "first_deviation": [], '
    '"recovery": [], "suggested_probes": []}, '
    '"limitations": []}'
)


def render_prompt(bundle: dict) -> str:
    encoded = canonical_json_bytes(bundle).decode("utf-8")
    return (
        "You are generating a constrained Consortium Insight report.\n"
        "Return a single JSON object with EXACTLY this top-level shape "
        "(fill the arrays, add no other top-level keys):\n"
        f"{_OUTPUT_SKELETON}\n"
        "The five section arrays MUST be nested inside the \"sections\" object.\n"
        "Each statement is {\"id\", \"kind\", \"text\", \"anchors\"}; kind is "
        "evidence-backed, inference, or unavailable.\n"
        "Use evidence-backed only with anchors copied exactly from the bundle.\n"
        "Never invent anchors. Never request execution. Statements in "
        "suggested_probes additionally include builder_draft "
        "{question, rationale, protocol_patch} and nothing that executes.\n"
        "builder_draft.protocol_patch MUST be a JSON object such as "
        '{"repeats": 2}, never a string and never prose.\n'
        "Write ALL human-readable content in Simplified Chinese: every statement "
        "text, builder_draft question and rationale, and every limitations entry.\n"
        "Keep JSON keys, kind values (evidence-backed/inference/unavailable), "
        "schema_version and anchors EXACTLY as specified in English — only the "
        "content text is Chinese.\n"
        f"Prompt version: {PROMPT_VERSION}\n"
        f"Evidence bundle:\n{encoded}"
    )
