import { useCallback, useEffect, useRef, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";

import { api, type AttemptDetail, type AttemptSecurity, type ArtifactDir, type AttemptSummary, type RunCost, type RunDetail as RunDetailModel, type SecurityEvent } from "../api/client";
import { ConversationPanel } from "./ConversationPanel";
import { NormalizedOutputPanel } from "../components/NormalizedOutputPanel";
import { ReJudgePanel } from "../components/ReJudgePanel";
import { DocumentViewer, HtmlArtifactView, PresentationViewer, WorkbookViewer, type OfficeSyncState } from "../components/viewers/ArtifactViewers";
import { ArtifactsPanel } from "../components/ArtifactsPanel";
import { CostCard, MoneyStat, RunCostCard, RunCostSummary, costLabel } from "../cost/RunCostCard";
import { WireBadge, WirePanel } from "../wire/WirePanel";
import { AGENT_COLORS } from "../agents";
import { useI18n } from "../i18n";

export { DocumentViewer, HtmlArtifactView, PresentationViewer, WorkbookViewer, type OfficeSyncState };
export { ArtifactsPanel, CostCard, MoneyStat, RunCostCard, RunCostSummary, WireBadge, WirePanel };

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
  artifact_contract: "runDetail.dim.artifact_contract", content_accuracy: "runDetail.dim.content_accuracy", structure: "runDetail.dim.structure",
  style_mapping: "runDetail.dim.style_mapping", technical_quality: "runDetail.dim.technical_quality",
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
type RubricDimension = { description?: string; criteria?: string[]; role?: string; method?: string };
function useEnvMeta(envName: string | undefined): { weights: Record<string, number>; dimensions: Record<string, RubricDimension>; threshold: number | null } {
  const [meta, setMeta] = useState<{ weights: Record<string, number>; dimensions: Record<string, RubricDimension>; threshold: number | null }>({ weights: {}, dimensions: {}, threshold: null });
  useEffect(() => {
    if (!envName) return;
    let alive = true;
    api.listEnvs().then((envs) => {
      if (!alive) return;
      const env = envs.find((e) => e.name === envName);
      if (!env) return;
      const weights: Record<string, number> = {};
      const dimensions: Record<string, RubricDimension> = {};
      for (const d of env.dimensions ?? []) {
        weights[d.name] = d.weight;
        dimensions[d.name] = { description: d.description, criteria: d.criteria, role: d.role, method: d.method };
      }
      setMeta({ weights, dimensions, threshold: env.pass_threshold });
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

function dimensionExplanation(t: TFn, score: AttemptDetail["scores"][number], envWeights?: Record<string, number>, rubric?: RubricDimension): string {
  const weight = dimWeight(score.dimension, envWeights);
  const weighted = Math.round((score.value * weight) / 100);
  const lost = Math.max(0, 100 - score.value);
  const weightedLost = Math.round((lost * weight) / 100);
  const reason = score.detail ? t("runDetail.dimExpl.reason", { detail: score.detail }) : t("runDetail.dimExpl.noReason");
  const contrib = weightedLost > 0
    ? t("runDetail.dimExpl.contribDeducted", { weighted, weight, weightedLost })
    : t("runDetail.dimExpl.contribFull", { weight });
  const rubricText = rubric?.description ? t("runDetail.dimExpl.rubric", { description: rubric.description, criteria: rubric.criteria?.length ? ` 检查项：${rubric.criteria.join("、")}` : "" }) : "";
  return [rubricText, t("runDetail.dimExpl.full", { value: score.value, weight, contrib, reason })].filter(Boolean).join(" ");
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
    setRun(null);
    setDetails({});
    setEventsMap({});
    setArtifactsMap({});
    setRunCost(null);
    setErr("");
    setFocusAgent(null);
  }, [runId]);

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
    const timer = window.setInterval(() => {
      const element = document.getElementById(targetId);
      tries += 1;
      if (element) element.scrollIntoView({ behavior: "smooth", block: "start" });
      if (element || tries > 20) window.clearInterval(timer);
    }, 300);
    return () => window.clearInterval(timer);
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
                      <div className="score-dim-detail">{dimensionExplanation(t, s, envMeta.weights, envMeta.dimensions[s.dimension])}</div>
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
                    <span className="score-dim-detail">{dimensionExplanation(t, s, envMeta.weights, envMeta.dimensions[s.dimension])}</span>
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
