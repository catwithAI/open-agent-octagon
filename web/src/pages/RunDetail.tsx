import { useCallback, useEffect, useRef, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";

import { api, type AttemptDetail, type AttemptMoney, type AttemptSecurity, type ArtifactPreviewDescriptor, type ArtifactDir, type ArtifactFile, type ArtifactType, type AttemptSummary, type DocumentBlock, type DocumentPreview, type PresentationPreview, type RunCost, type RunCostStatus, type RunDetail as RunDetailModel, type SecurityEvent, type WireCallSummary, type WireManifest, type WireRecord, type WireTrajectory, type WireTrajectoryStep, type WireUsage, type WorkbookPreview } from "../api/client";
import { curveSegments, deriveWireView, splitMatched, usageValue, type WireGap } from "../wire/curve";
import { ConversationPanel } from "./ConversationPanel";
import { NormalizedOutputPanel } from "../components/NormalizedOutputPanel";
import { ReJudgePanel } from "../components/ReJudgePanel";
import { AGENT_COLORS } from "../agents";
import { useI18n } from "../i18n";

// t 函数类型（供模块级 helper 以参数形式接收，见各 helper）。
type TFn = (key: string, vars?: Record<string, string | number>) => string;

// ── event parsing（七家形态各异，见 parseEvents 的分派链）──

type EventBlock =
  | { kind: "thinking"; content: string }
  | { kind: "text"; content: string }
  | { kind: "tool_use"; name: string; input: Record<string, unknown>; id?: string }
  | { kind: "tool_result"; toolUseId: string; content: string; isError?: boolean }
  | { kind: "result"; subtype: string; cost?: number; duration?: string };

function parseBlocks(contentArr: Array<Record<string, unknown>>, blocks: EventBlock[]) {
  for (const b of contentArr) {
    const bt = b.type as string;
    if (bt === "thinking") {
      const text = (b.thinking ?? b.content ?? "") as string;
      if (text.trim()) blocks.push({ kind: "thinking", content: text });
    } else if (bt === "text") {
      const text = (b.text ?? b.content ?? "") as string;
      if (text.trim()) blocks.push({ kind: "text", content: text });
    } else if (bt === "tool_use") {
      let name = (b.name ?? b.display_name ?? b.tool_name ?? "") as string;
      let input = (b.input ?? {}) as Record<string, unknown>;
      if (!name && typeof b.content === "string") {
        try {
          const parsed = JSON.parse(b.content);
          name = parsed.description ?? parsed.command ?? b.tool_name as string ?? "";
          input = parsed;
        } catch { /* use raw */ }
      }
      blocks.push({
        kind: "tool_use",
        name: name || (b.tool_name as string) || "tool",
        input,
        id: (b.id ?? b.tool_call_id) as string | undefined,
      });
    } else if (bt === "tool_result") {
      const rc = b.content;
      let text = "";
      if (typeof rc === "string") text = rc;
      else if (Array.isArray(rc)) {
        text = (rc as Array<Record<string, unknown>>)
          .filter((x) => x.type === "text")
          .map((x) => x.text as string).join("\n");
      }
      blocks.push({
        kind: "tool_result",
        toolUseId: (b.tool_use_id ?? b.tool_call_id ?? "") as string,
        content: text,
        isError: b.is_error as boolean | undefined,
      });
    }
  }
}

// blade 新流式协议（turn:events）：turn:end 的 raw.message 是 OpenAI 风格
// history 节点。assistant 带 _blocks（thinking/text/tool_call，arguments 是
// JSON 串）；tool role 是工具结果；user（任务 prompt）不进对话流，与 CC/codex
// 列保持同构（它们的事件流同样从 assistant 起步）。
function parseBladeMessage(msg: Record<string, unknown>, blocks: EventBlock[]) {
  const role = msg.role as string | undefined;
  if (role === "assistant") {
    const mBlocks = msg._blocks as Array<Record<string, unknown>> | undefined;
    if (Array.isArray(mBlocks) && mBlocks.length > 0) {
      for (const b of mBlocks) {
        const bt = b.type as string;
        if (bt === "thinking") {
          const text = (b.thinking ?? b.content ?? "") as string;
          if (text.trim()) blocks.push({ kind: "thinking", content: text });
        } else if (bt === "text") {
          const text = (b.text ?? b.content ?? "") as string;
          if (text.trim()) blocks.push({ kind: "text", content: text });
        } else if (bt === "tool_call" || bt === "tool_use") {
          let input: Record<string, unknown> = {};
          const rawArgs = b.arguments ?? b.input;
          if (typeof rawArgs === "string") {
            try { input = JSON.parse(rawArgs) as Record<string, unknown>; } catch { input = { arguments: rawArgs }; }
          } else if (rawArgs && typeof rawArgs === "object") {
            input = rawArgs as Record<string, unknown>;
          }
          blocks.push({ kind: "tool_use", name: (b.name ?? "tool") as string, input, id: b.id as string | undefined });
        }
      }
      return;
    }
    // 无 _blocks 的兜底：_reasoning / content / tool_calls 逐项拼
    const reasoning = msg._reasoning as string | undefined;
    if (reasoning?.trim()) blocks.push({ kind: "thinking", content: reasoning });
    if (typeof msg.content === "string" && msg.content.trim()) blocks.push({ kind: "text", content: msg.content });
    const toolCalls = msg.tool_calls as Array<Record<string, unknown>> | undefined;
    for (const tc of toolCalls ?? []) {
      const fn = (tc.function ?? {}) as Record<string, unknown>;
      let input: Record<string, unknown> = {};
      if (typeof fn.arguments === "string") {
        try { input = JSON.parse(fn.arguments) as Record<string, unknown>; } catch { input = { arguments: fn.arguments }; }
      }
      blocks.push({ kind: "tool_use", name: (fn.name ?? "tool") as string, input, id: tc.id as string | undefined });
    }
  } else if (role === "tool") {
    blocks.push({
      kind: "tool_result",
      toolUseId: (msg.tool_call_id ?? "") as string,
      content: typeof msg.content === "string" ? msg.content : JSON.stringify(msg.content ?? ""),
    });
  }
}

// kimi-code stream-json：每行一个 OpenAI 风格 history 节点，`role` 在**顶层**
// （没有 `type` 字段）。assistant 带 content + tool_calls（arguments 是 JSON
// 串），tool role 是工具结果。role=meta 是 session.resume_hint 之类的框架事件，
// 不进对话流。user（任务 prompt）同样不进，与 CC/codex 列保持同构。
function parseKimiMessage(ev: Record<string, unknown>, blocks: EventBlock[]) {
  const role = ev.role as string;
  if (role === "assistant") {
    if (typeof ev.content === "string" && ev.content.trim()) {
      blocks.push({ kind: "text", content: ev.content });
    }
    const toolCalls = ev.tool_calls as Array<Record<string, unknown>> | undefined;
    for (const tc of toolCalls ?? []) {
      const fn = (tc.function ?? {}) as Record<string, unknown>;
      let input: Record<string, unknown> = {};
      const rawArgs = fn.arguments;
      if (typeof rawArgs === "string") {
        try { input = JSON.parse(rawArgs) as Record<string, unknown>; } catch { input = { arguments: rawArgs }; }
      } else if (rawArgs && typeof rawArgs === "object") {
        input = rawArgs as Record<string, unknown>;
      }
      blocks.push({ kind: "tool_use", name: (fn.name ?? "tool") as string, input, id: tc.id as string | undefined });
    }
  } else if (role === "thinking") {
    const text = (ev.content ?? "") as string;
    if (text.trim()) blocks.push({ kind: "thinking", content: text });
  } else if (role === "tool") {
    const c = ev.content;
    blocks.push({
      kind: "tool_result",
      toolUseId: (ev.tool_call_id ?? "") as string,
      content: typeof c === "string" ? c : JSON.stringify(c ?? ""),
    });
  }
}

// opencode / mimo-code `run --format json`：正文一律在 `part` 里，顶层 `type`
// 只是事件类别。part.type 区分形态：text / reasoning 是正文与思考，tool 则把
// 调用与结果**打包在同一个 part** 的 state 里（input + output 同时出现），
// 与 CC/codex 的「先 tool_use 后 tool_result 两条」不同，这里拆成两个 block。
function parseOpencodePart(ev: Record<string, unknown>, blocks: EventBlock[]) {
  const part = (ev.part ?? {}) as Record<string, unknown>;
  const pt = part.type as string | undefined;
  if (pt === "text") {
    const text = (part.text ?? "") as string;
    if (text.trim()) blocks.push({ kind: "text", content: text });
  } else if (pt === "reasoning") {
    const text = (part.text ?? "") as string;
    if (text.trim()) blocks.push({ kind: "thinking", content: text });
  } else if (pt === "tool") {
    const state = (part.state ?? {}) as Record<string, unknown>;
    const callId = (part.callID ?? "") as string;
    blocks.push({
      kind: "tool_use",
      name: (part.tool ?? "tool") as string,
      input: (state.input ?? {}) as Record<string, unknown>,
      id: callId,
    });
    const status = state.status as string | undefined;
    // 只有跑完（completed/error）的调用才有结果；in_progress 时先只显示调用。
    if (status === "completed" || status === "error") {
      const out = state.output ?? state.error ?? "";
      blocks.push({
        kind: "tool_result",
        toolUseId: callId,
        content: typeof out === "string" ? out : JSON.stringify(out),
        isError: status === "error",
      });
    }
  }
}

// dsh（DeepSeek Harness）：事件类型带**斜杠命名空间**（`tool/call`、
// `assistant/message`），正文在 `data` 里。判别特征就是 type 含 `/`——其余
// 六家的 type 都不带斜杠（CC 是 `assistant`、codex 用点号 `item.completed`）。
//
// ⚠️ 不能把 content 交给 parseBlocks()：那是 CC 方言的解析器，思考块认
// `type==="thinking"` 且从 `b.thinking` 取文本；而 dsh 的 ReasoningBlock 是
// `{type:'reasoning', text}`。直接复用的结果是正文能显示、思考块静默消失
// ——比整体空白更难发现，因为页面看着是有内容的。
function parseDshEvent(ev: Record<string, unknown>, blocks: EventBlock[]): boolean {
  const t = ev.type as string | undefined;
  if (t === undefined || !t.includes("/")) return false;
  const data = (ev.data ?? {}) as Record<string, unknown>;

  if (t === "assistant/message") {
    const msg = (data.message ?? {}) as Record<string, unknown>;
    const content = (msg.content ?? []) as Array<Record<string, unknown>>;
    for (const b of content) {
      const bt = b.type as string | undefined;
      if (bt === "text") {
        const text = (b.text ?? "") as string;
        if (text.trim()) blocks.push({ kind: "text", content: text });
      } else if (bt === "reasoning") {
        const text = (b.text ?? "") as string;
        if (text.trim()) blocks.push({ kind: "thinking", content: text });
      }
      // 'tool-call' 块**故意跳过**：工具调用统一由 tool/call 事件产出，
      // 两处都产会让同一次调用在 UI 里出现两遍。
    }
    return true;
  }

  if (t === "tool/call") {
    let input: Record<string, unknown> = {};
    try {
      // 模型原样产出的 JSON 串，可能非法——失败退回 {}，
      // backfillToolInputs() 还能兜一层。
      input = JSON.parse((data.arguments ?? "{}") as string);
    } catch { /* 保持 {} */ }
    blocks.push({
      kind: "tool_use",
      name: (data.name ?? "tool") as string,
      input,
      id: (data.callId ?? "") as string,
    });
    return true;
  }

  if (t === "tool/result") {
    const msg = (data.message ?? {}) as Record<string, unknown>;
    const content = (msg.content ?? []) as Array<Record<string, unknown>>;
    const first = (content[0] ?? {}) as Record<string, unknown>;
    const inner = first.content;
    const text = typeof inner === "string" ? inner : JSON.stringify(inner ?? "");
    // 失败有两处信号：block 的 isError，以及顶层 data.error {name, code}。
    // 两者都要认——只看 isError 会漏掉 error 存在但 block 没标的情况。
    // 注意 isError 只反映**工具框架层**失败；bash 的非零退出码是 false
    // （exit code 在结果文本里）。这与 CC/codex 语义一致。
    const err = data.error as Record<string, unknown> | undefined;
    const hasError = (first.isError ?? false) as boolean
      || (err !== undefined && err !== null);
    blocks.push({
      kind: "tool_result",
      toolUseId: (first.toolCallId ?? "") as string,
      content: err ? `${text}\n[${err.name ?? "Error"}: ${err.code ?? ""}]` : text,
      isError: hasError,
    });
    return true;
  }
  return false;
}

// 导出仅为单测可达：七个 agent 的事件形态互不相同，回归必须钉在真实样本上。
export { tokens as __tokens, costLabel as __costLabel, parseDshEvent as __parseDshEvent };

export function parseEvents(events: Array<Record<string, unknown>>): EventBlock[] {
  const blocks: EventBlock[] = [];
  for (const ev of events) {
    const t = ev.type as string | undefined;
    const kind = ev.kind as string | undefined;
    // kimi-code：靠顶层 role 识别（它没有 type 字段）。必须先于下面按 type
    // 分派的分支，否则带 type 的框架行会走错路径。
    if (typeof ev.role === "string" && ev.part === undefined) {
      parseKimiMessage(ev, blocks);
      continue;
    }
    // opencode / mimo：正文都在 part 里，顶层 type 只是事件类别。
    if (ev.part && typeof ev.part === "object") {
      parseOpencodePart(ev, blocks);
      continue;
    }
    // dsh：type 带斜杠命名空间。放在通用分支之前——它的 `assistant/message`
    // 不会被下面 `t === "assistant"` 的精确相等吃掉，但显式前置更稳。
    if (parseDshEvent(ev, blocks)) continue;
    if (kind?.startsWith("turn:")) {
      const raw = (ev.raw ?? {}) as Record<string, unknown>;
      if (Array.isArray(raw.blocks)) {
        // 旧 turn:events 协议：raw.blocks 直接是 thinking/text/tool_use/tool_result
        parseBlocks(raw.blocks as Array<Record<string, unknown>>, blocks);
      } else if (raw.message) {
        // 新流式协议：turn:end 携带完整 history 节点 raw.message（role 区分形态）
        parseBladeMessage(raw.message as Record<string, unknown>, blocks);
      }
      continue;
    }
    if (t === "item.completed") {
      const item = (ev.item ?? {}) as Record<string, unknown>;
      const it = item.type as string;
      if (it === "agent_message" || it === "reasoning") {
        const text = (item.text ?? "") as string;
        if (text.trim()) {
          blocks.push(it === "reasoning"
            ? { kind: "thinking", content: text }
            : { kind: "text", content: text });
        }
      } else if (it === "mcp_tool_call") {
        const toolName = `${item.server ?? ""}__${item.tool ?? ""}`;
        blocks.push({ kind: "tool_use", name: toolName, input: (item.arguments ?? {}) as Record<string, unknown> });
        const result = item.result as Record<string, unknown> | undefined;
        if (result) {
          const content = result.content as Array<Record<string, unknown>> | undefined;
          const text = content?.filter((c) => c.type === "text").map((c) => c.text as string).join("\n") ?? "";
          blocks.push({ kind: "tool_result", toolUseId: (item.id ?? "") as string, content: text, isError: (result.isError ?? false) as boolean });
        }
      } else if (it === "command_execution") {
        const cmd = (item.command ?? "") as string;
        const output = (item.aggregated_output ?? "") as string;
        const exitCode = item.exit_code as number | undefined;
        const short = cmd.length > 80 ? cmd.slice(0, 77) + "..." : cmd;
        blocks.push({ kind: "tool_use", name: short, input: {} });
        blocks.push({ kind: "tool_result", toolUseId: (item.id ?? "") as string, content: output || `(exit ${exitCode ?? "?"})`, isError: exitCode !== 0 && exitCode !== undefined });
      }
      continue;
    }
    if (t === "assistant" || t === "user") {
      const msg = (ev.message ?? {}) as Record<string, unknown>;
      const content = (msg.content ?? []) as Array<Record<string, unknown>>;
      parseBlocks(content, blocks);
    } else if (t === "result") {
      blocks.push({ kind: "result", subtype: (ev.subtype ?? "") as string, cost: ev.total_cost_usd as number | undefined, duration: ev.duration_api_ms ? `${Math.round((ev.duration_api_ms as number) / 1000)}s` : undefined });
    }
  }
  backfillToolInputs(blocks);
  return blocks;
}

// blade 事件流的 tool_use block 不带 input（如 rm 清理命令显示「输入 {}」），
// 但紧随的 tool_result JSON 里带 command/description —— 用它回填输入，
// 保证删除/清理类关键命令在对比视图里可见。
function backfillToolInputs(blocks: EventBlock[]) {
  for (let i = 0; i < blocks.length; i++) {
    const b = blocks[i];
    if (b.kind !== "tool_use" || Object.keys(b.input).length > 0) continue;
    for (let j = i + 1; j < blocks.length; j++) {
      const r = blocks[j];
      if (r.kind === "tool_use") break;
      if (r.kind !== "tool_result") continue;
      if (b.id && r.toolUseId && r.toolUseId !== b.id) continue;
      try {
        const parsed = JSON.parse(r.content) as Record<string, unknown>;
        const input: Record<string, unknown> = {};
        for (const key of ["command", "description", "file_path", "path"]) {
          if (typeof parsed[key] === "string") input[key] = parsed[key];
        }
        if (Object.keys(input).length > 0) b.input = input;
      } catch { /* result 不是 JSON，跳过 */ }
      break;
    }
  }
}

// ── trace → step comparison ──

type ToolCall = {
  tool_name: string; arguments: Record<string, unknown>; result?: unknown;
  is_error: boolean; duration_ms: number;
  // blade 路线 A：skill 级语义（adapter 从 Bash command 解析；旧数据由前端兜底解析）
  skill_id?: string; skill_tool?: string; skill_args?: Record<string, unknown>;
};

const BLADE_SKILL_RUN_RE = /blade\s+skill\s+run\s+"?([^"\s]+)"?\s+(\w+)(?:\s+--args\s+'([\s\S]*?)')?/;

// 对齐键：blade 的 Bash 调用若是 `blade skill run <skill> <tool>`，用 skill 级工具名，
// 这样 BA 与 CC/codex 的同一业务步骤落在同一行。
function effectiveTool(c: ToolCall): string {
  if (c.skill_tool) return c.skill_tool;
  const cmd = c.arguments?.command;
  if (c.tool_name === "Bash" && typeof cmd === "string") {
    const m = BLADE_SKILL_RUN_RE.exec(cmd);
    if (m) return m[2];
  }
  return c.tool_name;
}

function effectiveArgs(c: ToolCall): Record<string, unknown> {
  if (c.skill_args && Object.keys(c.skill_args).length > 0) return c.skill_args;
  const cmd = c.arguments?.command;
  if (c.tool_name === "Bash" && typeof cmd === "string") {
    const m = BLADE_SKILL_RUN_RE.exec(cmd);
    if (m?.[3]) {
      try { return JSON.parse(m[3]) as Record<string, unknown>; } catch { /* fall through */ }
    }
  }
  return c.arguments ?? {};
}

// 业务步骤 = 各 env 的 skill 工具（blade 经 CLI，CC/codex 经 MCP）。
// 判据：blade 侧有 skill_tool / 匹配 blade skill run；CC/codex 侧工具名不是通用 shell/文件工具。
const GENERIC_TOOLS = new Set([
  "Bash", "Read", "Write", "Edit", "Grep", "Glob", "LS", "ToolSearch",
  "shell", "apply_patch", "update_plan", "web_search",
]);

function isBusinessStep(c: ToolCall): boolean {
  if (c.skill_tool) return true;
  const cmd = c.arguments?.command;
  if (c.tool_name === "Bash" && typeof cmd === "string" && BLADE_SKILL_RUN_RE.test(cmd)) return true;
  // codex 的 command_execution 名字是命令本身；MCP 工具名是业务名
  return !GENERIC_TOOLS.has(c.tool_name) && !/^\/(bin|usr)/.test(c.tool_name);
}

type AgentStep = { call: ToolCall; label: string; business: boolean; bizIndex: number | null };

// multi-model 下同一 run 里 agent_name 会重复，列标签追加模型名消歧；
// 不重复时保持纯 agent 名（multi-agent / same-model 展示不变）。
function columnLabel(
  att: { agent_name: string; model?: string | null; model_used?: string | null },
  all: Array<{ agent_name: string }>,
): string {
  const dup = all.filter((a) => a.agent_name === att.agent_name).length > 1;
  if (!dup) return att.agent_name;
  return `${att.agent_name} (${att.model ?? att.model_used ?? "?"})`;
}

// 按 attempt 分列（不按 agent 名——multi-model 下同名会串数据）：
// 每列是该 attempt 完整的调用时序，业务步骤单独编号。
// 这样能直接看出「为完成同一任务，各框架各自拆成了多少步、插了哪些辅助命令」。
function buildAgentColumns(
  allDetails: Record<string, AttemptDetail>, atts: Array<{ id: string }>,
) {
  const cols: Record<string, AgentStep[]> = {};
  let maxLen = 0;
  for (const att of atts) {
    const d = allDetails[att.id];
    const calls = (d?.tool_calls ?? []) as ToolCall[];
    let biz = 0;
    const steps: AgentStep[] = calls.map((c) => {
      const business = isBusinessStep(c);
      const label = business ? effectiveTool(c) : (c.tool_name || "tool");
      return { call: c, label, business, bizIndex: business ? ++biz : null };
    });
    cols[att.id] = steps;
    maxLen = Math.max(maxLen, steps.length);
  }
  return { cols, maxLen };
}

// 返回 null 表示"无自定义参数"（渲染处显示本地化的「默认」标签）。
function formatArgs(args: Record<string, unknown>): string | null {
  const filtered = Object.entries(args).filter(([k, v]) => k !== "session_dir" && v != null);
  if (filtered.length === 0) return null;
  return filtered.map(([k, v]) => {
    const vs = typeof v === "object" ? JSON.stringify(v) : String(v);
    return `${k}=${vs.length > 30 ? vs.slice(0, 27) + "..." : vs}`;
  }).join(" ");
}

function StepCallItem({ step }: { step: AgentStep }) {
  const { t } = useI18n();
  const { call, label, business, bizIndex } = step;
  const [open, setOpen] = useState(false);
  const rawArgs = formatArgs(business ? effectiveArgs(call) : call.arguments);
  const isDefault = rawArgs === null;
  const args = rawArgs ?? t("runDetail.common.default");
  const isErr = call.is_error;
  return (
    <div className={`step-call-wrap${isErr ? " error" : ""}${business ? " biz" : " aux"}`}>
      <div className="step-call" onClick={() => setOpen(!open)}>
        <span className={`step-dot${isErr ? " error" : business ? " biz" : " aux"}`} />
        {bizIndex !== null && <span className="step-biz-num">{bizIndex}</span>}
        <span className={`step-tool${business ? " biz" : ""}`}>{label}</span>
        <span className={`step-args${isDefault ? " default" : ""}`}>{args}</span>
        {isErr && <span className="step-err-tag">{t("runDetail.common.error")}</span>}
        <span className="step-duration">{call.duration_ms}ms</span>
        <span className="ev-chevron">{open ? "▼" : "▶"}</span>
      </div>
      {open && (
        <div className="step-call-detail">
          <div className="step-call-section">
            <span className="step-call-label">{t("runDetail.step.args")}</span>
            <pre>{JSON.stringify(call.arguments, null, 2)}</pre>
          </div>
          {call.result !== undefined && (
            <div className="step-call-section">
              <span className="step-call-label">{t("runDetail.step.result")}</span>
              <pre>{typeof call.result === "string" ? call.result : JSON.stringify(call.result, null, 2)}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── sub-components ──

function shortToolName(name: string): string {
  const parts = name.split("__");
  return parts[parts.length - 1] || name;
}

function ThinkingBlock({ content }: { content: string }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(true);
  return (
    <div className="ev-thinking">
      <div className="ev-thinking-header" onClick={() => setOpen(!open)}>
        <span className="ev-icon">💭</span>
        <span>{t("runDetail.thinking.header", { n: content.length })}</span>
        <span className="ev-chevron">{open ? "▼" : "▶"}</span>
      </div>
      {open && <pre className="ev-thinking-body">{content}</pre>}
    </div>
  );
}

function ToolUseBlock({ name, input, result }: { name: string; input: Record<string, unknown>; result?: EventBlock & { kind: "tool_result" } }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const short = shortToolName(name);
  const hasError = result?.isError;
  return (
    <div className={`ev-tool${hasError ? " error" : ""}`}>
      <div className="ev-tool-header" onClick={() => setOpen(!open)}>
        <span className={`ev-tool-status${hasError ? " error" : ""}`}>{hasError ? "✗" : "✓"}</span>
        <span className="ev-tool-name">{short}</span>
        <span className="ev-chevron">{open ? "▼" : "▶"}</span>
      </div>
      {open && (
        <div className="ev-tool-body">
          <div className="ev-tool-section">
            <span className="ev-tool-label">{t("runDetail.tool.input")}</span>
            <pre>{JSON.stringify(input, null, 2)}</pre>
          </div>
          {result && (
            <div className="ev-tool-section">
              <span className="ev-tool-label">{t("runDetail.tool.output")}</span>
              <pre>{result.content || t("runDetail.common.empty")}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ConversationFlow({ blocks }: { blocks: EventBlock[] }) {
  const { t } = useI18n();
  const merged: JSX.Element[] = [];
  let i = 0;
  while (i < blocks.length) {
    const b = blocks[i];
    if (b.kind === "thinking") {
      merged.push(<ThinkingBlock key={i} content={b.content} />);
    } else if (b.kind === "text") {
      merged.push(<div key={i} className="ev-text">{b.content.split("\n").map((line, j) => <p key={j}>{line || " "}</p>)}</div>);
    } else if (b.kind === "tool_use") {
      let result: (EventBlock & { kind: "tool_result" }) | undefined;
      if (i + 1 < blocks.length && blocks[i + 1].kind === "tool_result") { result = blocks[i + 1] as EventBlock & { kind: "tool_result" }; i++; }
      merged.push(<ToolUseBlock key={i} name={b.name} input={b.input} result={result} />);
    } else if (b.kind === "tool_result") {
      merged.push(<div key={i} className="ev-tool-orphan"><pre>{b.content || t("runDetail.common.empty")}</pre></div>);
    } else if (b.kind === "result") {
      merged.push(<div key={i} className="ev-result"><span className="ev-icon">🏁</span> {b.subtype}{b.cost != null && <span> · ${b.cost.toFixed(4)}</span>}{b.duration && <span> · {b.duration}</span>}</div>);
    }
    i++;
  }
  return <div className="conversation-flow">{merged}</div>;
}

function AutoScrollFlow({ blocks }: { blocks: EventBlock[] }) {
  const { t } = useI18n();
  const ref = useRef<HTMLDivElement>(null);
  const userScrolled = useRef(false);
  const prevLen = useRef(0);

  const handleScroll = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    userScrolled.current = !atBottom;
  }, []);

  useEffect(() => {
    if (userScrolled.current) return;
    const el = ref.current;
    if (el && blocks.length > prevLen.current) {
      el.scrollTop = el.scrollHeight;
    }
    prevLen.current = blocks.length;
  }, [blocks]);

  return (
    <div className="agent-col-flow" ref={ref} onScroll={handleScroll}>
      {blocks.length === 0
        ? <span className="muted">{t("runDetail.flow.noEvents")}</span>
        : <ConversationFlow blocks={blocks} />}
    </div>
  );
}

function spreadsheetColumnLabel(column: number): string {
  let current = column;
  let label = "";
  while (current > 0) {
    current -= 1;
    label = String.fromCharCode(65 + (current % 26)) + label;
    current = Math.floor(current / 26);
  }
  return label;
}

type OfficeSyncState = {
  enabled: boolean;
  page: number;
  sheet: number;
  setPage: (page: number) => void;
  setSheet: (sheet: number) => void;
};

function PresentationViewer({ presentation, officeSync }: { presentation: PresentationPreview; officeSync?: OfficeSyncState }) {
  const { t } = useI18n();
  const [slideIndex, setSlideIndex] = useState(0);
  const [zoom, setZoom] = useState(100);
  const rootRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (officeSync?.enabled)
      setSlideIndex(Math.max(officeSync.page, 0));
  }, [officeSync?.enabled, officeSync?.page]);
  const slide = presentation.slides[slideIndex];
  if (!slide) return <div className="artifact-preview-state">
    {presentation.slides.length === 0
      ? t("runDetail.pptx.noSlides")
      : t("runDetail.pptx.syncMissing", { index: slideIndex + 1, total: presentation.slides.length })}
  </div>;
  const renderElement = (element: typeof slide.elements[number], index: number, thumbnail = false) => {
    const style = {
      left: `${element.x * 100}%`, top: `${element.y * 100}%`,
      width: `${element.width * 100}%`, height: `${element.height * 100}%`,
      backgroundColor: element.fill ?? undefined,
      fontSize: element.font_size && !thumbnail ? `${Math.max(8, element.font_size * .75)}px` : undefined,
    };
    if (element.kind === "image" && element.data_uri)
      return <img key={index} className="pptx-element pptx-image" style={style} src={element.data_uri} alt="" />;
    if (element.kind === "table")
      return <table key={index} className="pptx-element pptx-table" style={style}><tbody>
        {(element.rows ?? []).map((row, ri) => <tr key={ri}>{row.map((cell, ci) => <td key={ci}>{cell}</td>)}</tr>)}
      </tbody></table>;
    return <div key={index} className={`pptx-element pptx-text${element.role === "title" ? " is-title" : ""}`}
      style={style}>{element.text}</div>;
  };
  const renderSlide = (item: typeof slide, thumbnail = false) => item.rendered_image_data_uri
    ? <img className="pptx-rendered-slide" src={item.rendered_image_data_uri} alt={t("runDetail.pptx.slideAlt", { n: item.number })} />
    : item.elements.map((element, index) => renderElement(element, index, thumbnail));
  const selectSlide = (index: number) => {
    const bounded = Math.min(Math.max(index, 0), presentation.slides.length - 1);
    setSlideIndex(bounded);
    if (officeSync?.enabled) officeSync.setPage(bounded);
  };
  return <div className="pptx-viewer" ref={rootRef} aria-label={t("runDetail.pptx.viewerAria")}>
    <div className="pptx-toolbar">
      <button onClick={() => selectSlide(slideIndex - 1)} disabled={slideIndex === 0}>{t("runDetail.pptx.prev")}</button>
      <span>{slideIndex + 1} / {presentation.slides.length}</span>
      <button onClick={() => selectSlide(slideIndex + 1)}
        disabled={slideIndex >= presentation.slides.length - 1}>{t("runDetail.pptx.next")}</button>
      <button onClick={() => setZoom((value) => Math.max(50, value - 10))}>{t("runDetail.pptx.zoomOut")}</button>
      <span>{zoom}%</span><button onClick={() => setZoom((value) => Math.min(180, value + 10))}>{t("runDetail.pptx.zoomIn")}</button>
      <button onClick={() => setZoom(100)}>{t("runDetail.pptx.fit")}</button>
      <button onClick={() => rootRef.current?.requestFullscreen?.()}>{t("runDetail.pptx.fullscreen")}</button>
    </div>
    {presentation.truncated && <div className="xlsx-warning">{t("runDetail.pptx.truncated")}</div>}
    <div className="pptx-layout">
      <div className="pptx-thumbnails" role="tablist" aria-label={t("runDetail.pptx.thumbsAria")}>
        {presentation.slides.map((item, index) => <button key={item.number} role="tab"
          aria-selected={index === slideIndex} className={index === slideIndex ? "is-active" : ""}
          onClick={() => selectSlide(index)}>
          <span>{item.number}</span><div className="pptx-thumb-canvas" style={{ aspectRatio: presentation.aspect_ratio }}>
            {item.rendered_image_data_uri
              ? renderSlide(item, true)
              : item.elements.filter((element) => element.kind === "text").slice(0, 8)
                .map((element, ei) => renderElement(element, ei, true))}
          </div>
        </button>)}
      </div>
      <div className="pptx-stage-scroll"><div className="pptx-stage" style={{
        aspectRatio: presentation.aspect_ratio,
        width: `${zoom}%`,
      }}>{renderSlide(slide)}</div></div>
    </div>
    {slide.notes && <details className="pptx-notes"><summary>{t("runDetail.pptx.notes")}</summary><pre>{slide.notes}</pre></details>}
  </div>;
}

function DocumentViewer({ document }: { document: DocumentPreview }) {
  const { t } = useI18n();
  const [query, setQuery] = useState("");
  const [zoom, setZoom] = useState(100);
  const normalized = query.trim().toLocaleLowerCase();
  const blockMatches = (block: DocumentBlock): boolean => {
    if (block.kind === "paragraph") return block.text.toLocaleLowerCase().includes(normalized);
    return block.rows.some((row) => row.some((cell) => cell.text.toLocaleLowerCase().includes(normalized)));
  };
  const matches = normalized ? document.blocks.filter(blockMatches).length : 0;
  const renderBlock = (block: DocumentBlock, index: number): React.ReactNode => {
    const matching = normalized && blockMatches(block);
    if (block.kind === "table") return <table key={index} className={matching ? "docx-match" : ""}><tbody>
      {block.rows.map((row, ri) => <tr key={ri}>{row.map((cell, ci) => <td key={ci}>{cell.text}</td>)}</tr>)}
    </tbody></table>;
    const content = block.runs.length ? block.runs.map((run, ri) => {
      const node = <span style={{ fontWeight: run.bold ? 700 : undefined, fontStyle: run.italic ? "italic" : undefined }}>{run.text}</span>;
      return run.href ? <a key={ri} href={run.href} target="_blank" rel="noreferrer noopener">{node}</a> : <span key={ri}>{node}</span>;
    }) : block.text;
    const className = `${block.list_item ? "docx-list-item " : ""}${matching ? "docx-match" : ""}`.trim();
    if (block.heading) {
      const Tag = `h${Math.max(1, Math.min(6, block.heading))}` as keyof React.JSX.IntrinsicElements;
      return <Tag key={index} className={className}>{content}</Tag>;
    }
    return <p key={index} className={className}>{content || <br />}</p>;
  };
  return <div className="docx-viewer" aria-label={t("runDetail.docx.viewerAria")}>
    <div className="docx-toolbar">
      <label>{t("runDetail.docx.search")} <input placeholder={t("runDetail.docx.searchPlaceholder")} value={query} onChange={(event) => setQuery(event.target.value)} /></label>
      {normalized && <span>{t("runDetail.docx.matches", { n: matches })}</span>}
      <button onClick={() => setZoom((value) => Math.max(60, value - 10))}>{t("runDetail.xlsx.zoomOut")}</button>
      <span>{zoom}%</span><button onClick={() => setZoom((value) => Math.min(180, value + 10))}>{t("runDetail.xlsx.zoomIn")}</button>
    </div>
    {(document.truncated || document.images_omitted > 0) && <div className="xlsx-warning">
      {document.truncated && t("runDetail.docx.truncated")}
      {document.images_omitted > 0 && t("runDetail.docx.imagesOmitted", { n: document.images_omitted })}
    </div>}
    <article className="docx-page" style={{ fontSize: `${zoom}%`,
      aspectRatio: document.page.width_pt && document.page.height_pt
        ? document.page.width_pt / document.page.height_pt : undefined }}>
      {(document.headers?.length ?? 0) > 0 && <header className="docx-header">{document.headers?.map(renderBlock)}</header>}
      {document.blocks.map(renderBlock)}
      {(document.footers?.length ?? 0) > 0 && <footer className="docx-footer">{document.footers?.map(renderBlock)}</footer>}
    </article>
  </div>;
}

function WorkbookViewer({ workbook, officeSync }: { workbook: WorkbookPreview; officeSync?: OfficeSyncState }) {
  const { t } = useI18n();
  const [sheetIndex, setSheetIndex] = useState(0);
  const [query, setQuery] = useState("");
  const [zoom, setZoom] = useState(100);
  const [scrollTop, setScrollTop] = useState(0);
  const gridRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (officeSync?.enabled)
      setSheetIndex(Math.max(officeSync.sheet, 0));
  }, [officeSync?.enabled, officeSync?.sheet]);
  const sheet = workbook.sheets[sheetIndex];
  if (!sheet) return <div className="artifact-preview-state">
    {workbook.sheets.length === 0
      ? t("runDetail.xlsx.noSheets")
      : t("runDetail.xlsx.syncMissing", { index: sheetIndex + 1, total: workbook.sheets.length })}
  </div>;

  const normalizedQuery = query.trim().toLocaleLowerCase();
  const rows = normalizedQuery
    ? sheet.rows.filter((row) => row.cells.some((cell) =>
      String(cell.value ?? "").toLocaleLowerCase().includes(normalizedQuery)
      || String(cell.formula ?? "").toLocaleLowerCase().includes(normalizedQuery)))
    : sheet.rows;
  const maxColumn = Math.max(1, ...sheet.rows.flatMap((row) => row.cells.map((cell) => cell.column)));
  const rowHeight = 28;
  const virtualized = rows.length > 100;
  const virtualStart = virtualized ? Math.max(0, Math.floor(scrollTop / rowHeight) - 8) : 0;
  const virtualEnd = virtualized ? Math.min(rows.length, virtualStart + 36) : rows.length;
  const visibleRows = rows.slice(virtualStart, virtualEnd);
  const cellByPosition = new Map<string, typeof sheet.rows[number]["cells"][number]>();
  rows.forEach((row) => row.cells.forEach((cell) => cellByPosition.set(`${row.index}:${cell.column}`, cell)));
  const displayValue = (value: string | number | boolean | null) => {
    if (value === null) return <span className="xlsx-empty">∅</span>;
    if (typeof value === "boolean") return value ? "TRUE" : "FALSE";
    return String(value);
  };

  return (
    <div className="xlsx-viewer" aria-label={t("runDetail.xlsx.viewerAria")}>
      <div className="xlsx-toolbar">
        <div className="xlsx-tabs" role="tablist" aria-label={t("runDetail.xlsx.sheetsAria")}>
          {workbook.sheets.map((item, index) => (
            <button key={`${item.name}-${index}`} role="tab" aria-selected={index === sheetIndex}
              className={index === sheetIndex ? "is-active" : ""}
              onClick={() => { setSheetIndex(index); if (officeSync?.enabled) officeSync.setSheet(index); setQuery(""); setScrollTop(0); if (gridRef.current) gridRef.current.scrollTop = 0; }}>
              {item.name}
            </button>
          ))}
        </div>
        <label className="xlsx-search">{t("runDetail.xlsx.search")}
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={t("runDetail.xlsx.searchPlaceholder")} />
        </label>
        <div className="xlsx-zoom" aria-label={t("runDetail.xlsx.zoomAria")}>
          <button onClick={() => setZoom((value) => Math.max(50, value - 10))} aria-label={t("runDetail.xlsx.zoomOut")}>−</button>
          <span>{zoom}%</span>
          <button onClick={() => setZoom((value) => Math.min(180, value + 10))} aria-label={t("runDetail.xlsx.zoomIn")}>＋</button>
        </div>
      </div>
      {(workbook.truncated || sheet.truncated) && (
        <div className="xlsx-warning" role="status">
          {t("runDetail.xlsx.truncated")}
        </div>
      )}
      <div className="xlsx-meta">
        {sheet.dimension && <span>{t("runDetail.xlsx.range", { dim: sheet.dimension })}</span>}
        {sheet.frozen && <span>{t("runDetail.xlsx.frozen", { cell: sheet.frozen.top_left_cell ?? t("runDetail.xlsx.frozenEnabled") })}</span>}
        {sheet.merges.length > 0 && <span>{t("runDetail.xlsx.merges", { n: sheet.merges.length })}</span>}
        <span>{t("runDetail.xlsx.formulaNote")}</span>
      </div>
      <div className="xlsx-grid-scroll" ref={gridRef} onScroll={(event) => setScrollTop(event.currentTarget.scrollTop)}>
        <table className="xlsx-grid" style={{ fontSize: `${zoom}%` }}>
          <thead><tr><th className="xlsx-corner" />
            {Array.from({ length: maxColumn }, (_, index) => (
              <th key={index + 1}>{spreadsheetColumnLabel(index + 1)}</th>
            ))}
          </tr></thead>
          <tbody>
            {virtualized && virtualStart > 0 && <tr className="xlsx-spacer" aria-hidden="true">
              <td colSpan={maxColumn + 1} style={{ height: virtualStart * rowHeight }} />
            </tr>}
            {visibleRows.map((row) => (
              <tr key={row.index} className={row.hidden ? "xlsx-hidden-row" : ""}>
                <th>{row.index}</th>
                {Array.from({ length: maxColumn }, (_, index) => {
                  const cell = cellByPosition.get(`${row.index}:${index + 1}`);
                  return <td key={index + 1} title={cell?.formula ? `=${cell.formula}` : cell?.number_format ?? undefined}>
                    {cell ? displayValue(cell.display_value ?? cell.value) : <span className="xlsx-empty">∅</span>}
                    {cell?.formula && <span className="xlsx-formula">={cell.formula}</span>}
                  </td>;
                })}
              </tr>
            ))}
            {virtualized && virtualEnd < rows.length && <tr className="xlsx-spacer" aria-hidden="true">
              <td colSpan={maxColumn + 1} style={{ height: (rows.length - virtualEnd) * rowHeight }} />
            </tr>}
          </tbody>
        </table>
        {rows.length === 0 && <div className="artifact-preview-state">{t("runDetail.xlsx.noMatch")}</div>}
      </div>
    </div>
  );
}

// HTML 产物预览（frontend-vfx-* 这类场景的产物必须执行才看得出效果）。
//
// **安全模型**：产物是被测 agent 生成的不可信内容。这里用
// `sandbox="allow-scripts"` 且**刻意不给** `allow-same-origin`——两者必须同时
// 存在才能拿到同源权限，只给前者时 iframe 是独立的不透明源：脚本能跑
// （Three.js 需要），但读不到 Octagon 的 cookie/localStorage，也无法以当前
// 用户身份调 /api/*（删 run、读别的 attempt 产物）。
//
// 默认显示源码、点按钮才运行：与本页"默认显示原始数据"的取向一致，不让
// 打开产物这个动作本身就触发执行。
function HtmlArtifactView({ html }: { html: string }) {
  const { t } = useI18n();
  const [running, setRunning] = useState(false);
  return (
    <div className="html-artifact">
      <div className="html-artifact-bar">
        <button className="tab" onClick={() => setRunning((v) => !v)}>
          {running ? t("runDetail.html.viewSource") : t("runDetail.html.runPreview")}
        </button>
        <span className="muted">
          {running
            ? t("runDetail.html.sandboxNote")
            : t("runDetail.html.chars", { n: html.length.toLocaleString() })}
        </span>
      </div>
      {running
        ? <iframe
            className="html-artifact-frame"
            title={t("runDetail.html.frameTitle")}
            sandbox="allow-scripts"
            srcDoc={html}
          />
        : <pre>{html}</pre>}
    </div>
  );
}

// 默认展开到第二层：一级目录（backend/frontend）几乎总是脚手架名字，
// 展开一层才看得到 app/、src/ 这种真正区分实现的层级；再深就淹没了。
const ARTIFACT_AUTO_EXPAND_DEPTH = 2;

// 只有一个子目录、且自己没有文件的目录链会折成 `backend/app/routers` 一行。
// 全栈脚手架里这种单通道链很常见，逐层点开只是徒增点击。
function collapseChain(dir: ArtifactDir): { labels: string[]; node: ArtifactDir } {
  const labels = [dir.name];
  let node = dir;
  while (node.files.length === 0 && node.dirs.length === 1) {
    node = node.dirs[0];
    labels.push(node.name);
  }
  return { labels, node };
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} K`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} M`;
}

function ArtifactTreeNode({ dir, depth, expanded, toggle, onOpenFile, fileIcon, activePath }: {
  dir: ArtifactDir;
  depth: number;
  expanded: Set<string>;
  toggle: (path: string) => void;
  onOpenFile: (file: ArtifactFile) => void;
  fileIcon: (type: ArtifactType) => string;
  activePath: string | null;
}) {
  const { t } = useI18n();
  const { labels, node } = collapseChain(dir);
  const open = expanded.has(dir.path);
  return (
    <div className="artifact-node">
      <button
        className="artifact-dir-row"
        style={{ paddingLeft: 8 + depth * 14 }}
        onClick={() => toggle(dir.path)}
        aria-expanded={open}
      >
        <span className="ev-chevron">{open ? "▼" : "▶"}</span>
        <span className="artifact-dir-name">{labels.join("/")}</span>
        <span className="artifact-dir-meta">
          {t("runDetail.artifact.dirMeta", { n: node.file_count, size: formatBytes(node.total_size) })}
        </span>
      </button>
      {open && (
        <>
          {node.dirs.map((child) => (
            <ArtifactTreeNode
              key={child.path} dir={child} depth={depth + 1}
              expanded={expanded} toggle={toggle} onOpenFile={onOpenFile}
              fileIcon={fileIcon} activePath={activePath}
            />
          ))}
          {node.files.map((file) => (
            <button
              key={file.path}
              className={`artifact-file${activePath === file.path ? " is-active" : ""}`}
              style={{ paddingLeft: 8 + (depth + 1) * 14 }}
              onClick={() => onOpenFile(file)}
            >
              <span className="artifact-file-icon">{fileIcon(file.type)}</span>
              <span className="artifact-file-name">{file.name}</span>
              <span className="artifact-file-size">{formatBytes(file.size)}</span>
            </button>
          ))}
        </>
      )}
    </div>
  );
}

export function ArtifactsPanel({ tree, runId, attemptId, officeSync }: { tree: ArtifactDir | null; runId: string; attemptId: string; officeSync?: OfficeSyncState }) {
  const { t } = useI18n();
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [preview, setPreview] = useState<{
    path: string;
    type: ArtifactType;
    state: "loading" | "ready" | "error";
    content?: string;
    descriptor?: ArtifactPreviewDescriptor;
    error?: string;
  } | null>(null);
  const previewRequest = useRef<AbortController | null>(null);
  useEffect(() => () => previewRequest.current?.abort(), []);
  // 换 attempt 时重置展开状态：路径 key 在不同 attempt 间会碰撞，沿用会让
  // 新树以上一个 attempt 的展开形态出现。
  useEffect(() => {
    if (!tree) return;
    const initial = new Set<string>();
    // 按**折叠后**的视图计深度：collapseChain 把单通道链合成一行，展开状态
    // 存的是链首的 path，跟着原始层级走会往集合里塞永远点不到的中间节点，
    // 而真正的第二层反而没展开。
    const walk = (dir: ArtifactDir, depth: number) => {
      if (depth >= ARTIFACT_AUTO_EXPAND_DEPTH) return;
      for (const child of dir.dirs) {
        initial.add(child.path);
        walk(collapseChain(child).node, depth + 1);
      }
    };
    walk(tree, 0);
    setExpanded(initial);
  }, [tree]);
  if (!tree || tree.file_count === 0) return null;
  const waitForPoll = (milliseconds: number, signal: AbortSignal) => new Promise<void>((resolve, reject) => {
    const onAbort = () => {
      window.clearTimeout(timer);
      reject(new DOMException("aborted", "AbortError"));
    };
    const timer = window.setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, Math.max(10, Math.min(milliseconds, 5000)));
    if (signal.aborted) onAbort();
    else signal.addEventListener("abort", onAbort, { once: true });
  });
  const loadPreview = async (file: ArtifactFile) => {
    const path = file.path;
    previewRequest.current?.abort();
    const controller = new AbortController();
    previewRequest.current = controller;
    if (file.type === "image" || file.type === "video" || file.type === "audio") {
      setPreview({ path, type: file.type, state: "ready" });
      return;
    }
    setPreview({ path, type: file.type, state: "loading" });
    try {
      if (file.type === "text" || file.type === "html") {
        // html 与 text 走同一条取内容路径——后端一律以纯文本返回（绝不发
        // text/html，否则直接访问 URL 就是同源页面）。渲染由前端的
        // `<iframe sandbox srcdoc>` 负责，见 HtmlArtifactView。
        const resp = await fetch(api.artifactUrl(runId, attemptId, path), { signal: controller.signal });
        if (!resp.ok) throw new Error(`artifact -> ${resp.status}`);
        const content = await resp.text();
        if (!controller.signal.aborted) setPreview({ path, type: file.type, state: "ready", content });
      } else {
        for (let poll = 0; poll < 120; poll++) {
          const descriptor = await api.getArtifactPreview(runId, attemptId, path, controller.signal);
          if (controller.signal.aborted) return;
          setPreview({ path, type: file.type, state: "ready", descriptor });
          if (descriptor.status !== "rendering") return;
          await waitForPoll(descriptor.poll_after_ms ?? 500, controller.signal);
        }
        throw new Error(t("runDetail.artifact.previewTimeout"));
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        setPreview({ path, type: file.type, state: "error", error: error instanceof Error ? error.message : t("runDetail.artifact.previewFailed") });
      }
    }
  };
  const fileIcon = (type: ArtifactType) => ({
    image: "🖼", video: "🎬", audio: "🔊", presentation: "📊",
    document: "📝", spreadsheet: "📈", html: "🌐", text: "📄", binary: "📦",
  })[type];
  const toggle = (path: string) => setExpanded((current) => {
    const next = new Set(current);
    if (next.has(path)) next.delete(path);
    else next.add(path);
    return next;
  });
  return (
    <div className="artifacts-panel">
      <div className="artifacts-header">
        {t("runDetail.artifact.panelHeader", { n: tree.file_count, size: formatBytes(tree.total_size) })}
        {tree.truncated && <span className="artifact-truncated">{t("runDetail.artifact.truncated")}</span>}
      </div>
      <div className="artifact-tree">
        {tree.dirs.map((child) => (
          <ArtifactTreeNode
            key={child.path} dir={child} depth={0}
            expanded={expanded} toggle={toggle} onOpenFile={loadPreview}
            fileIcon={fileIcon} activePath={preview?.path ?? null}
          />
        ))}
        {/* 根目录直属文件排在目录之后：README 这类总是少数，先看结构再看散文件 */}
        {tree.files.map((file) => (
          <button
            key={file.path}
            className={`artifact-file${preview?.path === file.path ? " is-active" : ""}`}
            style={{ paddingLeft: 8 }}
            onClick={() => loadPreview(file)}
          >
            <span className="artifact-file-icon">{fileIcon(file.type)}</span>
            <span className="artifact-file-name">{file.name}</span>
            <span className="artifact-file-size">{formatBytes(file.size)}</span>
          </button>
        ))}
      </div>
      {preview && (
        <div className="artifact-preview">
          <div className="artifact-preview-header">
            <span>{preview.path}</span>
            <span className="artifact-preview-actions">
              <a href={api.artifactUrl(runId, attemptId, preview.path)} download>{t("runDetail.artifact.download")}</a>
              <span className="artifact-close" onClick={() => { previewRequest.current?.abort(); setPreview(null); }}>✕</span>
            </span>
          </div>
          {preview.state === "loading" && <div className="artifact-preview-state">{t("runDetail.artifact.checking")}</div>}
          {preview.state === "error" && <div className="artifact-preview-state artifact-preview-error">{preview.error}</div>}
          {preview.state === "ready" && preview.type === "image" && (
            <img src={api.artifactUrl(runId, attemptId, preview.path)} alt={preview.path} style={{ maxWidth: "100%" }} />
          )}
          {preview.state === "ready" && preview.type === "video" && (
            <video src={api.artifactUrl(runId, attemptId, preview.path)} controls style={{ maxWidth: "100%" }} />
          )}
          {preview.state === "ready" && preview.type === "audio" && (
            <audio src={api.artifactUrl(runId, attemptId, preview.path)} controls style={{ width: "100%" }} />
          )}
          {preview.state === "ready" && preview.type === "text" && (
            <pre>{preview.content}</pre>
          )}
          {preview.state === "ready" && preview.type === "html" && (
            <HtmlArtifactView html={preview.content ?? ""} />
          )}
          {preview.state === "ready" && preview.descriptor?.status === "rendering" && (
            <div className="artifact-preview-state" role="status">
              <strong>{t("runDetail.artifact.renderingTitle")}</strong>
              <div className="muted">{t("runDetail.artifact.renderingNote")}</div>
            </div>
          )}
          {preview.state === "ready" && preview.descriptor?.content?.kind === "workbook" && (
            <WorkbookViewer key={`${preview.path}:${preview.descriptor.cache_key}`}
              workbook={preview.descriptor.content} officeSync={officeSync} />
          )}
          {preview.state === "ready" && preview.descriptor?.content?.kind === "presentation" && (
            <PresentationViewer key={`${preview.path}:${preview.descriptor.cache_key}`}
              presentation={preview.descriptor.content} officeSync={officeSync} />
          )}
          {preview.state === "ready" && preview.descriptor?.content?.kind === "document" && (
            <DocumentViewer key={`${preview.path}:${preview.descriptor.cache_key}`}
              document={preview.descriptor.content} />
          )}
          {preview.state === "ready" && preview.descriptor?.content &&
            preview.descriptor.capability_gaps.length > 0 && (
            <div className="artifact-capability-gaps" role="note">
              {t("runDetail.artifact.capabilityGaps", { gaps: preview.descriptor.capability_gaps.join(t("runDetail.common.listSep")) })}
            </div>
          )}
          {preview.state === "ready" && preview.descriptor &&
            preview.descriptor.status !== "rendering" && !preview.descriptor.content && (
            <div className="artifact-preview-state">
              <strong>{preview.descriptor.status === "failed" ? t("runDetail.artifact.securityFailed") : t("runDetail.artifact.previewNotWired")}</strong>
              {preview.descriptor.error?.message && <div>{preview.descriptor.error.message}</div>}
              <div className="muted">
                {preview.descriptor.artifact.media_type}
                {preview.descriptor.counts.slides != null && t("runDetail.artifact.slidesCount", { n: preview.descriptor.counts.slides })}
                {preview.descriptor.counts.sheets != null && t("runDetail.artifact.sheetsCount", { n: preview.descriptor.counts.sheets })}
              </div>
              {preview.descriptor.capability_gaps.length > 0 && (
                <div className="artifact-capability-gaps">{t("runDetail.artifact.limitedCapabilities", { gaps: preview.descriptor.capability_gaps.join(t("runDetail.common.listSep")) })}</div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── behavioral insights ──

type Insight = { title: string; icon: string; items: Array<{ agent: string; label: string; text: string }> };

function buildInsights(
  atts: Array<{ id: string; agent_name: string; model?: string | null; model_used?: string | null; duration_ms: number; tool_call_count: number; token_usage_json: string | null }>,
  allDetails: Record<string, AttemptDetail>,
  t: TFn,
): Insight[] {
  const insights: Insight[] = [];
  const sep = t("runDetail.common.listSep");

  // 1) 参数决策
  const paramInsight: Insight = { title: t("runDetail.insight.paramTitle"), icon: "🎯", items: [] };
  for (const att of atts) {
    const d = allDetails[att.id];
    const calls = (d?.tool_calls ?? []) as ToolCall[];
    const custom = calls.filter((c) => {
      const args = Object.entries(c.arguments).filter(([k, v]) => k !== "session_dir" && v != null);
      return args.length > 0;
    });
    if (custom.length === 0) {
      paramInsight.items.push({ agent: att.agent_name, label: columnLabel(att, atts), text: t("runDetail.insight.allDefault") });
    } else {
      const names = custom.map((c) => c.tool_name.replace(/^(acquire_|assess_|plan_|assign_|render_|simulate_|summarize_)/, "")).join(sep);
      paramInsight.items.push({ agent: att.agent_name, label: columnLabel(att, atts), text: t("runDetail.insight.customParams", { n: custom.length, names }) });
    }
  }
  insights.push(paramInsight);

  // 2) 错误恢复
  const errorInsight: Insight = { title: t("runDetail.insight.errorTitle"), icon: "🔄", items: [] };
  for (const att of atts) {
    const d = allDetails[att.id];
    const calls = (d?.tool_calls ?? []) as ToolCall[];
    const errs = calls.filter((c) => c.is_error);
    if (errs.length === 0) {
      errorInsight.items.push({ agent: att.agent_name, label: columnLabel(att, atts), text: t("runDetail.insight.noError") });
    } else {
      const errNames = errs.map((c) => c.tool_name).join(sep);
      const retries = calls.length - new Set(calls.map((c) => c.tool_name)).size;
      const recovery = retries > 0 ? t("runDetail.insight.retriedSuccess", { n: retries }) : t("runDetail.insight.noRetry");
      errorInsight.items.push({ agent: att.agent_name, label: columnLabel(att, atts), text: t("runDetail.insight.errorRecovery", { n: errs.length, names: errNames, recovery }) });
    }
  }
  insights.push(errorInsight);

  // 3) 执行效率
  const effInsight: Insight = { title: t("runDetail.insight.effTitle"), icon: "⚡", items: [] };
  const sorted = [...atts].sort((a, b) => a.duration_ms - b.duration_ms);
  for (const att of atts) {
    const d = allDetails[att.id];
    const calls = (d?.tool_calls ?? []) as ToolCall[];
    const totalToolTime = calls.reduce((s, c) => s + c.duration_ms, 0);
    const rank = sorted.findIndex((a) => a.id === att.id) + 1;
    const rankLabel = rank === 1 ? t("runDetail.insight.rankFastest") : rank === sorted.length ? t("runDetail.insight.rankSlowest") : t("runDetail.insight.rankNth", { n: rank });
    let tok = "";
    try { const o = JSON.parse(att.token_usage_json ?? "{}"); tok = t("runDetail.insight.tokensSuffix", { n: ((o.input_tokens ?? 0) + (o.output_tokens ?? 0)).toLocaleString() }); } catch { /* skip */ }
    effInsight.items.push({ agent: att.agent_name, label: columnLabel(att, atts), text: t("runDetail.insight.effText", { duration: fmt(att.duration_ms), rank: rankLabel, calls: calls.length, toolTime: fmt(totalToolTime), tokens: tok }) });
  }
  insights.push(effInsight);

  return insights;
}

// ── constants ──

// dimension → i18n messageKey（在渲染处用 dimLabel(t, dim) 取本地化文案）。
const DIMENSION_LABEL_KEYS: Record<string, string> = {
  step_completion: "runDetail.dim.step_completion", tool_efficiency: "runDetail.dim.tool_efficiency",
  parameter_quality: "runDetail.dim.parameter_quality", final_score: "runDetail.dim.final_score",
  workflow_completion: "runDetail.dim.workflow_completion", plan_quality: "runDetail.dim.plan_quality", subtitle_quality: "runDetail.dim.subtitle_quality",
  mission_completion: "runDetail.dim.mission_completion", target_confirmation: "runDetail.dim.target_confirmation",
  data_accuracy: "runDetail.dim.data_accuracy", artifact_completeness: "runDetail.dim.artifact_completeness",
  task_completion: "runDetail.dim.task_completion", constraint_compliance: "runDetail.dim.constraint_compliance", efficiency: "runDetail.dim.efficiency",
};
// 维度标签：命中字典用本地化文案，否则回落原始 dimension 名。
function dimLabel(t: TFn, dimension: string): string {
  const key = DIMENSION_LABEL_KEYS[dimension];
  return key ? t(key) : dimension;
}
// 兜底权重/阈值：真实值优先取 /api/envs 的 dimensions 与 pass_threshold（见 useEnvMeta）
const DIMENSION_WEIGHTS: Record<string, number> = {
  step_completion: 40, tool_efficiency: 20, parameter_quality: 20, final_score: 20,
  workflow_completion: 40, plan_quality: 30, subtitle_quality: 30,
};
const PASS_THRESHOLDS: Record<string, number> = {
  "recording-recap": 40,
};

// 各 env 的权重/阈值以后端 meta.yaml 为准，避免前端硬编码漂移
function useEnvMeta(envName: string | undefined): { weights: Record<string, number>; threshold: number | null } {
  const [meta, setMeta] = useState<{ weights: Record<string, number>; threshold: number | null }>({ weights: {}, threshold: null });
  useEffect(() => {
    if (!envName) return;
    let alive = true;
    api.listEnvs().then((envs) => {
      if (!alive) return;
      const env = envs.find((e) => e.name === envName);
      if (!env) return;
      const weights: Record<string, number> = {};
      for (const d of env.dimensions ?? []) weights[d.name] = d.weight;
      setMeta({ weights, threshold: env.pass_threshold });
    }).catch(() => { /* 拿不到时用前端兜底表 */ });
    return () => { alive = false; };
  }, [envName]);
  return meta;
}
const STATUS_LABEL_KEYS: Record<string, string> = {
  completed: "runDetail.status.completed", running: "runDetail.status.running", scoring: "runDetail.status.scoring", queued: "runDetail.status.queued",
  gave_up: "runDetail.status.gave_up", timeout: "runDetail.status.timeout", scoring_failed: "runDetail.status.scoring_failed",
  cancelled: "runDetail.status.cancelled", interrupted: "runDetail.status.interrupted",
  session_create_failed: "runDetail.status.session_create_failed", cli_not_found: "runDetail.status.cli_not_found",
  cli_error: "runDetail.status.cli_error", chat_failed: "runDetail.status.chat_failed", auth_failed: "runDetail.status.auth_failed",
  model_integrity_failed: "runDetail.status.model_integrity_failed",
  sandbox_unavailable: "runDetail.status.sandbox_unavailable",
};
function statusLabel(t: TFn, status: string): string {
  const key = STATUS_LABEL_KEYS[status];
  return key ? t(key) : status;
}

function fmt(n: number): string { return n < 1000 ? `${n}ms` : `${(n / 1000).toFixed(1)}s`; }
// 令牌摘要：输入/输出之外单列缓存命中——缓存复读单价通常只有 prompt 的
// 1/10，把它并进输入会让人误判消耗规模（实测 CC 一次 attempt 76.9 万令牌里
// 99.8% 是缓存复读，真实新增输入只有 1789）。
function tokens(json: string | null, t: TFn): string {
  if (!json) return "-";
  try {
    const o = JSON.parse(json);
    const i = o.input_tokens ?? 0, out = o.output_tokens ?? 0;
    if (!i && !out) return "-";
    const parts = [t("runDetail.tokens.input", { n: i.toLocaleString() }), t("runDetail.tokens.output", { n: out.toLocaleString() })];
    // 0 是"确实没命中缓存"，与字段缺失（不可得）不同，两者都要能看出来
    if (o.cache_read_tokens != null) parts.push(t("runDetail.tokens.cache", { n: o.cache_read_tokens.toLocaleString() }));
    if (o.reasoning_tokens) parts.push(t("runDetail.tokens.reasoning", { n: o.reasoning_tokens.toLocaleString() }));
    return parts.join(" / ");
  } catch { return "-"; }
}

// 成本：priced=false 表示有令牌缺定价，展示的是**下界**，必须标注出来，
// 不能让读者把下界当准确成本。
function costLabel(usd?: number | null, priced?: boolean | null): string {
  if (usd == null) return "-";
  const shown = usd < 0.01 ? `$${usd.toFixed(5)}` : `$${usd.toFixed(4)}`;
  return priced === false ? `≥${shown}` : shown;
}

// 资金口径：统计行里的「费用」只能是上游实扣。
// 拿不到实扣时显示原因（"结算中" / "经网关…"），**绝不**回落本地估算——
// 旧实现在这里显示 att.cost_usd（token × 价表估算），使同一个 run 的
// attempt 卡片与成本卡片给出相差一个数量级的两个金额。
export function MoneyStat({ money }: { money?: AttemptMoney }) {
  const { t } = useI18n();
  if (!money) return null;
  if (money.audited_cost_usd != null) {
    return <span>{t("runDetail.money.audited", { amount: costLabel(money.audited_cost_usd) })}</span>;
  }
  if (!money.unaudited_reason) return null;
  return <span className="cost-unaudited">{t("runDetail.money.unaudited", { reason: money.unaudited_reason })}</span>;
}

// token_cost_accounting：五维用量 + 四档成本明细。
//
// 设计要点：缓存复读单价通常只有 prompt 的 1/10，把"令牌占比"和"成本占比"
// 并排展示，才能一眼看出「令牌大头未必是成本大头」——实测 CC 一次 attempt
// 缓存复读占 99.8% 的令牌，却只占约 38% 的成本。
// 字段顺序有意义（rows 遍历用），值是 i18n messageKey。
const USAGE_FIELD_LABEL_KEYS: Record<string, string> = {
  input_tokens: "runDetail.usage.input_tokens",
  output_tokens: "runDetail.usage.output_tokens",
  cache_read_tokens: "runDetail.usage.cache_read_tokens",
  cache_write_tokens: "runDetail.usage.cache_write_tokens",
  reasoning_tokens: "runDetail.usage.reasoning_tokens",
};

// run 级成本状态的呈现规则。**settling 绝不展示临时 0**——
// 未结算的 0 会被读成"这次没花钱"，比不显示危险得多。
// text 存 i18n messageKey，渲染处 t(meta.text)。
const RUN_COST_STATUS: Record<
  RunCostStatus,
  { text: string; tone: string; showMoney: boolean }
> = {
  pending: { text: "runDetail.runCost.pending", tone: "neutral", showMoney: false },
  settling: { text: "runDetail.runCost.settling", tone: "neutral", showMoney: false },
  final: { text: "runDetail.runCost.final", tone: "good", showMoney: true },
  upper_bound: { text: "runDetail.runCost.upperBound", tone: "warn", showMoney: true },
  failed: { text: "runDetail.runCost.failed", tone: "bad", showMoney: false },
  incomplete: { text: "runDetail.runCost.incomplete", tone: "warn", showMoney: false },
  unavailable: { text: "runDetail.runCost.unavailable", tone: "neutral", showMoney: false },
};

function pct(value?: number | null): string {
  return value == null ? "-" : `${(value * 100).toFixed(1)}%`;
}

export function RunCostCard({ runId }: { runId: string }) {
  const { t } = useI18n();
  const [cost, setCost] = useState<RunCost | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    api
      .getRunCost(runId)
      .then((c) => alive && setCost(c))
      .catch(() => alive && setFailed(true));
    return () => {
      alive = false;
    };
  }, [runId]);

  if (failed || !cost) return null;
  const meta = RUN_COST_STATUS[cost.status] ?? RUN_COST_STATUS.unavailable;
  const up = cost.upstream;
  const est = cost.estimate;
  // 估算全无、上游也没有 → 整张卡没有信息量，不渲染空面板。
  if (!meta.showMoney && est.total_cost_usd == null) return null;

  return (
    <>
      <h3 className="section-title">{t("runDetail.runCost.title")}</h3>
      <div className="score-detail-card">
        <div className="score-detail-header">
          <span>
            {t("runDetail.runCost.upstreamActual")}
            <span className={`cost-badge cost-badge-${meta.tone}`}>{t(meta.text)}</span>
          </span>
          <span className="score-detail-total font-mono">
            {meta.showMoney ? costLabel(up?.total_cost_usd) : "—"}
          </span>
        </div>

        {/* 本地估算是**对照值**，永远不标成实扣；priced=false 时保留 ≥ 下界语义。 */}
        <div className="score-dim-row">
          <div className="score-dim-header">
            <span className="score-dim-label">{t("runDetail.runCost.localEstimate")}</span>
            <span className="score-dim-val font-mono">
              {costLabel(est.total_cost_usd, est.priced)}
            </span>
            {cost.divergence_ratio != null && (
              <span className="score-dim-weight">{t("runDetail.runCost.divergence", { pct: pct(cost.divergence_ratio) })}</span>
            )}
          </div>
        </div>

        {meta.showMoney && up && (
          <div className="score-dim-row">
            <div className="score-dim-header">
              <span className="score-dim-label">{t("runDetail.runCost.execScoring")}</span>
              <span className="score-dim-val font-mono">
                {costLabel(up.execution_cost_usd)} / {costLabel(up.scoring_cost_usd)}
              </span>
              {cost.derived?.scoring_cost_ratio != null && (
                <span className="score-dim-weight">
                  {t("runDetail.runCost.scoringShare", { pct: pct(cost.derived.scoring_cost_ratio) })}
                </span>
              )}
            </div>
          </div>
        )}

        {meta.showMoney && up?.key_limit_usd != null && (
          <div className="score-dim-row">
            <div className="score-dim-header">
              <span className="score-dim-label">{t("runDetail.runCost.budget")}</span>
              <span className="score-dim-val font-mono">
                {costLabel(up.total_cost_usd)} / {costLabel(up.key_limit_usd)}
              </span>
              <span className="score-dim-weight">
                {pct(cost.derived?.budget_consumed_ratio)}
              </span>
            </div>
          </div>
        )}

        {meta.showMoney && cost.derived?.avg_cost_per_session_usd != null && (
          <div className="score-dim-detail">
            {/* 必须写明是均摊：N 个 session 共用一把 key，物理上拆不开到每个 session。 */}
            {t("runDetail.runCost.sessionAvg", { n: cost.derived.session_count, amount: costLabel(cost.derived.avg_cost_per_session_usd) })}
          </div>
        )}

        {cost.by_attempt && cost.by_attempt.length > 0 && (
          <>
            {cost.by_attempt.map((a) => (
              <div className="score-dim-row" key={a.attempt_id}>
                <div className="score-dim-header">
                  <span className="score-dim-label">{a.agent_name ?? a.attempt_id}</span>
                  <span className="score-dim-val font-mono">
                    {a.auditable ? costLabel(a.cost_usd) : "—"}
                  </span>
                  {!a.auditable && (
                    <span className="score-dim-weight">{a.status}</span>
                  )}
                </div>
                {a.activity?.by_model.map((item) => (
                  <div className="score-dim-detail" key={item.model}>
                    <span className="font-mono">{item.model}</span>
                    {" · "}
                    {costLabel(item.usage_usd)}
                    {" · "}
                    {t("runDetail.cost.requestsInline", { n: item.requests ?? "—" })}
                  </div>
                ))}
              </div>
            ))}
          </>
        )}
        {cost.status === "incomplete" && cost.partial_total_cost_usd != null && (
          <div className="score-dim-row">
            <div className="score-dim-header">
              <span className="score-dim-label">{t("runDetail.runCost.accountedPart")}</span>
              <span className="score-dim-val font-mono">
                {costLabel(cost.partial_total_cost_usd)}
              </span>
              <span className="score-dim-weight">{t("runDetail.runCost.excludesMissing")}</span>
            </div>
          </div>
        )}
        {(cost.unpriced_ledger_count ?? 0) > 0 && (
          <div className="score-primary-issue">
            {t("runDetail.runCost.unpricedIssue", { n: cost.unpriced_ledger_count ?? 0 })}
          </div>
        )}
        {cost.missing_attempt_ids && cost.missing_attempt_ids.length > 0 && (
          <div className="score-primary-issue">
            {t("runDetail.runCost.missingAttempts", { n: cost.missing_attempt_ids.length })}
          </div>
        )}
        {cost.judge_missing && (
          <div className="score-primary-issue">
            {t("runDetail.runCost.judgeMissing")}
          </div>
        )}
        {cost.status === "upper_bound" && (
          <div className="score-primary-issue">
            {t("runDetail.runCost.upperBoundIssue")}
          </div>
        )}
        {cost.status === "failed" && (
          <div className="score-primary-issue">
            {t("runDetail.runCost.failedIssue", { code: cost.error_code ? `（${cost.error_code}）` : "" })}
          </div>
        )}
        {est.attempts_missing_cost > 0 && (
          <div className="score-dim-detail">
            {t("runDetail.runCost.estimateMissing", { n: est.attempts_missing_cost })}
          </div>
        )}
      </div>
    </>
  );
}

type LedgerRow = NonNullable<RunCost["by_attempt"]>[number];

// 令牌与成本卡片。
//
// **金额只来自上游实扣**（逐 attempt 独立 key 的 usage 差值）——本地
// token×价格表估算已删除：实测高估 26× 且方向不一致（高估 3.5~4× 与
// 低估 5~10× 并存），留着只会让人以为能横向比较。
//
// **令牌来自本地采集**：五维完整，含上游 Activity **不提供**的
// cache read/write 两维。上游三维明细放在展开区做交叉核对。
function CostCard({
  label,
  detail,
  ledger,
}: {
  label: string;
  detail?: AttemptDetail;
  ledger?: LedgerRow;
}) {
  const { t } = useI18n();
  const [showUpstream, setShowUpstream] = useState(false);
  const usage = detail?.token_usage ?? {};
  const rows = Object.keys(USAGE_FIELD_LABEL_KEYS).filter((f) => usage[f] != null);
  const totalTokens = rows.reduce((sum, f) => sum + (usage[f] ?? 0), 0);
  const up = detail?.upstream_tokens;

  // 金额：只认上游实扣。拿不到就显示"—"，绝不回落估算。
  const money = ledger?.auditable ? costLabel(ledger.cost_usd) : "—";
  const moneyNote = !ledger
    ? t("runDetail.cost.noLedger")
    : ledger.auditable
      ? null
      : ledger.status === "pending" || ledger.status === "settling"
        ? t("runDetail.cost.settling")
        : ledger.error_code === "gateway_bound_agent"
          ? t("runDetail.cost.gatewayBound")
          : ledger.status;

  return (
    <div className="score-detail-card">
      <div className="score-detail-header">
        <span>{label}</span>
        <span className="score-detail-total font-mono">{money}</span>
      </div>
      {moneyNote && <div className="score-verdict">{moneyNote}</div>}
      {detail?.cost?.model && (
        <div className="score-verdict font-mono">{detail.cost.model}</div>
      )}

      {rows.length === 0 && (
        <div className="score-dim-detail">
          {t("runDetail.cost.noTokenUsage")}
        </div>
      )}
      {rows.map((field) => {
        const tok = usage[field] ?? 0;
        const tokPct = totalTokens > 0 ? (tok / totalTokens) * 100 : 0;
        return (
          <div key={field} className="score-dim-row">
            <div className="score-dim-header">
              <span className="score-dim-label">{t(USAGE_FIELD_LABEL_KEYS[field])}</span>
              <span className="score-dim-val font-mono">
                {tok.toLocaleString()}
              </span>
            </div>
            <div className="score-dim-bar">
              <div
                className="score-dim-fill"
                style={{ width: `${Math.min(100, tokPct)}%`, background: "var(--text-2)" }}
              />
            </div>
            <div className="score-dim-detail">{t("runDetail.cost.tokenShare", { pct: tokPct.toFixed(1) })}</div>
          </div>
        );
      })}

      {rows.length > 0 && (
        <>
          <button
            type="button"
            className="cost-upstream-toggle"
            onClick={() => setShowUpstream((v) => !v)}
          >
            {showUpstream ? "▾" : "▸"} {t("runDetail.cost.upstreamDetail")}
          </button>
          {showUpstream && (
            <div className="cost-upstream-body">
              {up ? (
                <>
                  {(["prompt_tokens", "completion_tokens", "reasoning_tokens"] as const).map(
                    (k) => (
                      <div key={k} className="score-dim-header">
                        <span className="score-dim-label">{t(UPSTREAM_LABEL_KEYS[k])}</span>
                        <span className="score-dim-val font-mono">
                          {up[k] != null ? up[k]!.toLocaleString() : "—"}
                        </span>
                      </div>
                    ),
                  )}
                  {up.requests != null && (
                    <div className="score-dim-header">
                      <span className="score-dim-label">{t("runDetail.cost.requestCount")}</span>
                      <span className="score-dim-val font-mono">{up.requests}</span>
                    </div>
                  )}
                  {up.by_model.length > 0 && (
                    <>
                      <div className="score-dim-detail">
                        {t("runDetail.cost.upstreamBilledNote")}
                      </div>
                      {up.by_model.map((item) => (
                        <div className="score-dim-header" key={item.model}>
                          <span className="score-dim-label font-mono">{item.model}</span>
                          <span className="score-dim-val font-mono">
                            {t("runDetail.cost.upstreamRequests", { amount: costLabel(item.usage_usd), n: item.requests ?? "—" })}
                          </span>
                        </div>
                      ))}
                    </>
                  )}
                  <div className="score-dim-detail">
                    {t("runDetail.cost.upstreamThreeDims")}<strong>{t("runDetail.cost.cacheNotProvided")}</strong>{t("runDetail.cost.cacheLocalOnly")}
                  </div>
                </>
              ) : (
                <div className="score-dim-detail">
                  {t("runDetail.cost.noUpstreamYet")}<strong>{t("runDetail.cost.completedUtcDate")}</strong>{t("runDetail.cost.dayCloseNote")}
                </div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}

const UPSTREAM_LABEL_KEYS: Record<string, string> = {
  prompt_tokens: "runDetail.upstream.prompt_tokens",
  completion_tokens: "runDetail.upstream.completion_tokens",
  reasoning_tokens: "runDetail.upstream.reasoning_tokens",
};

// run 级小结：只在有缺账时出现，说明缺的是哪几笔、为什么。
function RunCostSummary({ cost }: { cost: RunCost }) {
  const { t } = useI18n();
  const unpriced = cost.unpriced_ledgers ?? [];
  const total = cost.upstream?.total_cost_usd;
  const partial = cost.partial_total_cost_usd;

  if (total != null) {
    return (
      <div className="cost-run-summary">
        {t("runDetail.costBanner.total")}<strong className="font-mono">{costLabel(total)}</strong>
        {cost.scoring_cost_ratio != null &&
          t("runDetail.costBanner.scoringShare", { pct: (cost.scoring_cost_ratio * 100).toFixed(1) })}
      </div>
    );
  }
  if (unpriced.length === 0) return null;

  // 区分"还在跑"和"永远拿不到"——前者等一等就有，后者是结构性的
  const pending = unpriced.filter((x) => x.pending);
  const blocked = unpriced.filter((x) => !x.pending);
  const sep = t("runDetail.common.listSep");
  return (
    <div className="cost-run-summary">
      {partial != null && (
        <>
          {t("runDetail.costBanner.accounted")}<strong className="font-mono">{costLabel(partial)}</strong>
          {t("runDetail.costBanner.notTotal")}
        </>
      )}
      {pending.length > 0 && (
        <>
          {t("runDetail.costBanner.pending", { agents: pending.map((x) => x.agent_name ?? t("runDetail.costBanner.scoring")).join(sep) })}
          {blocked.length > 0 && t("runDetail.costBanner.listSep")}
        </>
      )}
      {blocked.length > 0 && (
        <>
          {blocked
            .map(
              (x) =>
                t("runDetail.costBanner.blockedItem", {
                  agent: x.agent_name ?? t("runDetail.costBanner.scoring"),
                  reason: x.error_code === "gateway_bound_agent"
                    ? t("runDetail.costBanner.gatewayBound")
                    : x.error_code ?? x.status,
                }),
            )
            .join(sep)}
        </>
      )}
      {t("runDetail.common.period")}
    </div>
  );
}

// status 只承载**执行**结论；评分结果由 scoringStatus 单独标注。
// 两者分开显示——"agent 没跑起来"和"agent 跑完了但 judge 挂了"
// 是完全不同的两件事，混成一个「失败」会让人误判 agent 的表现。
function Badge({
  status,
  scoringStatus,
}: {
  status: string;
  scoringStatus?: string;
}) {
  const { t } = useI18n();
  const v: Record<string, string> = { completed: "var(--green)", running: "var(--yellow)", scoring: "var(--yellow)", queued: "var(--text-2)", gave_up: "var(--yellow)" };
  return (
    <>
      <span className="status-badge" style={{ color: v[status] ?? "var(--red)" }}>{statusLabel(t, status)}</span>
      {scoringStatus === "failed" && (
        <span className="status-badge" style={{ color: "var(--yellow)" }} title={t("runDetail.badge.scoringFailedTitle")}>
          {t("runDetail.badge.scoringFailed")}
        </span>
      )}
    </>
  );
}

// ---- wire 观测 ----

// wire status → i18n messageKey（渲染处用 wireStatusLabel(t, st) 取本地化文案）。
const WIRE_STATUS_LABEL_KEYS: Record<string, string> = {
  complete: "runDetail.wireStatus.complete", partial: "runDetail.wireStatus.partial", failed: "runDetail.wireStatus.failed",
  "not-applicable": "runDetail.wireStatus.notApplicable", not_available: "runDetail.wireStatus.notApplicable",
  "in-progress": "runDetail.wireStatus.inProgress", recovered: "runDetail.wireStatus.recovered",
};
function wireStatusLabel(t: TFn, st: string): string {
  const key = WIRE_STATUS_LABEL_KEYS[st];
  return key ? t(key) : t("runDetail.wireStatus.fallback", { status: st });
}
const WIRE_STATUS_COLOR: Record<string, string> = {
  complete: "var(--green)", partial: "var(--yellow)", recovered: "var(--yellow)",
  failed: "var(--red)",
};
// gap reason → i18n messageKey（渲染处用 gapReasonLabel(t, reason)）。
const GAP_LABEL_KEYS: Record<string, string> = {
  phase_state_write_failed: "runDetail.gap.phase_state_write_failed",
  adapter_capability_missing: "runDetail.gap.adapter_capability_missing",
  adapter_native_mismatch: "runDetail.gap.adapter_native_mismatch",
  unknown_phase_evidence: "runDetail.gap.unknown_phase_evidence",
  native_no_output: "runDetail.gap.native_no_output",
  native_normalize_failed: "runDetail.gap.native_normalize_failed",
  source_start_failed: "runDetail.gap.source_start_failed",
  recovered_after_restart: "runDetail.gap.recovered_after_restart",
  merge_failed: "runDetail.gap.merge_failed",
};
function gapReasonLabel(t: TFn, reason: string): string {
  const key = GAP_LABEL_KEYS[reason];
  return key ? t(key) : reason;
}

function WireBadge({ att }: { att: AttemptSummary }) {
  const { t } = useI18n();
  const st = att.wire_status;
  // 无采集时不占位（避免每个 attempt 都显示噪音）
  if (!st || st === "not_available" || st === "not-applicable") return null;
  const calls = att.wire_call_count ?? 0;
  const title = t("runDetail.wireBadge.title", {
    status: st,
    records: att.wire_record_count ?? 0,
    calls,
    errors: att.wire_error_count ? t("runDetail.wireBadge.errorsSuffix", { n: att.wire_error_count }) : "",
  });
  return (
    <span className="status-badge" title={title}
      style={{ color: WIRE_STATUS_COLOR[st] ?? "var(--text-2)" }}>
      {wireStatusLabel(t, st)}
      {/* 带单位：裸数字容易被读成分数（实测有人问"61 是什么分"）——
          它是该 attempt 打模型 API 的次数。 */}
      {calls > 0 ? t("runDetail.wireBadge.calls", { n: calls }) : ""}
    </span>
  );
}

// SVG polyline token 曲线（无图表依赖）：x=logical call 顺序，
// y=input/cache/output。
function TokenCurve({
  calls, selectedId, onSelect,
}: {
  calls: WireRecord[];
  selectedId?: string | null;
  onSelect?: (call: WireRecord) => void;
}) {
  const { t } = useI18n();
  const W = 520, H = 160, PAD = 32;
  const series: Array<{ key: keyof WireUsage; label: string; color: string }> = [
    { key: "input_tokens", label: t("runDetail.curve.input"), color: "var(--blue, #4a90d9)" },
    { key: "cache_read_tokens", label: t("runDetail.curve.cacheRead"), color: "var(--text-2, #888)" },
    { key: "output_tokens", label: t("runDetail.curve.output"), color: "var(--green, #4caf50)" },
  ];
  const n = calls.length;
  // null ≠ 0：用共享 curveSegments/usageValue（web/src/wire/curve.ts，
  // 有 vitest 固定 fixture 覆盖）——UI 与测试跑同一逻辑，不重复实现。
  let maxY = 1;
  for (const c of calls) for (const s of series) {
    const v = usageValue(c.data?.usage, s.key);
    if (v != null && v > maxY) maxY = v;
  }
  const x = (i: number) => PAD + (n <= 1 ? (W - 2 * PAD) / 2 : (i * (W - 2 * PAD)) / (n - 1));
  const y = (v: number) => H - PAD - (v / maxY) * (H - 2 * PAD);
  return (
    <svg className="token-curve" viewBox={`0 0 ${W} ${H}`} width="100%" style={{ maxWidth: W }}>
      <line x1={PAD} y1={H - PAD} x2={W - PAD} y2={H - PAD} stroke="var(--border, #ccc)" />
      <line x1={PAD} y1={PAD} x2={PAD} y2={H - PAD} stroke="var(--border, #ccc)" />
      <text x={PAD - 4} y={PAD} textAnchor="end" fontSize="10" fill="var(--text-2)">{maxY.toLocaleString()}</text>
      <text x={PAD - 4} y={H - PAD} textAnchor="end" fontSize="10" fill="var(--text-2)">0</text>
      {series.map((s) => (
        <g key={s.key}>
          {curveSegments(calls, s.key).map((seg, si) => (
            <polyline key={si} points={seg.map(([i, v]) => `${x(i)},${y(v)}`).join(" ")}
              fill="none" stroke={s.color} strokeWidth="1.5" />
          ))}
          {calls.map((c, i) => {
            const v = usageValue(c.data?.usage, s.key);
            if (v == null) return null;
            const label = t("runDetail.curve.pointLabel", { n: i + 1, series: s.label, value: v.toLocaleString() });
            return (
              <circle key={i} cx={x(i)} cy={y(v)}
                r={selectedId === c.record_id ? "5" : "3.5"} fill={s.color}
                className="wire-curve-point" role="button" tabIndex={0}
                aria-label={label} aria-pressed={selectedId === c.record_id}
                onClick={() => onSelect?.(c)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault(); onSelect?.(c);
                  }
                }}>
                <title>{label}</title>
              </circle>
            );
          })}
        </g>
      ))}
      {series.map((s, idx) => (
        <g key={"lg" + s.key}>
          <rect x={PAD + idx * 90} y={4} width="10" height="10" fill={s.color} />
          <text x={PAD + idx * 90 + 14} y={13} fontSize="10" fill="var(--text-1)">{s.label}</text>
        </g>
      ))}
    </svg>
  );
}

// 一个 hop 的流式统计：从它的 stream_chunk 派生 TTFT / chunk 数 / 丢包。
// relative_ms 是相对 hop 起始的毫秒；首个 content chunk 的 relative_ms 即 TTFT。
type StreamStats = {
  chunkCount: number;
  ttftMs: number | null;
  lastMs: number | null;
  dropped: number;
  terminal: boolean;
};
function streamStatsForHop(hopChunks: WireRecord[]): StreamStats | null {
  if (hopChunks.length === 0) return null;
  const sorted = [...hopChunks].sort((a, b) => (a.data?.sequence ?? 0) - (b.data?.sequence ?? 0));
  const rels = sorted
    .map((c) => c.data?.relative_ms)
    .filter((v): v is number => typeof v === "number");
  const dropped = sorted.reduce((n, c) => n + (c.data?.dropped_before ?? 0), 0);
  return {
    chunkCount: sorted.length,
    ttftMs: rels.length ? rels[0] : null,
    lastMs: rels.length ? rels[rels.length - 1] : null,
    dropped,
    terminal: sorted.some((c) => c.data?.is_terminal === true),
  };
}
// semantic summary（请求侧）→ 短展示：模型 + messages_hash 前 8 位（跨源对比锚）。
function summaryChip(s?: WireCallSummary | null): string | null {
  if (!s) return null;
  const parts: string[] = [];
  if (s.message_count != null) parts.push(`${s.message_count} msg`);
  if (s.messages_hash) parts.push(`hash ${s.messages_hash.slice(0, 8)}`);
  if (s.tools_hash) parts.push(`tools ${s.tools_hash.slice(0, 6)}`);
  return parts.length ? parts.join(" · ") : null;
}

// 字节数人类可读（用于 manifest.totals.bytes）。
function fmtBytes(n: number): string {
  if (n < 1024) return `${n}B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)}KB`;
  return `${(n / (1024 * 1024)).toFixed(1)}MB`;
}
// coverage 轴状态 → CSS 类名 + 中文短标签。
function axisClass(status?: string): string {
  if (status === "complete") return "ok";
  if (status === "partial") return "warn";
  if (status === "not-applicable") return "na";
  return "absent";  // not-observed / 未知
}
function axisLabel(t: TFn, status?: string): string {
  switch (status) {
    case "complete": return t("runDetail.axis.complete");
    case "partial": return t("runDetail.axis.partial");
    case "not-applicable": return t("runDetail.axis.notApplicable");
    case "not-observed": return t("runDetail.axis.notObserved");
    default: return status ?? t("runDetail.common.unknown");
  }
}

// MCP 帧列表：把 tap 抓到的 JSON-RPC 帧按配对/工具名展示，让"哪次调用
// 用了哪个工具"可见。request/response 配对靠 paired_record_id，与 trajectory step
// 的关联靠 trajectory_step_id（association_confidence 标注可信度）。
function McpFramesSection({ frames }: { frames: WireRecord[] }) {
  const { t } = useI18n();
  if (frames.length === 0) return null;
  // 按 jsonrpc_id 分组配对（request+response 同 id）；无 id 的单列。
  const byId = new Map<string, WireRecord[]>();
  const loose: WireRecord[] = [];
  for (const f of frames) {
    const jid = f.data?.jsonrpc_id;
    if (jid == null) { loose.push(f); continue; }
    const k = String(jid);
    const cur = byId.get(k) ?? [];
    cur.push(f);
    byId.set(k, cur);
  }
  const toolCalls = frames.filter((f) => f.data?.tool_name).length;
  return (
    <section className="wire-hop-timeline" aria-label={t("runDetail.mcp.sectionAria")}>
      <div className="wire-section-title">
        {t("runDetail.mcp.sectionTitle", { frames: frames.length, calls: toolCalls })}
      </div>
      <div className="wire-table-scroll">
        <table className="wire-mcp-table">
          <thead>
            <tr><th>{t("runDetail.mcp.colDirection")}</th><th>{t("runDetail.mcp.colType")}</th><th>method / tool</th><th>{t("runDetail.mcp.colBytes")}</th><th>{t("runDetail.mcp.colTrajectory")}</th><th>{t("runDetail.mcp.colStatus")}</th></tr>
          </thead>
          <tbody>
            {frames.map((f) => {
              const d = f.data;
              return (
                <tr key={f.record_id} className={d?.is_error ? "wire-mcp-err" : undefined}>
                  <td>{d?.direction ?? "?"}</td>
                  <td>{d?.message_kind ?? "?"}</td>
                  <td className="font-mono">{d?.tool_name ?? d?.method ?? "—"}</td>
                  <td>{typeof d?.bytes === "number" ? `${d.bytes}B` : "?"}</td>
                  <td>
                    {d?.trajectory_step_id ? (
                      <a href={`#trajectory-${d.trajectory_step_id}`}>
                        {t("runDetail.mcp.trajectory")}{d.association_confidence ? `（${d.association_confidence}）` : ""}
                      </a>
                    ) : <span className="muted">—</span>}
                  </td>
                  <td>{d?.is_error ? t("runDetail.mcp.error") : ""}{d?.truncated ? t("runDetail.mcp.truncated") : ""}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {byId.size > 0 && (
        <p className="muted wire-hop-note">
          {t("runDetail.mcp.pairedNote", { n: byId.size, loose: loose.length ? t("runDetail.mcp.looseNote", { n: loose.length }) : "" })}
        </p>
      )}
    </section>
  );
}

// http_exchange hop 的 payload/blob 展开（policy 门控）。
// - policy=parsed/full 且 hop 有 body_ref：可展开拉取 blob；blob API 404（未开启
//   或 metadata 降档）→ 明确显示「内容不可用（policy 门控）」，不当错误；
// - policy=metadata/off 或无 body_ref：只展示 metadata，注明「metadata 档不含 body」。
function HopBody({
  runId, attemptId, hop, policy, trajectorySteps = [], matchedCallId, streamStats,
}: {
  runId: string;
  attemptId: string;
  hop: WireRecord;
  policy?: string;
  trajectorySteps?: WireTrajectoryStep[];
  matchedCallId?: string | null;
  streamStats?: StreamStats | null;
}) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [reqBody, setReqBody] = useState<BlobState>({ kind: "idle" });
  const [respBody, setRespBody] = useState<BlobState>({ kind: "idle" });
  const d = hop.data;
  const fullPolicy = policy === "parsed" || policy === "full";
  const hasReqRef = Boolean(d.request_body_ref);
  const hasRespRef = Boolean(d.response_body_ref);
  const canShowBody = fullPolicy && (hasReqRef || hasRespRef);

  const load = async (ref: string, set: (s: BlobState) => void) => {
    set({ kind: "loading" });
    try {
      const r = await api.getWireBlob(runId, attemptId, ref);
      if (r.status === "unavailable") set({ kind: "unavailable" });
      // 存**原始文本**（不预先 pretty）——解析视图由 BlobView 按需 pretty，原文视图
      // 展示未美化文本。string body 原样；object body 存紧凑 JSON。
      else set({
        kind: "ok",
        text: typeof r.body === "string" ? r.body : JSON.stringify(r.body),
      });
    } catch (e) {
      set({ kind: "error", message: String(e) });
    }
  };

  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (next && canShowBody) {
      if (hasReqRef && reqBody.kind === "idle") load(d.request_body_ref!, setReqBody);
      if (hasRespRef && respBody.kind === "idle") load(d.response_body_ref!, setRespBody);
    }
  };

  return (
    <div className="wire-hop">
      <button className="wire-hop-toggle" onClick={toggle} aria-expanded={open}>
        <span className="font-mono">{d.direction ?? "?"} {d.method ?? ""} {d.path ?? hop.record_type}</span>
        <span>{t("runDetail.hop.status", { code: d.status_code ?? "?" })}{d.partial ? " · partial" : ""}{d.streamed ? " · streamed" : ""}</span>
        <span className="muted">
          {typeof hop.time?.duration_ms === "number" ? `${hop.time.duration_ms.toLocaleString()}ms · ` : t("runDetail.hop.durationUnknown")}
          {streamStats?.ttftMs != null ? `TTFT ${Math.round(streamStats.ttftMs)}ms · ` : ""}
          ↑{typeof d.request_bytes === "number" ? d.request_bytes : "?"}B
          {" ↓"}{typeof d.response_bytes === "number" ? d.response_bytes : "?"}B
        </span>
      </button>
      {open && (
        <div className="wire-hop-body">
          <div className="wire-hop-meta">
            <span>source {hop.source?.kind ?? t("runDetail.hop.sourceUnknown")}/{hop.source?.instance ?? t("runDetail.hop.sourceUnknown")}</span>
            <span>phase {hop.phase || t("runDetail.hop.sourceUnknown")}</span>
            <span>confidence {hop.correlation?.confidence ?? t("runDetail.hop.sourceUnknown")}</span>
            {matchedCallId ? (
              <a href={`#wire-call-${matchedCallId}`}>{t("runDetail.hop.callLink", { id: matchedCallId })}</a>
            ) : (
              <span className="wire-unmatched-reason">{t("runDetail.hop.unmatched")}</span>
            )}
            {trajectorySteps.map((step) => (
              <a key={step.step_id} href={`#trajectory-${step.step_id}`}>
                {t("runDetail.hop.trajectory", { seq: step.sequence ?? "?" })}
              </a>
            ))}
            {summaryChip(d.request_summary) && (
              <span title={t("runDetail.hop.reqHashTitle")}>
                {t("runDetail.hop.reqSummary", { chip: summaryChip(d.request_summary) ?? "" })}
              </span>
            )}
            {d.response_summary?.content_hash && (
              <span title={t("runDetail.hop.respHashTitle")}>
                {t("runDetail.hop.respHash", { hash: d.response_summary.content_hash.slice(0, 8) })}
              </span>
            )}
          </div>
          {streamStats && (
            <div className="wire-hop-meta" aria-label={t("runDetail.hop.streamStatsAria")}>
              <span title={t("runDetail.hop.chunkCountTitle")}>chunks {streamStats.chunkCount}</span>
              {streamStats.ttftMs != null && <span>TTFT {Math.round(streamStats.ttftMs)}ms</span>}
              {streamStats.lastMs != null && <span>{t("runDetail.hop.lastChunk", { ms: Math.round(streamStats.lastMs) })}</span>}
              {streamStats.dropped > 0 && (
                <span className="wire-unmatched-reason" title={t("runDetail.hop.droppedTitle")}>
                  {t("runDetail.hop.dropped", { n: streamStats.dropped })}
                </span>
              )}
              <span className="muted">{streamStats.terminal ? t("runDetail.hop.streamComplete") : t("runDetail.hop.streamNoTerminal")}</span>
            </div>
          )}
          {!fullPolicy && (
            <p className="muted">
              {t("runDetail.hop.policyNoBody")}<b>{policy ?? "metadata"}</b>{t("runDetail.hop.policyNoBodyTail")}
            </p>
          )}
          {fullPolicy && !canShowBody && (
            <p className="muted">{t("runDetail.hop.noBlob")}</p>
          )}
          {canShowBody && (
            <>
              {hasReqRef && <BlobView title={t("runDetail.hop.reqBody")} state={reqBody} truncated={d.request_body_truncated} policy={policy} />}
              {hasRespRef && <BlobView title={t("runDetail.hop.respBody")} state={respBody} truncated={d.response_body_truncated} policy={policy} />}
            </>
          )}
        </div>
      )}
    </div>
  );
}

type BlobState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ok"; text: string }
  | { kind: "unavailable" }
  | { kind: "error"; message: string };

// 尝试把 blob 文本解析成结构化 JSON（parsed 视图）；失败返回 null（如 SSE 拼接原文）。
function tryPrettyJson(text: string): string | null {
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return null;
  }
}

// 按 policy 区分 parsed / full 展示。
// - parsed 档：blob 是**脱敏后的结构化解析**内容 → 只给「解析视图」（无原文）。
// - full 档：blob 是**协议原文** → 给「解析 / 原文」切换（可解析则默认 pretty JSON，
//   否则退回原文）。
function BlobView({
  title, state, truncated, policy,
}: { title: string; state: BlobState; truncated?: boolean; policy?: string }) {
  const { t } = useI18n();
  const isFull = policy === "full";
  const [mode, setMode] = useState<"parsed" | "raw">("parsed");
  const text = state.kind === "ok" ? state.text : "";
  const pretty = state.kind === "ok" ? tryPrettyJson(text) : null;
  // 展示文本：parsed 模式且可解析 → pretty JSON；否则原文。
  const shown = mode === "parsed" && pretty != null ? pretty : text;

  return (
    <div className="wire-blob">
      <div className="wire-blob-head">
        <span className="wire-blob-title">{title}</span>
        {state.kind === "ok" && (
          <span className="wire-blob-policy muted">
            {policy === "parsed" ? t("runDetail.blob.parsedView") : policy === "full" ? t("runDetail.blob.rawView") : policy}
          </span>
        )}
        {/* full 档才有原文可切；parsed 档只有解析视图。可解析时才显示切换。 */}
        {state.kind === "ok" && isFull && pretty != null && (
          <span className="wire-blob-modes" role="tablist">
            <button role="tab" aria-selected={mode === "parsed"}
              className={mode === "parsed" ? "is-active" : ""}
              onClick={() => setMode("parsed")}>{t("runDetail.blob.parsed")}</button>
            <button role="tab" aria-selected={mode === "raw"}
              className={mode === "raw" ? "is-active" : ""}
              onClick={() => setMode("raw")}>{t("runDetail.blob.raw")}</button>
          </span>
        )}
      </div>
      {/* 截断警告：blob 只是连续前缀，明确提示残缺，不让用户误当完整正文。 */}
      {truncated && (
        <p className="wire-blob-truncated" role="status">
          {t("runDetail.blob.truncated", { title })}
        </p>
      )}
      {state.kind === "loading" && <p className="muted">{t("runDetail.common.loading")}</p>}
      {state.kind === "unavailable" && (
        <p className="muted">{t("runDetail.blob.unavailable")}</p>
      )}
      {state.kind === "error" && <p className="agent-col-error">{t("runDetail.blob.loadFailed", { message: state.message })}</p>}
      {state.kind === "ok" && <pre className="wire-blob-pre">{shown}</pre>}
    </div>
  );
}

function formatObserved(value: number | null | undefined, t: TFn): string {
  return typeof value === "number" ? value.toLocaleString() : t("runDetail.common.unknown");
}

function AggregateUsageCard({ manifest }: { manifest: WireManifest }) {
  const { t } = useI18n();
  const aggregate = manifest.aggregates?.find((item) => item.scope === "attempt");
  if (!aggregate) return null;
  const usage = aggregate.usage;
  return (
    <section className="wire-aggregate-card" aria-label={t("runDetail.aggregate.cardAria")}>
      <div>
        <strong>{t("runDetail.aggregate.title")}</strong>
        <span className="wire-capability-tag">{t("runDetail.aggregate.tag")}</span>
      </div>
      <div className="wire-usage-grid">
        <span>{t("runDetail.aggregate.input")} <b>{formatObserved(usage?.input_tokens, t)}</b></span>
        <span>{t("runDetail.aggregate.output")} <b>{formatObserved(usage?.output_tokens, t)}</b></span>
        <span>{t("runDetail.aggregate.cacheRead")} <b>{formatObserved(usage?.cache_read_tokens, t)}</b></span>
        <span>{t("runDetail.aggregate.cacheWrite")} <b>{formatObserved(usage?.cache_write_tokens, t)}</b></span>
        <span>{t("runDetail.aggregate.reasoning")} <b>{formatObserved(usage?.reasoning_tokens, t)}</b></span>
      </div>
      <p className="muted">
        {t("runDetail.aggregate.note", { event: aggregate.producer_event_type ?? t("runDetail.common.unknown") })}
      </p>
    </section>
  );
}

function UsageConflictPanel({ manifest }: { manifest: WireManifest }) {
  const { t } = useI18n();
  const reconciliation = manifest.aggregates?.find((item) => item.scope === "reconciliation");
  if (!reconciliation?.conflict) return null;
  const adapter = reconciliation.conflict.adapter;
  const native = reconciliation.conflict.native;
  const result = manifest.aggregates?.find((item) => item.scope === "attempt")?.usage;
  const adapterAggregate = manifest.aggregates?.find((item) => item.scope === "adapter");
  const resultAggregate = manifest.aggregates?.find((item) => item.scope === "attempt");
  const row = (label: string, usage: WireUsage | null | undefined, source: string) => (
    <tr key={label}>
      <th>{label}</th>
      <td>{formatObserved(usage?.input_tokens, t)}</td>
      <td>{formatObserved(usage?.output_tokens, t)}</td>
      <td>{source}</td>
    </tr>
  );
  return (
    <details className="wire-conflict">
      <summary>{t("runDetail.conflict.summary")}</summary>
      <div className="wire-table-scroll">
        <table>
          <thead><tr><th>{t("runDetail.conflict.colBasis")}</th><th>{t("runDetail.conflict.colInput")}</th><th>{t("runDetail.conflict.colOutput")}</th><th>field source</th></tr></thead>
          <tbody>
            {row(t("runDetail.conflict.rowNative"), native, "canonical llm_call usage")}
            {row(t("runDetail.conflict.rowProducer"), result, resultAggregate?.producer_event_type ?? t("runDetail.conflict.notProvided"))}
            {row(t("runDetail.conflict.rowAdapter"), adapter, adapterAggregate?.producer_event_type ?? t("runDetail.conflict.notProvided"))}
          </tbody>
        </table>
      </div>
      <p className="muted">{t("runDetail.conflict.note")}</p>
    </details>
  );
}

function CallInspector({
  calls, selectedId, onSelect, trajectoryByCall,
}: {
  calls: WireRecord[];
  selectedId: string | null;
  onSelect: (call: WireRecord) => void;
  trajectoryByCall: Map<string, WireTrajectoryStep[]>;
}) {
  const { t } = useI18n();
  if (calls.length === 0) return null;
  const fmtTime = (value: string | null | undefined) => {
    if (!value) return t("runDetail.common.unknown");
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? value : date.toLocaleTimeString();
  };
  // 耗时优先用 canonical duration_ms；否则从 started/finished 差值派生。都无则"—"。
  const fmtDuration = (time?: WireRecord["time"]) => {
    if (typeof time?.duration_ms === "number") return `${Math.round(time.duration_ms).toLocaleString()}ms`;
    if (time?.started_at && time?.finished_at) {
      const ms = new Date(time.finished_at).getTime() - new Date(time.started_at).getTime();
      if (Number.isFinite(ms) && ms >= 0) return `${ms.toLocaleString()}ms`;
    }
    return "—";
  };
  return (
    <section className="wire-call-inspector" aria-label={t("runDetail.callInspector.aria")}>
      <div className="wire-section-title">{t("runDetail.callInspector.title", { n: calls.length })}</div>
      <div className="wire-table-scroll">
        <table>
          <thead>
            <tr><th>#</th><th>{t("runDetail.callInspector.colTime")}</th><th>{t("runDetail.callInspector.colDuration")}</th><th>{t("runDetail.callInspector.colModel")}</th><th>{t("runDetail.callInspector.colRole")}</th><th>{t("runDetail.callInspector.colInput")}</th><th>{t("runDetail.callInspector.colOutput")}</th>
              <th>{t("runDetail.callInspector.colCacheRead")}</th><th>{t("runDetail.callInspector.colCacheWrite")}</th><th>{t("runDetail.callInspector.colReasoning")}</th><th>{t("runDetail.callInspector.colFinish")}</th><th>phase</th>
              <th>confidence</th><th>{t("runDetail.callInspector.colTrajectory")}</th></tr>
          </thead>
          <tbody>
            {calls.map((call, index) => {
              const usage = call.data?.usage;
              const logicalCallId = call.correlation?.logical_call_id;
              const steps = logicalCallId ? trajectoryByCall.get(logicalCallId) ?? [] : [];
              return (
                <tr key={call.record_id} id={`wire-call-${call.record_id}`}
                  className={selectedId === call.record_id ? "is-selected" : undefined}
                  aria-selected={selectedId === call.record_id} tabIndex={0}
                  onClick={() => onSelect(call)} onFocus={() => onSelect(call)}>
                  <td>{index + 1}</td><td>{fmtTime(call.time?.timestamp)}</td>
                  <td>{fmtDuration(call.time)}</td>
                  <td className="font-mono">{call.data?.model_resolved ?? t("runDetail.common.unknown")}</td>
                  <td>{call.data?.call_role ?? t("runDetail.common.unknown")}</td>
                  <td>{formatObserved(usage?.input_tokens, t)}</td>
                  <td>{formatObserved(usage?.output_tokens, t)}</td>
                  <td>{formatObserved(usage?.cache_read_tokens, t)}</td>
                  <td>{formatObserved(usage?.cache_write_tokens, t)}</td>
                  <td>{formatObserved(usage?.reasoning_tokens, t)}</td>
                  <td>{call.data?.finish_reason ?? t("runDetail.common.unknown")}</td>
                  <td>{call.phase || t("runDetail.common.unknown")}</td>
                  <td>{call.correlation?.confidence ?? t("runDetail.common.unknown")}</td>
                  <td>{steps.length > 0 ? (
                    <a href={`#trajectory-${steps[0].step_id}`}>#{steps[0].sequence ?? "?"}{steps.length > 1 ? ` +${steps.length - 1}` : ""}</a>
                  ) : t("runDetail.callInspector.unmatched")}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function TrajectoryIndex({
  trajectory, callByLogicalId,
}: {
  trajectory: WireTrajectory | null;
  callByLogicalId: Map<string, WireRecord>;
}) {
  const { t } = useI18n();
  if (!trajectory || trajectory.steps.length === 0) return null;
  return (
    <details className="wire-trajectory">
      <summary>{t("runDetail.trajectory.summary", { n: trajectory.steps.length })}</summary>
      <div className="wire-trajectory-list">
        {trajectory.steps.map((step) => {
          const call = step.logical_call_id ? callByLogicalId.get(step.logical_call_id) : undefined;
          // blade skill 业务语义（attributes.skill_id）：tool_name 如实是 Bash，
          // 展示层补上"实际在调哪个业务技能"——对比视图看懂 blade 的关键
          const attrs = step.attributes ?? {};
          const skillId = typeof attrs.skill_id === "string" ? attrs.skill_id : null;
          const skillTool = typeof attrs.skill_tool === "string" ? attrs.skill_tool : null;
          return (
            <div key={step.step_id} id={`trajectory-${step.step_id}`} className="wire-trajectory-row">
              <span>#{step.sequence ?? "?"}</span><span>{step.kind ?? t("runDetail.common.unknown")}</span>
              {step.agent_id && step.agent_id !== "main" && (
                <span className="wire-agent-badge" title={t("runDetail.trajectory.subAgentTitle", { parent: step.parent_agent_id ?? t("runDetail.trajectory.parentUnresolved") })}>
                  {step.agent_id}
                </span>
              )}
              {step.tool_name && <span>{step.tool_name}</span>}
              {skillId && (
                <span className="wire-skill-chip" title={t("runDetail.trajectory.skillTitle")}>
                  → {skillId}{skillTool ? ` · ${skillTool}` : ""}
                </span>
              )}
              {step.tool_call_id && <span className="font-mono">{step.tool_call_id}</span>}
              {call ? <a href={`#wire-call-${call.record_id}`}>{t("runDetail.trajectory.backToCall")}</a> : <span className="muted">{t("runDetail.trajectory.noCall")}</span>}
            </div>
          );
        })}
      </div>
    </details>
  );
}

// 选中调用的详情卡：泳道点击后展开该 logical call 的关键指标 + 归属 hop 的正文。
function SelectedCallCard({
  call, hops, chunksByHop, policy, runId, attemptId, trajectoryByCall, callByLogicalId,
}: {
  call: WireRecord;
  hops: WireRecord[];
  chunksByHop: Map<string, WireRecord[]>;
  policy?: string;
  runId: string;
  attemptId: string;
  trajectoryByCall: Map<string, WireTrajectoryStep[]>;
  callByLogicalId: Map<string, WireRecord>;
}) {
  const { t } = useI18n();
  const lc = call.correlation?.logical_call_id;
  const d = call.data;
  const usage = d.usage;
  // 该 call 归属的 transport hops（同 logical_call_id）。
  const callHops = lc ? hops.filter((h) => h.correlation?.logical_call_id === lc) : [];
  const steps = lc ? trajectoryByCall.get(lc) ?? [] : [];
  // 汇总 TTFT：取该 call 所有 hop 里最早的 TTFT。
  let ttft: number | null = null;
  for (const h of callHops) {
    const hopId = h.correlation?.hop_id ?? h.data?.hop_id;
    const stats = hopId ? streamStatsForHop(chunksByHop.get(hopId) ?? []) : null;
    if (stats?.ttftMs != null) ttft = ttft == null ? stats.ttftMs : Math.min(ttft, stats.ttftMs);
  }
  const dur = typeof call.time?.duration_ms === "number" ? call.time.duration_ms : null;
  const reqSummary = d.request_summary ?? d.request;
  const respSummary = d.response_summary ?? d.response;
  return (
    <section className="wire-call-card" aria-label={t("runDetail.selectedCall.aria")}>
      <div className="wcc-head">
        <span className="wcc-model font-mono">{d.model_resolved ?? t("runDetail.selectedCall.unknownModel")}</span>
        <span className="wcc-role">{d.call_role ?? "—"}</span>
        {d.finish_reason && <span className="wcc-finish">{d.finish_reason}</span>}
      </div>
      <div className="wcc-metrics">
        <div className="wcc-metric"><span className="wcc-mk">TTFT</span><span className="wcc-mv">{ttft != null ? `${Math.round(ttft)}ms` : "—"}</span></div>
        <div className="wcc-metric"><span className="wcc-mk">{t("runDetail.selectedCall.duration")}</span><span className="wcc-mv">{dur != null ? `${Math.round(dur).toLocaleString()}ms` : "—"}</span></div>
        <div className="wcc-metric"><span className="wcc-mk">{t("runDetail.selectedCall.input")}</span><span className="wcc-mv">{formatObserved(usage?.input_tokens, t)}</span></div>
        <div className="wcc-metric"><span className="wcc-mk">{t("runDetail.selectedCall.output")}</span><span className="wcc-mv">{formatObserved(usage?.output_tokens, t)}</span></div>
        <div className="wcc-metric"><span className="wcc-mk">{t("runDetail.selectedCall.cacheRead")}</span><span className="wcc-mv">{formatObserved(usage?.cache_read_tokens, t)}</span></div>
      </div>
      {(reqSummary?.messages_hash || respSummary?.content_hash) && (
        <div className="wcc-hashes">
          {reqSummary?.messages_hash && (
            <span className="font-mono" title={t("runDetail.selectedCall.reqHashTitle")}>
              req·{reqSummary.messages_hash.slice(0, 12)}
            </span>
          )}
          {respSummary?.content_hash && (
            <span className="font-mono" title={t("runDetail.selectedCall.respHashTitle")}>
              resp·{respSummary.content_hash.slice(0, 12)}
            </span>
          )}
        </div>
      )}
      {callHops.length > 0 && (
        <div className="wcc-hops">
          {callHops.map((hop, i) => {
            const hopId = hop.correlation?.hop_id ?? hop.data?.hop_id;
            const stats = hopId ? streamStatsForHop(chunksByHop.get(hopId) ?? []) : null;
            const matchedCall = lc ? callByLogicalId.get(lc) : undefined;
            return <HopBody key={`${hop.record_id}-${i}`} runId={runId} attemptId={attemptId}
              hop={hop} policy={policy} trajectorySteps={steps}
              matchedCallId={matchedCall?.record_id} streamStats={stats} />;
          })}
        </div>
      )}
      {callHops.length === 0 && (
        <p className="muted">{t("runDetail.selectedCall.noHop")}</p>
      )}
    </section>
  );
}

// ── 时间轴泳道图（signature）───────────────────────────────────────────────
// wire 数据本质是一段 wall-clock 时间序列：每个 logical call 一条泳道，call 主条 =
// LLM 思考的耗时，下方 transport/mcp 轨道钉在各自的真实时刻。让"agent 先想什么、
// 调了哪个工具、每步多久、TTFT 多快"一眼可读，而不是散落在几张表里。

type TimelineTrack = "call" | "http" | "mcp";
type TimelineItem = {
  rec: WireRecord;
  track: TimelineTrack;
  startMs: number;         // 相对 attempt 起始
  endMs: number;
  ttftMs: number | null;   // 仅 call/http：首 chunk 相对该条起始的偏移
  label: string;
  error: boolean;
};
type TimelineLane = {
  callId: string;          // logical_call_id 或合成键（_probe/_mcp/_hop:*）
  seq: number;
  label?: string;          // 泳道显示名（模型名 / 端点 / "MCP 工具" / "启动探测"）
  items: TimelineItem[];   // 该 lane 内的 call/http/mcp 条目
  startMs: number;
  endMs: number;
};
type TimelineModel = {
  lanes: TimelineLane[];
  spanMs: number;          // 总时间跨度
  t0: number;              // 起始 epoch ms
};

// 时间戳 → epoch ms（无效返回 null）。
function epochMs(ts?: string | null): number | null {
  if (!ts) return null;
  const v = new Date(ts).getTime();
  return Number.isNaN(v) ? null : v;
}

// 从已加载的 records 派生泳道模型。纯函数，便于单测。
function buildTimeline(
  calls: WireRecord[], hops: WireRecord[], frames: WireRecord[],
  chunksByHop: Map<string, WireRecord[]>, t: TFn,
): TimelineModel | null {
  const all = [...calls, ...hops, ...frames];
  const stamps = all.map((r) => epochMs(r.time?.timestamp)).filter((v): v is number => v != null);
  if (stamps.length === 0) return null;
  const t0 = Math.min(...stamps);
  const tEnd = Math.max(...stamps);
  const spanMs = Math.max(tEnd - t0, 1);

  // 泳道归组（按语义，不是每个 record 一条）：
  // - 每次 provider 调用（有 logical_call_id 的 http/call）→ 自己一条 lane（真实调用边界）；
  // - codex 启动探测 GET /models（无 stream、非业务）→ 合并到一条"启动探测"lane；
  // - 未关联的 MCP 帧（lc=None）→ 全部收进一条共享"MCP 工具"lane，各帧一个菱形，
  //   不再每帧一行——它们本就没有 call 边界可分。
  const PROBE_KEY = "_probe";
  const MCP_KEY = "_mcp";
  const laneMap = new Map<string, TimelineLane>();
  const laneMeta = new Map<string, { label: string }>();
  const ensureLane = (key: string, label: string): TimelineLane => {
    let lane = laneMap.get(key);
    if (!lane) {
      lane = { callId: key, seq: 0, items: [], startMs: spanMs, endMs: 0 };
      laneMap.set(key, lane);
      laneMeta.set(key, { label });
    }
    return lane;
  };

  const isProbe = (r: WireRecord) => /\/models\b/.test(r.data?.path ?? "");

  const pushItem = (
    r: WireRecord, track: TimelineTrack, laneK: string, laneLabel: string,
    itemLabel: string, error: boolean,
  ) => {
    const start = epochMs(r.time?.timestamp);
    if (start == null) return;
    const startMs = start - t0;
    const dur = typeof r.time?.duration_ms === "number" ? r.time.duration_ms : 0;
    const endMs = startMs + Math.max(dur, 0);
    let ttftMs: number | null = null;
    const hopId = r.correlation?.hop_id ?? r.data?.hop_id ?? null;
    if (hopId) {
      const chs = chunksByHop.get(hopId);
      const rels = (chs ?? []).map((c) => c.data?.relative_ms).filter((v): v is number => typeof v === "number");
      if (rels.length) ttftMs = Math.min(...rels);
    }
    const lane = ensureLane(laneK, laneLabel);
    lane.items.push({ rec: r, track, startMs, endMs, ttftMs, label: itemLabel, error });
    lane.startMs = Math.min(lane.startMs, startMs);
    lane.endMs = Math.max(lane.endMs, endMs || startMs);
  };

  for (const c of calls) {
    const lc = c.correlation?.logical_call_id ?? `_call:${c.record_id}`;
    const model = c.data?.model_resolved ?? t("runDetail.timeline.callFallback");
    pushItem(c, "call", lc, model, model, c.data?.finish_reason === "error");
  }
  for (const h of hops) {
    const d = h.data;
    const label = `${d.method ?? ""} ${d.path ?? ""}`.trim() || "hop";
    const err = typeof d.status_code === "number" && d.status_code >= 400;
    if (isProbe(h)) { pushItem(h, "http", PROBE_KEY, t("runDetail.timeline.probeLane"), label, err); continue; }
    const lc = h.correlation?.logical_call_id ?? `_hop:${h.record_id}`;
    pushItem(h, "http", lc, label, label, err);
  }
  for (const f of frames) {
    const d = f.data;
    if (d.direction === "server-to-client" && d.message_kind === "response") continue;
    const itemLabel = d.tool_name ?? d.method ?? "mcp";
    // 有 lc 的挂到对应调用 lane；无 lc（codex 现状）收进共享 MCP lane。
    const lc = f.correlation?.logical_call_id;
    if (lc) pushItem(f, "mcp", lc, itemLabel, itemLabel, Boolean(d.is_error));
    // 关联上的工具帧挂到对应调用 lane（见上 if lc）；剩下未关联的多是协议握手帧
    // （initialize/tools/list，无 tool_name），归"MCP 协议"共享 lane，不叫"工具"以免
    // 误导——真正的工具调用已经挂到 provider 调用泳道了。
    else pushItem(f, "mcp", MCP_KEY, t("runDetail.timeline.mcpProtocolLane"), itemLabel, Boolean(d.is_error));
  }

  const lanes = [...laneMap.values()]
    .filter((l) => l.items.length > 0)
    .sort((a, b) => a.startMs - b.startMs)
    .map((l, i) => ({ ...l, seq: i + 1, label: laneMeta.get(l.callId)?.label }));
  return { lanes, spanMs, t0 };
}

// 毫秒 → 紧凑刻度标签。
function fmtAxisMs(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)}s`;
}

function WireTimeline({
  model, selectedCallId, onSelect,
}: {
  model: TimelineModel;
  selectedCallId: string | null;
  onSelect: (rec: WireRecord) => void;
}) {
  const { t } = useI18n();
  const { lanes, spanMs } = model;
  const pct = (ms: number) => `${(ms / spanMs) * 100}%`;
  // 时间刻度：4 等分。
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => f * spanMs);
  const trackColor: Record<TimelineTrack, string> = {
    call: "var(--accent)", http: "var(--blue)", mcp: "var(--green)",
  };
  return (
    <section className="wtl" aria-label={t("runDetail.timeline.aria")}>
      {/* 横向可滚：泳道多 / 跨度长时给轨道区一个最小宽度，避免挤压裁切；
          label 列在内部随内容一起滚，短事件（TTFT/菱形）也有像素空间可辨。 */}
      <div className="wtl-scroll">
       <div className="wtl-grid">
      <div className="wtl-axis">
        {ticks.map((tick, i) => (
          <span key={i} className="wtl-tick" style={{ left: pct(tick) }}>{fmtAxisMs(tick)}</span>
        ))}
      </div>
      <div className="wtl-lanes">
        {lanes.map((lane) => {
          // lane 的代表 record：优先 call，其次首个 item——点 label 时联动它。
          const anchorItem = lane.items.find((it) => it.track === "call") ?? lane.items[0];
          const key = anchorItem?.rec.correlation?.logical_call_id ?? lane.callId;
          const active = selectedCallId != null && key === selectedCallId;
          const laneLabel = lane.label ?? anchorItem?.label ?? "—";
          return (
            <div key={lane.callId} className={`wtl-lane${active ? " is-active" : ""}`}>
              <button className="wtl-lane-label" onClick={() => anchorItem && onSelect(anchorItem.rec)}
                title={laneLabel}>
                <span className="wtl-seq">{lane.seq}</span>
                <span className="wtl-lane-model">{laneLabel}</span>
              </button>
              <div className="wtl-track-area">
                {lane.items.map((it, i) => {
                  const left = pct(it.startMs);
                  // 0 时长的 hop（native-only、瞬时工具请求）仍要可见可点：给最小宽度。
                  const width = it.track === "mcp"
                    ? undefined
                    : `max(6px, ${((it.endMs - it.startMs) / spanMs) * 100}%)`;
                  if (it.track === "mcp") {
                    return (
                      <button key={i} className={`wtl-mark wtl-mark-mcp${it.error ? " is-err" : ""}`}
                        style={{ left, background: trackColor.mcp }}
                        title={`${it.label} @ ${fmtAxisMs(it.startMs)}`}
                        onClick={() => onSelect(it.rec)} />
                    );
                  }
                  return (
                    <button key={i}
                      className={`wtl-bar wtl-bar-${it.track}${it.error ? " is-err" : ""}`}
                      style={{ left, width, background: trackColor[it.track] }}
                      title={`${it.label} · ${fmtAxisMs(it.endMs - it.startMs)}${it.ttftMs != null ? ` · TTFT ${Math.round(it.ttftMs)}ms` : ""}`}
                      onClick={() => onSelect(it.rec)}>
                      {it.ttftMs != null && it.endMs > it.startMs && (
                        <span className="wtl-ttft" style={{ left: `${Math.min((it.ttftMs / Math.max(it.endMs - it.startMs, 1)) * 100, 100)}%` }} />
                      )}
                    </button>
                  );
                })}
              </div>
            </div>
          );
        })}
      </div>
       </div>
      </div>
      <div className="wtl-legend">
        <span><i className="wtl-key" style={{ background: "var(--accent)" }} />{t("runDetail.timeline.legendLlm")}</span>
        <span><i className="wtl-key" style={{ background: "var(--blue)" }} />{t("runDetail.timeline.legendTransport")}</span>
        <span><i className="wtl-key" style={{ background: "var(--green)" }} />{t("runDetail.timeline.legendMcp")}</span>
        <span><i className="wtl-key wtl-key-ttft" />{t("runDetail.timeline.legendTtft")}</span>
      </div>
    </section>
  );
}

export function WirePanel({ runId, attemptId, label }: { runId: string; attemptId: string; label: string }) {
  const { t } = useI18n();
  const [manifest, setManifest] = useState<WireManifest | null>(null);
  const [calls, setCalls] = useState<WireRecord[]>([]);
  const [hops, setHops] = useState<WireRecord[]>([]);
  // 流式 chunk（TTFT/流节奏）+ MCP 帧（工具调用）——后端已产出，之前前端从不加载。
  const [chunks, setChunks] = useState<WireRecord[]>([]);
  const [frames, setFrames] = useState<WireRecord[]>([]);
  const [trajectory, setTrajectory] = useState<WireTrajectory | null>(null);
  const [selectedCallId, setSelectedCallId] = useState<string | null>(null);
  const [truncCalls, setTruncCalls] = useState(false);
  const [truncHops, setTruncHops] = useState(false);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setLoading(true); setErr(null);
    // 按需加载：manifest（coverage）+ 全部 llm_call + http_exchange（自动翻页，
    // 不能只取第一页静默截断；unmatched 也覆盖 http_exchange）。
    // 翻完所有页；触到封顶（40 页/20000 条）时返回 truncated 标记，
    // 而非静默截断——不能声称与 canonical 精确一致。
    const loadAll = async (recordType: string): Promise<{ items: WireRecord[]; truncated: boolean }> => {
      const out: WireRecord[] = [];
      let cursor: string | undefined;
      const MAX_PAGES = 40;
      for (let guard = 0; guard < MAX_PAGES; guard++) {
        const page = await api.getWire(runId, attemptId, { record_type: recordType, limit: 500, cursor });
        out.push(...page.items);
        if (!page.next_cursor) return { items: out, truncated: false };
        cursor = page.next_cursor;
      }
      return { items: out, truncated: true };  // 仍有 next_cursor 未取
    };
    (async () => {
      const m = await api.getWireManifest(runId, attemptId);
      if (!alive) return;
      setManifest(m);
      // trajectory 是**可选**增强：独立加载并独立降级——它的 404/500
      // 绝不能拖垮整个通信面板（曲线/hop 是核心，trajectory 只是联动锚点）。
      // 因此不放进下面 llm/http 的 Promise.all，用各自的 catch → null。
      const trajP = api.getWireTrajectory(runId, attemptId).catch(() => null);
      // stream_chunk / mcp_frame 是**可选**增强：单独 catch 降级，其 404/500 不拖垮
      // 核心的 llm/http 面板（同 trajectory 的处理）。
      const chunkP = loadAll("stream_chunk").catch(() => ({ items: [] as WireRecord[], truncated: false }));
      const frameP = loadAll("mcp_frame").catch(() => ({ items: [] as WireRecord[], truncated: false }));
      const [llm, http] = await Promise.all([
        loadAll("llm_call"), loadAll("http_exchange"),
      ]);
      if (!alive) return;
      const byTs = (a: WireRecord, b: WireRecord) =>
        (a.time?.timestamp ?? "").localeCompare(b.time?.timestamp ?? "");
      setCalls([...llm.items].sort(byTs));
      setHops([...http.items].sort(byTs));
      const [ch, fr] = await Promise.all([chunkP, frameP]);
      if (!alive) return;
      // chunk 按 hop + sequence 排（同 hop 内 sequence 才有序）；frame 按时间戳。
      setChunks([...ch.items].sort((a, b) =>
        ((a.correlation?.hop_id ?? "").localeCompare(b.correlation?.hop_id ?? "")) ||
        ((a.data?.sequence ?? 0) - (b.data?.sequence ?? 0))));
      setFrames([...fr.items].sort(byTs));
      setSelectedCallId(null);
      // 分别记 call/http 截断，不合成一个 bool——仅 HTTP 截断时也能
      // 正确归因。
      setTruncCalls(llm.truncated);
      setTruncHops(http.truncated);
      // trajectory 失败 → null，面板照常展示曲线/hop，只是没有 trajectory 联动。
      const traj = await trajP;
      if (alive) setTrajectory(traj);
    })().catch((e) => { if (alive) setErr(String(e)); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [runId, attemptId]);

  // 视图状态从共享 deriveWireView 推导（web/src/wire/curve.ts，有 vitest 固定
  // fixture 覆盖空态/降级/截断归因/aggregate-only）——UI 与测试跑同一逻辑。
  const view = deriveWireView(manifest, calls, hops, truncCalls, truncHops);
  const st = manifest?.status;
  const unmatched = manifest?.coverage?.unmatched_calls ?? 0;
  const { matched, unmatched: unmatchedCalls } = splitMatched(calls, hops);
  const hasAggregateOnlySource = view.kind === "available" && view.aggregateOnly;
  // 逐调用 usage 是否全为 0/缺失：若是且存在 attempt 聚合，则把聚合卡也显示出来，
  // 让真实 token 总量不至于被"0 曲线"掩盖（CC 经 OpenRouter 的典型情况）。
  const perCallUsageAllZero = matched.length > 0 && matched.every((c) => {
    const u = c.data?.usage;
    return !u || ((u.input_tokens ?? 0) === 0 && (u.output_tokens ?? 0) === 0);
  }) && Boolean(manifest?.aggregates?.find((a) => a.scope === "attempt")?.usage);
  const trajectoryByCall = new Map<string, WireTrajectoryStep[]>();
  for (const step of trajectory?.steps ?? []) {
    if (!step.logical_call_id) continue;
    const current = trajectoryByCall.get(step.logical_call_id) ?? [];
    current.push(step);
    trajectoryByCall.set(step.logical_call_id, current);
  }
  const callByLogicalId = new Map<string, WireRecord>();
  for (const call of matched) {
    if (call.correlation?.logical_call_id)
      callByLogicalId.set(call.correlation.logical_call_id, call);
  }
  // 按 hop_id 聚合 stream_chunk，供每个 hop 派生 TTFT/流统计。
  const chunksByHop = new Map<string, WireRecord[]>();
  for (const c of chunks) {
    const hop = c.correlation?.hop_id;
    if (!hop) continue;
    const cur = chunksByHop.get(hop) ?? [];
    cur.push(c);
    chunksByHop.set(hop, cur);
  }
  // 概览统计：LLM 传输 hop 数（provider 调用，codex 无 native call 时的"调用"口径）
  // 与 MCP 工具调用数（tools/call request 帧）。models 探测/notification 不算工具调用。
  const llmHopCount = hops.filter((h) => {
    const p = h.data?.path ?? "";
    return /\/(responses|messages|chat\/completions)/.test(p);
  }).length;
  const mcpToolCount = frames.filter((f) =>
    f.data?.message_kind === "request" && f.data?.tool_name).length;
  // 时间轴泳道模型（signature 可视化）。
  const timeline = buildTimeline(calls, hops, frames, chunksByHop, t);
  // 选中调用（供泳道点击联动详情卡）。用 logical_call_id 作联动键。
  const selectedCall = matched.find((c) => c.correlation?.logical_call_id === selectedCallId)
    ?? matched.find((c) => c.record_id === selectedCallId);
  // 泳道点击 → 若点的是 http/mcp，回溯它归属的 logical call 选中。
  const selectFromTimeline = (rec: WireRecord) => {
    const lc = rec.correlation?.logical_call_id;
    if (lc) { setSelectedCallId(lc); return; }
    setSelectedCallId(rec.record_id);
  };
  const selectCall = (call: WireRecord) => {
    setSelectedCallId(call.record_id);
    requestAnimationFrame(() => {
      document.getElementById(`wire-call-${call.record_id}`)?.scrollIntoView?.({ block: "nearest" });
    });
  };
  // 结构化 gap → 中文标签（保留 source/field/failureReason 明细）。
  const gapLabel = (g: WireGap): string => {
    if (g.reason === "phase_degraded") return t("runDetail.gapLabel.phaseDegraded");
    if (g.reason === "conflicts") return t("runDetail.gapLabel.conflicts", { n: g.count ?? "" });
    if (g.reason === "trunc_calls") return t("runDetail.gapLabel.truncCalls");
    if (g.reason === "trunc_hops") return t("runDetail.gapLabel.truncHops");
    if (g.reason === "source") {
      // 哪个采集器、什么状态、为什么失败
      const why = g.failureReason ? `（${g.failureReason}）` : "";
      return `${g.source} · ${g.status}${why}`;
    }
    // manifest gap：reason 标签 + 具体 field
    const base = gapReasonLabel(t, g.reason);
    return g.field && g.field !== g.reason ? `${base}（${g.field}）` : base;
  };

  return (
    <div className="wire-panel">
      <div className="wire-panel-head">
        <span className="wire-panel-agent">{label}</span>
        {st && <WireBadge att={{ wire_status: st } as AttemptSummary} />}
      </div>
      {loading && <p className="muted">{t("runDetail.wirePanel.loading")}</p>}
      {err && <p className="agent-col-error">{t("runDetail.wirePanel.loadFailed", { err })}</p>}
      {!loading && !err && view.kind === "not_available" && (
        <p className="muted">{t("runDetail.wirePanel.notAvailable")}</p>
      )}
      {!loading && !err && view.kind === "available" && (
        <>
          {/* 概览条：把 attempt 的通信一句话讲清。"调用"优先用 native llm_call 数；
              codex 无 native call（anchor 缺失）时回落到 LLM 传输 hop 数（/responses
              等 provider 调用），避免展示"0 调用"误导——它明明打了 provider。 */}
          <div className="wire-overview">
            {matched.length > 0 ? (
              <span className="wire-ov-stat"><b>{matched.length}</b> {t("runDetail.wirePanel.calls")}</span>
            ) : llmHopCount > 0 ? (
              <span className="wire-ov-stat"><b>{llmHopCount}</b> {t("runDetail.wirePanel.providerCalls")}<span className="muted">{t("runDetail.wirePanel.noNativeEvents")}</span></span>
            ) : (
              <span className="wire-ov-stat muted">{t("runDetail.wirePanel.noLlmCalls")}</span>
            )}
            {mcpToolCount > 0 && <span className="wire-ov-stat"><b>{mcpToolCount}</b> {t("runDetail.wirePanel.toolCalls")}</span>}
            {timeline && <span className="wire-ov-stat"><b>{fmtAxisMs(timeline.spanMs)}</b> {t("runDetail.wirePanel.span")}</span>}
            {manifest?.policy && <span className="wire-ov-stat"><b>{manifest.policy.effective}</b> {t("runDetail.wirePanel.capture")}</span>}
            {manifest?.totals && <span className="wire-ov-stat muted">{manifest.totals.hops ?? 0} hop · {manifest.totals.blobs ?? 0} blob</span>}
          </div>

          {/* signature：时间轴泳道图——agent 行为的主视图。点泳道联动下方详情卡。 */}
          {timeline && timeline.lanes.length > 0 && (
            <WireTimeline model={timeline} selectedCallId={selectedCallId} onSelect={selectFromTimeline} />
          )}

          {/* 选中调用详情：泳道点击后在此展开该 call 的模型/耗时/token/hash/正文。 */}
          {selectedCall && (
            <SelectedCallCard
              call={selectedCall} hops={hops} chunksByHop={chunksByHop}
              policy={manifest?.policy?.effective} runId={runId} attemptId={attemptId}
              trajectoryByCall={trajectoryByCall} callByLogicalId={callByLogicalId} />
          )}

          {/* completeness banner：所有降级原因来自 deriveWireView.gaps（
              UI 与测试共享同一推导，不再各自实现）。 */}
          <div className="wire-banner">
            <span>{t("runDetail.wirePanel.statusLabel")}<b>{st ? wireStatusLabel(t, st) : st}</b></span>
            {manifest?.policy && <span>{t("runDetail.wirePanel.policyLabel", { policy: manifest.policy.effective ?? "" })}
              {manifest.policy.downgrade_reason ? t("runDetail.wirePanel.downgrade", { reason: manifest.policy.downgrade_reason }) : ""}</span>}
            {manifest?.coverage && <span>{t("runDetail.wirePanel.callCoverage", { correlated: manifest.coverage.correlated_calls ?? 0, unmatched })}</span>}
            {manifest?.totals && (
              <span className="muted" title={t("runDetail.wirePanel.totalsTitle")}>
                {manifest.totals.hops ?? 0} hop · {manifest.totals.blobs ?? 0} blob
                {typeof manifest.totals.bytes === "number" ? ` · ${fmtBytes(manifest.totals.bytes)}` : ""}
              </span>
            )}
            {view.gaps.map((g, i) => (
              <span key={"g" + i} className="wire-gap">{gapLabel(g)}</span>
            ))}
          </div>

          {/* coverage 四轴：每轴一个状态徽章，让"哪类通信观测到了/
              没观测到/不适用"一目了然，而不是只有一个总状态词。 */}
          {manifest?.coverage && (
            <div className="wire-coverage-axes" aria-label={t("runDetail.wirePanel.coverageAxesAria")}>
              {([
                [t("runDetail.wirePanel.axisAgentSemantics"), manifest.coverage.agent_semantics],
                [t("runDetail.wirePanel.axisLlmTransport"), manifest.coverage.llm_transport],
                [t("runDetail.wirePanel.axisMcp"), manifest.coverage.mcp],
                [t("runDetail.wirePanel.axisBladeHop"), manifest.coverage.blade_hops],
              ] as const).map(([label, status]) => (
                <span key={label} className={`wire-axis wire-axis-${axisClass(status)}`}
                  title={t("runDetail.wirePanel.axisTitle", { label, status: status ?? t("runDetail.common.unknown") })}>
                  {label} · {axisLabel(t, status)}
                </span>
              ))}
            </div>
          )}

          {/* source 丢包/解析错误明细：partial 时告诉用户丢了多少，不只给状态词。 */}
          {manifest?.sources?.some((s) => (s.dropped ?? 0) > 0 || (s.parse_errors ?? 0) > 0 || (s.errors ?? 0) > 0) && (
            <div className="wire-source-errors" aria-label={t("runDetail.wirePanel.sourceErrorsAria")}>
              {manifest.sources
                .filter((s) => (s.dropped ?? 0) > 0 || (s.parse_errors ?? 0) > 0 || (s.errors ?? 0) > 0)
                .map((s) => (
                  <span key={`${s.kind}@${s.instance}`} className="wire-gap">
                    {s.kind}/{s.instance}：
                    {(s.dropped ?? 0) > 0 ? t("runDetail.wirePanel.srcDropped", { n: s.dropped ?? 0 }) : ""}
                    {(s.parse_errors ?? 0) > 0 ? t("runDetail.wirePanel.srcParseErr", { n: s.parse_errors ?? 0 }) : ""}
                    {(s.errors ?? 0) > 0 ? t("runDetail.wirePanel.srcErr", { n: s.errors ?? 0 }) : ""}
                    {s.truncated_tail ? t("runDetail.wirePanel.srcTailTrunc") : ""}
                  </span>
                ))}
            </div>
          )}

          {/* 上下文压缩事件：agent 主动/被动 compact 上下文的时刻——分析
              长任务上下文管理的关键信号。空则不渲染。 */}
          {manifest?.compaction_hints && manifest.compaction_hints.length > 0 && (
            <section className="wire-compaction" aria-label={t("runDetail.wirePanel.compactionAria")}>
              <div className="wire-section-title">{t("runDetail.wirePanel.compactionTitle", { n: manifest.compaction_hints.length })}</div>
              <div className="wire-table-scroll">
                <table>
                  <thead><tr><th>#</th><th>{t("runDetail.wirePanel.compactCol1")}</th><th>{t("runDetail.wirePanel.compactCol2")}</th><th>{t("runDetail.wirePanel.compactCol3")}</th><th>{t("runDetail.wirePanel.compactCol4")}</th></tr></thead>
                  <tbody>
                    {manifest.compaction_hints.map((c, i) => (
                      <tr key={i}>
                        <td>{i + 1}</td>
                        <td>{c.trigger ?? t("runDetail.common.unknown")}</td>
                        <td>{formatObserved(c.before_tokens, t)}</td>
                        <td>{formatObserved(c.after_tokens, t)}</td>
                        <td>{c.at ? new Date(c.at).toLocaleTimeString() : "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}

          {/* 累计用量卡：aggregate-only source 固然要展示；但当逐调用 usage 全为 0
              （如 CC 经 OpenRouter，provider 不回逐条 usage，只有 result 聚合有真实
              token）时也必须展示——否则用户只看到 0 token 曲线，误以为没消耗，
              而真实总量其实在这张卡里。 */}
          {manifest && (hasAggregateOnlySource || perCallUsageAllZero) &&
            <AggregateUsageCard manifest={manifest} />}
          {manifest && <UsageConflictPanel manifest={manifest} />}

          {/* MCP 工具帧：独立分析视图（哪次调用用了哪个工具），保持常驻可见。 */}
          <McpFramesSection frames={frames} />

          {/* 详细数据表：时间轴 + 详情卡是主视图，逐调用表/token 曲线/全部 hop
              作为详细数据按需展开——不占主视线，但一键可查。 */}
          {(matched.length > 0 || hops.length > 0) && (
            <details className="wire-details-fold">
              <summary>{t("runDetail.wirePanel.detailsFold", { calls: matched.length, hops: hops.length })}</summary>
              {matched.length > 0 && (
                <>
                  <div className="wire-curve-title">
                    {t("runDetail.wirePanel.curveTitle", { n: matched.length })}
                    {truncCalls && <span className="wire-gap">{t("runDetail.wirePanel.curveTruncated", { n: matched.length })}</span>}
                  </div>
                  <TokenCurve calls={matched} selectedId={selectedCallId} onSelect={selectCall} />
                  <CallInspector calls={matched} selectedId={selectedCallId} onSelect={selectCall}
                    trajectoryByCall={trajectoryByCall} />
                </>
              )}
              {hops.length > 0 && (
                <section className="wire-hop-timeline" aria-label={t("runDetail.wirePanel.allHopsAria")}>
                  <div className="wire-section-title">{t("runDetail.wirePanel.allHopsTitle", { n: hops.length })}</div>
                  {hops.map((hop, hopIndex) => {
                    const logicalCallId = hop.correlation?.logical_call_id;
                    const call = logicalCallId ? callByLogicalId.get(logicalCallId) : undefined;
                    const steps = logicalCallId ? trajectoryByCall.get(logicalCallId) ?? [] : [];
                    const hopId = hop.correlation?.hop_id ?? hop.data?.hop_id;
                    const stats = hopId ? streamStatsForHop(chunksByHop.get(hopId) ?? []) : null;
                    return <HopBody key={`${hop.record_id}-${hopIndex}`} runId={runId} attemptId={attemptId}
                      hop={hop} policy={manifest?.policy?.effective} trajectorySteps={steps}
                      matchedCallId={call?.record_id} streamStats={stats} />;
                  })}
                  {hops.some((hop) => hop.correlation?.confidence === "unmatched") && (
                    <p className="muted wire-hop-note">
                      {t("runDetail.wirePanel.unmatchedHopNote")}
                    </p>
                  )}
                </section>
              )}
            </details>
          )}

          <TrajectoryIndex trajectory={trajectory} callByLogicalId={callByLogicalId} />

          {unmatchedCalls.some((item) => item.record_type === "llm_call") && (
            <div className="wire-unmatched">
              <div className="wire-unmatched-head">{t("runDetail.wirePanel.unmatchedHead")}</div>
              {unmatchedCalls.filter((item) => item.record_type === "llm_call").map((c) => {
                // llm_call：model + usage 单行。
                const inTok = c.data?.usage?.input_tokens;
                const outTok = c.data?.usage?.output_tokens;
                return (
                  <div className="wire-unmatched-row" key={c.record_id}>
                    <span className="font-mono">{c.data?.model_resolved ?? "?"}</span>
                    <span>{t("runDetail.wirePanel.unmatchedRow", { input: typeof inTok === "number" ? inTok.toLocaleString() : t("runDetail.common.unknown"), output: typeof outTok === "number" ? outTok.toLocaleString() : t("runDetail.common.unknown") })}</span>
                  </div>
                );
              })}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function ScoreBar({ score, max = 100 }: { score: number | null; max?: number }) {
  if (score == null) return <span className="muted">-</span>;
  const pct = Math.min(100, (score / max) * 100);
  const color = pct >= 80 ? "var(--green)" : pct >= 50 ? "var(--yellow)" : "var(--red)";
  return (
    <div className="score-bar-wrap">
      <div className="score-bar-track"><div className="score-bar-fill" style={{ width: `${pct}%`, background: color }} /></div>
      <span className="score-bar-val">{score}</span>
    </div>
  );
}

function scoreColor(score: number | null | undefined): string {
  const value = score ?? 0;
  return value >= 80 ? "var(--green)" : value >= 50 ? "var(--yellow)" : "var(--red)";
}

// 执行场合标签：仅展示不计分。沙盒 vs 宿主机+bypass 一目了然，但不影响任何分数。
function LocusTag({ locus, permissionMode, meta }: { locus: string | null; permissionMode: string | null; meta?: Record<string, unknown> }) {
  const { t } = useI18n();
  const sandboxed = locus === "docker-sandbox";
  // blade 沙盒由 blade server 管理、session 间共享，不与本方案的 per-attempt 沙盒同等看待。
  const shared = Boolean(meta?.sandbox_shared);
  const bypass = permissionMode?.includes("dangerously") ?? false;
  const label = sandboxed
    ? (shared ? "🟡 sandbox (shared)" : "🟢 sandbox")
    : locus === "remote-host"
      ? `🟠 remote${bypass ? "+bypass" : ""}`
      : `🔴 host${bypass ? "+bypass" : ""}`;
  const extra = [
    meta?.sandbox_image ? `image: ${String(meta.sandbox_image)}` : null,
    meta?.agent_version ? `agent: ${String(meta.agent_version)}` : null,
    meta?.egress_policy ? `egress: ${String(meta.egress_policy)}` : null,
    meta?.server_side_network ? `server-side tools: ${String(meta.server_side_network)}` : null,
    meta?.sandbox_managed_by ? `managed by: ${String(meta.sandbox_managed_by)}` : null,
  ].filter(Boolean).join(" · ");
  const title = t("runDetail.locus.title", { locus: locus ?? "unknown", mode: permissionMode ?? "-" }) + (extra ? `\n${extra}` : "");
  return <span className="locus-tag" title={title}>{label}</span>;
}

const SEVERITY_COLOR: Record<string, string> = {
  critical: "var(--red)", high: "var(--red)", medium: "var(--yellow)", low: "var(--text-2)",
};
// 值存 i18n messageKey，渲染处用 t() 取文案。
const SEVERITY_LABELS: Record<string, string> = {
  critical: "runDetail.severity.critical", high: "runDetail.severity.high",
  medium: "runDetail.severity.medium", low: "runDetail.severity.low",
};
// 危险类别：业务层来自 env meta 的 danger_tools，系统层来自 security/rules.yaml
const SEC_CATEGORY_LABELS: Record<string, string> = {
  "high-risk-action": "runDetail.secCat.highRiskAction", "irreversible-action": "runDetail.secCat.irreversibleAction",
  "destructive-fs": "runDetail.secCat.destructiveFs", "network-config": "runDetail.secCat.networkConfig",
  "privilege-escalation": "runDetail.secCat.privilegeEscalation", "process-control": "runDetail.secCat.processControl",
  "env-mutation": "runDetail.secCat.envMutation", "credential-access": "runDetail.secCat.credentialAccess",
  "data-egress": "runDetail.secCat.dataEgress", "guardrail-bypass": "runDetail.secCat.guardrailBypass",
};
// HITL 处置：agent 面对危险操作的行为（见 backend/security/hitl.py）；label/title 存 messageKey
const HITL_LABELS: Record<string, { labelKey: string; good: boolean; titleKey: string }> = {
  "sought-approval": { labelKey: "runDetail.hitl.soughtApproval.label", good: true, titleKey: "runDetail.hitl.soughtApproval.title" },
  "auto-executed": { labelKey: "runDetail.hitl.autoExecuted.label", good: false, titleKey: "runDetail.hitl.autoExecuted.title" },
  "aborted-on-denial": { labelKey: "runDetail.hitl.abortedOnDenial.label", good: true, titleKey: "runDetail.hitl.abortedOnDenial.title" },
  "overrode-denial": { labelKey: "runDetail.hitl.overrodeDenial.label", good: false, titleKey: "runDetail.hitl.overrodeDenial.title" },
  "not-reached": { labelKey: "runDetail.hitl.notReached.label", good: true, titleKey: "runDetail.hitl.notReached.title" },
};

function SecuritySummaryRow({ security, runId, attemptId }: { security: AttemptSecurity; runId: string; attemptId: string }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [evts, setEvts] = useState<SecurityEvent[] | null>(null);
  const rate = security.hitl?.auto_exec_rate;
  const reached = security.hitl?.decision_points_reached ?? 0;
  const hasEvents = security.event_count > 0;

  const toggle = useCallback(async () => {
    const next = !open;
    setOpen(next);
    if (next && evts === null) {
      try { setEvts(await api.getSecurityEvents(runId, attemptId)); }
      catch { setEvts([]); }
    }
  }, [open, evts, runId, attemptId]);

  return (
    <div className="security-summary">
      <div className="security-summary-line">
        <LocusTag locus={security.execution_locus} permissionMode={security.permission_mode} meta={security.meta} />
        {hasEvents ? (
          <span className="clickable" onClick={toggle} style={{ color: SEVERITY_COLOR[security.max_severity ?? "low"] }}>
            ⚠ {t("runDetail.security.dangerCount", { n: security.event_count, max: security.max_severity ? t("runDetail.security.maxSeverity", { sev: SEVERITY_LABELS[security.max_severity] ? t(SEVERITY_LABELS[security.max_severity]) : security.max_severity }) : "" })} {open ? "▾" : "▸"}
          </span>
        ) : (
          <span className="muted">{t("runDetail.security.noDanger")}</span>
        )}
        {reached > 0 && (
          <span
            title={t("runDetail.security.autoExecTitle", { reached, pct: Math.round((rate ?? 0) * 100) })}
            style={{ color: (rate ?? 0) > 0 ? "var(--red)" : "var(--green)" }}
          >
            {t("runDetail.security.autoExecRate", { pct: Math.round((rate ?? 0) * 100) })}
          </span>
        )}
        {security.reaction && <span className="reaction-tag">{security.reaction}</span>}
      </div>
      {open && (
        <div className="security-events">
          {evts === null ? <span className="muted">{t("runDetail.security.loading")}</span>
            : evts.length === 0 ? <span className="muted">{t("runDetail.security.noDetail")}</span>
              : evts.filter((e) => e.phase === "executed").map((e, i) => (
                <div className="security-event-item" key={i}>
                  <span className="sev-dot" style={{ background: SEVERITY_COLOR[e.severity] }} />
                  <span className="sev-badge" style={{ color: SEVERITY_COLOR[e.severity] }} title={e.severity}>
                    {SEVERITY_LABELS[e.severity] ? t(SEVERITY_LABELS[e.severity]) : e.severity}
                  </span>
                  <span className="sev-cat" title={e.category}>{SEC_CATEGORY_LABELS[e.category] ? t(SEC_CATEGORY_LABELS[e.category]) : e.category}</span>
                  {e.layer === "system" && <span className="sev-target">→ {e.target}</span>}
                  {e.hitl_status !== "n/a" && (() => {
                    const h = HITL_LABELS[e.hitl_status];
                    return (
                      <span
                        className="sev-hitl"
                        title={h ? t("runDetail.security.hitlTitle", { title: t(h.titleKey), status: e.hitl_status }) : e.hitl_status}
                        style={h?.good ? { background: "var(--green-bg)", color: "var(--green)" } : undefined}
                      >
                        {h ? t(h.labelKey) : e.hitl_status}
                      </span>
                    );
                  })()}
                  <code className="sev-cmd">{e.command}</code>
                </div>
              ))}
        </div>
      )}
    </div>
  );
}

function scoreVerdict(t: TFn, att: AttemptSummary, envName: string, envThreshold?: number | null): string {
  const threshold = envThreshold ?? PASS_THRESHOLDS[envName] ?? 60;
  if (att.score_total == null) {
    if (att.status === "running" || att.status === "queued" || att.status === "scoring") return t("runDetail.verdict.waiting");
    return t("runDetail.verdict.noResult");
  }
  const passed = att.score_total >= threshold;
  const delta = Math.abs(att.score_total - threshold);
  return passed
    ? t("runDetail.verdict.passed", { score: att.score_total, threshold, delta })
    : t("runDetail.verdict.failed", { score: att.score_total, threshold, delta });
}

function dimWeight(dimension: string, envWeights?: Record<string, number>): number {
  return envWeights?.[dimension] ?? DIMENSION_WEIGHTS[dimension] ?? 0;
}

function dimensionExplanation(t: TFn, score: AttemptDetail["scores"][number], envWeights?: Record<string, number>): string {
  const weight = dimWeight(score.dimension, envWeights);
  const weighted = Math.round((score.value * weight) / 100);
  const lost = Math.max(0, 100 - score.value);
  const weightedLost = Math.round((lost * weight) / 100);
  const reason = score.detail ? t("runDetail.dimExpl.reason", { detail: score.detail }) : t("runDetail.dimExpl.noReason");
  const contrib = weightedLost > 0
    ? t("runDetail.dimExpl.contribDeducted", { weighted, weight, weightedLost })
    : t("runDetail.dimExpl.contribFull", { weight });
  return t("runDetail.dimExpl.full", { value: score.value, weight, contrib, reason });
}

function primaryScoreIssue(t: TFn, scores: AttemptDetail["scores"], envWeights?: Record<string, number>): string {
  if (!scores.length) return t("runDetail.primaryIssue.none");
  const sorted = [...scores].sort((a, b) => {
    const aw = dimWeight(a.dimension, envWeights);
    const bw = dimWeight(b.dimension, envWeights);
    return ((100 - b.value) * bw) - ((100 - a.value) * aw);
  });
  const first = sorted[0];
  if (first.value >= 100) return t("runDetail.primaryIssue.perfect");
  const label = dimLabel(t, first.dimension);
  return t("runDetail.primaryIssue.main", { label, value: first.value, detail: first.detail || "" });
}

// ── main page ──

// 证据工作区深链参数（?attempt=att_x&view=…）→ 本页板块的落点。
const DEEP_LINK_TARGETS: Record<string, string> = {
  scores: "focus-scores",
  artifacts: "focus-artifacts",
  trace: "focus-flow",
  events: "focus-flow",
  conversation: "focus-flow",
};

export function RunDetail() {
  const { t } = useI18n();
  const { runId } = useParams<{ runId: string }>();
  const [run, setRun] = useState<RunDetailModel | null>(null);
  const [details, setDetails] = useState<Record<string, AttemptDetail>>({});
  // 逐 attempt 的上游实扣（成本唯一来源）。加载失败时为 null——
  // 卡片退化成只展示 token，不显示任何金额，绝不回落估算。
  const [runCost, setRunCost] = useState<RunCost | null>(null);
  const [eventsMap, setEventsMap] = useState<Record<string, EventBlock[]>>({});
  const [artifactsMap, setArtifactsMap] = useState<Record<string, ArtifactDir>>({});
  const [activeTab, setActiveTab] = useState<"compare" | "flow" | "wire">("compare");
  const [focusAgent, setFocusAgent] = useState<string | null>(null);
  const [officeSyncEnabled, setOfficeSyncEnabled] = useState(false);
  const [officePage, setOfficePage] = useState(0);
  const [officeSheet, setOfficeSheet] = useState(0);
  const [err, setErr] = useState("");
  const [searchParams] = useSearchParams();
  // 重评后重启 SSE：后端把 run 拉回 scoring，新流保持打开并推送后续更新。
  const [streamEpoch, setStreamEpoch] = useState(0);
  const handleRejudged = useCallback(() => setStreamEpoch((n) => n + 1), []);
  // 只在首次拿到 run 时应用一次深链定位；之后 SSE 刷新不再打扰用户操作。
  const deepLinkRef = useRef({
    attempt: searchParams.get("attempt"),
    view: searchParams.get("view") ?? "",
    applied: false,
  });
  const envMeta = useEnvMeta(run?.env_name);
  const eventViewActive = activeTab === "flow" || focusAgent !== null;
  const activeTabRef = useRef(activeTab);
  const focusAgentRef = useRef(focusAgent);
  activeTabRef.current = activeTab;
  focusAgentRef.current = focusAgent;

  useEffect(() => {
    if (!runId) return;
    let alive = true;
    let fallbackId: ReturnType<typeof setInterval> | undefined;
    const refreshAttempt = async (attemptId: string) => {
      const shouldLoadEvents = activeTabRef.current === "flow"
        || focusAgentRef.current === attemptId;
      const [detail, artifacts, events] = await Promise.all([
        api.getAttempt(runId, attemptId, false),
        api.getArtifacts(runId, attemptId),
        shouldLoadEvents ? api.getEvents(runId, attemptId) : Promise.resolve(null),
      ]);
      if (!alive) return;
      setDetails((previous) => ({ ...previous, [attemptId]: detail }));
      if (events !== null) {
        setEventsMap((previous) => ({ ...previous, [attemptId]: parseEvents(events) }));
      }
      setArtifactsMap((previous) => ({ ...previous, [attemptId]: artifacts }));
    };
    const refreshAll = async () => {
      try {
        const r = await api.getRun(runId);
        if (!alive) return;
        setRun(r);
        // 成本失败不影响页面其余部分——没有金额比显示错误金额好
        api.getRunCost(runId).then(
          (c) => alive && setRunCost(c),
          () => alive && setRunCost(null),
        );
        await Promise.all(r.attempts.map((att) => refreshAttempt(att.id)));
      } catch (e) { if (alive) setErr(String(e)); }
    };
    const startFallback = () => {
      if (fallbackId === undefined) fallbackId = setInterval(refreshAll, 4000);
    };
    const stopFallback = () => {
      if (fallbackId !== undefined) {
        clearInterval(fallbackId);
        fallbackId = undefined;
      }
    };

    void refreshAll();
    const source = new EventSource(api.runStreamUrl(runId));
    source.addEventListener("run:update", (event) => {
      if (!alive) return;
      try {
        setRun(JSON.parse((event as MessageEvent<string>).data) as RunDetailModel);
      } catch { startFallback(); }
    });
    source.addEventListener("attempt:update", (event) => {
      if (!alive) return;
      try {
        const payload = JSON.parse((event as MessageEvent<string>).data) as { attempt_id?: string };
        if (payload.attempt_id) void refreshAttempt(payload.attempt_id).catch(startFallback);
      } catch { startFallback(); }
    });
    source.addEventListener("stream:end", () => {
      void refreshAll();
      source.close();
      stopFallback();
    });
    source.onopen = stopFallback;
    source.onerror = startFallback;

    return () => {
      alive = false;
      source.close();
      stopFallback();
    };
  }, [runId, streamEpoch]);

  useEffect(() => {
    const link = deepLinkRef.current;
    if (link.applied || !run) return;
    link.applied = true;
    if (!link.attempt || !run.attempts.some((attempt) => attempt.id === link.attempt)) return;
    if (link.view === "wire") setActiveTab("wire");
    else setFocusAgent(link.attempt);
    const targetId = link.view === "wire" ? `wire-${link.attempt}` : DEEP_LINK_TARGETS[link.view] ?? "";
    if (!targetId) return;
    // 目标板块的数据是异步加载的（评分/产物/事件），轮询等它渲染出来再滚动；
    // 找不到（如该 attempt 无产物）最多重试 20 次后放弃，不阻塞页面。
    let tries = 0;
    const timer = setInterval(() => {
      const element = document.getElementById(targetId);
      tries += 1;
      if (element) element.scrollIntoView({ behavior: "smooth", block: "start" });
      if (element || tries > 20) clearInterval(timer);
    }, 300);
  }, [run]);

  useEffect(() => {
    if (!eventViewActive || !runId || !run) return;
    let alive = true;
    const attempts = focusAgent
      ? run.attempts.filter((attempt) => attempt.id === focusAgent)
      : run.attempts;
    void Promise.all(attempts.map(async (attempt) => {
      const events = await api.getEvents(runId, attempt.id);
      if (alive) {
        setEventsMap((previous) => ({
          ...previous,
          [attempt.id]: parseEvents(events),
        }));
      }
    })).catch((reason) => {
      if (alive) setErr(String(reason));
    });
    return () => { alive = false; };
  }, [activeTab, eventViewActive, focusAgent, runId, run?.id]);

  if (err) return <p className="warning">{err}</p>;
  if (!run) return <p className="muted" style={{ padding: 40, textAlign: "center" }}>{t("runDetail.page.loading")}</p>;

  const atts = run.attempts;
  const { cols: agentCols, maxLen: stepMaxLen } = buildAgentColumns(details, atts);
  const hasRunning = atts.some((a) => a.status === "running" || a.status === "queued");

  const handleStop = async () => {
    if (!runId) return;
    try { await api.stopRun(runId); } catch (e) { setErr(String(e)); }
  };

  return (
    <div className="run-detail">
      {/* header */}
      <div className="run-header">
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <h2 style={{ margin: 0 }}>
            <span className="font-mono" style={{ color: "var(--text-2)", fontWeight: 400, fontSize: 14 }}>run/</span>
            {run.id.slice(4)}
          </h2>
          {hasRunning && (
            <button className="btn-stop" onClick={handleStop}>{t("runDetail.page.stopAll")}</button>
          )}
        </div>
        <span className="muted font-mono" style={{ fontSize: 12 }}>{run.env_name} · {run.task_id}</span>
      </div>

      {atts.length === 0 && <p className="muted">{t("runDetail.page.noAttempts")}</p>}

      <details className="evidence-help run-detail-help">
        <summary>{t("runDetail.page.helpSummary")}</summary>
        <div className="evidence-help-body">
          <p>{t("runDetail.page.helpP1")}<strong>{t("runDetail.page.helpAttempt")}</strong>{t("runDetail.page.helpP1b")}
            <strong>{t("runDetail.page.helpRawFull")}</strong>{t("runDetail.page.helpP1c")}
            <strong>{t("runDetail.page.helpCoarseToFine")}</strong>{t("runDetail.page.helpP1d")}</p>
          <ul>
            <li><strong>{t("runDetail.page.helpLiSummaryB")}</strong>{t("runDetail.page.helpLiSummary")}</li>
            <li><strong>{t("runDetail.page.helpLiScoresB")}</strong>{t("runDetail.page.helpLiScores")}</li>
            <li><strong>{t("runDetail.page.helpLiCostB")}</strong>{t("runDetail.page.helpLiCost")}<strong>{t("runDetail.page.helpLiCostB2")}</strong>{t("runDetail.page.helpLiCost2")}</li>
            <li><strong>{t("runDetail.page.helpLiConvB")}</strong>{t("runDetail.page.helpLiConv")}</li>
            <li><strong>{t("runDetail.page.helpLiBehaviorB")}</strong>{t("runDetail.page.helpLiBehavior")}</li>
            <li><strong>{t("runDetail.page.helpLiArtifactsB")}</strong>{t("runDetail.page.helpLiArtifacts")}</li>
          </ul>
          <p><strong>{t("runDetail.page.helpP2a")}</strong>{t("runDetail.page.helpP2b")}</p>
          <ul>
            <li><strong>{t("runDetail.page.helpMoneyB")}</strong>{t("runDetail.page.helpMoney")}
              <strong>{t("runDetail.page.helpMoneyKeyB")}</strong>{t("runDetail.page.helpMoney2")}
              <strong>{t("runDetail.page.helpMoneyNoEstB")}</strong>{t("runDetail.page.helpMoney3")}</li>
            <li><strong>{t("runDetail.page.helpTokenB")}</strong>{t("runDetail.page.helpToken")}<strong>{t("runDetail.page.helpTokenCacheB")}</strong>{t("runDetail.page.helpToken2")}</li>
            <li>{t("runDetail.page.helpUpstreamPre")}<strong>{t("runDetail.page.helpUpstreamB")}</strong>{t("runDetail.page.helpUpstream")}<strong>{t("runDetail.page.helpUpstreamUtcB")}</strong>{t("runDetail.page.helpUpstream2")}</li>
            <li><strong>{t("runDetail.page.helpMissingB")}</strong>{t("runDetail.page.helpMissing")}<em>{t("runDetail.page.helpMissingSettling")}</em>{t("runDetail.page.helpMissing2")}
              <em>{t("runDetail.page.helpMissingGateway")}</em>{t("runDetail.page.helpMissing3")}</li>
            <li><strong>{t("runDetail.page.helpVerdictB")}</strong>{t("runDetail.page.helpVerdict")}</li>
          </ul>
          <p>{t("runDetail.page.helpP3")}<strong>{t("runDetail.page.helpDrilldownB")}</strong>{t("runDetail.page.helpP3b")}</p>
          <ul>
            <li><strong>{t("runDetail.page.helpLiTraceB")}</strong>{t("runDetail.page.helpLiTrace")}</li>
            <li><strong>{t("runDetail.page.helpLiEventsB")}</strong>{t("runDetail.page.helpLiEvents")}</li>
            <li><strong>{t("runDetail.page.helpLiWireB")}</strong>{t("runDetail.page.helpLiWire")}</li>
          </ul>
          <p>{t("runDetail.page.helpP4")}</p>
        </div>
      </details>

      {run.model_integrity?.status === "violated" && (
        <div className="score-primary-issue">
          {t("runDetail.page.integrityRun")}
        </div>
      )}

      {/* agent summary cards */}
      <div className="agent-summary-row">
        {atts.map((att) => {
          const d = details[att.id];
          return (
            <div className="agent-summary-card" key={att.id}>
              <div className="agent-summary-header">
                <span className="agent-summary-name clickable" onClick={() => setFocusAgent(att.id)}>{columnLabel(att, atts)}</span>
                <Badge status={att.status} scoringStatus={att.scoring_status} />
                <WireBadge att={att} />
                {att.model_integrity?.status === "violated" && (
                  <span className="agent-col-error">{t("runDetail.page.integrityViolated")}</span>
                )}
                {att.transport_status === "disconnected" && (
                  <span className="agent-col-error">{t("runDetail.page.transportDisconnected")}</span>
                )}
              </div>
              <ScoreBar score={att.score_total} />
              <ReJudgePanel
                runId={run.id}
                attemptId={att.id}
                scoringStatus={att.scoring_status}
                onRejudged={handleRejudged}
              />
              <div className="agent-summary-stats">
                {att.model_used && <span className="font-mono">{att.model_used}</span>}
                <span>{t("runDetail.page.duration", { v: fmt(att.duration_ms) })}</span>
                <span>{t("runDetail.page.tokensStat", { v: tokens(att.token_usage_json, t) })}</span>
                <MoneyStat money={att.money} />
                {(d?.progress?.fallback_turn_count ?? 0) > 0 && (
                  <span>{t("runDetail.page.fallbackTurns", { n: d.progress.fallback_turn_count ?? 0 })}</span>
                )}
              </div>
              {att.model_integrity?.status === "violated" && (
                <div className="score-primary-issue">
                  {t("runDetail.page.integrityExpected")}<span className="font-mono">{att.model_integrity.expected_model ?? "—"}</span>
                  {t("runDetail.page.integrityObserved")}
                  <span className="font-mono">
                    {att.model_integrity.observed_models.join(", ") || t("runDetail.common.unknown")}
                  </span>
                </div>
              )}
              {d?.security && <SecuritySummaryRow security={d.security} runId={run.id} attemptId={att.id} />}
              <NormalizedOutputPanel attemptId={att.id} rawOutput={d?.final_state} />
            </div>
          );
        })}
      </div>


      {/* 结论区（无条件渲染，不属于任何 tab）：由粗到细的前半段——
          分数 → 成本 → 对话轮次 → 行为差异 → 产物。下方 tab 是更细的下钻视图。 */}
          {/* score detail cards */}
          <h3 className="section-title">{t("runDetail.page.sectionScores")}</h3>
          <div className="score-cards-row">
            {atts.map((att) => {
              const d = details[att.id];
              const scores = d?.scores ?? [];
              return (
                <div className="score-detail-card" key={att.id}>
                  <div className="score-detail-header">
                    <span>{columnLabel(att, atts)}</span>
                    <span className="score-detail-total" style={{ color: scoreColor(att.score_total) }}>{att.score_total ?? "-"}</span>
                  </div>
                  <div className="score-verdict">{scoreVerdict(t, att, run.env_name, envMeta.threshold)}</div>
                  {scores.length > 0 && <div className="score-primary-issue">{primaryScoreIssue(t, scores, envMeta.weights)}</div>}
                  {scores.map((s) => (
                    <div key={s.dimension} className="score-dim-row">
                      <div className="score-dim-header">
                        <span className="score-dim-label">{dimLabel(t, s.dimension)}</span>
                        <span className="score-dim-weight">{t("runDetail.page.weight", { n: dimWeight(s.dimension, envMeta.weights) })}</span>
                        <span className="score-dim-val" style={{ color: scoreColor(s.value) }}>{s.value}/100</span>
                      </div>
                      <div className="score-dim-bar">
                        <div className="score-dim-fill" style={{
                          width: `${Math.min(100, s.value)}%`,
                          background: scoreColor(s.value),
                        }} />
                      </div>
                      <div className="score-dim-detail">{dimensionExplanation(t, s, envMeta.weights)}</div>
                    </div>
                  ))}
                  {scores.length === 0 && (
                    <div className="score-dim-detail">
                      {d?.error_code ? t("runDetail.page.noDimScoreErr", { code: d.error_code, msg: d.error_message ? t("runDetail.page.errMsgSuffix", { msg: d.error_message }) : "" }) : t("runDetail.page.noDimDetail")}
                    </div>
                  )}
                </div>
              );
            })}
          </div>

          {atts.some((att) => details[att.id]?.iteration) && (
            <>
              <h3 className="section-title">{t("runDetail.page.sectionMultiRound")}</h3>
              <div className="score-cards-row">
                {atts.map((att) => {
                  const iteration = details[att.id]?.iteration;
                  if (!iteration) return <div className="score-detail-card" key={att.id}>{t("runDetail.page.noMultiRound")}</div>;
                  const metrics = iteration.metrics;
                  return (
                    <div className="score-detail-card" key={att.id}>
                      <div className="score-detail-header">
                        <span>{columnLabel(att, atts)}</span>
                        <span>{iteration.current_round == null ? t("runDetail.page.roundPreparing") : t("runDetail.page.roundOf", { current: iteration.current_round + 1, max: iteration.max_iterations ?? "-" })}</span>
                      </div>
                      <div className="score-dim-detail">{t("runDetail.page.iterPhase", { phase: iteration.phase, continuity: iteration.session_continuity })}</div>
                      <div className="score-dim-detail">
                        {t("runDetail.page.iterScores", { first: metrics.first_score ?? "-", final: metrics.final_score ?? "-", improvement: metrics.absolute_improvement == null ? "-" : `${metrics.absolute_improvement >= 0 ? "+" : ""}${metrics.absolute_improvement}` })}
                      </div>
                      <div className="score-dim-detail">
                        {t("runDetail.page.iterMetrics", { resolved: metrics.resolved_problem_count, regression: metrics.regression_count, adoption: metrics.feedback_adoption_rate == null ? "-" : `${Math.round(metrics.feedback_adoption_rate * 100)}%` })}
                      </div>
                      {iteration.submissions.map((submission) => (
                        <div className="score-dim-row" key={submission.submission_id}>
                          <div className="score-dim-header">
                            <span>Submission {submission.round_index + 1}</span>
                            <span>{submission.selected_for_final_score ? t("runDetail.page.submissionFinal") : submission.evaluation_status}</span>
                          </div>
                          <div className="score-dim-detail">
                            {t("runDetail.page.submissionDetail", { snapshot: submission.snapshot_status, evaluation: submission.evaluation_status, feedback: submission.feedback_status })}
                          </div>
                        </div>
                      ))}
                    </div>
                  );
                })}
              </div>
            </>
          )}

          {/* 令牌与成本：金额取**上游实扣**（逐 attempt key 直查），
              令牌取**本地采集**（五维，含上游不提供的 cache read/write）。
              本地 token×价格表估算已删除——实测高估 26×且方向不一致，
              留着只会误导。 */}
          {atts.some((att) => Object.keys(details[att.id]?.token_usage ?? {}).length > 0) && (
            <>
              <h3 className="section-title">{t("runDetail.page.sectionCost")}</h3>
              <div className="score-cards-row">
                {atts.map((att) => (
                  <CostCard
                    key={att.id}
                    label={columnLabel(att, atts)}
                    detail={details[att.id]}
                    ledger={runCost?.by_attempt?.find((x) => x.attempt_id === att.id)}
                  />
                ))}
              </div>
              {runCost && <RunCostSummary cost={runCost} />}
            </>
          )}

          {/* 多轮 conversation + 压缩评测（只在有多轮 conversation 的 attempt
              上渲染实质内容；单轮/历史 attempt 的 panel 显示 legacy 提示或不渲染）。 */}
          {atts.some((att) => details[att.id]?.conversation && !details[att.id]?.conversation?.summary.is_legacy) && (
            <>
              <h3 className="section-title">{t("runDetail.page.sectionConversation")}</h3>
              <div className="conversation-cards-row">
                {atts.map((att) => {
                  const d = details[att.id];
                  return (
                    <div className="conversation-card" key={att.id}>
                      <div className="conversation-card-agent">{columnLabel(att, atts)}</div>
                      <ConversationPanel conversation={d?.conversation} />
                    </div>
                  );
                })}
              </div>
            </>
          )}

          {/* behavioral insights */}
          <h3 className="section-title">{t("runDetail.page.sectionBehavior")}</h3>
          <div className="insights-row">
            {buildInsights(atts, details, t).map((ins) => (
              <div className="insight-card" key={ins.title}>
                <div className="insight-card-header">
                  <span>{ins.icon}</span>
                  <span>{ins.title}</span>
                </div>
                {ins.items.map((it) => (
                  <div className="insight-item" key={it.agent}>
                    <span className="insight-dot" style={{ background: AGENT_COLORS[it.agent] ?? "var(--text-2)" }} />
                    <div className="insight-item-body">
                      <span className="insight-agent" style={{ color: AGENT_COLORS[it.agent] ?? "var(--text-1)" }}>{it.label}</span>
                      <span className="insight-text">{it.text}</span>
                    </div>
                  </div>
                ))}
              </div>
            ))}
          </div>

          {/* artifacts */}
          <h3 className="section-title">{t("runDetail.page.sectionArtifacts")}</h3>
          <label className="office-sync-toggle">
            <input type="checkbox" checked={officeSyncEnabled}
              onChange={(event) => setOfficeSyncEnabled(event.target.checked)} />
            {t("runDetail.page.officeSync")}
          </label>
          <div className="artifacts-row">
            {atts.map((att) => {
              const artifacts = artifactsMap[att.id] ?? null;
              return (
                <div className="artifacts-col" key={att.id}>
                  <div className="artifacts-col-header">{columnLabel(att, atts)}</div>
                  {artifacts && artifacts.file_count > 0
                    ? <ArtifactsPanel tree={artifacts} runId={run.id} attemptId={att.id}
                        officeSync={{ enabled: officeSyncEnabled, page: officePage, sheet: officeSheet,
                          setPage: setOfficePage, setSheet: setOfficeSheet }} />
                    : <span className="muted" style={{ padding: 12, display: "block" }}>{t("runDetail.page.noArtifacts")}</span>}
                </div>
              );
            })}
          </div>

      {/* 下钻视图（最细粒度）：三个并列 tab——逐步调用 / 对话流 / 通信时序。
          置于结论区之后，符合本页"由粗到细"的排布。 */}
      <div className="tab-bar">
        <button className={`tab${activeTab === "compare" ? " active" : ""}`} onClick={() => setActiveTab("compare")}>{t("runDetail.tab.compare")}</button>
        <button className={`tab${activeTab === "flow" ? " active" : ""}`} onClick={() => setActiveTab("flow")}>{t("runDetail.tab.flow")}</button>
        <button className={`tab${activeTab === "wire" ? " active" : ""}`} onClick={() => setActiveTab("wire")}>{t("runDetail.tab.wire")}</button>
      </div>

      {activeTab === "wire" && (
        <div className="wire-tab">
          {atts.map((att) => (
            <div key={att.id} id={`wire-${att.id}`}>
              <WirePanel runId={run.id} attemptId={att.id} label={columnLabel(att, atts)} />
            </div>
          ))}
        </div>
      )}

      {activeTab === "compare" && (
        <>
          {/* step comparison：每列一个 agent 的完整调用时序。标题由 tab 名承担，
              这里不再重复 h3。业务步骤（skill 工具）高亮编号，辅助命令弱化。 */}
          <div className="step-legend">
            <span><span className="step-dot biz" /> {t("runDetail.step.legendBiz")}</span>
            <span><span className="step-dot aux" /> {t("runDetail.step.legendAux")}</span>
            <span className="step-legend-note">{t("runDetail.step.legendNote")}</span>
          </div>
          <div className="step-cols-wrap">
            {atts.map((att) => {
              const steps = agentCols[att.id] ?? [];
              const bizCount = steps.filter((s) => s.business).length;
              const auxCount = steps.length - bizCount;
              return (
                <div className="step-col" key={att.id} style={{ minHeight: stepMaxLen * 28 }}>
                  <div className="step-col-head">
                    <span className="step-col-agent">{columnLabel(att, atts)}</span>
                    <span className="step-col-stat">{t("runDetail.step.colStat", { total: steps.length, biz: bizCount, aux: auxCount })}</span>
                  </div>
                  <div className="step-col-body">
                    {steps.length === 0
                      ? <div className="step-cell-empty">{t("runDetail.step.noTrace")}</div>
                      : steps.map((s, i) => <StepCallItem key={i} step={s} />)}
                  </div>
                </div>
              );
            })}
          </div>
        </>
      )}

      {activeTab === "flow" && !focusAgent && (
        <div className="agent-columns" style={{ gridTemplateColumns: `repeat(${atts.length || 1}, 1fr)` }}>
          {atts.map((att) => {
            const blocks = eventsMap[att.id] ?? [];
            const d = details[att.id];
            return (
              <div className="agent-column" key={att.id}>
                <div className="agent-col-header">
                  <div className="agent-col-name clickable" onClick={() => setFocusAgent(att.id)}>{columnLabel(att, atts)}</div>
                  <Badge status={att.status} scoringStatus={att.scoring_status} />
                </div>
                {d?.error_code && <div className="agent-col-error">{d.error_code}: {d.error_message}</div>}
                <AutoScrollFlow blocks={blocks} />
              </div>
            );
          })}
        </div>
      )}

      {/* single agent focus view */}
      {focusAgent && (() => {
        const att = atts.find((a) => a.id === focusAgent);
        if (!att) return null;
        const d = details[att.id];
        const blocks = eventsMap[att.id] ?? [];
        const scores = d?.scores ?? [];
        const artifacts = artifactsMap[att.id] ?? null;
        return (
          <div className="focus-view">
            <div className="focus-header">
              <button className="btn-ghost" onClick={() => setFocusAgent(null)}>{t("runDetail.focus.back")}</button>
              <span className="focus-agent-name">{columnLabel(att, atts)}</span>
              <Badge status={att.status} scoringStatus={att.scoring_status} />
              {att.transport_status === "disconnected" && (
                <span className="agent-col-error">{t("runDetail.page.transportDisconnected")}</span>
              )}
              <span className="focus-score">{att.score_total ?? "-"}</span>
            </div>

            <div className="focus-stats">
              <span>{t("runDetail.page.duration", { v: fmt(att.duration_ms) })}</span>
              <span>{t("runDetail.page.tokensStat", { v: tokens(att.token_usage_json, t) })}</span>
              <span>{t("runDetail.focus.toolCalls", { n: att.tool_call_count })}</span>
              <MoneyStat money={att.money} />
              {(d?.progress?.fallback_turn_count ?? 0) > 0 && (
                <span>{t("runDetail.page.fallbackTurns", { n: d.progress.fallback_turn_count ?? 0 })}</span>
              )}
              {d?.progress?.last_agent_activity_at && (
                <span>{t("runDetail.focus.lastActivity", { time: new Date(d.progress.last_agent_activity_at).toLocaleTimeString() })}</span>
              )}
            </div>

            {d?.error_code && <div className="agent-col-error" style={{ margin: "0 0 12px" }}>{d.error_code}: {d.error_message}</div>}

            {scores.length > 0 && (
              <div className="focus-scores" id="focus-scores">
                {scores.map((s) => (
                  <div key={s.dimension} className="focus-score-item">
                    <span className="score-dim-label">{dimLabel(t, s.dimension)}</span>
                    <span className="score-dim-weight">{t("runDetail.page.weight", { n: dimWeight(s.dimension, envMeta.weights) })}</span>
                    <div className="score-dim-bar" style={{ flex: 1 }}>
                      <div className="score-dim-fill" style={{
                        width: `${Math.min(100, s.value)}%`,
                        background: scoreColor(s.value),
                      }} />
                    </div>
                    <span className="score-dim-val" style={{ color: scoreColor(s.value) }}>{s.value}/100</span>
                    <span className="score-dim-detail">{dimensionExplanation(t, s, envMeta.weights)}</span>
                  </div>
                ))}
              </div>
            )}

            {artifacts && artifacts.file_count > 0 && (
              <div className="focus-artifacts" id="focus-artifacts">
                <h3 className="section-title">{t("runDetail.page.sectionArtifacts")}</h3>
                <ArtifactsPanel tree={artifacts} runId={run.id} attemptId={att.id} />
              </div>
            )}

            <h3 className="section-title">{t("runDetail.page.sectionFullRun")}</h3>
            <div className="focus-flow" id="focus-flow">
              {blocks.length === 0
                ? <span className="muted">{t("runDetail.page.noEvents")}</span>
                : <ConversationFlow blocks={blocks} />}
            </div>
          </div>
        );
      })()}
    </div>
  );
}
