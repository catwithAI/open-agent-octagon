"""确定性 ID 生成。

所有 wire ID 用 uuid5 从稳定输入派生，保证离线重建（rebuild）时同一份原始
证据得到同一 ID——不依赖运行时随机数或时钟，这是「parser 升级后对历史
raw events 离线重建」的前提。

前缀约定：
- ``wr_`` canonical record（finalized wire.jsonl 一行）
- ``we_`` source evidence（spool 一行）
- ``lc_`` logical call（一次业务意义的 LLM 调用，可跨多 hop）
- ``hop_`` 一次具体传输 hop
- ``ts_`` trajectory step
"""

from __future__ import annotations

import uuid

# wire ID 的 uuid5 命名空间。固定值，改动会使所有历史 attempt 的 rebuild 结果
# 与旧数据不一致，因此视为 schema 的一部分，不可变更。
_WIRE_NS = uuid.UUID("6f2a5b1e-9c3d-5e4f-8a1b-0c9d8e7f6a5b")

# 组合多段输入时用的分隔符。用 NUL 避免任何字段值里的可见字符造成拼接歧义
# （如 "a" + "b/c" 与 "a/b" + "c" 必须不同）。
_SEP = "\x00"


def _uuid5(*parts: str) -> str:
    """对若干字符串段做 uuid5，返回 32 位 hex（无连字符）。"""
    name = _SEP.join(parts)
    return uuid.uuid5(_WIRE_NS, name).hex


def evidence_id(
    *,
    attempt_id: str,
    source_kind: str,
    source_instance: str,
    raw_ref: str,
    producer_id: str = "",
) -> str:
    """we_ —— source evidence ID。

    ``raw_ref`` 必须是相对路径 + 行号/事件稳定 ID 的字符串，禁止绝对路径
    （否则同一 attempt 换机器重建会得到不同 ID）。
    """
    return "we_" + _uuid5(
        attempt_id, source_kind, source_instance, raw_ref, producer_id
    )


def logical_call_id(*, attempt_id: str, call_anchor: str) -> str:
    """lc_ —— logical call ID。

    ``call_anchor`` 是 finalizer 按优先级选出的稳定锚点（producer
    call/response id > native turn id > proxy request id > source 顺序锚）。
    同一 anchor 恒定映射到同一 lc_，因此后到的 gateway evidence 不会在同一次
    finalize 里产生第二个 call。
    """
    return "lc_" + _uuid5(attempt_id, call_anchor)


def hop_id(*, attempt_id: str, source_instance: str, hop_anchor: str) -> str:
    """hop_ —— 一次具体传输 hop。"""
    return "hop_" + _uuid5(attempt_id, source_instance, hop_anchor)


def trajectory_step_id(*, attempt_id: str, step_anchor: str) -> str:
    """ts_ —— trajectory step。

    ``step_anchor`` 通常是 native event 的相对文件路径 + 行号，使 step_id 与
    events.jsonl 行锚定，可做 referential-integrity check。
    """
    return "ts_" + _uuid5(attempt_id, step_anchor)


def record_id(*, attempt_id: str, record_kind: str, record_anchor: str) -> str:
    """wr_ —— canonical record ID（finalized wire.jsonl 一行）。

    ``record_anchor`` 由 finalizer 按 record type 选取（如 hop 用 hop_anchor、
    llm_call 用 chosen call anchor），保证同一份证据重建出同一 wr_。
    """
    return "wr_" + _uuid5(attempt_id, record_kind, record_anchor)
