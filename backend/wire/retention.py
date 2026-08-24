"""retention scorer：probe 对 setup facts 的保真度评分。

**只读 probe 输出与 facts manifest**（答案 hash / 结构化 expected），**绝不读取
隐藏 reasoning**——scorer 的输入是 agent 对每个 fact 的最终答案，
不是它的思考过程。这样 retention 度量的是「信息是否被保留并复现」，而非「它是否
在中间步骤提到过」。

facts manifest：固定 seed 生成、不可从常识补全的 key/value facts，
每个 fact 声明 ``answer_hash``（规范化答案的 sha256，隐私保护、不落明文）**或**
结构化 ``expected``（用于等价格式 / 部分保留评分）。生成器版本纳入 manifest。

**answer normalization**（确定性、可执行）：比较前对两侧答案做同一套规范化——
大小写折叠、首尾与内部空白归一、去数字千分位、统一 ASCII 引号——让「1,000」与
「1000」「 True 」与「true」等价。规范化规则本身是评分口径的一部分，与生成器版本
一同 versioned。

评分（每个 fact ∈ [0,1]，retention_score = 均值）：
- **精确/等价匹配** → 1.0；
- **缺失**（fact 未作答 / 空）→ 0.0；
- **幻觉补全**（作答但与 expected 不符）→ 0.0；
- **部分保留**（expected 是集合/列表时）→ 命中比例。

``answer_hash`` 型 fact 只能判精确/等价（hash 不可逆，无法算部分比例）；需要部分
保留评分的 fact 必须给结构化 ``expected``。

是否把 retention 纳入环境总分由**场景 meta 显式配置**（``score_weight``），默认
仅作独立诊断指标，不改历史 benchmark 口径。本模块只算分，不决定
是否进总分。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from backend.wire.hashing import raw_bytes_hash

RETENTION_NORMALIZER_VERSION = "octagon-retention-normalizer-v1"
# answer_hash 的 domain 前缀：避免与其它 sha256 用途撞库。
_ANSWER_HASH_DOMAIN = "octagon-retention-answer-v1"

_WS_RE = re.compile(r"\s+")
# 数字千分位：仅当逗号夹在数字之间才当分隔符去掉（不动 "a,b" 这类列表分隔）。
_THOUSANDS_RE = re.compile(r"(?<=\d),(?=\d{3}\b)")


def normalize_answer(value: Any) -> str:
    """把一个答案规范成可比较的字符串（确定性）。

    None → 空串（视作缺失）。规则：转字符串 → 统一引号 → 去数字千分位 → 折叠
    内部空白并首尾 strip → 小写。数字/布尔按其字符串形态处理（1000/1,000 等价，
    True/true 等价）。
    """
    if value is None:
        return ""
    s = str(value)
    # 统一 ASCII 引号（中文/花引号 → 直引号），避免格式差异误判。
    s = s.replace("“", '"').replace("”", '"')
    s = s.replace("‘", "'").replace("’", "'")
    s = _THOUSANDS_RE.sub("", s)
    s = _WS_RE.sub(" ", s).strip()
    return s.casefold()


def answer_hash(value: Any) -> str:
    """规范化答案的 domain 化 sha256（facts manifest 里 answer_hash 的构造方式）。

    manifest 作者用它把明文答案转成 hash；scorer 用同一函数 hash agent 答案后比对，
    双方走同一规范化 + 同一 domain，等价格式自然对上，且明文不落 manifest。
    """
    payload = f"{_ANSWER_HASH_DOMAIN}:{normalize_answer(value)}".encode("utf-8")
    return f"sha256:{raw_bytes_hash(payload)}"


@dataclass(frozen=True)
class Fact:
    """一个可评分事实。answer_hash 与 expected 至少给一个。

    - ``answer_hash``：规范化答案的 domain sha256（只能判精确/等价）；
    - ``expected``：结构化期望值。标量 → 精确/等价；list/set → 部分保留（命中比例）。
    """

    id: str
    answer_hash: str | None = None
    expected: Any = None

    def __post_init__(self) -> None:
        if self.answer_hash is None and self.expected is None:
            raise ValueError(f"fact {self.id!r} 必须给 answer_hash 或 expected 其一")


@dataclass(frozen=True)
class FactsManifest:
    """setup 材料的可评分事实清单。"""

    facts: list[Fact]
    seed: int | None = None
    content_sha256: str | None = None
    bytes: int | None = None
    estimated_tokens: int | None = None
    generator_version: str | None = None
    normalizer_version: str = RETENTION_NORMALIZER_VERSION


@dataclass
class FactScore:
    fact_id: str
    score: float
    verdict: str  # exact | equivalent | partial | missing | hallucinated
    detail: dict[str, Any] = field(default_factory=dict)


def _score_scalar_expected(fact_id: str, answer: Any, expected: Any) -> FactScore:
    na, ne = normalize_answer(answer), normalize_answer(expected)
    if na == "":
        return FactScore(fact_id, 0.0, "missing")
    if na == ne:
        # 完全一致（含等价格式，规范化后相等）→ 1.0。verdict 区分精确/等价供诊断。
        verdict = "exact" if str(answer) == str(expected) else "equivalent"
        return FactScore(fact_id, 1.0, verdict)
    return FactScore(fact_id, 0.0, "hallucinated", {"expected_norm": ne, "got_norm": na})


def _score_collection_expected(
    fact_id: str, answer: Any, expected: Any
) -> FactScore:
    """expected 是 list/set：部分保留 = 命中的 expected 元素比例（去重、规范化）。"""
    exp_norm = {normalize_answer(x) for x in expected}
    exp_norm.discard("")
    if not exp_norm:
        # expected 全空：退化为标量比较（避免除零）。
        return _score_scalar_expected(fact_id, answer, expected)
    if answer is None:
        return FactScore(fact_id, 0.0, "missing")
    got = answer if isinstance(answer, (list, tuple, set)) else [answer]
    got_norm = {normalize_answer(x) for x in got}
    got_norm.discard("")
    if not got_norm:
        return FactScore(fact_id, 0.0, "missing")
    hits = exp_norm & got_norm
    score = len(hits) / len(exp_norm)
    if score == 1.0:
        # 全部命中：仍可能有多余项（幻觉附加）；design 只按 expected 命中算保留，
        # 多余项不扣分但记进 detail 供诊断。
        verdict = "exact"
    elif score == 0.0:
        verdict = "hallucinated"
    else:
        verdict = "partial"
    return FactScore(
        fact_id, score, verdict,
        {"expected_count": len(exp_norm), "hit_count": len(hits),
         "extra": sorted(got_norm - exp_norm)},
    )


def _score_fact(fact: Fact, answer: Any) -> FactScore:
    # 结构化 expected 优先（支持等价/部分）；否则用 answer_hash（只判精确/等价）。
    if fact.expected is not None:
        if isinstance(fact.expected, (list, tuple, set)):
            return _score_collection_expected(fact.id, answer, fact.expected)
        return _score_scalar_expected(fact.id, answer, fact.expected)
    # answer_hash 路径：hash 不可逆 → 只能精确/等价（规范化后 hash 相等）。
    if normalize_answer(answer) == "":
        return FactScore(fact.id, 0.0, "missing")
    if answer_hash(answer) == fact.answer_hash:
        return FactScore(fact.id, 1.0, "equivalent")
    return FactScore(fact.id, 0.0, "hallucinated")


@dataclass
class RetentionResult:
    retention_score: float | None
    facts_total: int
    facts_scored: int
    per_fact: list[FactScore]
    normalizer_version: str


def score_retention(
    manifest: FactsManifest, probe_answers: dict[str, Any]
) -> RetentionResult:
    """对 probe 的**结构化答案**（``{fact_id: answer}``）评每个 fact 并求均值。

    ``probe_answers`` 是 agent 对各 fact 的最终答案映射，由 probe turn 的可机器解析
    输出提供——**不是自由文本 reasoning**（只读答案不读思考）。fact 未
    出现在 map 里 → 缺失（0.0）。

    retention_score = 各 fact 得分均值；facts 为空时为 None（无可评分事实，交由
    evaluation summary 走 insufficient/incomplete，不硬凑 0 或 1）。
    """
    per_fact: list[FactScore] = []
    for fact in manifest.facts:
        answer = probe_answers.get(fact.id)
        per_fact.append(_score_fact(fact, answer))
    total = len(manifest.facts)
    if total == 0:
        return RetentionResult(
            retention_score=None, facts_total=0, facts_scored=0,
            per_fact=[], normalizer_version=manifest.normalizer_version,
        )
    mean = sum(fs.score for fs in per_fact) / total
    return RetentionResult(
        retention_score=mean,
        facts_total=total,
        facts_scored=total,
        per_fact=per_fact,
        normalizer_version=manifest.normalizer_version,
    )
