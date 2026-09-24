"""把 attempt 的 ATIF 轨迹物化成文件，供外部 judge 按需自取。

为什么 judge 该读 ATIF 而不是 raw ``events.jsonl``：

1. **归一化**。events.jsonl 是各家 adapter 各自的原始事件流，六个 agent 的结构
   互不相同。拿它当证据，等于给不同 agent 喂结构不同的材料——尤其在比较式
   评分里，排序会被证据形态本身带偏，而不是被交付物质量带偏。ATIF 是同一套
   step/tool_call/observation 结构。
2. **体积**。2026-09-24 实测 run_c954f893a840 的六个 attempt：

       agent          events.jsonl      ATIF
       dsh                6022 KB     130 KB     46×
       claude-code        1814 KB     310 KB      6×
       kimi-code           115 KB     131 KB      —
       mimo-code           103 KB      58 KB      2×
       opencode             81 KB      43 KB      2×

   raw 的跨 agent 极差是 74 倍（81KB ↔ 6022KB），ATIF 收敛到 7 倍。

3. **不内联**。产物写成文件、只把**路径**交给 judge。agentic judge 有
   read/bash，需要哪段自取；把 135KB 证据内联进 prompt 反而把网关卡死
   （2026-09-24 实测单次请求挂了 10 分钟，0.4% CPU，纯等响应）。

已知缺口：``blade-agent`` 不在 ATIF 的 ``_SUPPORTED_AGENTS`` 里，emit 直接
``not_available``。该 agent 的 judge 只能退回 attempt_dir 原始文件——这条链上
它与其他五家的证据形态**不对等**，比较式评分里尤其要留意。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

#: 物化产物相对 attempt 目录的位置。与 automation/diagnosis.json、
#: attribution/<dim>.json 并列——都是派生投影，可随时重建。
ATIF_RELPATH = Path("atif") / "trajectory.json"


def atif_path(data_path: Path, attempt_id: str) -> Path:
    return Path(data_path) / "attempts" / attempt_id / ATIF_RELPATH


def attempt_agent_name(data_path: Path, attempt_id: str) -> str | None:
    """库里的 agent 名——权威值，优先于按沙盒目录标记推断。"""
    db = Path(data_path) / "octagon.db"
    if not db.is_file():
        return None
    try:
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT agent_name FROM attempts WHERE id=?", (attempt_id,)
            ).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row and row[0] else None


def materialize_atif(
    data_path: Path, attempt_id: str, *, agent_name: str | None = None
) -> Path | None:
    """生成（或复用）该 attempt 的 ATIF 文件，返回路径；不可用时 None。

    best-effort：ATIF 不可用（如 blade-agent 无转换器、转录缺失）不是错误，
    judge 退回读 attempt_dir 原始文件即可——所以这里只记日志，不抛。
    """
    from .atif.emitter import emit_attempt_atif

    target = atif_path(data_path, attempt_id)
    attempt_dir = Path(data_path) / "attempts" / attempt_id
    if not attempt_dir.is_dir():
        return None
    agent = agent_name or attempt_agent_name(data_path, attempt_id)
    try:
        outcome = emit_attempt_atif(
            attempt_dir, agent_name=agent, attempt_id=attempt_id
        )
    except Exception:  # pragma: no cover - 转换器异常不该拖垮评分
        logger.exception("ATIF 物化失败 attempt=%s", attempt_id)
        return None
    if outcome.status != "ready" or outcome.trajectory is None:
        logger.info(
            "ATIF 不可用 attempt=%s agent=%s: %s",
            attempt_id, agent, outcome.reason,
        )
        return None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(outcome.trajectory, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except OSError:
        logger.warning("ATIF 写入失败 attempt=%s", attempt_id)
        return None
    return target
