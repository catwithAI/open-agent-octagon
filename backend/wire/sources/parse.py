"""三协议 request/response parser → 公共 semantic IR。

反向代理观测到的 HTTP body 有三种 LLM 协议：

- ``anthropic-messages``：``{model, system, messages, tools}``；
- ``openai-chat-completions``：``{model, messages, tools:[{function}]}``；
- ``openai-responses``：``{model, instructions, input, tools}``。

三者都映射到规定的**公共 messages IR** ``[{role, content:[part...]}]``，
再用 ``hashing.semantic_hash("messages", ir)`` 得跨协议一致的 semantic hash——
本任务不另造协议私有 hash。part 形状：

- ``{"type":"text","text":...}``
- ``{"type":"tool_call","name":...,"arguments":{...}}``
- ``{"type":"tool_result","content":...}``

**透明性优先**：解析失败（未知协议、body 非 JSON、字段缺失）返回带
``degraded`` 标记的 summary（只有 bytes/null，绝不伪造 semantic hash），调用方
据此写 capability 退化 metadata 并照常转发。

跨协议一致性由 ``tests/test_wire_hashing.py`` 的 golden fixture 守护。
"""

from __future__ import annotations

import json
import re
from typing import Any

from backend.wire import hashing
from backend.wire.evidence import RequestSummary, ResponseSummary

# 已过 canonical 化的 provider kind。
_ANTHROPIC = "anthropic-messages"
_OPENAI_CHAT = "openai-chat-completions"
_OPENAI_RESPONSES = "openai-responses"


def _text_of(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _content_to_ir_parts(content: Any) -> list[dict[str, Any]]:
    """协议 content（str 或 block list）→ 公共 IR parts。

    Anthropic block（type=text/thinking/tool_use/tool_result）与 OpenAI
    Responses block（type=input_text/output_text/...）统一映射到公共 part。
    """
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        return [{"type": "text", "text": _text_of(content)}]
    parts: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, str):
            if block:
                parts.append({"type": "text", "text": block})
            continue
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype in ("text", "input_text", "output_text"):
            parts.append({"type": "text", "text": block.get("text", "")})
        elif btype in ("thinking", "reasoning"):
            parts.append({"type": "text", "text": block.get("thinking") or block.get("text", "")})
        elif btype in ("tool_use", "function_call"):
            # Anthropic tool_use.input / Responses function_call.arguments
            raw = block.get("input")
            if raw is None:
                raw = block.get("arguments")
            parts.append({
                "type": "tool_call",
                "name": block.get("name", ""),
                "arguments": _normalize_tool_args(raw),
            })
        elif btype in ("tool_result", "function_call_output"):
            parts.append({
                "type": "tool_result",
                "content": block.get("content") if "content" in block else block.get("output", ""),
            })
    return parts


def _maybe_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return value
    return value


def _normalize_tool_args(raw: Any) -> dict[str, Any]:
    """tool_call arguments 归一：三协议统一到同一形状，否则同一逻辑
    tool_call 的 arguments 若是 JSON 数组/字符串，各协议路径产出不同 → hash 不一致。

    - None → {}；
    - JSON 字符串 → 解析（Chat/Responses 的 arguments 常是字符串）；
    - 解析后是 dict → 原样；非 dict（数组/标量/解析失败字符串）→ 包 {"_raw": ...}。
    """
    if raw is None:
        return {}
    parsed = _maybe_json(raw)
    if isinstance(parsed, dict):
        return parsed
    return {"_raw": parsed}


def _openai_chat_message_parts(msg: dict[str, Any]) -> list[dict[str, Any]]:
    """Chat Completions message → IR parts（含 tool_calls / tool role）。"""
    parts: list[dict[str, Any]] = []
    content = msg.get("content")
    if content:
        parts.extend(_content_to_ir_parts(content))
    for call in msg.get("tool_calls") or []:
        fn = (call or {}).get("function") or {}
        parts.append({
            "type": "tool_call",
            "name": fn.get("name", ""),
            "arguments": _normalize_tool_args(fn.get("arguments")),
        })
    if msg.get("role") == "tool":
        parts.append({"type": "tool_result", "content": msg.get("content", "")})
    return parts


def _messages_ir_anthropic(payload: dict[str, Any]) -> list[dict[str, Any]]:
    ir: list[dict[str, Any]] = []
    for msg in payload.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        ir.append({
            "role": msg.get("role", ""),
            "content": _content_to_ir_parts(msg.get("content")),
        })
    return ir


def _messages_ir_openai_chat(payload: dict[str, Any]) -> list[dict[str, Any]]:
    ir: list[dict[str, Any]] = []
    for msg in payload.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        # system 走 system_hash（与 Anthropic 顶层 system / Responses instructions
        # 对齐），不进 messages IR——否则同一逻辑对话在 chat 协议下多一条 system
        # 消息，messages_hash 跨协议不一致。
        if msg.get("role") == "system":
            continue
        ir.append({"role": msg.get("role", ""), "content": _openai_chat_message_parts(msg)})
    return ir


def _messages_ir_openai_responses(payload: dict[str, Any]) -> list[dict[str, Any]]:
    ir: list[dict[str, Any]] = []
    inp = payload.get("input")
    if isinstance(inp, str):
        return [{"role": "user", "content": [{"type": "text", "text": inp}]}] if inp else []
    for item in inp or []:
        if not isinstance(item, dict):
            continue
        ir.append({
            "role": item.get("role", ""),
            "content": _content_to_ir_parts(item.get("content")),
        })
    return ir


def _system_ir(payload: dict[str, Any], kind: str) -> Any:
    """system/instructions → IR（str 或 block list 归一为纯文本序列）。"""
    if kind == _ANTHROPIC:
        sys = payload.get("system")
    elif kind == _OPENAI_RESPONSES:
        sys = payload.get("instructions")
    else:  # chat completions: system 是 messages 里的 role=system，这里另取顶层无
        sys = None
        for msg in payload.get("messages") or []:
            if isinstance(msg, dict) and msg.get("role") == "system":
                sys = msg.get("content")
                break
    if sys is None:
        return None
    if isinstance(sys, str):
        return sys or None
    if isinstance(sys, list):
        texts = [b.get("text", "") for b in sys if isinstance(b, dict)]
        return texts or None
    return _text_of(sys)


def _tools_ir(payload: dict[str, Any], kind: str) -> list[dict[str, Any]] | None:
    """tools → 归一 IR ``[{name, description, input_schema}]``，按 name 排序。"""
    raw = payload.get("tools")
    if not raw or not isinstance(raw, list):
        return None
    out: list[dict[str, Any]] = []
    for tool in raw:
        if not isinstance(tool, dict):
            continue
        if kind == _ANTHROPIC:
            out.append({
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "input_schema": tool.get("input_schema", {}),
            })
        else:
            # OpenAI chat/responses: {type:"function", function:{name,description,parameters}}
            fn = tool.get("function") or tool
            out.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", fn.get("input_schema", {})),
            })
    if not out:
        return None
    return hashing.sort_tools_ir(out)


def _hash_or_none(kind: hashing.IRKind, value: Any) -> str | None:
    if value is None or value == [] or value == "":
        return None
    try:
        return hashing.semantic_hash(kind, value)
    except hashing.SemanticHashError:
        return None


def parse_request(provider_kind: str, body: bytes) -> RequestSummary | None:
    """request body → RequestSummary（含跨协议 semantic hash）。

    解析失败（非 JSON / 未知协议 / 无消息）返回带 hash_domain=None 的退化
    summary（bytes 计数保留，hash 全 None）；完全无法读时返回 None（capability
    退化，调用方标 metadata）。
    """
    if not body:
        return None
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _degraded_request_summary(len(body))
    if not isinstance(payload, dict):
        return _degraded_request_summary(len(body))

    if provider_kind == _ANTHROPIC:
        messages_ir = _messages_ir_anthropic(payload)
    elif provider_kind == _OPENAI_CHAT:
        messages_ir = _messages_ir_openai_chat(payload)
    elif provider_kind == _OPENAI_RESPONSES:
        messages_ir = _messages_ir_openai_responses(payload)
    else:
        return _degraded_request_summary(len(body), model=payload.get("model"))

    system_ir = _system_ir(payload, provider_kind)
    tools_ir = _tools_ir(payload, provider_kind)

    messages_hash = _hash_or_none("messages", messages_ir)
    system_hash = _hash_or_none("system", system_ir)
    tools_hash = _hash_or_none("tools", tools_ir)
    has_semantic = any(h is not None for h in (messages_hash, system_hash, tools_hash))

    return RequestSummary(
        model=payload.get("model"),
        message_count=len(messages_ir) if messages_ir else None,
        message_bytes=len(body),
        system_hash=system_hash,
        messages_hash=messages_hash,
        tools_hash=tools_hash,
        # 无任何完整语义内容时不伪造 domain（评审：只写 bytes/null）。
        hash_domain=hashing.DOMAIN_SEMANTIC if has_semantic else None,
    )


def _degraded_request_summary(nbytes: int, *, model: Any = None) -> RequestSummary:
    return RequestSummary(
        model=model if isinstance(model, str) else None,
        message_count=None, message_bytes=nbytes,
        system_hash=None, messages_hash=None, tools_hash=None,
        hash_domain=None,
    )


def parse_response(provider: str, body: bytes) -> ResponseSummary | None:
    """response body → ResponseSummary。

    非流式 JSON response 解析出 content_hash / output_blocks；SSE 拼接或解析失败
    时返回带 hash_domain=None 的退化 summary（只 message_bytes）。
    """
    if not body:
        return None
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        # SSE / 非 JSON：无法可靠还原语义，只记 bytes（不伪造 hash）。
        return ResponseSummary(
            content_hash=None, hash_domain=None,
            message_bytes=len(body), output_blocks=None,
        )
    if not isinstance(payload, dict):
        return ResponseSummary(
            content_hash=None, hash_domain=None, message_bytes=len(body), output_blocks=None,
        )
    parts = _response_content_parts(payload)
    content_hash = _hash_or_none("messages", [{"role": "assistant", "content": parts}] if parts else None)
    return ResponseSummary(
        content_hash=content_hash,
        hash_domain=hashing.DOMAIN_SEMANTIC if content_hash else None,
        message_bytes=len(body),
        output_blocks=len(parts) if parts else None,
    )


def _provider_id_from_obj(obj: Any) -> str | None:
    """从单个已解析 JSON 对象提取 provider call ID（覆盖非流式 + SSE 各协议形态）。

    - 非流式 / OpenAI chat chunk：顶层 ``id``（chatcmpl-.../resp_...）；
    - Anthropic SSE ``message_start``：``message.id``（msg_...）；
    - Responses SSE ``response.created``/``response.completed``：``response.id``（resp_...）。
    """
    if not isinstance(obj, dict):
        return None
    # 嵌套优先（SSE 事件把 id 放进 message/response）。
    for nest in ("message", "response"):
        inner = obj.get(nest)
        if isinstance(inner, dict):
            rid = inner.get("id")
            if isinstance(rid, str) and rid:
                return rid
    rid = obj.get("id")
    return rid if isinstance(rid, str) and rid else None


def extract_provider_response_id(body: bytes) -> str | None:
    """从 response body 提取 provider call ID。

    覆盖**非流式 JSON 与 SSE 流式**——CC/Codex 主要走流式，只支持非流式会导致真实
    comparison run 的 hop 大概率 unmatched。三协议：
    - Anthropic Messages：非流式顶层 ``id``；SSE ``message_start`` 的 ``message.id``；
    - OpenAI Chat Completions：非流式顶层 ``id``；SSE chunk 顶层 ``id``；
    - OpenAI Responses：非流式顶层 ``id``；SSE ``response.created`` 的 ``response.id``。

    流中断只要已收到带 id 的早期事件即可提取（扫到第一个即返回）。无法解析 / 无 id
    时返回 None——不猜、不伪造。
    """
    if not body:
        return None
    # 1) 先试整体非流式 JSON。
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        payload = None
    if payload is not None:
        return _provider_id_from_obj(payload)
    # 2) SSE：逐 data: 事件解析，扫到第一个带 id 的即返回（id 通常在首个事件）。
    try:
        text = body.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    for raw_event in re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n")):
        data_parts = [
            ln[len("data:"):].lstrip(" ")
            for ln in raw_event.split("\n") if ln.startswith("data:")
        ]
        if not data_parts:
            continue
        joined = "\n".join(data_parts)
        if joined.strip() in ("[DONE]",):
            continue
        try:
            obj = json.loads(joined)
        except (json.JSONDecodeError, ValueError):
            continue
        rid = _provider_id_from_obj(obj)
        if rid:
            return rid
    return None


def _response_content_parts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """三协议 response → assistant content IR parts。"""
    # Anthropic Messages: {content:[block...]}
    if isinstance(payload.get("content"), list):
        return _content_to_ir_parts(payload["content"])
    # OpenAI Responses: {output:[{content:[...]}...]}
    if isinstance(payload.get("output"), list):
        parts: list[dict[str, Any]] = []
        for item in payload["output"]:
            if isinstance(item, dict):
                parts.extend(_content_to_ir_parts(item.get("content")))
        return parts
    # Chat Completions: {choices:[{message:{content, tool_calls}}]}
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        msg = (choices[0] or {}).get("message") or {}
        return _openai_chat_message_parts(msg)
    return []
