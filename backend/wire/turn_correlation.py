"""conversation turn 与 canonical record 的关联。

conversation turn **不是**新的 canonical record type——turn 关联通过可选
correlation 字段进入 canonical，旧 reader 忽略、新 reader 可按 turn 分组
。本模块是 adapter-agnostic 的通用逻辑，只吃两类输入：

1. **显式 turn header**（explicit）：adapter 每轮 subprocess/请求前设置动态
   ``X-Octagon-Turn-Id`` / ``X-Octagon-Turn-Index``（见 HEADER 常量）；HTTP proxy
   source 把它写进 evidence extensions ``x-octagon.turn-id`` / ``x-octagon.turn-index``
   。finalizer 把 extension 投影进 canonical correlation，
   confidence=explicit。

2. **时间窗口关联**（inferred 兜底）：source 拿不到动态 header 时（如 Blade：模型
   调用在 SDK 进程内，反代够不着，session metadata 也不能按轮更新），
   用 conversation.jsonl 的 turn.started/turn.completed 边界给每条 record 按时间戳
   落到唯一覆盖它的 turn 窗口，confidence=inferred。

**并发/边界歧义不强行关联**（验收）：一个时间戳同时落进多个窗口（窗口重叠 /
边界并发）、或落在所有窗口之外时，宁可不标 turn，也不按顺序猜——inferred 只在
「恰好唯一命中」时成立。显式 header 永远优先于时间窗口。

关联算法只依赖 canonical record 的 ``time`` / ``extensions`` / ``correlation``，与
任何 adapter 的多轮实现解耦；具体哪个 adapter 走 explicit、哪个走 inferred，由各自
的验证任务判定，不在本模块假设。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

# adapter 每轮请求前设置的动态 turn header。取值规则：
# - Turn-Id：task 定义里的稳定 turn_id（不可含换行/控制字符，adapter 保证）；
# - Turn-Index：从 0 开始的十进制整数字符串。
# proxy source 把这两个头投影成下面的 evidence extension key，不转发给 upstream
# （x-octagon-* 属 inbound-strip 前缀）。
HEADER_TURN_ID = "x-octagon-turn-id"
HEADER_TURN_INDEX = "x-octagon-turn-index"

# canonical / evidence 里的 turn extension key（namespaced）。
EXT_TURN_ID = "x-octagon.turn-id"
EXT_TURN_INDEX = "x-octagon.turn-index"

# conversation.jsonl 事件名（与 conversation.models 同集合；这里显式列出避免
# import backend.conversation 造成 wire→conversation 反向依赖）。
_EVENT_TURN_STARTED = "turn.started"
_EVENT_TURN_COMPLETED = "turn.completed"
_EVENT_TURN_FAILED = "turn.failed"
_EVENT_INTERACTION_ANSWERED = "turn.interaction_answered"
_CONVERSATION_FILENAME = "conversation.jsonl"


@dataclass(frozen=True)
class TurnWindow:
    """一个 turn 的时间窗口 [start, end]（闭区间，ISO 时间戳解析成 epoch ms）。

    end 为 None 表示该 turn 只见到 started、未见终态（进行中 / 崩溃截断）——
    这样的窗口无右边界，不参与「唯一命中」判定（见 infer_turn），避免把它当
    成一个吞掉后续所有时间戳的开区间。
    """

    turn_id: str
    turn_index: int | None
    start_ms: float
    end_ms: float | None


def _epoch_ms(ts: Any) -> float | None:
    """ISO8601 → epoch 毫秒；无效返回 None。"""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000.0
    except (ValueError, AttributeError):
        return None


def load_turn_windows(attempt_dir: Path) -> list[TurnWindow]:
    """从 conversation.jsonl 读出每个 turn 的时间窗口（inferred 关联的来源）。

    turn.started 给左边界；turn.completed / turn.failed / turn.interaction_answered
    给右边界（取先到的终态）。缺文件 / 全部截断 → 空列表（inferred 路径整体退化，
    不误关联）。同一 turn_id 多次 started（不应发生）取首个 started + 首个终态。
    """
    path = Path(attempt_dir) / _CONVERSATION_FILENAME
    if not path.is_file():
        return []
    starts: dict[str, tuple[int | None, float]] = {}
    ends: dict[str, float] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # 截断尾行 / 损坏行：fail-open 跳过
        if not isinstance(rec, dict):
            continue
        event = rec.get("event")
        turn_id = rec.get("turn_id")
        if not isinstance(turn_id, str) or not turn_id:
            continue
        ts = _epoch_ms(rec.get("timestamp"))
        if ts is None:
            continue
        if event == _EVENT_TURN_STARTED:
            if turn_id not in starts:
                idx = rec.get("turn_index")
                starts[turn_id] = (idx if isinstance(idx, int) else None, ts)
        elif event in (
            _EVENT_TURN_COMPLETED,
            _EVENT_TURN_FAILED,
            _EVENT_INTERACTION_ANSWERED,
        ):
            if turn_id not in ends:
                ends[turn_id] = ts
    windows: list[TurnWindow] = []
    for turn_id, (idx, start_ms) in starts.items():
        windows.append(
            TurnWindow(
                turn_id=turn_id,
                turn_index=idx,
                start_ms=start_ms,
                end_ms=ends.get(turn_id),
            )
        )
    windows.sort(key=lambda w: w.start_ms)
    return windows


def explicit_turn(extensions: dict[str, Any] | None) -> tuple[str, int | None] | None:
    """evidence/canonical extensions 里的显式 turn（explicit）。

    只认带 namespace 的 EXT_TURN_ID；turn_index 可缺（写 None）。turn_id 缺失
    时整体返回 None——没有稳定 ID 就不是显式关联。
    """
    ext = extensions or {}
    turn_id = ext.get(EXT_TURN_ID)
    if not isinstance(turn_id, str) or not turn_id:
        return None
    idx = ext.get(EXT_TURN_INDEX)
    return turn_id, (idx if isinstance(idx, int) else None)


def infer_turn(ts_ms: float | None, windows: list[TurnWindow]) -> TurnWindow | None:
    """时间戳落进**唯一**覆盖它的 turn 窗口 → 该窗口（inferred）；否则 None。

    并发/边界歧义不强行关联（验收）：
    - ts 为 None（无时间戳）→ None；
    - 落在 0 个窗口 → None（turn 间隙 / 窗口外）；
    - 落在 ≥2 个窗口（窗口重叠、边界并发）→ None（歧义，不猜）。
    只有 end_ms 明确（闭合窗口）且 start<=ts<=end 才算命中；未闭合窗口不参与，
    避免开区间吞掉后续时间戳。
    """
    if ts_ms is None:
        return None
    hits = [
        w
        for w in windows
        if w.end_ms is not None and w.start_ms <= ts_ms <= w.end_ms
    ]
    if len(hits) == 1:
        return hits[0]
    return None


def _record_ts_ms(rec: dict[str, Any]) -> float | None:
    """record 的关联时间点：优先 started_at，其次 timestamp（与压缩检测同源）。"""
    time = rec.get("time") or {}
    return _epoch_ms(time.get("started_at")) or _epoch_ms(time.get("timestamp"))


def _assign_turn(
    corr: dict[str, Any],
    turn_id: str,
    turn_index: int | None,
    confidence: str,
) -> None:
    corr["turn_id"] = turn_id
    corr["turn_index"] = turn_index
    corr["turn_confidence"] = confidence


def project_turn_correlation(
    records: list[dict[str, Any]], windows: list[TurnWindow]
) -> None:
    """把 turn 关联原地写进每条 record 的 ``correlation``。

    两遍：
    1. **每条 record 独立定 turn**——finalizer 在 _base_record 阶段已把 evidence
       的显式 turn extension 投影成 correlation.turn_confidence=="explicit"；本函数
       尊重已有 explicit 不覆盖。无 explicit 时用时间窗口唯一命中（inferred）；
       都没有则不标 turn。
    2. **合入 logical call**——同一 logical_call_id 的所有 record（native call +
       它的 http hop / stream chunk 等）应属同一 turn。取该 lc 组内**最高置信**的
       turn 作为组内 canonical，回填给组内尚无 turn 或只有 inferred 的成员：
       - 组内有 explicit → 全组用该 explicit（http hop 没带 header 也能借 native
         call 的 header 定位到 turn）；
       - 组内只有 inferred → 全组用该 inferred；
       - 组内 explicit 之间 turn_id 冲突 → 记 conflict、不静默择一（见下）。
    不改动已有字段语义，只新增可选 correlation.turn_* 字段。
    """
    # 第一遍：逐 record 定 turn。explicit 已由 finalizer 的 _base_record 投影进
    # correlation（见 finalize._base_record）——这里只补 inferred，不覆盖 explicit。
    for rec in records:
        corr = rec.setdefault("correlation", {})
        if corr.get("turn_confidence") == "explicit":
            continue
        win = infer_turn(_record_ts_ms(rec), windows)
        if win is not None:
            _assign_turn(corr, win.turn_id, win.turn_index, "inferred")

    # 第二遍：按 logical_call_id 合并 turn（explicit 优先，缺则借组内值回填）。
    _merge_turn_into_logical_calls(records)


# turn confidence 的合并优先级：explicit > inferred。
_TURN_CONF_RANK = {"explicit": 2, "inferred": 1}


def _merge_turn_into_logical_calls(records: list[dict[str, Any]]) -> None:
    by_lc: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        lc = (rec.get("correlation") or {}).get("logical_call_id")
        if lc:
            by_lc.setdefault(lc, []).append(rec)

    for members in by_lc.values():
        # 组内最高置信 turn 作为 canonical。explicit 之间 turn_id 不一致时不
        # 静默择一——记 conflict 并保留各自原值，供上层暴露（不伪造统一）。
        best: tuple[int, str, int | None] | None = None
        explicit_turn_ids: set[str] = set()
        for rec in members:
            corr = rec.get("correlation") or {}
            conf = corr.get("turn_confidence")
            tid = corr.get("turn_id")
            if not conf or not tid:
                continue
            rank = _TURN_CONF_RANK.get(conf, 0)
            if conf == "explicit":
                explicit_turn_ids.add(tid)
            cand = (rank, tid, corr.get("turn_index"))
            if best is None or cand[0] > best[0]:
                best = cand
        if best is None:
            continue
        if len(explicit_turn_ids) > 1:
            # 同一 logical call 被打上互相冲突的 explicit turn：不合并，登记
            # conflict（每个成员各记一次，provenance 完整）。
            for rec in members:
                rec.setdefault("conflicts", []).append({
                    "field": "turn_id",
                    "selected": None,
                    "candidates": sorted(explicit_turn_ids),
                    "rule": "explicit-turn-conflict-unmerged",
                })
            continue
        _, best_tid, best_idx = best
        for rec in members:
            corr = rec.setdefault("correlation", {})
            cur_conf = corr.get("turn_confidence")
            # 已有 explicit 的成员不被覆盖；只回填「无 turn」或「inferred」成员，
            # 且回填后 confidence 取组内 canonical 的（可能从 inferred 升为 explicit）。
            if cur_conf == "explicit":
                continue
            best_conf = "explicit" if explicit_turn_ids else "inferred"
            _assign_turn(corr, best_tid, best_idx, best_conf)
