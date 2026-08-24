"""跨 source 语义 hash。

问题：同一次 LLM 调用被不同 source 看到时（gateway 对其 JSON 算、反代对原始
body 算、MCP tap 对包了 content[] 的 result 算），若直接对原始字节做 hash，
JSON 键序 / 空白 / unicode 转义 / 结构包装任一差异都让 hash 永不相等，compaction
分型和工具结果形态判定就退化成只剩 size 启发。

解法：每个协议 normalizer 先把 message/system/tools/tool_result 映射到与协议
无关的 semantic IR，再统一 NFC + RFC 8785 JCS 序列化后 SHA-256。等价的
Anthropic/OpenAI/Responses 输入因此得到同一 hash。

本模块只实现协议无关的 IR → hash；各协议 → IR 的映射由 native normalizer
和 HTTP parser 各自实现，输出本模块的 IR 形状。
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any, Literal

import rfc8785

# hash_domain 常量。每条 hash 必须同时携带 algorithm 和 domain，只有同 domain
# 才能比较。
DOMAIN_RAW_BYTES = "raw-bytes-v1"
DOMAIN_SEMANTIC = "octagon-semantic-jcs-nfc-v1"

HashDomain = Literal["raw-bytes-v1", "octagon-semantic-jcs-nfc-v1"]

IRKind = Literal["messages", "system", "tools", "tool_result"]


class SemanticHashError(ValueError):
    """无法生成 canonical semantic hash（如 NFC 后 key 冲突）。

    调用方收到此异常时必须回退到 bytes/null 或 producer 私有 domain，
    绝不能伪造一个 ``octagon-semantic-jcs-nfc-v1`` 值。
    """


def raw_bytes_hash(data: bytes) -> str:
    """raw-bytes-v1：对确切收到的字节做 SHA-256，返回 64 位小写 hex。

    只用于完整性、同一 hop 重放、同一 domain 内比较——不能跨 source 比较。
    """
    return hashlib.sha256(data).hexdigest()


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _nfc_recursive(value: Any) -> Any:
    """对 IR 里所有字符串做 NFC；保留 JSON 类型（int/float/bool/None 原样）。

    NFC 后出现 dict key 冲突（两个不同码点序列规范化成同一字符串）时抛
    SemanticHashError——静默合并会让内容在不同 source 下不可复现。
    """
    if isinstance(value, str):
        return _nfc(value)
    if isinstance(value, list):
        return [_nfc_recursive(v) for v in value]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            nk = _nfc(k) if isinstance(k, str) else k
            if nk in out:
                raise SemanticHashError(f"NFC 后 key 冲突: {nk!r}")
            out[nk] = _nfc_recursive(v)
        return out
    return value


def semantic_hash(kind: IRKind, value: Any) -> str:
    """对 semantic IR 计算 octagon-semantic-jcs-nfc-v1，返回 64 位小写 hex。

    IR 形状固定为 ``{"kind": <IRKind>, "value": <value>}``。
    步骤：递归 NFC → RFC 8785 JCS 序列化 → SHA-256。
    """
    ir = {"kind": kind, "value": _nfc_recursive(value)}
    try:
        canonical = rfc8785.dumps(ir)
    except (TypeError, ValueError) as exc:
        raise SemanticHashError(f"JCS 序列化失败: {exc}") from exc
    return hashlib.sha256(canonical).hexdigest()


# ---------- IR 构造辅助 --------------------------------------------------
#
# 协议 normalizer 用下面的辅助把各家 message/tools 映射成规定的 IR，
# 再传给 semantic_hash。集中在此保证所有协议走同一套规则。


def sort_tools_ir(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """tools IR：按 NFC 后的 name 排序（协议间工具声明顺序不具语义）。

    每项形状为 ``{name, description, input_schema}``。同名工具保持原相对顺序
    （稳定排序），由调用方另记 parse gap。
    """
    return sorted(tools, key=lambda t: _nfc(str(t.get("name", ""))))


def part_semantic_hash(
    parts: list[dict[str, Any]], role: str = "assistant"
) -> tuple[str | None, int | None]:
    """content parts → (semantic_hash, utf8 bytes)，用规定的
    messages IR ``[{role, content:[part...]}]`` 形状（不是裸 parts）。

    跨 producer 共用（CC/Codex/Blade normalizer 都调它）：等价内容得同 hash。
    空 parts 或 hash 失败返回 (None, None)，不伪造 hash。

    原 ``claude_code._part_semantic_hash``，Phase 1 搬到此处——它本来就是
    跨 producer 的公共 helper，不应长在某个
    具体 normalizer 里（codex.py 曾靠函数级动态 import 复用）。
    """
    if not parts:
        return None, None
    ir = [{"role": role, "content": parts}]
    try:
        h = semantic_hash("messages", ir)
    except SemanticHashError:
        return None, None
    try:
        size = len(json.dumps(parts, ensure_ascii=False, default=str).encode("utf-8"))
    except Exception:
        size = None
    return h, size
