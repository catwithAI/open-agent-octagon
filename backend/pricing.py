"""模型定价与 attempt 成本计算（token_cost_accounting）。

**为什么必须按四档分开计价**：cache read 的单价普遍是 prompt 的 1/10
（实测 OpenRouter：sonnet-5 $2/M vs $0.2/M、ds4-flash $0.14/M vs $0.028/M）。
把缓存复读并进 input 会把成本高估近一个数量级——2026-07-27 六平台实验里
claude-code 自报 157 万 input token 而 wire 累计送入仅 3.2MB，就是全量历史
重发 + prompt caching 的结果。不拆开就没法比性价比。

定价来源是 OpenRouter `/models`（`pricing.prompt` / `.completion` /
`.input_cache_read` / `.input_cache_write`，单位是**美元每 token**）。缓存到
本地 JSON，离线可算、结果可复现；不在评分链路里发网络请求。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

PRICING_SCHEMA_VERSION = "octagon-pricing-v1"

# Non-overlapping billable tier → pricing rate.  ``input_tokens`` and
# ``output_tokens`` reported by OpenAI-style APIs are totals containing their
# cache/reasoning detail fields, so compute_cost first removes those subsets.
# cache_write 缺档时回落 prompt 单价（多数 provider 的写入价与 prompt 同级或
# 略高，回落比记 0 更接近真相）。
_FIELD_TO_RATE = {
    "input_tokens": "prompt",
    "output_tokens": "completion",
    "cache_read_tokens": "input_cache_read",
    "cache_write_tokens": "input_cache_write",
}


@dataclass(frozen=True)
class ModelPricing:
    """单个模型的四档单价（美元 / token）。

    `None` 表示该档位 provider 未提供——**不是 0**。计价时该档 token 记入
    `unpriced_tokens` 而非按 0 计费，避免静默低估。
    """

    model: str
    prompt: float | None = None
    completion: float | None = None
    input_cache_read: float | None = None
    input_cache_write: float | None = None

    def rate(self, name: str) -> float | None:
        value = getattr(self, name, None)
        # cache_write 缺档回落 prompt：多数 provider 不单列写入价。
        if value is None and name == "input_cache_write":
            return self.prompt
        return value


@dataclass(frozen=True)
class CostBreakdown:
    """attempt 级成本明细。

    `unpriced_tokens > 0` 表示有 token 因缺定价未计入 `total_usd`——此时
    `total_usd` 是**下界**，不能当作准确成本用于排名。调用方必须检查它，
    不能只读 total_usd（静默低估比报错更危险）。
    """

    model: str
    total_usd: float
    by_field: dict[str, float]
    unpriced_tokens: int
    priced: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "total_usd": self.total_usd,
            "by_field": dict(self.by_field),
            "unpriced_tokens": self.unpriced_tokens,
            "priced": self.priced,
        }


class PricingTable:
    """模型 → 定价的查表。未知模型返回 None，由调用方决定降级行为。"""

    def __init__(self, entries: dict[str, ModelPricing], *, fetched_at: str | None = None):
        self._entries = entries
        self.fetched_at = fetched_at

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, model: str | None) -> ModelPricing | None:
        """按模型名查定价，自动剥离 Octagon 的 provider 前缀。

        提交时的模型串形如 `or-cc/anthropic/claude-sonnet-5`，而定价表的键是
        上游原名 `anthropic/claude-sonnet-5`。逐段剥前缀重试，直到命中——
        不能只剥一段：blade 的 `nofree/deepseek/deepseek-v4-flash` 要剥一段，
        而裸名本身就带一个 `/`。
        """
        if not model:
            return None
        candidate = model.strip()
        while candidate:
            hit = self._entries.get(candidate)
            if hit is not None:
                return hit
            if "/" not in candidate:
                return None
            candidate = candidate.split("/", 1)[1]
        return None

    @classmethod
    def from_openrouter_models(cls, payload: dict[str, Any]) -> "PricingTable":
        """OpenRouter `/models` 响应 → 定价表。"""
        entries: dict[str, ModelPricing] = {}
        for item in payload.get("data") or []:
            if not isinstance(item, dict):
                continue
            model_id = item.get("id")
            pricing = item.get("pricing")
            if not isinstance(model_id, str) or not isinstance(pricing, dict):
                continue
            entries[model_id] = ModelPricing(
                model=model_id,
                prompt=_to_float(pricing.get("prompt")),
                completion=_to_float(pricing.get("completion")),
                input_cache_read=_to_float(pricing.get("input_cache_read")),
                input_cache_write=_to_float(pricing.get("input_cache_write")),
            )
        return cls(entries)

    @classmethod
    def load(cls, path: str | Path) -> "PricingTable":
        p = Path(path)
        if not p.is_file():
            return cls({})
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("定价表读取失败，按空表处理: %s", p)
            return cls({})
        entries = {
            model: ModelPricing(
                model=model,
                prompt=_to_float(v.get("prompt")),
                completion=_to_float(v.get("completion")),
                input_cache_read=_to_float(v.get("input_cache_read")),
                input_cache_write=_to_float(v.get("input_cache_write")),
            )
            for model, v in (raw.get("models") or {}).items()
            if isinstance(v, dict)
        }
        return cls(entries, fetched_at=raw.get("fetched_at"))

    def dump(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": PRICING_SCHEMA_VERSION,
            "fetched_at": self.fetched_at
            or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "models": {
                m: {
                    "prompt": e.prompt,
                    "completion": e.completion,
                    "input_cache_read": e.input_cache_read,
                    "input_cache_write": e.input_cache_write,
                }
                for m, e in sorted(self._entries.items())
            },
        }
        p.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")


def compute_cost(
    usage: dict[str, Any] | None,
    model: str | None,
    table: PricingTable,
    *,
    input_semantics: Literal["auto", "inclusive", "disjoint"] = "auto",
) -> CostBreakdown | None:
    """五维用量 + 定价表 → 成本明细。模型无定价时返回 None（不猜、不填 0）。

    只对**已知定价**的档位累加；缺档的 token 计入 `unpriced_tokens` 并把
    `priced` 置 False，让调用方知道这个数字只是下界。
    """
    if not usage:
        return None
    pricing = table.get(model)
    if pricing is None:
        return None

    # Convert producer totals into non-overlapping billing tiers.  OpenAI-style
    # input includes cached tokens; Anthropic-style input is already disjoint
    # (billable_input_tokens detects the latter by the cache > input invariant).
    tier_tokens: dict[str, int] = {}
    noncached_input = billable_input_tokens(
        usage,
        semantics=input_semantics,
    )
    if noncached_input is not None:
        tier_tokens["input_tokens"] = noncached_input
    for field in ("cache_read_tokens", "cache_write_tokens"):
        value = _nonnegative_int(usage.get(field))
        if value is not None:
            tier_tokens[field] = value

    output = _nonnegative_int(usage.get("output_tokens"))
    if output is not None:
        # completion_tokens_details.reasoning_tokens is a subset of output.
        tier_tokens["output_tokens"] = output
    else:
        # A producer that exposes only reasoning still has billable completion
        # tokens; retain a conservative standalone tier in that rare shape.
        reasoning = _nonnegative_int(usage.get("reasoning_tokens"))
        if reasoning is not None:
            tier_tokens["reasoning_tokens"] = reasoning

    by_field: dict[str, float] = {}
    total = 0.0
    unpriced = 0
    for field, tokens in tier_tokens.items():
        if tokens <= 0:
            continue
        rate_name = (
            "completion"
            if field == "reasoning_tokens"
            else _FIELD_TO_RATE[field]
        )
        rate = pricing.rate(rate_name)
        if rate is None:
            unpriced += tokens
            continue
        cost = tokens * rate
        by_field[field] = round(cost, 10)
        total += cost

    return CostBreakdown(
        model=pricing.model,
        total_usd=round(total, 10),
        by_field=by_field,
        unpriced_tokens=unpriced,
        priced=unpriced == 0,
    )


def billable_input_tokens(
    usage: dict[str, Any] | None,
    *,
    semantics: Literal["auto", "inclusive", "disjoint"] = "auto",
) -> int | None:
    """真实计费的非缓存输入 token。

    **两种 provider 语义并存**，必须先判别再相减，否则会算出负数：

    - OpenAI 系：`prompt_tokens` **含**缓存命中/写入部分
      → 非缓存 = input - cache_read - cache_write
    - Anthropic 系：`input_tokens` **不含** cache_read/cache_creation（三者
      并列） → input 本身就是非缓存量

    生产成本链路按 agent producer 显式传 ``inclusive`` / ``disjoint``。
    ``auto`` 只用于缺少 producer 元数据的历史/直接调用，判别依据是
    `cache_read + cache_write > input`：实测 2026-07-27
    claude-code 一次 attempt 报 input=1789 而 cache_read=768787，若无脑相减会
    clamp 成 0，把"真实新增输入 1789"错报成"零输入"。缓存量大于总输入在
    "含缓存"语义下不可能，故据此判定为 Anthropic 语义，直接返回 input。

    返回 None 表示缺 input 字段、无法判定。
    """
    if not usage:
        return None
    total = _nonnegative_int(usage.get("input_tokens"))
    if total is None:
        return None
    if semantics == "disjoint":
        return total
    if semantics not in {"auto", "inclusive"}:
        raise ValueError(f"unknown input token semantics: {semantics}")
    cached_read = _nonnegative_int(usage.get("cache_read_tokens")) or 0
    cached_write = _nonnegative_int(usage.get("cache_write_tokens")) or 0
    cached = cached_read + cached_write
    if cached == 0:
        return max(0, total)
    if semantics == "auto" and cached > total:
        # Anthropic 语义：input 与 cache_read 并列，input 已是非缓存量。
        return max(0, total)
    return max(0, total - cached)


def _nonnegative_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    # OpenRouter 用 "0" 表示免费档；0 是有效单价，保留。
    return f
