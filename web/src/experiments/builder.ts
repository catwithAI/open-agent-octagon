export type CompareMode = "multi-agent" | "same-model" | "multi-model";
export type CapturePolicy = "off" | "metadata" | "parsed" | "full";

export type AgentModel = { agent: string; model: string | null };
export type BuilderVariant = {
  schema_version: "octagon-variant-spec-v1";
  mutator: string;
  version: string;
  seed: number;
  intensity: string;
  params: Record<string, unknown>;
};

export type ExperimentProtocolDraft = {
  schema_version: "octagon-experiment-protocol-v1";
  compare_mode: CompareMode;
  profile: { id: string; version?: string; recommendation_id?: string } | null;
  agents: AgentModel[];
  variant_specs: BuilderVariant[];
  repeats: number;
  execution: "serial" | "parallel";
  max_concurrency: number | null;
  timeout_seconds: number | null;
  notify_model_of_timeout: boolean;
  capture_policy: CapturePolicy;
  leader: {
    schema_version: "octagon-leader-config-v1";
    scope: "variant-repeat";
    metric: "task_score";
    tie_break: "duration" | "candidate_id";
  };
  limits: { max_cells: number; max_attempts: number | null };
};

export type ProfileRecommendation = {
  recommendation_id: string;
  profile_id: string;
  profile_version: string;
};

export type BuilderValidation = {
  cells: number;
  attempts: number;
  blocking: string[];
};

export type MutationContractSummary = {
  supported_mutators?: readonly string[];
  conditional_mutators?: Readonly<Record<string, { requires?: readonly string[] }>>;
};

export function availableMutators(
  env: MutationContractSummary | null | undefined,
  taskContext: Readonly<Record<string, unknown>> | null | undefined,
): ReadonlySet<string> {
  const available = new Set(env?.supported_mutators ?? ["baseline"]);
  const mutation = taskContext?._mutation;
  const capabilities = new Set(
    mutation && typeof mutation === "object" && Array.isArray(
      (mutation as { capabilities?: unknown }).capabilities,
    )
      ? (mutation as { capabilities: unknown[] }).capabilities.filter(
        (item): item is string => typeof item === "string",
      )
      : [],
  );
  for (const [mutator, rule] of Object.entries(env?.conditional_mutators ?? {})) {
    const requires = rule.requires ?? [];
    if (requires.every((requirement) => capabilities.has(requirement))) {
      available.add(mutator);
    }
  }
  return available;
}

export function sameModelForAgent(
  agent: string,
  bareModel: string,
  agentPrefix: Readonly<Record<string, string>>,
  bladeModelIds: readonly string[],
): string {
  if (agent === "blade-agent") {
    const catalogMatch = bladeModelIds.find(
      (id) => id === bareModel || id.endsWith(`/${bareModel}`),
    );
    if (catalogMatch) return catalogMatch;
    // Blade 的上游模型使用原生 source 前缀。目录接口可能因鉴权/版本暂时
    // 不可用，而执行层本就允许 off-catalog passthrough；此时不能把裸
    // OpenRouter ID 直接交给 BA，否则会选不到 upstream profile。
    return bareModel.startsWith("upstream/") ? bareModel : `upstream/${bareModel}`;
  }
  const prefix = agentPrefix[agent];
  return prefix ? `${prefix}/${bareModel}` : bareModel;
}

export function cardinality(protocol: ExperimentProtocolDraft): BuilderValidation {
  const cells = protocol.variant_specs.length * protocol.repeats;
  const attempts = cells * protocol.agents.length;
  const blocking: string[] = [];
  if (protocol.variant_specs.filter((item) => item.mutator === "baseline").length !== 1) {
    blocking.push("exactly one baseline variant is required");
  }
  if (cells > protocol.limits.max_cells) blocking.push("cell limit exceeded");
  if (protocol.limits.max_attempts !== null && attempts > protocol.limits.max_attempts) {
    blocking.push("attempt limit exceeded");
  }
  if (protocol.compare_mode === "multi-model"
      && (protocol.agents.length < 2
        || new Set(protocol.agents.map((item) => item.agent)).size !== 1
        || protocol.agents.some((item) => !item.model))) {
    blocking.push("multi-model requires one agent and at least two explicit models");
  }
  return { cells, attempts, blocking };
}

export function acceptRecommendation(
  protocol: ExperimentProtocolDraft,
  recommendation: ProfileRecommendation,
  accepted: boolean,
): ExperimentProtocolDraft {
  if (!accepted) return protocol;
  return {
    ...protocol,
    profile: {
      id: recommendation.profile_id,
      version: recommendation.profile_version,
      recommendation_id: recommendation.recommendation_id,
    },
  };
}

export function legacyProtocol(
  mode: CompareMode,
  candidates: AgentModel[],
): ExperimentProtocolDraft {
  return {
    schema_version: "octagon-experiment-protocol-v1",
    compare_mode: mode,
    profile: null,
    agents: candidates,
    variant_specs: [{
      schema_version: "octagon-variant-spec-v1",
      mutator: "baseline",
      version: "1",
      seed: 0,
      intensity: "identity",
      params: {},
    }],
    repeats: 1,
    execution: "parallel",
    max_concurrency: null,
    timeout_seconds: null,
    notify_model_of_timeout: false,
    capture_policy: "full",
    leader: {
      schema_version: "octagon-leader-config-v1",
      scope: "variant-repeat",
      metric: "task_score",
      tie_break: "candidate_id",
    },
    limits: { max_cells: 1000, max_attempts: 32000 },
  };
}

function formatTimeBudget(seconds: number): string {
  if (seconds % 60 === 0) return `${seconds / 60} 分钟`;
  if (seconds < 60) return `${seconds} 秒`;
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}

export function timeBudgetNotice(seconds: number): string {
  return `本任务限时 ${formatTimeBudget(seconds)}。请合理分配时间：`
    + "先尽快产出一个可用/可提交的结果，再用剩余时间迭代优化以争取更高分。"
    + "时间到评测即结束，请确保届时已有最好结果。";
}

function visibleContext(context: Record<string, unknown>): Record<string, unknown> {
  const visible = Object.fromEntries(
    Object.entries(context).filter(([key]) =>
      key !== "uploaded_files" && !key.startsWith("_")
    ),
  );
  const uploaded = context.uploaded_files;
  if (Array.isArray(uploaded)) {
    const names = uploaded.flatMap((item) => {
      if (!item || typeof item !== "object") return [];
      const name = (item as { name?: unknown }).name;
      return typeof name === "string" && name ? [name] : [];
    });
    if (names.length > 0) visible["工作目录下的输入文件"] = names;
  }
  return visible;
}

export function modelVisiblePrompt(
  prompt: string,
  context: Record<string, unknown>,
  timeoutSeconds: number,
  notifyModelOfTimeout: boolean,
): string {
  const parts: string[] = [];
  if (notifyModelOfTimeout) parts.push(timeBudgetNotice(timeoutSeconds), "");
  parts.push(prompt);
  const shownContext = visibleContext(context);
  if (Object.keys(shownContext).length > 0) {
    parts.push("", "上下文:", JSON.stringify(shownContext, null, 2));
  }
  return parts.join("\n");
}

export function unsupportedMutators(
  protocol: ExperimentProtocolDraft,
  allowed: ReadonlySet<string>,
): string[] {
  return [...new Set(
    protocol.variant_specs
      .map((item) => item.mutator)
      .filter((mutator) => !allowed.has(mutator)),
  )].sort();
}
