export type Lang = "zh" | "en";

type Bilingual = { zh: string; en: string };

const STATUS_LABELS: Record<string, Bilingual> = {
  queued: { zh: "排队中", en: "Queued" },
  provisioning: { zh: "准备中", en: "Provisioning" },
  running: { zh: "运行中", en: "Running" },
  scoring: { zh: "评分中", en: "Scoring" },
  completed: { zh: "已完成", en: "Completed" },
  partial: { zh: "部分完成", en: "Partial" },
  failed: { zh: "失败", en: "Failed" },
  cancelled: { zh: "已取消", en: "Cancelled" },
  gave_up: { zh: "未通过", en: "Gave up" },
  timeout: { zh: "超时", en: "Timeout" },
  scoring_failed: { zh: "评分失败", en: "Scoring failed" },
  interrupted: { zh: "已中断", en: "Interrupted" },
  session_create_failed: { zh: "会话创建失败", en: "Session create failed" },
  cli_not_found: { zh: "CLI 未找到", en: "CLI not found" },
  cli_error: { zh: "CLI 错误", en: "CLI error" },
  chat_failed: { zh: "对话失败", en: "Chat failed" },
  auth_failed: { zh: "认证失败", en: "Auth failed" },
  model_integrity_failed: { zh: "模型完整性失败", en: "Model integrity failed" },
  current: { zh: "当前版本", en: "Current" },
  stale: { zh: "已过期", en: "Stale" },
  not_generated: { zh: "尚未生成", en: "Not generated" },
  supported: { zh: "支持", en: "Supported" },
  unsupported: { zh: "不支持", en: "Unsupported" },
  error: { zh: "错误", en: "Error" },
  observed: { zh: "已观测", en: "Observed" },
  available: { zh: "可用", en: "Available" },
  unavailable: { zh: "不可用", en: "Unavailable" },
};

const CAPABILITY_LABELS: Record<string, Bilingual> = {
  experiments: { zh: "实验", en: "Experiments" },
  task_variants: { zh: "任务变体", en: "Task variants" },
  run_groups: { zh: "运行组", en: "Run groups" },
  leader_events: { zh: "优胜事件", en: "Leader events" },
  robustness: { zh: "稳健性分析", en: "Robustness" },
  profiles: { zh: "研究方案", en: "Profiles" },
  auto_profile: { zh: "自动方案推荐", en: "Auto profile" },
  insights: { zh: "研究洞察", en: "Insights" },
  research_feedback: { zh: "研究反馈", en: "Research feedback" },
  normalized_output: { zh: "规范化输出", en: "Normalized output" },
  attack_coverage: { zh: "攻击覆盖分析", en: "Attack coverage" },
};

const REASON_LABELS: Record<string, Bilingual> = {
  schema_missing: { zh: "数据库结构未就绪", en: "Database schema not ready" },
  dependency_missing: { zh: "代码依赖缺失", en: "Missing dependency" },
  configuration_missing: { zh: "运行配置不完整", en: "Incomplete configuration" },
};

const MUTATOR_LABELS: Record<string, Bilingual> = {
  baseline: { zh: "原始对照", en: "Baseline" },
  spacing: { zh: "空格扰动", en: "Spacing" },
  "letter-case": { zh: "字母大小写", en: "Letter case" },
  "unicode-homoglyph": { zh: "相似字符替换", en: "Unicode homoglyph" },
  "instruction-position": { zh: "指令位置", en: "Instruction position" },
};

const PREVIEW_REASON_LABELS: Record<string, Bilingual> = {
  "exactly one baseline variant is required": {
    zh: "必须且只能启用一个原始对照变体",
    en: "Exactly one baseline variant is required",
  },
  "cell limit exceeded": { zh: "实验单元数量超过上限", en: "Cell limit exceeded" },
  "attempt limit exceeded": { zh: "总执行次数超过上限", en: "Attempt limit exceeded" },
  "multi-model requires one agent and at least two explicit models": {
    zh: "多模型对比需要选择一个 Agent，并至少选择两个明确的模型",
    en: "Multi-model requires one agent and at least two explicit models",
  },
  "text modality required": { zh: "该变体仅适用于文本任务", en: "Text modality required" },
  "explicit /prompt mutable region required": {
    zh: "任务没有声明可修改的提示词区域",
    en: "Explicit /prompt mutable region required",
  },
  "no applicable characters": { zh: "任务内容中没有可应用该变体的字符", en: "No applicable characters" },
  "structured instruction_blocks required": {
    zh: "任务没有声明结构化 instruction_blocks",
    en: "Structured instruction_blocks required",
  },
  "instruction blocks must occur exactly once": {
    zh: "每个指令块必须在任务中恰好出现一次",
    en: "Instruction blocks must occur exactly once",
  },
  "instruction block overlaps protected span": {
    zh: "指令块与受保护内容重叠",
    en: "Instruction block overlaps protected span",
  },
  "unknown target position": { zh: "指令位置参数无效", en: "Unknown target position" },
  "mutation unsupported": { zh: "当前任务不支持该变体", en: "Mutation unsupported" },
};

const LEADER_REASON_LABELS: Record<string, Bilingual> = {
  "scope-finalized": { zh: "本轮优胜候选已确定", en: "Leader candidate finalized" },
  "leader-changed": { zh: "优胜候选发生变化", en: "Leader candidate changed" },
  "first-score": { zh: "产生首个可比较分数", en: "First comparable score" },
};

const LEADER_REASON_FALLBACK: Bilingual = { zh: "优胜候选已更新", en: "Leader candidate updated" };

export const statusLabel = (value: string, lang: Lang = "zh"): string =>
  STATUS_LABELS[value]?.[lang] ?? value;
export const capabilityLabel = (value: string, lang: Lang = "zh"): string =>
  CAPABILITY_LABELS[value]?.[lang] ?? value;
export const reasonLabel = (value: string, lang: Lang = "zh"): string =>
  REASON_LABELS[value]?.[lang] ?? value;
export const mutatorLabel = (value: string, lang: Lang = "zh"): string =>
  MUTATOR_LABELS[value]?.[lang] ?? value;

export const leaderReasonLabel = (value: string, lang: Lang = "zh"): string =>
  (LEADER_REASON_LABELS[value] ?? LEADER_REASON_FALLBACK)[lang];

export function previewMessageLabel(message: string, lang: Lang = "zh"): string {
  if (lang === "en") return message;
  const forbidden = /^mutator forbidden by env contract: (.+)$/.exec(message);
  if (forbidden) {
    const mutator = forbidden[1];
    return `场景契约未允许使用变体：${mutatorLabel(mutator, lang)}（${mutator}）`;
  }
  const scoped = /^([^:]+): (.+)$/.exec(message);
  if (scoped && PREVIEW_REASON_LABELS[scoped[2]]) {
    return `${mutatorLabel(scoped[1], lang)}：${PREVIEW_REASON_LABELS[scoped[2]][lang]}`;
  }
  return PREVIEW_REASON_LABELS[message]?.[lang] ?? message;
}
