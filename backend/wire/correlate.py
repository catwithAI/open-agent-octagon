"""显式 anchor correlation。

anchor 带 namespace（`producer-call:` / `provider-response:` / `proxy-request:` /
`source-seq:`），避免不同 ID 空间的裸值撞车。选择优先级：

1. producer/provider 的 call ID（能跨 evidence 出现）；
2. provider response ID；
3. 反代收到请求时生成的 proxy request ID；
4. attempt 内 source 顺序锚点（source-local，永远不会跨 source 合并）。

MCP 的 ``jsonrpc_id`` **只**用于 MCP frame 的 request/response 配对，禁止与
LLM logical call ID 交叉合并——它是每个 MCP 连接自己的小整数序号，跨空间
合并必然错配。

heuristic 评分留后续实现；这里只留接口，显式 anchor 缺失时一律
``unmatched``，不按时间/顺序强配。

``correlation-map.json``（放 ``wire-sources/``）持久化 anchor→logical call ID
映射：离线重建时优先复用旧 ID，parser 升级不会漂移历史 ID。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.wire import ids, writer

CORRELATION_MAP_VERSION = "octagon-wire-correlation-map-v1"

# 显式 anchor 的 namespace 前缀，按优先级排序。
ANCHOR_PRODUCER_CALL = "producer-call"
ANCHOR_PROVIDER_RESPONSE = "provider-response"
ANCHOR_PROXY_REQUEST = "proxy-request"
ANCHOR_SOURCE_SEQ = "source-seq"


def explicit_anchors(hints: dict[str, Any]) -> list[str]:
    """按优先级抽取 evidence correlation_hints 里的显式 anchor（带 namespace）。

    注意：jsonrpc_id 故意不在此列——它只属于 MCP frame 配对空间。
    """
    anchors: list[str] = []
    if hints.get("producer_call_id"):
        anchors.append(f"{ANCHOR_PRODUCER_CALL}:{hints['producer_call_id']}")
    if hints.get("provider_response_id"):
        anchors.append(f"{ANCHOR_PROVIDER_RESPONSE}:{hints['provider_response_id']}")
    if hints.get("request_id"):
        anchors.append(f"{ANCHOR_PROXY_REQUEST}:{hints['request_id']}")
    return anchors


def sequence_anchor(source_kind: str, source_instance: str, sequence: int) -> str:
    """顺序锚点：source-local，包含 source 身份，跨 source 不可能相等。"""
    return f"{ANCHOR_SOURCE_SEQ}:{source_kind}:{source_instance}:{sequence}"


@dataclass
class CorrelationMap:
    """anchor → logical call ID 的持久映射（wire-sources/correlation-map.json）。"""

    attempt_id: str
    anchors: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path, attempt_id: str) -> "CorrelationMap":
        path = Path(path)
        if not path.exists():
            return cls(attempt_id=attempt_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cls(attempt_id=attempt_id)
        if data.get("attempt_id") != attempt_id:
            # 不属于本 attempt 的映射不复用（拷贝/污染防护）。
            return cls(attempt_id=attempt_id)
        anchors = data.get("anchors")
        return cls(
            attempt_id=attempt_id,
            anchors=dict(anchors) if isinstance(anchors, dict) else {},
        )

    def save(self, path: Path) -> None:
        writer.atomic_write_json(
            Path(path),
            {
                "schema_version": CORRELATION_MAP_VERSION,
                "attempt_id": self.attempt_id,
                "anchors": dict(sorted(self.anchors.items())),
            },
        )

    def resolve_groups(self, groups: list[list[str]]) -> list[tuple[str, str, str]]:
        """批量解析：先对全部 anchor 关系做 union，再确定 canonical lc。

        单遍「遇到已知 anchor 就用第一个」会产生 split-brain：native 先出现
        （producer-call:p → lc_A）、gateway 先出现（provider-response:r → lc_B），
        桥接 evidence 同时带 p+r 时必须把两个集合合并到**同一个** lc，并把
        所有成员 anchor 重指过去——否则后续只带 r 的 evidence 永久落在 lc_B。

        规则（确定性，重建幂等）：
        - union 域 = 本批 groups 的边 + 旧映射中同 lc 的 anchor 集；
        - 集合内已有旧 lc 时取字典序最小者为 canonical（与到达顺序无关）；
        - 全新集合用最高优先级 anchor 生成 lc；
        - 返回与输入 groups 对齐的 (lc, chosen_anchor, confidence)。
        """
        parent: dict[str, str] = {}

        def find(a: str) -> str:
            parent.setdefault(a, a)
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        # 旧映射：同 lc 的 anchor 天然同集合
        by_lc: dict[str, list[str]] = {}
        for anchor, lc in self.anchors.items():
            by_lc.setdefault(lc, []).append(anchor)
        for members in by_lc.values():
            for other in members[1:]:
                union(members[0], other)
        # 本批边（单元素 group 也要注册进 union 域）
        for group in groups:
            if not group:
                raise ValueError("resolve_groups 的每个 group 需要至少一个 anchor")
            find(group[0])
            for other in group[1:]:
                union(group[0], other)

        # 每个集合确定 canonical lc
        members_by_root: dict[str, list[str]] = {}
        for anchor in parent:
            members_by_root.setdefault(find(anchor), []).append(anchor)
        lc_by_root: dict[str, str] = {}
        for root, members in members_by_root.items():
            existing = sorted({self.anchors[a] for a in members if a in self.anchors})
            if existing:
                lc = existing[0]  # 字典序最小，确定性合并 split 集合
            else:
                lc = ids.logical_call_id(
                    attempt_id=self.attempt_id, call_anchor=_best_anchor(members)
                )
            lc_by_root[root] = lc
            for a in members:
                self.anchors[a] = lc

        out: list[tuple[str, str, str]] = []
        for group in groups:
            chosen = _best_anchor(group)
            out.append((lc_by_root[find(group[0])], chosen, _anchor_confidence(chosen)))
        return out

    def resolve_call(self, anchors: list[str]) -> tuple[str, str, str]:
        """单 group 便捷入口（语义同 resolve_groups([anchors])[0]）。"""
        return self.resolve_groups([anchors])[0]


# namespace 优先级：producer-call > provider-response > proxy-request > seq。
_ANCHOR_PRIORITY = {
    ANCHOR_PRODUCER_CALL: 0,
    ANCHOR_PROVIDER_RESPONSE: 1,
    ANCHOR_PROXY_REQUEST: 2,
    ANCHOR_SOURCE_SEQ: 3,
}


def _best_anchor(anchors: list[str]) -> str:
    return min(anchors, key=lambda a: (_ANCHOR_PRIORITY.get(a.split(":", 1)[0], 9), a))


def _anchor_confidence(anchor: str) -> str:
    return (
        "inferred" if anchor.startswith(f"{ANCHOR_SOURCE_SEQ}:") else "explicit"
    )


def pair_mcp_frames(frames: list[dict[str, Any]]) -> None:
    """配对 MCP request/response，原地写 ``data.paired_record_id``。

    优先用 tap 计算好的 ``_paired_anchor``（类型/方向正确，request 与
    response 携带同一 anchor）——按 anchor 分组即得配对。无 anchor 的记录（非 tap
    源）退回 (instance, direction, id) 配对：MCP 双向 RPC，response 与它应答的
    request 方向相反，故按方向区分避免 client/server 同 id 互相覆盖。
    notification（无 id）不参与配对。pair 后删内部 ``_paired_anchor``。
    """
    _opposite = {
        "client-to-server": "server-to-client",
        "server-to-client": "client-to-server",
    }
    by_anchor: dict[str, list[dict[str, Any]]] = {}
    # (instance, request_direction, id) → request record（fallback 路径）。
    pending: dict[tuple[str, str, str], dict[str, Any]] = {}
    for record in frames:
        data = record.get("data", {})
        kind = data.get("message_kind")
        if kind not in ("request", "response"):
            continue
        anchor = data.get("_paired_anchor")
        if anchor:
            by_anchor.setdefault(anchor, []).append(record)
            continue
        # fallback：无 tap anchor。
        rpc_id = data.get("jsonrpc_id")
        if not rpc_id:
            continue
        inst = record.get("source", {}).get("instance", "")
        direction = data.get("direction") or ""
        if kind == "request":
            pending[(inst, direction, str(rpc_id))] = record
        else:
            req_dir = _opposite.get(direction, "")
            req = pending.pop((inst, req_dir, str(rpc_id)), None)
            if req is not None:
                req["data"]["paired_record_id"] = record["record_id"]
                data["paired_record_id"] = req["record_id"]
    # anchor 分组内配对（一个 request + 一个 response 共享 anchor）。
    for group in by_anchor.values():
        reqs = [r for r in group if r["data"]["message_kind"] == "request"]
        resps = [r for r in group if r["data"]["message_kind"] == "response"]
        if reqs and resps:
            req, resp = reqs[0], resps[0]
            req["data"]["paired_record_id"] = resp["record_id"]
            resp["data"]["paired_record_id"] = req["record_id"]
    # 清理内部字段。
    for record in frames:
        record.get("data", {}).pop("_paired_anchor", None)


def heuristic_match(*_args: Any, **_kwargs: Any) -> None:
    """heuristic 评分留后续实现。

    契约：没有显式 anchor 的跨 source 候选一律不合并（unmatched），
    绝不按时间/顺序强配——这里显式返回 None 而不是悄悄打分。
    """
    return None
