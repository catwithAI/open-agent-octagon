// 对话流事件解析回归。
//
// 六个 agent 的 events.jsonl 形态互不相同，parseEvents 靠字段形状分派：
//   blade        kind: "turn:*"，正文在 raw.blocks / raw.message
//   claude-code  type: "assistant"/"user"，正文在 message.content[]
//   codex        type: "item.completed"，正文在 item
//   kimi-code    顶层 role（**没有** type 字段），正文在 content/tool_calls
//   opencode     type 是事件类别，正文一律在 part 里
//   mimo-code    同 opencode（fork，逐字段同构）
//
// 下面的样本全部取自 49 机器 2026-07-27 的真实 events.jsonl，不是臆造形状——
// 新接三个 agent 的对话流一开始整列空白，就是因为解析器不认这两种形态。

import { describe, expect, it } from "vitest";

import { parseEvents, __tokens, __costLabel, __parseDshEvent } from "./RunDetail";
import { MESSAGES_RUN_DETAIL } from "../i18n/messages.runDetail";

// 测试只关心 __tokens 的解析/拼装逻辑，不关心翻译层：用一个按 zh 模板渲染的
// mock t，让断言仍能对上原始中文标签。
const t = (key: string, vars?: Record<string, string | number>): string => {
  let s = MESSAGES_RUN_DETAIL[key]?.zh ?? key;
  if (vars) for (const [k, v] of Object.entries(vars)) s = s.replace(`{${k}}`, String(v));
  return s;
};

describe("parseEvents · dsh", () => {
  // 样本取自 tests/fixtures/dsh/events.jsonl（rc6 真机跑出的原始
  // RunResult.events）。dsh 的 type 带斜杠命名空间，三种事件在加分支之前
  // 一个都命中不了——UI 上表现为空白轨迹，看着像"这个 agent 什么都没做"。
  const assistantWithToolCall = {
    type: "assistant/message",
    seq: 57,
    time: 1786636460225,
    data: {
      turn: 1, step: 1,
      message: {
        role: "assistant",
        // ⚠️ 连字符 `tool-call`，且 id 字段叫 `id` 不叫 callId
        content: [{ type: "tool-call", id: "call_2a2e", name: "write",
                    arguments: "{\"file_path\":\"note.txt\"}" }],
      },
      usage: { inputTokens: 2688, outputTokens: 118 },
    },
  };
  const toolCall = {
    type: "tool/call",
    seq: 59,
    time: 1786636460226,
    data: {
      turn: 1, step: 1, callId: "call_2a2e", name: "write",
      arguments: "{\"file_path\":\"note.txt\",\"content\":\"hello\"}",
    },
  };
  const toolResult = {
    type: "tool/result",
    seq: 60,
    time: 1786636460243,
    data: {
      turn: 1, step: 1,
      message: {
        role: "user",
        content: [{
          type: "tool-result", toolCallId: "call_2a2e", isError: false,
          content: [{ type: "text", text: "<type>file</type>" }],
        }],
      },
    },
  };

  it("一次工具调用只产出一个 tool_use（不重复计）", () => {
    // assistant/message 里的 tool-call 块必须被跳过——两处都产会让同一次
    // 调用在 UI 里出现两遍。
    const blocks = parseEvents([assistantWithToolCall, toolCall, toolResult]);
    const uses = blocks.filter((b) => b.kind === "tool_use");
    expect(uses).toHaveLength(1);
    expect(uses[0]).toMatchObject({ name: "write", id: "call_2a2e" });
    // input 来自 data.arguments 的 JSON 串
    expect((uses[0] as { input: Record<string, unknown> }).input)
      .toMatchObject({ file_path: "note.txt", content: "hello" });

    const results = blocks.filter((b) => b.kind === "tool_result");
    expect(results).toHaveLength(1);
    expect(results[0]).toMatchObject({ toolUseId: "call_2a2e", isError: false });
  });

  it("text 块进正文", () => {
    const blocks = parseEvents([{
      type: "assistant/message", seq: 1, time: 1786636460225,
      data: { turn: 1, step: 1,
              message: { role: "assistant",
                         content: [{ type: "text", text: "已创建 note.txt" }] } },
    }]);
    expect(blocks).toEqual([{ kind: "text", content: "已创建 note.txt" }]);
  });

  it("reasoning 块进思考栏（不能复用 CC 的 thinking 解析）", () => {
    // dsh 的 ReasoningBlock 是 {type:'reasoning', text}，而 parseBlocks 认的是
    // {type:'thinking', thinking}。直接复用会让思考块静默消失——页面看着有
    // 内容，只是"这个 agent 好像不思考"，比整体空白更难发现。
    //
    // 判据用手写样例而非真实 fixture：经 OpenRouter 采不到 reasoning 块
    // （dsh 读 reasoning_content，OpenRouter 发 reasoning，字段名不同），
    // 换 DeepSeek 官方端点才会出现。这里测的是**映射规则**，规则本身由
    // dsh 的 ReasoningBlock 类型定义确定，不是靠猜。
    const blocks = parseEvents([{
      type: "assistant/message", seq: 1, time: 1786636460225,
      data: { turn: 1, step: 1,
              message: { role: "assistant",
                         content: [{ type: "reasoning", text: "先看文件是否存在" },
                                   { type: "text", text: "存在" }] } },
    }]);
    expect(blocks).toEqual([
      { kind: "thinking", content: "先看文件是否存在" },
      { kind: "text", content: "存在" },
    ]);
  });

  it("arguments 非法 JSON 时退回空对象，不抛异常", () => {
    const blocks = parseEvents([{
      type: "tool/call", seq: 1, time: 1,
      data: { turn: 1, step: 1, callId: "c1", name: "bash",
              arguments: "{不是合法 JSON" },
    }]);
    expect(blocks).toHaveLength(1);
    expect((blocks[0] as { input: Record<string, unknown> }).input).toEqual({});
  });

  it("顶层 data.error 也算失败（不只看 block.isError）", () => {
    // 真实形状取自 fixtures/dsh/events_tool_error.jsonl
    const blocks = parseEvents([{
      type: "tool/result", seq: 1, time: 1,
      data: {
        turn: 1, step: 1,
        error: { name: "FsError", code: "FS_NOT_FOUND" },
        message: {
          role: "user",
          content: [{
            type: "tool-result", toolCallId: "c1", isError: true,
            content: [{ type: "text", text: "Error: cannot read" }],
          }],
        },
      },
    }]);
    expect(blocks[0]).toMatchObject({ kind: "tool_result", isError: true });
    expect((blocks[0] as { content: string }).content).toContain("FS_NOT_FOUND");
  });

  it("bash 非零退出不标红（isError 只反映框架层失败）", () => {
    const blocks = parseEvents([{
      type: "tool/result", seq: 1, time: 1,
      data: {
        turn: 1, step: 1,
        message: {
          role: "user",
          content: [{
            type: "tool-result", toolCallId: "c1", isError: false,
            content: [{ type: "text", text: "[stderr]\ncat: no such file" }],
          }],
        },
      },
    }]);
    // exit code 在结果文本里，不该被当成工具失败——与 CC/codex 语义一致
    expect(blocks[0]).toMatchObject({ isError: false });
  });

  it("不误吃其他 agent 的事件", () => {
    // CC 的 assistant 不含斜杠，dsh 解析器必须放行
    expect(__parseDshEvent({ type: "assistant", message: {} }, [])).toBe(false);
    // codex 用点号
    expect(__parseDshEvent({ type: "item.completed", item: {} }, [])).toBe(false);
    expect(__parseDshEvent({ role: "assistant" }, [])).toBe(false);
  });
});

describe("parseEvents · kimi-code", () => {
  it("从顶层 role 解析正文与工具调用", () => {
    const blocks = parseEvents([
      {
        timestamp: "2026-07-27T07:12:46.762Z",
        role: "assistant",
        content: "Let me start by understanding the problem better",
        tool_calls: [
          {
            type: "function",
            id: "call_d92d6c83",
            function: {
              name: "mcp__octagon-ad-placement__get_task",
              arguments: '{"detail":"full"}',
            },
          },
        ],
      },
      {
        timestamp: "2026-07-27T07:12:46.762Z",
        role: "tool",
        tool_call_id: "call_d92d6c83",
        content: '{"final_file": "solution.cpp"}',
      },
    ]);

    expect(blocks).toEqual([
      { kind: "text", content: "Let me start by understanding the problem better" },
      {
        kind: "tool_use",
        name: "mcp__octagon-ad-placement__get_task",
        input: { detail: "full" },
        id: "call_d92d6c83",
      },
      {
        kind: "tool_result",
        toolUseId: "call_d92d6c83",
        content: '{"final_file": "solution.cpp"}',
      },
    ]);
  });

  it("thinking role 归入思考块", () => {
    const blocks = parseEvents([{ role: "thinking", content: "我先读题" }]);
    expect(blocks).toEqual([{ kind: "thinking", content: "我先读题" }]);
  });

  it("meta 行（session.resume_hint）不进对话流", () => {
    const blocks = parseEvents([
      {
        role: "meta",
        type: "session.resume_hint",
        session_id: "session_abc",
        content: "To resume this session: kimi -r session_abc",
      },
    ]);
    expect(blocks).toEqual([]);
  });

  it("arguments 不是合法 JSON 时不丢调用，原文兜底", () => {
    const blocks = parseEvents([
      {
        role: "assistant",
        tool_calls: [{ id: "c1", function: { name: "bash", arguments: "{not json" } }],
      },
    ]);
    expect(blocks).toEqual([
      { kind: "tool_use", name: "bash", input: { arguments: "{not json" }, id: "c1" },
    ]);
  });
});

describe("parseEvents · opencode / mimo-code", () => {
  it("part.text 是正文、part.reasoning 是思考", () => {
    const blocks = parseEvents([
      { type: "step_start", sessionID: "ses_1", part: { type: "step-start" } },
      { type: "reasoning", sessionID: "ses_1", part: { type: "reasoning", text: "先设计算法" } },
      { type: "text", sessionID: "ses_1", part: { type: "text", text: "已写入 solution.cpp" } },
    ]);
    expect(blocks).toEqual([
      { kind: "thinking", content: "先设计算法" },
      { kind: "text", content: "已写入 solution.cpp" },
    ]);
  });

  it("tool part 拆成调用 + 结果两个块（两者打包在同一个 part 里）", () => {
    const blocks = parseEvents([
      {
        type: "tool_use",
        sessionID: "ses_1",
        part: {
          type: "tool",
          tool: "todowrite",
          callID: "call_39f9b7ea",
          state: {
            status: "completed",
            input: { todos: [{ content: "Understand problem" }] },
            output: "[{...}]",
          },
        },
      },
    ]);
    expect(blocks).toEqual([
      {
        kind: "tool_use",
        name: "todowrite",
        input: { todos: [{ content: "Understand problem" }] },
        id: "call_39f9b7ea",
      },
      { kind: "tool_result", toolUseId: "call_39f9b7ea", content: "[{...}]", isError: false },
    ]);
  });

  it("失败的工具调用用 state.error 兜底并标红", () => {
    // 实测形状：error 态只有 error 字段，没有 output。
    const blocks = parseEvents([
      {
        type: "tool_use",
        part: {
          type: "tool",
          tool: "edit",
          callID: "c2",
          state: { status: "error", input: {}, error: "edit: file has not been read" },
        },
      },
    ]);
    expect(blocks[1]).toEqual({
      kind: "tool_result",
      toolUseId: "c2",
      content: "edit: file has not been read",
      isError: true,
    });
  });

  it("in_progress 的调用只显示调用、不伪造结果", () => {
    const blocks = parseEvents([
      {
        type: "tool_use",
        part: {
          type: "tool",
          tool: "bash",
          callID: "c3",
          state: { status: "in_progress", input: { command: "g++ solution.cpp" } },
        },
      },
    ]);
    expect(blocks).toHaveLength(1);
    expect(blocks[0]).toMatchObject({ kind: "tool_use", name: "bash" });
  });
});

describe("parseEvents · 既有 agent 不受影响", () => {
  it("claude-code 的 assistant message 仍照旧解析", () => {
    const blocks = parseEvents([
      {
        type: "assistant",
        message: { content: [{ type: "text", text: "开始解题" }] },
      },
    ]);
    expect(blocks).toEqual([{ kind: "text", content: "开始解题" }]);
  });

  it("codex 的 item.completed 仍照旧解析", () => {
    const blocks = parseEvents([
      {
        type: "item.completed",
        item: { id: "item_1", type: "agent_message", text: "已完成" },
      },
    ]);
    expect(blocks).toEqual([{ kind: "text", content: "已完成" }]);
  });

  it("blade 的 turn:end raw.message 仍照旧解析", () => {
    const blocks = parseEvents([
      {
        kind: "turn:end",
        raw: { message: { role: "assistant", content: "blade 回复" } },
      },
    ]);
    expect(blocks).toEqual([{ kind: "text", content: "blade 回复" }]);
  });
});

describe("令牌与成本展示", () => {
  it("缓存命中与推理令牌单列，不并进输入", () => {
    // 实测形状：76.9 万令牌里 99.8% 是缓存复读，真实新增输入只有 1789——
    // 并进输入会让人误判消耗规模。
    const label = __tokens(JSON.stringify({
      input_tokens: 1789, output_tokens: 10749,
      cache_read_tokens: 768787, cache_write_tokens: 55798,
    }), t);
    expect(label).toContain("1,789 输入");
    expect(label).toContain("10,749 输出");
    expect(label).toContain("768,787 缓存");
  });

  it("缓存为 0（确实没命中）仍展示，与字段缺失区分", () => {
    expect(__tokens(JSON.stringify({ input_tokens: 10, output_tokens: 2, cache_read_tokens: 0 }), t))
      .toContain("0 缓存");
    // 字段缺失 = 不可得，不展示
    expect(__tokens(JSON.stringify({ input_tokens: 10, output_tokens: 2 }), t))
      .not.toContain("缓存");
  });

  it("无令牌数据显示占位符", () => {
    expect(__tokens(null, t)).toBe("-");
    expect(__tokens("{}", t)).toBe("-");
  });

  it("部分令牌缺定价时把成本标成下界", () => {
    expect(__costLabel(0.4043, true)).toBe("$0.4043");
    // priced=false → 下界，必须让读者看出来
    expect(__costLabel(0.4043, false)).toBe("≥$0.4043");
    // 极小额用更多位，避免显示成 $0.0000
    expect(__costLabel(0.0000098, true)).toBe("$0.00001");
    expect(__costLabel(null)).toBe("-");
  });
});
