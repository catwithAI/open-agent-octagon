"""把各 agent 的 events.jsonl 归一成 trace 形状的工具调用序列。

存在的理由是采集通道按 agent 分化：只有 blade-agent 写 trace.jsonl，五个 CLI
adapter 的工具调用只落在 events.jsonl 里。安全扫描的业务层、HITL 两条通道都只吃
trace，于是 opencode / kimi-code / mimo-code 的安全轴恒为零——不是低，是绝对零，
因为 evaluator.load_trace() 在文件缺失时静默返回 []，「没采集」和「真干净」在数据上
长得一模一样。

这里把 events 归一成与 trace 同构的行：

    {"tool_name": str, "arguments": dict, "result": Any, "source_ref": {...}}

下游（业务层匹配 danger_tools、hitl.py 的顺序状态机）因此不必再认六种形状。

六家的实测形状（data/attempts 样本 + backend/wire/normalizers 记录）：

- claude-code / ssh-claude-code：``type=assistant`` → ``message.content[].tool_use``，
  ``name`` + ``input``（真 dict）
- codex：``type=item.started|item.completed`` → ``item.type=command_execution``，
  整条 shell 串在 ``item.command``（没有 name/args 之分），以及
  ``item.type=function_call`` 形态的 ``item.name`` + ``item.arguments``（JSON 串）
- opencode / mimo-code：``type=tool_use`` → ``part.tool`` + ``part.state.input``
- kimi-code：``{"role": ...}`` 平铺行，工具调用走 OpenAI 的
  ``tool_calls[].function.{name,arguments}``，arguments 是 JSON **字符串**
- blade-agent：``kind=llm:tool_call:created`` 信封，``raw.payload.function.name``
  加分片流式的 ``raw.payload.arguments_delta``——参数要按 tool_call_id 拼接才完整

blade 自己有 trace，这里覆盖它只是为了 trace 缺失时（socket 断流等）仍有兜底。
"""

from __future__ import annotations

import json
from typing import Any

# 结果类事件：用于给已归一的调用回填 result（HITL 判 approval 决定要读 result.decision）。
_RESULT_KINDS = ("tool:result:done", "tool:result")


def _loads_maybe(value: Any) -> Any:
    """OpenAI 系的 arguments 是 JSON 字符串；解不动就原样返回。"""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _as_args(value: Any) -> dict[str, Any]:
    decoded = _loads_maybe(value)
    if isinstance(decoded, dict):
        return decoded
    if decoded in (None, "", {}):
        return {}
    # 非 dict（如 codex 的整条 command 串）保留原文，下游按 _raw 兜底读。
    return {"_raw": decoded}


def _row(
    tool_name: str,
    arguments: Any,
    *,
    line: int,
    result: Any = None,
    call_id: str = "",
    exit_code: Any = None,
) -> dict[str, Any]:
    """一行归一后的工具调用。

    `exit_code` 独立成字段而不塞进 `result`：安全轴已经在消费 `result` 的原始形状
    （各家不同：codex 是 aggregated_output、opencode 是 output），改它会波及既有扫描。
    swebench 的 validation 维度要求命令**成功**（`exit_code in (0, "0")`），只看
    「跑过 pytest」会把失败的测试也判成通过。各家藏退出码的位置不同，在各自的
    `_*_calls` 里取出后统一落到这里。
    """
    return {
        "tool_name": tool_name or "unknown",
        "arguments": _as_args(arguments),
        "result": result,
        "exit_code": exit_code,
        "tool_call_id": call_id,
        "source_ref": {"log": "events.jsonl", "line": line},
    }


def _cc_calls(ev: dict[str, Any], line: int) -> list[dict[str, Any]]:
    """claude-code / ssh-claude-code：assistant 消息里的 tool_use 块。"""
    out: list[dict[str, Any]] = []
    content = (ev.get("message") or {}).get("content") or []
    if not isinstance(content, list):
        return out
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        out.append(
            _row(
                block.get("name", ""),
                block.get("input") or {},
                line=line,
                call_id=str(block.get("id") or ""),
            )
        )
    return out


def _codex_calls(ev: dict[str, Any], line: int) -> list[dict[str, Any]]:
    """codex：item 信封。command_execution 没有 name/args 之分，整串在 command。"""
    item = ev.get("item")
    if not isinstance(item, dict):
        return []
    itype = item.get("type")
    if itype == "command_execution":
        command = item.get("command")
        if not isinstance(command, str) or not command.strip():
            return []
        return [
            _row(
                "run_shell",
                {"command": command},
                line=line,
                result=item.get("aggregated_output"),
                call_id=str(item.get("id") or ""),
                exit_code=item.get("exit_code"),
            )
        ]
    if itype in ("function_call", "tool_call", "mcp_tool_call"):
        return [
            _row(
                item.get("name") or item.get("tool") or "",
                item.get("arguments") or item.get("input") or {},
                line=line,
                result=item.get("output") or item.get("result"),
                call_id=str(item.get("id") or ""),
            )
        ]
    return []


def _opencode_calls(ev: dict[str, Any], line: int) -> list[dict[str, Any]]:
    """opencode / mimo-code：part 信封，两者字段逐一同构。"""
    part = ev.get("part")
    if not isinstance(part, dict) or part.get("type") != "tool":
        return []
    state = part.get("state")
    state = state if isinstance(state, dict) else {}
    meta = state.get("metadata")
    meta = meta if isinstance(meta, dict) else {}
    return [
        _row(
            part.get("tool", ""),
            state.get("input") or {},
            line=line,
            result=state.get("output"),
            call_id=str(part.get("callID") or ""),
            exit_code=meta.get("exit"),
        )
    ]


def _dsh_calls(ev: dict[str, Any], line: int) -> list[dict[str, Any]]:
    """dsh：`tool/call` 事件本身就是 {name, arguments, callId}，最直白的一家。

    `arguments` 是模型原样产出的 JSON **字符串**（未解析），`_as_args` 负责
    还原成 dict。结果由 `tool/result` 事件按 callId 回填（见 _dsh_result）。
    """
    data = ev.get("data")
    if not isinstance(data, dict):
        return []
    return [
        _row(
            str(data.get("name") or ""),
            data.get("arguments"),
            line=line,
            call_id=str(data.get("callId") or ""),
        )
    ]


def _dsh_result(ev: dict[str, Any]) -> tuple[str, Any] | None:
    """dsh 的 `tool/result` → (call_id, 结果正文)。

    配对键在 `data.message.content[0].toolCallId`，不在顶层。
    注意 `isError` 只反映**工具框架层**失败——bash 命令的非零退出码是
    `isError=False`，exit code 写在结果文本里（实测，见
    tests/fixtures/dsh/README.md）。
    """
    from backend.adapters.dsh_events import tool_result_call_id

    data = ev.get("data")
    if not isinstance(data, dict):
        return None
    call_id = tool_result_call_id(data)   # 配对键与 adapter/normalizer 同源
    if not call_id:
        return None
    message = data.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    block = content[0] if isinstance(content, list) and content else {}
    return call_id, block.get("content") if isinstance(block, dict) else None


def _openai_tool_calls(container: dict[str, Any], line: int) -> list[dict[str, Any]]:
    """kimi-code 等：OpenAI 风格 tool_calls[]，arguments 是 JSON 字符串。"""
    calls = container.get("tool_calls")
    if not isinstance(calls, list):
        return []
    out: list[dict[str, Any]] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        fn = call.get("function")
        fn = fn if isinstance(fn, dict) else {}
        out.append(
            _row(
                fn.get("name") or call.get("name") or "",
                fn.get("arguments") if "arguments" in fn else call.get("arguments"),
                line=line,
                call_id=str(call.get("id") or ""),
            )
        )
    return out


def _blade_call_fragment(ev: dict[str, Any]) -> tuple[str, str, str] | None:
    """blade 的 llm:tool_call:created 分片 → (call_id, name, arguments_fragment)。"""
    payload = (ev.get("raw") or {}).get("payload")
    if not isinstance(payload, dict):
        return None
    fn = payload.get("function")
    fn = fn if isinstance(fn, dict) else {}
    call_id = str(payload.get("id") or "")
    name = str(fn.get("name") or "")
    fragment = payload.get("arguments_delta")
    if not isinstance(fragment, str):
        fragment = fn.get("arguments") if isinstance(fn.get("arguments"), str) else ""
    return call_id, name, fragment or ""


def tool_calls_from_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """归一后的工具调用序列，保持事件里的先后顺序（HITL 判定依赖顺序）。

    blade 的流式分片按 tool_call_id 拼回完整参数；调用在序列里的位置以第一个分片
    出现的位置为准，这样 approval 与危险调用的先后关系与实际执行一致。
    """
    out: list[dict[str, Any]] = []
    # blade 流式：call_id → (在 out 里的下标, 已累积的参数串)
    blade_pending: dict[str, list[Any]] = {}
    # call_id → out 下标，用于回填 result
    by_call_id: dict[str, int] = {}

    for i, ev in enumerate(events):
        if not isinstance(ev, dict):
            continue
        line = i + 1
        etype = ev.get("type")
        kind = ev.get("kind")
        rows: list[dict[str, Any]] = []

        if etype == "assistant":
            rows = _cc_calls(ev, line)
        elif etype in ("item.started", "item.completed") or (
            etype is None and isinstance(ev.get("item"), dict)
        ):
            rows = _codex_calls(ev, line)
        elif etype == "tool_use" and isinstance(ev.get("part"), dict):
            rows = _opencode_calls(ev, line)
        elif etype == "tool/call":
            rows = _dsh_calls(ev, line)
        elif etype == "tool/result":
            pair = _dsh_result(ev)
            if pair is not None:
                idx = by_call_id.get(pair[0])
                if idx is not None:
                    out[idx]["result"] = pair[1]
            continue
        elif kind == "llm:tool_call:created":
            fragment = _blade_call_fragment(ev)
            if fragment is not None:
                call_id, name, chunk = fragment
                key = call_id or f"__line{line}"
                if key not in blade_pending:
                    row = _row(name, {}, line=line, call_id=call_id)
                    out.append(row)
                    blade_pending[key] = [len(out) - 1, ""]
                    if call_id:
                        by_call_id[call_id] = len(out) - 1
                idx, acc = blade_pending[key]
                if name and out[idx]["tool_name"] in ("", "unknown"):
                    out[idx]["tool_name"] = name
                blade_pending[key] = [idx, acc + chunk]
                out[idx]["arguments"] = _as_args(blade_pending[key][1])
            continue
        elif kind in _RESULT_KINDS:
            payload = (ev.get("raw") or {}).get("payload")
            if isinstance(payload, dict):
                idx = by_call_id.get(str(payload.get("tool_call_id") or ""))
                if idx is not None:
                    out[idx]["result"] = payload.get("content")
            continue
        elif "tool_calls" in ev:
            rows = _openai_tool_calls(ev, line)
        elif isinstance(ev.get("message"), dict) and "tool_calls" in ev["message"]:
            rows = _openai_tool_calls(ev["message"], line)

        for row in rows:
            out.append(row)
            if row["tool_call_id"]:
                by_call_id[row["tool_call_id"]] = len(out) - 1

    # codex 的 item.started / item.completed 是同一次调用的两条事件，按 call_id 去重，
    # 保留先出现的那条（顺序正确），并把后到的 result 合并进去。
    deduped: list[dict[str, Any]] = []
    seen: dict[tuple[str, str], int] = {}
    for row in out:
        key = (row["tool_call_id"], row["tool_name"])
        if row["tool_call_id"] and key in seen:
            prev = deduped[seen[key]]
            if prev["result"] in (None, "") and row["result"] not in (None, ""):
                prev["result"] = row["result"]
            if not prev["arguments"] and row["arguments"]:
                prev["arguments"] = row["arguments"]
            # 退出码只在 item.completed 上（item.started 时命令还没跑完），而保留的是
            # 先出现的 started 行——不在这里合并，exit_code 会永远是 None，
            # swebench 的 validation 维度（要求命令成功）就永远判不出成功。
            if prev.get("exit_code") is None and row.get("exit_code") is not None:
                prev["exit_code"] = row["exit_code"]
            continue
        deduped.append(row)
        if row["tool_call_id"]:
            seen[key] = len(deduped) - 1
    return deduped


# ---------- thinking 兜底 ----------

# 各家把推理文本放在哪：thinking.jsonl 只是 events.jsonl 的投影，缺文件不等于没推理。
def reasoning_texts_from_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从 events.jsonl 还原推理文本块，形状与 thinking.jsonl 一致（{content}）。"""
    out: list[dict[str, Any]] = []
    for i, ev in enumerate(events):
        if not isinstance(ev, dict):
            continue
        line = i + 1
        etype = ev.get("type")
        texts: list[str] = []

        if etype == "assistant":
            # CC：message.content[] 里的 thinking 块
            for block in (ev.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "thinking":
                    text = block.get("thinking") or block.get("content") or ""
                    if isinstance(text, str) and text.strip():
                        texts.append(text)
        elif etype == "reasoning" or (
            isinstance(ev.get("part"), dict) and ev["part"].get("type") == "reasoning"
        ):
            # opencode / mimo：part.text
            part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
            text = part.get("text") or ev.get("text") or ""
            if isinstance(text, str) and text.strip():
                texts.append(text)
        elif etype == "thinking" or ev.get("role") == "thinking":
            # kimi：平铺 role=thinking
            text = ev.get("content") or ev.get("text") or ""
            if isinstance(text, str) and text.strip():
                texts.append(text)
        elif isinstance(ev.get("item"), dict) and ev["item"].get("type") in (
            "reasoning",
            "agent_reasoning",
        ):
            # codex：item 信封里的 reasoning
            item = ev["item"]
            text = item.get("text") or item.get("content") or item.get("message") or ""
            if isinstance(text, str) and text.strip():
                texts.append(text)
        elif ev.get("kind") in ("llm:thinking:delta", "turn:end"):
            # blade：分片 delta 或 turn:end 的 blocks[]
            payload = (ev.get("raw") or {}).get("payload")
            if isinstance(payload, dict):
                chunk = payload.get("delta") or payload.get("content") or ""
                if isinstance(chunk, str) and chunk.strip():
                    texts.append(chunk)
                for block in payload.get("blocks") or []:
                    if isinstance(block, dict) and block.get("type") == "thinking":
                        text = block.get("content") or ""
                        if isinstance(text, str) and text.strip():
                            texts.append(text)

        for text in texts:
            out.append({"content": text, "source_ref": {"log": "events.jsonl", "line": line}})
    return out
