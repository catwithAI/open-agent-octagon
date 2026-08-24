// 被测 agent 的单一事实源。
//
// 存在的理由：这份名单原本在三个地方各写一遍——`ExperimentBuilder.tsx` 与
// `SameModelSubmit.tsx` 各有一个逐字相同的 `ALL_AGENTS` 数组，`RunDetail.tsx`
// 另有一张配色表。接第 7 家（dsh）时要同时改三处，漏掉任何一处都是静默错误：
// 漏数组 → 那个页面选不到这个 agent；漏配色 → 洞察条目退化成灰点。
//
// 后端也有同类问题（16 处注册点），那个重构横跨全部七家、
// 风险大得多，留给独立 spec。前端这一步不碰后端，可以先做。
//
// **新增 agent 只改这个文件**（外加后端的注册点）。

/** 一个被测 agent 的前端元信息。 */
export type AgentInfo = {
  /** 与后端 `KNOWN_AGENTS` 一致的 agent name，也是 API 的取值。 */
  readonly name: string;
  /**
   * 图表/洞察里的代表色，取 `styles.css` 的调色板变量。
   * 七家必须互相可区分——同色会让对比视图里两条线糊在一起。
   */
  readonly color: string;
};

export const AGENTS: readonly AgentInfo[] = [
  { name: "blade-agent", color: "var(--accent)" },
  { name: "claude-code", color: "var(--blue)" },
  { name: "codex", color: "var(--green)" },
  { name: "kimi-code", color: "var(--purple)" },
  { name: "opencode", color: "var(--yellow)" },
  // mimo 是 opencode 的 fork，取与 kimi(--purple) 同色系的深色变体区分开。
  { name: "mimo-code", color: "var(--violet-line)" },
  // dsh（DeepSeek Harness）——第 7 家，青橙色系与既有六家区分。
  { name: "dsh", color: "var(--tangerine)" },
] as const;

/** agent name 列表，顺序即 UI 展示顺序。 */
export const AGENT_NAMES: readonly string[] = AGENTS.map((a) => a.name);

/** name → 代表色。未知 agent（历史数据里的旧名字）由调用方给兜底色。 */
export const AGENT_COLORS: Readonly<Record<string, string>> =
  Object.fromEntries(AGENTS.map((a) => [a.name, a.color]));
