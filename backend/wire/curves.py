"""从 canonical wire 派生分析曲线。

**只读 canonical records**，纯函数派生，不写主 DB（前端/分析按需重建）。产出：

- context 曲线：每个 main call 的 input/cache token、message count/bytes；
- tool-result size 曲线：mcp_frame response 的 bytes；
- 工具结果回传形态：full / truncated（有证据支撑）；summarized/reduced 需内容级
  证据，无支撑时 ``unknown``——**不臆断**；
- 并发度：由 call 区间重叠算；native call 只有完成时间（无区间）→ 降级为 ``sequence``。
"""

from __future__ import annotations

from typing import Any


def _get(d: dict[str, Any], *path, default=None):
    cur: Any = d
    for k in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return cur if cur is not None else default


def context_series(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """main call 的 context 曲线点，按时间排序。null 保留（不可得写 None，不补 0）。"""
    pts = []
    for r in records:
        if r.get("record_type") != "llm_call":
            continue
        if _get(r, "data", "call_role") != "main":
            continue
        pts.append({
            "logical_call_id": _get(r, "correlation", "logical_call_id"),
            "timestamp": _get(r, "time", "timestamp"),
            "input_tokens": _get(r, "data", "usage", "input_tokens"),
            "cache_read_tokens": _get(r, "data", "usage", "cache_read_tokens"),
            "message_count": _get(r, "data", "request", "message_count"),
            "message_bytes": _get(r, "data", "request", "message_bytes"),
        })
    pts.sort(key=lambda p: p["timestamp"] or "")
    return pts


def tool_result_series(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """tool-result size 曲线：mcp_frame 的 server→client response（工具回传）。

    回传形态（return_form）四态**有证据才判**：
    - ``truncated``：mcp_frame.truncated=True；
    - ``full``：truncated=False 且非 error；
    - ``error``：is_error=True；
    - ``unknown``：truncated 不可得（None）——不臆断 summarized/reduced（需内容判定）。
    """
    pts = []
    for r in records:
        if r.get("record_type") != "mcp_frame":
            continue
        data = r.get("data") or {}
        if data.get("message_kind") != "response":
            continue
        pts.append({
            "jsonrpc_id": data.get("jsonrpc_id"),
            "paired_record_id": data.get("paired_record_id"),
            "trajectory_step_id": data.get("trajectory_step_id"),
            "timestamp": _get(r, "time", "timestamp"),
            "bytes": data.get("bytes"),
            "return_form": _classify_return_form(data),
        })
    pts.sort(key=lambda p: p["timestamp"] or "")
    return pts


def _classify_return_form(data: dict[str, Any]) -> str:
    if data.get("is_error") is True:
        return "error"
    truncated = data.get("truncated")
    if truncated is True:
        return "truncated"
    if truncated is False:
        return "full"
    # truncated 不可得：无法证明是 summarized/reduced（需内容级证据）→ 不臆断。
    return "unknown"


def concurrency(records: list[dict[str, Any]]) -> dict[str, Any]:
    """并发度：call 区间重叠算；缺 started_at 的 native call 无区间 → 降级 sequence。

    返回 ``{"mode": "interval"|"sequence", "max_concurrent": int|None, "n": int}``。
    只要有一个 main call 缺 started_at，就整体降级为 sequence（不混算，诚实降级）。
    """
    mains = [
        r for r in records
        if r.get("record_type") == "llm_call"
        and _get(r, "data", "call_role") == "main"
    ]
    n = len(mains)
    intervals = []
    for r in mains:
        start = _get(r, "time", "started_at")
        end = _get(r, "time", "finished_at") or _get(r, "time", "timestamp")
        if not start:
            # 有 call 无起点 → 无法算区间重叠，整体降级。
            return {"mode": "sequence", "max_concurrent": None, "n": n}
        intervals.append((start, end or start))

    # 区间重叠：按起点扫描，维护活跃计数峰值。
    events = []
    for s, e in intervals:
        events.append((s, 1))
        events.append((e, -1))
    events.sort(key=lambda x: (x[0], -x[1]))  # 同刻先入后出，取真实峰值
    cur = peak = 0
    for _t, delta in events:
        cur += delta
        peak = max(peak, cur)
    return {"mode": "interval", "max_concurrent": peak, "n": n}
