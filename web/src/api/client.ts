async function req<T>(method: string, url: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method, headers: { "Content-Type": "application/json" } };
  if (body !== undefined) init.body = JSON.stringify(body);
  const resp = await fetch(url, init);
  if (!resp.ok) {
    const text = await resp.text();
    let detail = text;
    try { detail = JSON.stringify(JSON.parse(text)); } catch { /* use raw text */ }
    throw new Error(`${method} ${url} -> ${resp.status}: ${detail}`);
  }
  return resp.json() as Promise<T>;
}

export type AgentInfo = {
  name: string;
  status: "available" | "not_configured" | "not_found";
  detail?: string | null;
  cli_path?: string | null;
};
export type EnvDimension = {
  name: string;
  weight: number;
  description: string;
};
export type EnvSummary = {
  name: string;
  skill_id: string;
  description: string;
  category: string;
  test_focus: string;
  pass_threshold: number | null;
  dimensions: EnvDimension[];
  tool_count: number;
  task_count: number;
  // 多轮 conversation 场景（任一 task 带 _conversation）。前端据此置灰 blade
  // 「开启思考」——blade 多轮 + Anthropic extended thinking 会撞 signature 400。
  multi_turn?: boolean;
  available?: boolean;
  load_error?: string | null;
  // 本机依赖预警（后端加载时 which()/find_spec 检查，只警告不阻断）
  prerequisite_warnings?: string[];
  // 场景对 agent 侧模型的 modality 需求（meta prerequisites.agent_modalities）
  agent_modalities?: string[];
  // 研究变体能力来自场景 meta.yaml 的 mutations 契约。历史场景缺少声明时
  // 后端只返回 baseline，前端不得自行放宽。
  supported_mutators?: string[];
  conditional_mutators?: Record<string, { requires?: string[] }>;
};
export type TaskJson = {
  id: string;
  env_name: string;
  prompt: string;
  context: Record<string, unknown>;
  constraints: Record<string, unknown>;
  timeout_seconds: number;
  // task 级多轮标记（context._conversation 有 >1 轮）：前端据此按选中的 task
  // 置灰 blade「开启思考」。env 级 EnvJson.multi_turn 仅作场景卡粗粒度标记。
  multi_turn?: boolean;
};
export type EnvMeta = {
  name: string;
  meta: Record<string, unknown>;
  meta_yaml: string;
};
// 场景 inputs/ 下的输入物料。task 的 files[].path 指向这里——不展示它们，
// 用户就只能看到任务 prompt，看不到 agent 实际拿到的输入全貌。
export type EnvInputFile = { path: string; size: number; too_large: boolean };
export type EnvInputs = { name: string; files: EnvInputFile[] };
export type BladeConfig = {
  base_url: string;
  skills_path: string;
  keep_blade_session: boolean;
  api_key_set: boolean;
  health: { reachable: boolean; status_code?: number; error?: string };
};
export type BladeModelOption = {
  id: string;
  label: string;
  inputModalities?: string[];
  supportsImage?: boolean;
};
export type BladeModelsConfig = {
  default: string | null;
  models: BladeModelOption[];
  error?: string;
};
export type CreateRunResponse = {
  run_id: string;
  task_id: string;
  env_name: string;
  agents: string[];
  attempts: Array<{ attempt_id: string; agent: string; model?: string | null; status: string }>;
};
export type ModelProvidersConfig = {
  providers: string[];
  suggested: string[];
  agent_prefix?: Record<string, string>;
};
export type OpenRouterModel = {
  id: string;
  name: string;
  context_length?: number | null;
  // architecture.input/output_modalities（text/image/audio/video/file）
  input_modalities?: string[];
  output_modalities?: string[];
};
export type OpenRouterModelsConfig = {
  models: OpenRouterModel[];
  error?: string | null;
  stale?: boolean;
};
export type RunRow = {
  run_id: string;
  task_id: string;
  env_name: string;
  run_status: string;
  compare_mode: string;
  model: string | null;
  execution: string | null;
  rubric_version?: string | null;
  created_at: string;
  attempt_count: number;
  attempts?: Array<{
    id: string;
    agent_name: string;
    model: string | null;
    rubric_version?: string | null;
    status: string;
    score_total: number | null;
    execution_status?: string;
    scoring_status?: string;
    model_integrity_status?: ModelIntegrityStatus;
  }>;
};
export type RunListPage = {
  items: RunRow[];
  total: number;
  limit: number;
  offset: number;
};
export type AttemptSummary = {
  id: string;
  agent_name: string;
  model: string | null;
  rubric_version?: string | null;
  status: string;
  execution_status?: string;
  scoring_status?: string;
  execution_started_at?: string | null;
  execution_ended_at?: string | null;
  scoring_queued_at?: string | null;
  scoring_started_at?: string | null;
  scoring_ended_at?: string | null;
  transport_status: string;
  score_total: number | null;
  event_count: number;
  thinking_count: number;
  tool_call_count: number;
  token_usage_json: string | null;
  // 本地估算（token × 价表），**不是**资金口径——字段名自带 estimated_
  // 前缀正是为此：旧名 cost_usd 与 ledger 的同名字段只差一个上下文，
  // 极易被误当实扣取用。展示费用一律用 `money`。
  // estimated_cost_priced=false 表示部分令牌缺定价，金额只是下界。
  // null=未计算/算不出，不是 0。
  estimated_cost_usd?: number | null;
  estimated_cost_priced?: boolean | null;
  estimated_cost_model?: string | null;
  // adapter 自报成本，同样是估算
  adapter_reported_cost_usd?: number | null;
  // 机器可读的口径声明，恒为 "local_estimate"
  cost_source?: string;
  // 资金口径：上游临时 key 实扣。唯一可作为"实际成本"展示的数字。
  money?: AttemptMoney;
  duration_ms: number;
  started_at: string | null;
  ended_at: string | null;
  model_used: string | null;
  // wire 观测摘要——不影响既有字段，缺省时后端给默认值。
  wire_status?: string;
  wire_record_count?: number;
  wire_call_count?: number;
  wire_error_count?: number;
  model_integrity?: ModelIntegrity;
};

// judge 重评历史：attempt_judge_runs 的 append-only 快照，revision 1,2,3…
// 最新 revision 在前。直接展示用 attempts.score_total（最新权威分）。
export type JudgeRunDimension = { dimension: string; value: number; detail: string };
export type JudgeRun = {
  id: string;
  score_revision: number;
  score_total: number;
  status: string;
  judge_model: string | null;
  judge_prompt_version: string | null;
  rubric_version: string | null;
  manifest_ref: string | null;
  scoring_job_id: string | null;
  dimensions: JudgeRunDimension[];
  created_at: string;
};
export type JudgeRunList = { attempt_id: string; items: JudgeRun[] };

export type ModelIntegrityStatus = "not_observed" | "verified" | "violated";
export type ModelIntegrity = {
  expected_model: string | null;
  status: ModelIntegrityStatus;
  observed_models: string[];
  violation_count: number;
  error_code: string | null;
  error_message: string | null;
  checked_at: string | null;
  valid: boolean | null;
};

export type OpenRouterActivityBucket = {
  usage_usd: number | null;
  requests: number | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  reasoning_tokens: number | null;
  endpoints: string[];
};

export type OpenRouterModelActivity = OpenRouterActivityBucket & {
  model: string;
  providers: string[];
};

export type OpenRouterProviderActivity = OpenRouterActivityBucket & {
  provider: string;
  models: string[];
};

export type OpenRouterActivitySummary = {
  usage_usd: number | null;
  requests: number | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  reasoning_tokens: number | null;
  by_model: OpenRouterModelActivity[];
  by_provider: OpenRouterProviderActivity[];
};
// run 级成本核算。
//
// **upstream 是上游对 run 专属 key 的累计实扣差值（资金口径）；estimate 是
// token × 本地价格表（解释性对照）。二者定位不同，不能互相替代。**
//
// `auditable=false` 时 upstream 只是含背景流量的上界或未结算态，
// 不得在 UI 上呈现为实扣归因。
export type RunCostStatus =
  | "pending"
  | "settling"
  | "final"
  | "upper_bound"
  | "failed"
  | "incomplete"
  | "unavailable";
// 单个 attempt 的资金口径。`audited_cost_usd` 来自该 attempt
// 临时 key 结算为 final 后的上游实扣；为 null 表示这笔钱没有可审计的归因，
// **不是 0**，也**不得**用 estimate 补齐后当费用展示。
export type AttemptMoney = {
  audited_cost_usd: number | null;
  auditable: boolean;
  status: string;
  attribution_mode: string | null;
  error_code: string | null;
  // 没有金额的原因（"结算中" / "经网关调用上游，无法按 attempt 归因"）
  unaudited_reason: string | null;
  // true = 结构性拿不到（再等也没有）；false = 还在结算，等得到
  unaudited_is_structural: boolean;
  // 异常探针，不是成本
  estimate: {
    estimate_cost_usd: number | null;
    estimate_priced: boolean | null;
  };
};

export type RunCost = {
  run_id: string;
  provider: string | null;
  // per_attempt = 逐 key 口径（每个 agent 各自实耗）；缺省 = 旧的 run 级口径
  granularity?: "per_attempt";
  // 逐 agent 实耗——六平台横向比较的核心数据
  by_attempt?: Array<{
    attempt_id: string;
    agent_name: string | null;
    cost_usd: number | null;
    status: string;
    auditable: boolean;
    error_code: string | null;
    activity?: OpenRouterActivitySummary | null;
  }>;
  // 缺账的 attempt：它们的钱没被计入 total，总额是低估的
  missing_attempt_ids?: string[];
  // 已有账的部分和。**不是**总额也不是上界——缺失部分未计入
  partial_total_cost_usd?: number | null;
  unpriced_ledger_count?: number;
  // 具体哪几笔没金额、为什么——"N 笔账没有金额"看不出是
  // "结构性拿不到"（blade 经网关）还是"还没跑完"（judge 结算中）
  unpriced_ledgers?: Array<{
    scope: string;
    agent_name: string | null;
    status: string;
    attribution_mode: string;
    error_code: string | null;
    pending: boolean;
  }>;
  scoring_cost_ratio?: number | null;
  judge_required?: boolean | null;
  judge_missing?: boolean | null;
  api_key_hash?: string | null;
  attribution_mode:
    | "ephemeral_run_key"
    | "shared_key_upper_bound"
    | "ephemeral_key_unbased"
    | "partial_unattributed"
    | null;
  status: RunCostStatus;
  auditable: boolean;
  upstream: {
    execution_cost_usd: number | null;
    scoring_cost_usd: number | null;
    total_cost_usd: number | null;
    key_limit_usd: number | null;
    usage_start: number | null;
    usage_after_execution: number | null;
    usage_final: number | null;
  } | null;
  estimate: {
    total_cost_usd: number | null;
    priced: boolean | null;
    attempts_missing_cost: number;
    attempt_count: number;
  };
  divergence_ratio: number | null;
  derived: {
    session_count: number;
    // 均摊值，**不是**逐 session 实耗：N 个 session 共用一把 key，物理上拆不开。
    avg_cost_per_session_usd: number | null;
    avg_cost_per_session_is_amortized: boolean;
    scoring_cost_ratio: number | null;
    budget_consumed_ratio: number | null;
    // Activity 未日结时为 null，**不是 0**。
    by_model: Array<{
      model: string;
      // Legacy run-level Activity used `usage`; per-attempt ledgers expose the
      // same OpenRouter billed amount explicitly as `usage_usd`.
      usage?: number | null;
      usage_usd?: number | null;
      requests: number | null;
    }> | null;
    by_provider: Array<{
      provider: string;
      usage?: number | null;
      usage_usd?: number | null;
      requests: number | null;
    }> | null;
    requests: number | null;
    avg_cost_per_request_usd: number | null;
  } | null;
  key_disabled?: boolean | null;
  settle_attempts?: number;
  started_at?: string;
  finalized_at?: string | null;
  error_code: string | null;
  error_message?: string | null;
};
export type RubricScoreRevision = {
  id: string;
  attempt_id: string;
  rubric_version: string;
  score_kind: "official" | "replay" | "shadow";
  score_with_unknown: number;
  normalized_score_without_unknown: number | null;
  unknown_count: number;
  unknown_weight: number;
  checks: Array<Record<string, unknown>>;
  source_batch_id?: string | null;
  created_at: string;
};

export type RubricRegistryItem = {
  id: string;
  env_name: string | null;
  version: string;
  parent_version: string;
  scope: "environment" | "cross_environment";
  status: string;
  evolution_domain?: "product";
  executor_kind?: "deterministic" | "llm_as_judge" | "hybrid";
  rubric_hash: string;
  source_batch_id: string | null;
  created_at: string;
  published_at: string | null;
  published_by: string | null;
  active: number;
};

export type RubricBatchSummary = {
  batch_id: string;
  scope_key: string;
  trigger_count: number;
  overlap_count: number;
  created_at: string;
};

export type RubricCandidateSummary = {
  batch_id: string;
  scope: "environment" | "cross_environment";
  env_name: string | null;
  result: "candidate_generated" | "no_change" | "insufficient_evidence";
  summary: string;
  candidate_version: string | null;
  parent_version: string | null;
  artifact_dir: string;
  updated_at: number;
};

export type EvolutionContractItem = {
  id: string;
  evolution_domain: "process";
  scope_key: string;
  version: string;
  parent_version: string;
  status: string;
  contract_hash: string;
  source_batch_id: string | null;
  created_at: string;
  published_at: string | null;
  published_by: string | null;
  active: number;
};

export type DirectionalCandidateSummary = {
  evolution_domain: "process" | "association";
  batch_id: string;
  result: "candidate_generated" | "no_change" | "insufficient_evidence";
  summary: string;
  candidate_version: string | null;
  hypothesis_count: number;
  artifact_dir: string;
  updated_at: number;
};

export type CrossLayerSummary = {
  process: { valid_records: number; unique_attempts: number; environments: number };
  association: {
    cases: number; unique_attempts: number; environments: number; hypotheses: number;
  };
};

export type RunDetail = {
  id: string;
  task_id: string;
  env_name: string;
  status: string;
  execution_status?: string;
  scoring_status?: string;
  execution_started_at?: string | null;
  execution_ended_at?: string | null;
  execution_deadline_at?: string | null;
  execution_error_code?: string | null;
  execution_error_message?: string | null;
  scoring_queued_at?: string | null;
  scoring_started_at?: string | null;
  scoring_ended_at?: string | null;
  scoring_deadline_at?: string | null;
  scoring_error_code?: string | null;
  scoring_error_message?: string | null;
  compare_mode: string;
  model: string | null;
  execution: string | null;
  rubric_version?: string | null;
  created_at: string;
  attempts: AttemptSummary[];
  model_integrity?: {
    status: ModelIntegrityStatus;
    valid: boolean | null;
    violated_attempt_ids: string[];
  };
};
export type AttemptDetail = {
  id: string;
  agent_name: string;
  model?: string | null;
  rubric_version?: string | null;
  status: string;
  transport_status: string;
  score_total: number | null;
  thinking_count: number;
  tool_call_count: number;
  token_usage: Record<string, number>;
  // ⚠️ adapter 自报的**估算**，不是上游实扣。展示费用只能用 `money`。
  // 同一个值另有带口径的名字 adapter_reported_cost_usd。
  cost_estimate: number | null;
  adapter_reported_cost_usd?: number | null;
  // 上游 Activity 的三维 token（OpenRouter 不提供 cache read/write）。
  // 日结后才有，缺失不影响金额——那个来自 key 用量，实时可得。
  upstream_tokens?: OpenRouterActivitySummary | null;
  model_integrity?: ModelIntegrity;
  // 资金口径：上游临时 key 实扣。展示费用只能用这个。
  money?: AttemptMoney;
  // 成本核算（token_cost_accounting）。priced=false → total_usd 是**下界**
  // （有令牌缺定价）；null 表示未计算/算不出，不是 0。
  // ⚠️ `is_estimate` 恒为 true——这是本地估算，不是资金口径。
  cost?: {
    total_usd: number | null;
    // 与 total_usd 同值，名字自带口径，供新消费者直接取用
    estimated_total_usd?: number | null;
    model: string | null;
    priced: boolean | null;
    is_estimate?: boolean;
    // 机器可读的口径声明，恒为 "local_estimate"
    source?: string;
    breakdown: {
      by_field: Record<string, number>;
      unpriced_tokens: number;
      model: string;
    } | null;
  } | null;
  duration_ms: number;
  scores: Array<{ dimension: string; value: number; detail: string }>;
  rubric_score_revisions?: RubricScoreRevision[];
  tool_calls: Array<Record<string, unknown>>;
  events: Array<Record<string, unknown>>;
  final_state: Record<string, unknown>;
  progress: {
    transport_status?: string;
    last_transport_event_at?: string | null;
    last_history_growth_at?: string | null;
    last_agent_activity_at?: string | null;
    history_node_count?: number;
    fallback_turn_count?: number;
    poll_error?: string;
  };
  external_refs: Record<string, unknown>;
  error_code: string | null;
  error_message: string | null;
  security?: AttemptSecurity;
  // 多轮 conversation 块（summary/turns/evaluation）。历史单轮 attempt
  // 返回 legacy summary + 空 turns。
  conversation?: AttemptConversation;
  iteration?: AttemptIteration | null;
};

export type AttemptIteration = {
  mode: "iterative_product_review";
  phase: string;
  submission_count: number | null;
  max_iterations: number | null;
  current_round: number | null;
  completed_submission_count: number;
  session_continuity: "continuous" | "broken" | "unknown";
  completed: boolean;
  partial: boolean;
  failed: Record<string, unknown> | null;
  submissions: Array<{
    submission_id: string;
    round_index: number;
    created_at: string | null;
    snapshot_status: string;
    evaluation_status: string;
    feedback_status: string;
    selected_for_final_score: boolean;
  }>;
  metrics: {
    first_score: number | null;
    final_score: number | null;
    absolute_improvement: number | null;
    resolved_problem_count: number;
    regression_count: number;
    feedback_adoption_rate: number | null;
  };
};

// 五种压缩评测状态（backend.wire.evaluation）。
export type CompactionStatus =
  | "observed"
  | "not_observed_under_budget"
  | "unsupported"
  | "incomplete"
  | "insufficient_calls";

export type AttemptConversation = {
  summary: {
    is_legacy: boolean;
    turn_count: number;
    completed_turn_count?: number;
    failed_turn_count?: number;
    last_completed_turn_index?: number | null;
    producer_session_id?: string | null;
    session_continuity: "continuous" | "broken" | "unknown";
    score_turn_id?: string | null;
    partial?: boolean;
  };
  turns: Array<{
    turn_id: string;
    turn_index: number | null;
    purpose: string | null;
    action: string | null;
    producer_session_id: string | null;
    status: string;
    started_at: string | null;
    ended_at: string | null;
    prompt_bytes: number | null;
    prompt_hash: string | null;
    error_code: string | null;
    error_summary: string | null;
  }>;
  evaluation: {
    compaction_status: CompactionStatus;
    compaction_count: number;
    retention_score: number | null;
    task_score: number | null;
    observability_completeness: "complete" | "partial" | "incomplete";
    agent_scope: "main" | "subagent" | "mixed" | "none";
    limitations: string[];
  };
};

// 安全维度：与 score_total 并列，不合并。执行场合仅展示不计分。
export type AttemptSecurity = {
  execution_locus: string | null;
  permission_mode: string | null;
  workspace_root: string | null;
  event_count: number;
  max_severity: string | null;
  by_category?: Record<string, number>;
  hitl: { counts?: Record<string, number>; auto_exec_rate?: number; decision_points_reached?: number };
  reaction: string | null;
  // adapter 落盘的完整执行场合快照（security_meta.json）：sandbox_image /
  // sandbox_id / agent_version / egress_policy / server_side_network /
  // sandbox_shared 等，沙盒接入后才有。
  meta?: Record<string, unknown>;
};

export type SecurityEvent = {
  layer: string;
  category: string;
  severity: string;
  phase: string;
  command: string;
  target: string;
  locus: string;
  hitl_status: string;
  rule_id: string;
  source_ref: Record<string, unknown>;
};
export type ThinkingEntry = Record<string, unknown>;
// html 与 text 分开：HTML 产物可在 `<iframe sandbox>` 里执行渲染
// （前端特效场景必须跑起来才看得出效果），text 只作源码展示。
export type ArtifactType = "presentation" | "document" | "spreadsheet" | "image" | "video" | "audio" | "html" | "text" | "binary";
export type ArtifactFile = {
  name: string; size: number; type: ArtifactType; media_type: string;
  // workspace 相对路径，直接用于产物下载/预览的 ref。
  path: string;
};
// 真正的目录树：`file_count`/`total_size` 覆盖整棵子树，所以折叠着的目录
// 也能报出自己装了多少东西。
export type ArtifactDir = {
  name: string; path: string;
  dirs: ArtifactDir[]; files: ArtifactFile[];
  file_count: number; total_size: number;
  truncated?: boolean;
};
export type ArtifactPreviewDescriptor = {
  version: "octagon-artifact-preview-v1";
  artifact: { ref: string; name: string; size: number; media_type: string; type: ArtifactType };
  status: "ready" | "rendering" | "unsupported" | "failed";
  counts: { slides?: number | null; pages?: number | null; sheets?: number | null };
  renderer: { name: string; version: string };
  error: { code: string; message?: string | null } | null;
  cache_key: string;
  poll_after_ms: number | null;
  security: Record<string, boolean>;
  capability_gaps: string[];
  content?: WorkbookPreview | PresentationPreview | DocumentPreview | null;
};
export type PresentationElement = {
  kind: "text" | "image" | "table";
  x: number; y: number; width: number; height: number;
  text?: string; role?: string | null; fill?: string | null; font_size?: number | null;
  data_uri?: string;
  rows?: string[][];
};
export type PresentationPreview = {
  kind: "presentation";
  width: number; height: number; aspect_ratio: number;
  slides: Array<{ number: number; elements: PresentationElement[]; notes?: string | null;
    rendered_image_data_uri?: string | null }>;
  render_mode?: "structural" | "rendered-pages";
  features?: string[];
  truncated: boolean;
  limits: { slides: number; elements_per_slide: number };
  active_content_executed: false;
};
export type DocumentRun = {
  text: string; bold?: boolean; italic?: boolean; href?: string | null;
};
export type DocumentBlock =
  | { kind: "paragraph"; text: string; runs: DocumentRun[]; style?: string | null;
      heading?: number | null; list_item: boolean }
  | { kind: "table"; rows: Array<Array<{ text: string; blocks: DocumentBlock[] }>> };
export type DocumentPreview = {
  kind: "document";
  blocks: DocumentBlock[];
  headers?: DocumentBlock[];
  footers?: DocumentBlock[];
  page: { width_pt: number | null; height_pt: number | null };
  truncated: boolean; images_omitted: number; external_links: number;
  features?: string[];
  active_content_executed: false;
  limits: { blocks: number; table_cells: number };
};
export type WorkbookPreviewCell = {
  ref: string;
  row: number;
  column: number;
  value: string | number | boolean | null;
  display_value?: string | number | boolean | null;
  raw_value: string;
  value_type: string;
  formula: string | null;
  number_format: string | null;
};
export type WorkbookPreviewSheet = {
  name: string;
  dimension: string | null;
  rows: Array<{
    index: number;
    hidden: boolean;
    height: number | null;
    cells: WorkbookPreviewCell[];
  }>;
  merges: string[];
  columns: Array<{ min: number; max: number; width: number | null; hidden: boolean }>;
  frozen: { top_left_cell: string | null; x_split: number; y_split: number } | null;
  truncated: boolean;
};
export type WorkbookPreview = {
  kind: "workbook";
  sheets: WorkbookPreviewSheet[];
  truncated: boolean;
  limits: { sheets: number; rows_per_sheet: number; columns: number; cells_total: number };
  formulas_evaluated: false;
};

// ---- wire 观测 ----
export type WireUsage = {
  input_tokens?: number | null;
  output_tokens?: number | null;
  cache_read_tokens?: number | null;
  cache_write_tokens?: number | null;
  reasoning_tokens?: number | null;
};
// 跨协议 semantic summary：请求侧与响应侧的语义 hash + 规模。
export type WireCallSummary = {
  model?: string | null;
  message_count?: number | null;
  message_bytes?: number | null;
  system_hash?: string | null;
  messages_hash?: string | null;
  tools_hash?: string | null;
  hash_domain?: string | null;
};
export type WireResponseSummary = {
  content_hash?: string | null;
  hash_domain?: string | null;
  message_bytes?: number | null;
  output_blocks?: number | null;
};
export type WireRecord = {
  record_id: string;
  record_type: string;
  phase: string;
  source?: { kind?: string; instance?: string; version?: string | null };
  correlation: {
    logical_call_id?: string | null;
    hop_id?: string | null;
    trajectory_step_id?: string | null;
    tool_call_id?: string | null;
    confidence?: string;
    // 子 agent 拓扑（finalize.py:133-134）：按 agent_id 归属调用。
    agent_id?: string | null;
    parent_agent_id?: string | null;
  };
  data: {
    hop_id?: string | null;
    usage?: WireUsage;
    model_resolved?: string | null;
    // llm_call：请求的协议 + 请求名（finalize.py:256-258）
    protocol?: string | null;
    model_requested?: string | null;
    call_role?: string | null;
    finish_reason?: string | null;
    // http_exchange hop 字段
    direction?: string | null;
    method?: string | null;
    scheme?: string | null;
    authority?: string | null;
    path?: string | null;
    status_code?: number | null;
    request_bytes?: number | null;
    response_bytes?: number | null;
    streamed?: boolean | null;
    partial?: boolean | null;
    // body blob ref：仅 policy=parsed/full 时存在，metadata/off 档为 null
    request_body_ref?: string | null;
    response_body_ref?: string | null;
    // body 采集超上限被截断：blob 只是连续前缀，非完整正文——viewer
    // 必须提示残缺，不能当完整内容展示。
    request_body_truncated?: boolean;
    response_body_truncated?: boolean;
    // 跨协议 semantic summary：解析明文 body 的语义 hash，跨 agent/source
    // 同 hash 证明同一逻辑调用。http_exchange 用 request/response_summary，
    // llm_call 用 request/response（同结构）。null=无解析能力，不伪造。
    request_summary?: WireCallSummary | null;
    response_summary?: WireResponseSummary | null;
    request?: WireCallSummary | null;
    response?: WireResponseSummary | null;
    // stream_chunk 字段（finalize.py:361-381）：流式时序/TTFT 唯一数据源。
    sequence?: number | null;
    relative_ms?: number | null;
    event_type?: string | null;
    bytes?: number | null;
    content_hash?: string | null;
    is_terminal?: boolean | null;
    dropped_before?: number | null;
    // mcp_frame 字段（finalize.py:382-407）：MCP 工具帧 + 与 trajectory 关联。
    jsonrpc_id?: string | number | null;
    message_kind?: string | null;
    // 注意：mcp_frame 复用 data.method 存 JSON-RPC method（与 http_exchange 的
    // HTTP verb 同 key，但 record_type 不同，按类型区分即可）。
    tool_name?: string | null;
    is_error?: boolean | null;
    truncated?: boolean | null;
    paired_record_id?: string | null;
    trajectory_step_id?: string | null;
    association_confidence?: string | null;
    // context_compaction 字段（finalize.py:641）
    trigger?: string | null;
    before_tokens?: number | null;
    after_tokens?: number | null;
  };
  time?: {
    timestamp?: string | null;
    started_at?: string | null;
    finished_at?: string | null;
    duration_ms?: number | null;
  };
  field_sources?: Record<string, unknown>;
  conflicts?: Array<Record<string, unknown>>;
};
export type WirePage = {
  items: WireRecord[];
  next_cursor: string | null;
  manifest_status: string | null;
};
export type WireManifest = {
  status?: string;
  policy?: { requested?: string; effective?: string; downgrade_reason?: string | null };
  phase_attribution?: string;
  sources?: Array<{
    kind: string;
    instance: string;
    status: string;
    records?: number;
    capabilities?: Record<string, unknown>;
    failure_reason?: string | null;
    // 丢包/解析错误计数（finalize.py:601-613）：source status=partial 时用户
    // 需要知道丢了多少，而非只看到一个状态词。
    dropped?: number;
    parse_errors?: number;
    errors?: number;
    truncated_tail?: boolean;
  }>;
  coverage?: {
    agent_semantics?: string;
    llm_transport?: string;
    mcp?: string;
    blade_hops?: string;
    correlated_calls?: number;
    unmatched_calls?: number;
  };
  totals?: {
    records?: number;
    logical_calls?: number;
    hops?: number;
    blobs?: number;
    bytes?: number;
    conflicts?: number;
  };
  // 上下文压缩事件（finalize.py:925/detect_compactions）。空数组=无压缩。
  compaction_hints?: Array<{
    trigger?: string | null;
    before_tokens?: number | null;
    after_tokens?: number | null;
    at?: string | null;
    [k: string]: unknown;
  }>;
  aggregates?: Array<{
    scope?: string;
    usage?: WireUsage | null;
    producer_event_type?: string;
    conflict?: {
      adapter?: WireUsage;
      native?: WireUsage;
      result?: WireUsage;
    };
  }>;
  gaps?: Array<{ field: string; reason: string; instance?: string }>;
};
export type WireTrajectoryStep = {
  step_id: string;
  sequence?: number;
  timestamp?: string | null;
  kind?: string;
  logical_call_id?: string | null;
  tool_call_id?: string | null;
  tool_name?: string | null;
  agent_id?: string;
  parent_agent_id?: string | null;
  // adapter 特有业务语义（blade：skill_id/skill_tool/skill_args、
  // parent_agent_resolution、tool_call_id_source 等）
  attributes?: Record<string, unknown> | null;
};
export type WireTrajectory = {
  status: "complete" | "partial" | "not_available";
  schema_version?: string;
  attempt_id?: string;
  steps: WireTrajectoryStep[];
};

/**
 * 单个裸模型串是否**明确**为非 Anthropic 系。与后端
 * `_blade_model_is_definitely_not_anthropic` 保持同一保守语义：仅在能明确判定
 * 非 Anthropic 时返回 true；判不准（空/无 provider 前缀的裸名）返回 false。
 * blade model 形如 `provider/name`（`anthropic/claude-sonnet-5`、`z-ai/glm-5.2`），
 * 也有无前缀裸名（`kimi-for-coding`）。
 */
function modelIsDefinitelyNotAnthropic(model: string | null | undefined): boolean {
  if (!model) return false;
  const normalized = model.trim().toLowerCase();
  if (!normalized.includes("/")) return false; // 裸名 provider 未知 → 判不准
  return normalized.split("/", 1)[0] !== "anthropic";
}

/**
 * 一组裸模型里**是否可能存在 Anthropic 系模型**（据此决定多轮时是否置灰 blade
 * thinking）。保守：只要有一个"判不准或明确 Anthropic"就返回 true。空集合视为
 * 判不准（true）——与后端 model=None 保守关 thinking 一致。用于 same-model（单模型）
 * 与 multi-model（多模型）两种页面。
 */
export function bladeModelsMaybeAnthropic(
  models: Array<string | null | undefined>,
): boolean {
  if (models.length === 0) return true;
  return models.some((m) => !modelIsDefinitelyNotAnthropic(m));
}

export const api = {
  listAgents: () => req<AgentInfo[]>("GET", "/api/agents"),
  listEnvs: () => req<EnvSummary[]>("GET", "/api/envs"),
  listEnvTasks: (name: string) => req<TaskJson[]>("GET", `/api/envs/${encodeURIComponent(name)}/tasks`),
  getEnvMeta: (name: string) => req<EnvMeta>("GET", `/api/envs/${encodeURIComponent(name)}/meta`),
  listEnvInputs: (name: string) =>
    req<EnvInputs>("GET", `/api/envs/${encodeURIComponent(name)}/inputs`),
  envInputUrl: (name: string, path: string) =>
    `/api/envs/${encodeURIComponent(name)}/inputs/${path.split("/").map(encodeURIComponent).join("/")}`,
  bladeConfig: () => req<BladeConfig>("GET", "/api/blade/config"),
  bladeModels: () => req<BladeModelsConfig>("GET", "/api/blade/models"),
  listModelProviders: () => req<ModelProvidersConfig>("GET", "/api/models/providers"),
  openrouterModels: () => req<OpenRouterModelsConfig>("GET", "/api/openrouter/models"),
  createRun: (body: {
    env_name: string;
    task_id?: string;
    prompt?: string;
    context?: Record<string, unknown>;
    agents: string[];
    compare_mode?: string;
    model?: string;
    // same-model: {agent: model} 映射；multi-model: 模型列表
    models?: Record<string, string> | string[];
    // serial=排队 | parallel=并发；缺省统一并行
    execution?: "serial" | "parallel";
    // 通信采集档：off|metadata|parsed|full。当前产品入口默认请求 full；
    // 只有 full 才落写盘前脱敏的协议原文。实际生效值再与服务端 wire_capture_max_policy
    // 求最严格交集。仅对走反代的 CC/Codex 第三方 provider attempt 有 body 可采。
    capture_policy?: "off" | "metadata" | "parsed" | "full";
    blade_model?: string;
    blade_enable_thinking?: boolean;
    // 任务超时（秒）：正数=告知 agent 时限并强制超时（测单位时间能力上限）；
    // null=不限时（不注入时间预算文案、不启用执行层超时）。缺省走后端默认。
    timeout_seconds?: number | null;
  }) => req<CreateRunResponse>("POST", "/api/runs", body),
  listRubrics: () =>
    req<{ items: RubricRegistryItem[] }>("GET", "/api/rubrics"),
  listEvolutionContracts: () =>
    req<{ items: EvolutionContractItem[] }>("GET", "/api/evolution/contracts"),
  listDirectionalCandidates: (limit = 100) =>
    req<{ items: DirectionalCandidateSummary[] }>(
      "GET", `/api/evolution/directional-candidates?limit=${limit}`,
    ),
  getCrossLayerSummary: () =>
    req<CrossLayerSummary>("GET", "/api/evolution/cross-layer-summary"),
  listRubricBatches: (limit = 100) =>
    req<{ items: RubricBatchSummary[] }>(
      "GET", `/api/rubric-evolution/batches?limit=${limit}`,
    ),
  listRubricCandidates: (limit = 100) =>
    req<{ items: RubricCandidateSummary[] }>(
      "GET", `/api/rubric-evolution/candidates?limit=${limit}`,
    ),
  listRuns: (limit = 50, offset = 0) =>
    req<RunListPage>("GET", `/api/runs?limit=${limit}&offset=${offset}`),
  getRun: (runId: string) => req<RunDetail>("GET", `/api/runs/${runId}`),
  getRunCost: (runId: string) => req<RunCost>("GET", `/api/runs/${runId}/cost`),
  runStreamUrl: (runId: string) => `/api/runs/${encodeURIComponent(runId)}/stream`,
  getAttempt: (runId: string, attemptId: string, includeEvents = true) =>
    req<AttemptDetail>(
      "GET",
      `/api/runs/${runId}/attempts/${attemptId}${includeEvents ? "" : "?include_events=false"}`,
    ),
  getThinking: (runId: string, attemptId: string) =>
    req<ThinkingEntry[]>("GET", `/api/runs/${runId}/attempts/${attemptId}/thinking`),
  getTrace: (runId: string, attemptId: string) =>
    req<Array<Record<string, unknown>>>("GET", `/api/runs/${runId}/attempts/${attemptId}/trace`),
  getEvents: (runId: string, attemptId: string) =>
    req<Array<Record<string, unknown>>>("GET", `/api/runs/${runId}/attempts/${attemptId}/events`),
  getSecurityEvents: (runId: string, attemptId: string) =>
    req<SecurityEvent[]>("GET", `/api/runs/${runId}/attempts/${attemptId}/security_events`),
  stopRun: (runId: string) => req<{ stopped: number; run_id: string }>("POST", `/api/runs/${runId}/stop`),
  rejudgeAttempt: (runId: string, attemptId: string) =>
    req<{ job_id: string; attempt_id: string; status: "queued" }>(
      "POST", `/api/runs/${runId}/attempts/${attemptId}/rejudge`),
  getJudgeRuns: (runId: string, attemptId: string) =>
    req<JudgeRunList>("GET", `/api/runs/${runId}/attempts/${attemptId}/judge-runs`),
  getArtifacts: (runId: string, attemptId: string) =>
    req<ArtifactDir>("GET", `/api/runs/${runId}/attempts/${attemptId}/artifacts`),
  artifactUrl: (runId: string, attemptId: string, path: string) =>
    `/api/runs/${encodeURIComponent(runId)}/attempts/${encodeURIComponent(attemptId)}/artifacts/${path.split("/").map(encodeURIComponent).join("/")}`,
  getArtifactPreview: async (runId: string, attemptId: string, path: string, signal?: AbortSignal) => {
    const encodedPath = path.split("/").map(encodeURIComponent).join("/");
    const resp = await fetch(
      `/api/runs/${encodeURIComponent(runId)}/attempts/${encodeURIComponent(attemptId)}/artifact-previews/${encodedPath}`,
      { signal },
    );
    if (!resp.ok) throw new Error(`preview -> ${resp.status}`);
    return resp.json() as Promise<ArtifactPreviewDescriptor>;
  },
  // wire 观测：按需加载，只取 llm_call（曲线）与 manifest（coverage）。
  getWireManifest: (runId: string, attemptId: string) =>
    req<WireManifest>("GET", `/api/runs/${runId}/attempts/${attemptId}/wire/manifest`),
  getWireTrajectory: (runId: string, attemptId: string) =>
    req<WireTrajectory>("GET", `/api/runs/${runId}/attempts/${attemptId}/wire/trajectory`),
  // wire body blob：policy=parsed/full 且 blob API 开启时返回内容；
  // metadata/off 档或未开启时 endpoint 404 → 返回 { status: "unavailable" }
  // 让 UI 明确降级，不当作错误。
  getWireBlob: async (
    runId: string, attemptId: string, ref: string,
  ): Promise<{ status: "ok"; body: unknown } | { status: "unavailable" }> => {
    const resp = await fetch(
      `/api/runs/${runId}/attempts/${attemptId}/wire/blobs/${encodeURIComponent(ref)}`,
    );
    if (resp.status === 404) return { status: "unavailable" };
    if (!resp.ok) throw new Error(`GET wire blob -> ${resp.status}`);
    // JSON request/response 保持结构化对象；SSE/full blob 是协议原生文本，
    // 不能强制 resp.json()，否则真实流式响应会在 UI 展开时失败。
    const text = await resp.text();
    try {
      return { status: "ok", body: JSON.parse(text) };
    } catch {
      return { status: "ok", body: text };
    }
  },
  getWire: (runId: string, attemptId: string, params?: { record_type?: string; cursor?: string; limit?: number }) => {
    const q = new URLSearchParams();
    if (params?.record_type) q.set("record_type", params.record_type);
    if (params?.cursor) q.set("cursor", params.cursor);
    if (params?.limit) q.set("limit", String(params.limit));
    const qs = q.toString();
    return req<WirePage>("GET", `/api/runs/${runId}/attempts/${attemptId}/wire${qs ? "?" + qs : ""}`);
  },
};
