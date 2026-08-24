"""上下文压缩检测。

消费 canonical `llm_call`（被动检测）+ 已有的 explicit-hint `context_compaction`
（显式融合）records，不重新解析私有格式。按 ``(attempt_id, agent_id,
producer_session_id)`` 分段——避免把**新 session/新 attempt**当成压缩，也让每个
子 agent 在自己的段内独立判定（禁止跨 attempt/agent 比较相邻 calls）。
段内对**对话主线**调用（排除 compaction/planning/meta 这些旁路 call_role；
``subagent`` 是子 agent 自己的主线，参与检测）算 token/message/hash delta，
按证据优先级分四档 confidence，并推断 strategy，产出 canonical
``context_compaction`` record。

**返回值只含 record 列表、不含状态字段**：空列表无法区分「没发生
压缩」「证据不足」「aggregate-only」「unattributed」，这四者的区分由后续状态判定结合
manifest/completeness/capability gap 判定，不在本模块返回值里混入状态语义。

证据优先级：
1. producer 显式 compaction event → explicit：作为 explicit-hint
   ``context_compaction`` 输入，与被动 record 对相同 boundary **融合去重**
   （见 _fuse），不重复计数、confidence 升 explicit、provenance 合并；
2. summary/compaction call 后 token/message 大幅下降 → high；
3. token 下降且 message hash diff 显示中段删除/摘要插入 → medium；
4. 只有 token 突降 → low；
5. session ID 改变 → new-session，不记 compaction。

阈值做成 **versioned analyzer config**（沿用 llm-gateway 保守值）。message 级
prefix/suffix 精确 diff 需要逐消息 hash；canonical RequestSummary 只有聚合
messages_hash，故 strategy 只能靠 size 启发（domain/粒度不足降 size
启发并降 confidence），不足时 strategy=unknown。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

ANALYZER_VERSION = "compaction-analyzer-v1"


@dataclass(frozen=True)
class AnalyzerConfig:
    """versioned 阈值（沿用 llm-gateway 保守值）。"""

    version: str = ANALYZER_VERSION
    # current_input / previous_input < ratio_drop → 视为大幅下降。
    ratio_drop: float = 0.6
    # previous_input - current_input > abs_drop → 绝对下降门槛。
    abs_drop: int = 5000
    # summary/compaction 回看窗口（秒）：相邻 main call 时间差在此内才算「紧接」。
    summary_lookback_s: float = 5.0


_DEFAULT_CONFIG = AnalyzerConfig()


def _input_tokens(rec: dict[str, Any]) -> int | None:
    usage = (rec.get("data") or {}).get("usage") or {}
    v = usage.get("input_tokens")
    return v if isinstance(v, int) else None


def _message_count(rec: dict[str, Any]) -> int | None:
    req = (rec.get("data") or {}).get("request") or {}
    v = req.get("message_count")
    return v if isinstance(v, int) else None


def _messages_hash(rec: dict[str, Any]) -> str | None:
    req = (rec.get("data") or {}).get("request") or {}
    h = req.get("messages_hash")
    return h if isinstance(h, str) and h else None


def _ts(rec: dict[str, Any]) -> str:
    return (rec.get("time") or {}).get("timestamp") or ""


def _seconds_between(prev_ts: str, cur_ts: str) -> float | None:
    """两个 ISO 时间戳的秒差；解析失败返回 None（不因缺时间戳误判紧接）。"""
    from datetime import datetime

    def _parse(s: str):
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    a, b = _parse(prev_ts), _parse(cur_ts)
    if a is None or b is None:
        return None
    return (b - a).total_seconds()


# 参与压缩检测的 call_role **白名单**：对话主线调用。
#
# - "main"：根 agent 的主线；
# - "subagent"：子 agent 自己的主线——跨 agent 隔离由分段键 (agent_id,
#   session) 完成，不能靠丢弃它来实现（那会让子 agent 压缩永远检测不到）。
#
# **"unknown" 不在此列**：它的语义正是"无法确定这是 main 还是 compaction/
# planning/aggregate"。finalize.py 明确写 unknown 而非伪造 main，就是为了
# 不污染依赖 main role 的压缩分析（"main 是聚合/compaction 分析的业务事实"）。
# 把它纳入会造假阳性：`_segment_key` 对缺失的 agent_id 兜底成 "main"，两个
# 无法归类的 aggregate/旁路调用会被凑成一段，token 差被判成压缩；而状态判定又
# 规定"有 record → observed"，observability_completeness 撤销不了这个结论。
# 元数据不足应走 capability/completeness 判定，不进被动检测。
#
# 用白名单而非黑名单：将来 producer 新增旁路角色（tool/summary/…）时默认
# 被排除，不会静默混进 token 曲线污染判定。
_CONVERSATIONAL_ROLES = frozenset({"main", "subagent"})


def _segment_key(rec: dict[str, Any]) -> tuple[str, str, str]:
    """分段键：(attempt_id, agent_id, producer_session_id)。

    attempt/agent/session 任一不同即新段——不跨 attempt、跨 agent、跨 session 比较
    相邻 calls。finalize 按 attempt 调用时输入本已单 attempt，但把
    attempt_id 纳入 key 让检测器对**混合 attempt 输入**也不误判（跨 attempt
    反例的直接依据），不依赖调用方保证隔离。"""
    corr = rec.get("correlation") or {}
    return (
        rec.get("attempt_id") or "",
        corr.get("agent_id") or "main",
        corr.get("producer_session_id") or "",
    )


def _classify(
    prev: dict[str, Any], cur: dict[str, Any], cfg: AnalyzerConfig,
) -> dict[str, Any] | None:
    """相邻两个 main call 是否构成压缩 + confidence/strategy。无迹象返回 None。"""
    prev_in = _input_tokens(prev)
    cur_in = _input_tokens(cur)
    # token 是被动检测的核心信号；缺失无法判定（不猜）。
    if prev_in is None or cur_in is None or prev_in <= 0:
        return None

    ratio = cur_in / prev_in
    abs_drop = prev_in - cur_in
    big_token_drop = ratio < cfg.ratio_drop and abs_drop > cfg.abs_drop
    if not big_token_drop:
        return None  # 没有大幅 token 下降 → 不是压缩

    prev_msgs = _message_count(prev)
    cur_msgs = _message_count(cur)
    msg_drop = (
        prev_msgs is not None and cur_msgs is not None and cur_msgs < prev_msgs
    )
    # message hash 变化（中段删除/摘要插入的间接信号）。
    hash_changed = (
        _messages_hash(prev) is not None
        and _messages_hash(cur) is not None
        and _messages_hash(prev) != _messages_hash(cur)
    )
    # 时间紧接（summary lookback 窗口内）。
    dt = _seconds_between(_ts(prev), _ts(cur))
    tight = dt is not None and 0 <= dt <= cfg.summary_lookback_s

    # confidence 分档：
    # high  = token+message 大幅下降且时间紧接（像 summary/compaction）；
    # medium= token 下降 + message hash diff（中段删除/摘要插入的间接证据）；
    # low   = 只有 token 突降。
    if msg_drop and tight:
        confidence = "high"
    elif msg_drop or hash_changed:
        confidence = "medium"
    else:
        confidence = "low"

    strategy = _infer_strategy(prev_msgs, cur_msgs, hash_changed)
    return {
        "confidence": confidence,
        "strategy": strategy,
        "before_tokens": prev_in,
        "after_tokens": cur_in,
        "dropped_messages": (prev_msgs - cur_msgs)
        if (prev_msgs is not None and cur_msgs is not None) else None,
    }


def _infer_strategy(
    prev_msgs: int | None, cur_msgs: int | None, hash_changed: bool,
) -> str:
    """strategy 推断（canonical 只有聚合 messages_hash，靠 size 启发）。

    逐消息 hash 缺失时无法做 prefix/suffix 精确 diff → 只能按消息数变化粗判，
    证据不足一律 unknown（不臆断 full/selective/sliding）。
    """
    if prev_msgs is None or cur_msgs is None:
        return "unknown"
    if cur_msgs <= 2 and prev_msgs - cur_msgs >= 5:
        # 大量删除后只剩极少消息（+可能一条摘要）：像 full-summary。
        return "full-summary"
    # 其余：有下降但缺逐消息 hash 无法区分 selective/sliding → unknown。
    return "unknown"


def detect_compactions(
    records: list[dict[str, Any]], *, config: AnalyzerConfig | None = None,
) -> list[dict[str, Any]]:
    """从 canonical records 检测压缩，返回 ``context_compaction`` record 列表。

    输入 canonical records（``llm_call`` + 可能已有的 explicit-hint
    ``context_compaction``），返回**仅** ``context_compaction`` record 列表。

    **返回值边界**：只产出 record 列表（可为空），**不**返回
    ``unsupported`` / ``incomplete`` / ``not_observed_under_budget`` /
    ``identity unattributed`` 这类状态字符串——空列表本身无法区分「没发生压缩」
    「证据不足」「aggregate-only」「identity unattributed」四种情况，这四者的区分
    由 evaluation summary 结合 manifest/completeness/capability gap 输出，不在
    本函数的返回值形状里混入状态语义。aggregate-only / unattributed 输入在这里表现
    为「该 segment 没有产出可判定 record」（即空结果），而非造假 record。

    流程：
    1. **被动检测**：只看 ``llm_call`` 且 ``call_role`` 在 _CONVERSATIONAL_ROLES 白
       名单内；按 (attempt_id, agent_id, session) 分段，段内按时间排序取相邻对，
       token 大幅下降 → 产 passive record；
    2. **显式 hint 融合去重**：输入里已有的 explicit-hint
       ``context_compaction``（producer 直接汇报的压缩边界，source 非
       passive-detector）与被动 record 对**相同 (before_call_id, after_call_id)
       boundary** 融合——**不重复计数**：显式证据提升 confidence 到 explicit 并置
       ``source="explicit+passive"``，保留双方 provenance；显式独有的 boundary
       原样保留；被动独有的照常产出。
    """
    cfg = config or _DEFAULT_CONFIG
    # 参与检测的是每个 agent 自己的**对话主线**调用。
    #
    # 跨 agent 的隔离由分段键 (attempt, agent_id, session) 完成，不靠 call_role
    # 过滤：早期实现只收 call_role=="main"，而 normalizer 给子 agent 的调用标
    # "subagent"，结果子 agent 的所有调用被整段丢弃——子 agent
    # 压缩永远检测不到（要求的正是"同一子 agent invocation 内多个可比较
    # calls"）。
    #
    # 参与检测的角色见 _CONVERSATIONAL_ROLES（白名单）：旁路调用
    # （compaction/planning/meta/tool…）混进序列会污染 token 曲线。
    conversational = [
        r for r in records
        if r.get("record_type") == "llm_call"
        and (r.get("data") or {}).get("call_role") in _CONVERSATIONAL_ROLES
    ]
    # 分段：同一 attempt 的同一 agent 的同一 session 才互相比较。
    segments: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for r in conversational:
        segments.setdefault(_segment_key(r), []).append(r)

    passive: list[dict[str, Any]] = []
    for calls in segments.values():
        calls.sort(key=_ts)
        for prev, cur in zip(calls, calls[1:]):
            verdict = _classify(prev, cur, cfg)
            if verdict is None:
                continue
            passive.append(_compaction_record(prev, cur, verdict, cfg))

    # 已有的 explicit-hint context_compaction（source 非 passive-detector）。
    explicit = [
        r for r in records
        if r.get("record_type") == "context_compaction"
        and (r.get("data") or {}).get("source") != "passive-detector"
    ]
    return _fuse(passive, explicit)


# confidence 强弱序（融合取更强者）。explicit 是 producer 直接汇报的最高档。
_CONFIDENCE_RANK = {"low": 1, "medium": 2, "high": 3, "explicit": 4}


def _boundary_key(rec: dict[str, Any]) -> tuple[str, str, str] | None:
    """压缩边界去重键：(attempt_id, before_call_id, after_call_id)。

    before/after 有一个缺 lc 就无法确定 boundary（不同源可能各自 None），返回 None
    表示「不可去重」——这样的 record 一律原样保留，不与任何东西融合。
    """
    data = rec.get("data") or {}
    before = data.get("before_call_id")
    after = data.get("after_call_id")
    if not before or not after:
        return None
    return (rec.get("attempt_id") or "", before, after)


def _fuse(
    passive: list[dict[str, Any]], explicit: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """显式 hint 与被动 record 对相同 boundary 融合去重。

    同一 (attempt, before, after) boundary 上：
    - 只有一方 → 原样保留；
    - 两方都有 → 合成一条：confidence 取更强者、source="explicit+passive"，
      provenance 合并（保留双方 evidence 引用），token/strategy 优先取显式（producer
      权威），显式缺失的字段用被动补。**不产出两条**（不重复计数）。

    boundary key 不可得（before/after 缺 lc）的 record 不参与融合，全部原样保留。
    """
    explicit_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    explicit_unkeyed: list[dict[str, Any]] = []
    for e in explicit:
        key = _boundary_key(e)
        if key is None:
            explicit_unkeyed.append(e)
        else:
            # 同 boundary 多条显式 hint：保留先到的（确定性），其余并入 provenance。
            if key in explicit_by_key:
                explicit_by_key[key].setdefault("provenance", []).extend(
                    e.get("provenance") or []
                )
            else:
                explicit_by_key[key] = e

    out: list[dict[str, Any]] = []
    fused_keys: set[tuple[str, str, str]] = set()
    for p in passive:
        key = _boundary_key(p)
        if key is not None and key in explicit_by_key:
            out.append(_merge_boundary(explicit_by_key[key], p))
            fused_keys.add(key)
        else:
            out.append(p)
    # 未被融合的显式独有 boundary + 不可去重的显式。
    for key, e in explicit_by_key.items():
        if key not in fused_keys:
            out.append(e)
    out.extend(explicit_unkeyed)
    return out


def _merge_boundary(
    explicit: dict[str, Any], passive: dict[str, Any]
) -> dict[str, Any]:
    """把同 boundary 的显式 hint 与被动 record 合成一条（显式为主，被动补空）。"""
    merged = {**explicit}
    e_data = dict(explicit.get("data") or {})
    p_data = passive.get("data") or {}
    # 显式缺失的诊断字段用被动补（producer hint 往往只给 boundary + strategy）。
    for field in (
        "before_tokens", "after_tokens", "dropped_messages", "inserted_messages",
        "kept_prefix", "kept_suffix", "before_turn_id", "after_turn_id",
    ):
        if e_data.get(field) is None and p_data.get(field) is not None:
            e_data[field] = p_data[field]
    if not e_data.get("strategy") or e_data.get("strategy") == "unknown":
        if p_data.get("strategy") and p_data["strategy"] != "unknown":
            e_data["strategy"] = p_data["strategy"]
    # confidence 取更强者。
    e_conf = e_data.get("confidence")
    p_conf = p_data.get("confidence")
    best = max(
        (e_conf, p_conf),
        key=lambda c: _CONFIDENCE_RANK.get(c, 0),
    )
    e_data["confidence"] = best
    e_data["source"] = "explicit+passive"
    # analyzer_version：被动侧带的（显式 hint 无 analyzer），保留以示阈值来源。
    if e_data.get("analyzer_version") is None and p_data.get("analyzer_version"):
        e_data["analyzer_version"] = p_data["analyzer_version"]
    merged["data"] = e_data
    # correlation：显式为主，缺的拓扑字段（parent/session/turn）用被动补——被动
    # record 的 correlation 来自真实相邻 call，往往比 producer hint 更全。
    p_corr = passive.get("correlation") or {}
    merged_corr = {**p_corr, **(explicit.get("correlation") or {})}
    merged_corr["confidence"] = best
    merged["correlation"] = merged_corr
    # provenance 合并（双方 evidence 引用都保留，不丢证据）。
    merged["provenance"] = list(explicit.get("provenance") or []) + list(
        passive.get("provenance") or []
    )
    return merged


def _compaction_record(
    prev: dict[str, Any], cur: dict[str, Any], verdict: dict[str, Any],
    cfg: AnalyzerConfig,
) -> dict[str, Any]:
    from backend.wire import ids

    prev_corr = prev.get("correlation") or {}
    cur_corr = cur.get("correlation") or {}
    before_lc = prev_corr.get("logical_call_id")
    after_lc = cur_corr.get("logical_call_id")
    anchor = f"compaction:{before_lc}:{after_lc}"
    return {
        "schema_version": "octagon-wire-v1",
        "record_id": ids.record_id(
            attempt_id=cur.get("attempt_id", ""),
            record_kind="context_compaction", record_anchor=anchor,
        ),
        "record_type": "context_compaction",
        "attempt_id": cur.get("attempt_id"),
        "phase": cur.get("phase", "agent_run"),
        "source": {"kind": "octagon-analyzer", "instance": "compaction", "version": None},
        "time": {"timestamp": _ts(cur), "started_at": None, "finished_at": None},
        "correlation": {
            "agent_id": cur_corr.get("agent_id", "main"),
            # 补可得的 parent_agent_id / producer_session_id（before/after
            # 同段，取 cur 侧即可）。缺失写 None，不伪造。
            "parent_agent_id": cur_corr.get("parent_agent_id"),
            "producer_session_id": cur_corr.get("producer_session_id"),
            "confidence": verdict["confidence"],
        },
        "provenance": [
            {"evidence_id": before_lc, "raw_ref": None},
            {"evidence_id": after_lc, "raw_ref": None},
        ],
        "field_sources": {},
        "conflicts": [],
        "data": {
            "before_call_id": before_lc,
            "after_call_id": after_lc,
            # 补 before/after turn（turn correlation 已写进 llm_call
            # 的 correlation.turn_id）。缺失写 None，不伪造。
            "before_turn_id": prev_corr.get("turn_id"),
            "after_turn_id": cur_corr.get("turn_id"),
            "summary_call_id": None,
            "before_tokens": verdict["before_tokens"],
            "after_tokens": verdict["after_tokens"],
            "dropped_messages": verdict["dropped_messages"],
            "inserted_messages": None,
            "kept_prefix": None,
            "kept_suffix": None,
            "strategy": verdict["strategy"],
            "source": "passive-detector",
            "confidence": verdict["confidence"],
            "analyzer_version": cfg.version,
        },
    }
