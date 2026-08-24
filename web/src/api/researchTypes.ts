export type ResearchFeature =
  | "experiments"
  | "task_variants"
  | "run_groups"
  | "leader_events"
  | "robustness"
  | "profiles"
  | "auto_profile"
  | "insights"
  | "research_feedback"
  | "normalized_output"
  | "attack_coverage";

export type CapabilityDetail = {
  enabled: boolean;
  schema_ready: boolean;
  dependencies_ready: boolean;
  unavailable_reasons: string[];
};

export type ResearchCapabilities = {
  schema_version: "octagon-capabilities-v1";
  features: Record<string, boolean>;
  details: Record<string, CapabilityDetail>;
};

export type ExperimentDto = {
  id: string;
  title: string;
  env_name: string;
  question: string;
  status: string;
  protocol_json?: string;
};

export type VariantDto = {
  id: string;
  kind: string;
  mutator_id: string;
  mutator_version: string;
  seed: number;
  status: string;
};

export type RunGroupCellDto = {
  id: string;
  variant_id: string;
  repeat_index: number;
  run_id: string | null;
  status: string;
  error_code: string | null;
  kind?: string;
  mutator_id?: string;
  mutator_version?: string;
  seed?: number;
  source_hash?: string;
  content_hash?: string;
};

export type RunGroupDto = {
  id: string;
  experiment_id: string;
  strategy: string;
  status: string;
  total_cells: number;
  completed_cells: number;
  partial_cells: number;
  failed_cells: number;
  cancelled_cells: number;
};

export type LeaderEventDto = {
  schema_version: "octagon-leader-event-v1";
  event_id: string;
  scope_key: string;
  sequence: number;
  metric: string;
  previous_attempt_id: string | null;
  current_attempt_id: string | null;
  previous_value: number | null;
  current_value: number | null;
  delta: number | null;
  reason: string;
  provisional: boolean;
  source_score_fingerprint: string;
  producer_version: string;
  source_outbox_id: string;
  created_at?: string;
};

export type RunGroupSnapshot = {
  schema_version: "octagon-run-group-snapshot-v1";
  cursor: number;
  group: RunGroupDto;
  cells: RunGroupCellDto[];
  leaders: LeaderEventDto[];
};

export type RunGroupEvent = {
  schema_version: "octagon-run-group-event-v1";
  sequence: number;
  event_type: string;
  created_at?: string;
  data: Record<string, unknown> | RunGroupSnapshot;
};

export type LeaderDto = {
  scope_key: string;
  sequence: number;
  metric: string;
  leader: { candidate_id: string; value: number } | null;
  provisional: boolean;
};

export type AggregateDto = {
  sample_count: number;
  expected_count: number;
  mean: number | null;
  minimum: number | null;
  labels: string[];
};

export type InsightDto = {
  id: string;
  experiment_id: string;
  version: number;
  status: string;
  schema_version: string;
};

export class ResearchProtocolError extends Error {
  constructor(public readonly path: string, message: string) {
    super(`${path}: ${message}`);
    this.name = "ResearchProtocolError";
  }
}

export type BoundaryResult<T> =
  | { ok: true; value: T }
  | { ok: false; error: ResearchProtocolError };

function objectAt(value: unknown, path: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new ResearchProtocolError(path, "expected object");
  }
  return value as Record<string, unknown>;
}

function stringAt(value: unknown, path: string): string {
  if (typeof value !== "string") throw new ResearchProtocolError(path, "expected string");
  return value;
}

function numberAt(value: unknown, path: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new ResearchProtocolError(path, "expected finite number");
  }
  return value;
}

function booleanAt(value: unknown, path: string): boolean {
  if (typeof value !== "boolean") throw new ResearchProtocolError(path, "expected boolean");
  return value;
}

function nullableStringAt(value: unknown, path: string): string | null {
  if (value === null) return null;
  return stringAt(value, path);
}

function nullableNumberAt(value: unknown, path: string): number | null {
  if (value === null) return null;
  return numberAt(value, path);
}

export function boundary<T>(parser: (value: unknown) => T, value: unknown): BoundaryResult<T> {
  try {
    return { ok: true, value: parser(value) };
  } catch (error) {
    return {
      ok: false,
      error: error instanceof ResearchProtocolError
        ? error
        : new ResearchProtocolError("$", "unexpected payload"),
    };
  }
}

export function parseCapabilities(value: unknown): ResearchCapabilities {
  const root = objectAt(value, "$capabilities");
  if (root.schema_version !== "octagon-capabilities-v1") {
    throw new ResearchProtocolError("$capabilities.schema_version", "unsupported version");
  }
  const featuresObject = objectAt(root.features, "$capabilities.features");
  const detailsObject = objectAt(root.details, "$capabilities.details");
  const features = Object.fromEntries(
    Object.entries(featuresObject).map(([name, enabled]) => [
      name,
      booleanAt(enabled, `$capabilities.features.${name}`),
    ]),
  );
  const details = Object.fromEntries(
    Object.entries(detailsObject).map(([name, raw]) => {
      const item = objectAt(raw, `$capabilities.details.${name}`);
      if (!Array.isArray(item.unavailable_reasons)) {
        throw new ResearchProtocolError(
          `$capabilities.details.${name}.unavailable_reasons`,
          "expected array",
        );
      }
      return [name, {
        enabled: booleanAt(item.enabled, `$capabilities.details.${name}.enabled`),
        schema_ready: booleanAt(item.schema_ready, `$capabilities.details.${name}.schema_ready`),
        dependencies_ready: booleanAt(
          item.dependencies_ready,
          `$capabilities.details.${name}.dependencies_ready`,
        ),
        unavailable_reasons: item.unavailable_reasons.map((reason, index) =>
          stringAt(reason, `$capabilities.details.${name}.unavailable_reasons[${index}]`)),
      }];
    }),
  );
  return { schema_version: "octagon-capabilities-v1", features, details };
}

function parseCell(value: unknown, index: number): RunGroupCellDto {
  const path = `$snapshot.cells[${index}]`;
  const item = objectAt(value, path);
  return {
    id: stringAt(item.id, `${path}.id`),
    variant_id: stringAt(item.variant_id, `${path}.variant_id`),
    repeat_index: numberAt(item.repeat_index, `${path}.repeat_index`),
    run_id: nullableStringAt(item.run_id, `${path}.run_id`),
    status: stringAt(item.status, `${path}.status`),
    error_code: nullableStringAt(item.error_code, `${path}.error_code`),
    ...(typeof item.kind === "string" ? { kind: item.kind } : {}),
    ...(typeof item.mutator_id === "string" ? { mutator_id: item.mutator_id } : {}),
    ...(typeof item.mutator_version === "string" ? { mutator_version: item.mutator_version } : {}),
    ...(typeof item.seed === "number" ? { seed: item.seed } : {}),
    ...(typeof item.source_hash === "string" ? { source_hash: item.source_hash } : {}),
    ...(typeof item.content_hash === "string" ? { content_hash: item.content_hash } : {}),
  };
}

function parseLeader(value: unknown, index: number): LeaderEventDto {
  const path = `$snapshot.leaders[${index}]`;
  const item = objectAt(value, path);
  if (item.schema_version !== "octagon-leader-event-v1") {
    throw new ResearchProtocolError(`${path}.schema_version`, "unsupported version");
  }
  return {
    schema_version: "octagon-leader-event-v1",
    event_id: stringAt(item.event_id, `${path}.event_id`),
    scope_key: stringAt(item.scope_key, `${path}.scope_key`),
    sequence: numberAt(item.sequence, `${path}.sequence`),
    metric: stringAt(item.metric, `${path}.metric`),
    previous_attempt_id: nullableStringAt(item.previous_attempt_id, `${path}.previous_attempt_id`),
    current_attempt_id: nullableStringAt(item.current_attempt_id, `${path}.current_attempt_id`),
    previous_value: nullableNumberAt(item.previous_value, `${path}.previous_value`),
    current_value: nullableNumberAt(item.current_value, `${path}.current_value`),
    delta: nullableNumberAt(item.delta, `${path}.delta`),
    reason: stringAt(item.reason, `${path}.reason`),
    provisional: booleanAt(item.provisional, `${path}.provisional`),
    source_score_fingerprint: stringAt(
      item.source_score_fingerprint,
      `${path}.source_score_fingerprint`,
    ),
    producer_version: stringAt(item.producer_version, `${path}.producer_version`),
    source_outbox_id: stringAt(item.source_outbox_id, `${path}.source_outbox_id`),
    ...(typeof item.created_at === "string" ? { created_at: item.created_at } : {}),
  };
}

export function parseRunGroupSnapshot(value: unknown): RunGroupSnapshot {
  const root = objectAt(value, "$snapshot");
  if (root.schema_version !== "octagon-run-group-snapshot-v1") {
    throw new ResearchProtocolError("$snapshot.schema_version", "unsupported version");
  }
  const group = objectAt(root.group, "$snapshot.group");
  if (!Array.isArray(root.cells)) {
    throw new ResearchProtocolError("$snapshot.cells", "expected array");
  }
  return {
    schema_version: "octagon-run-group-snapshot-v1",
    cursor: numberAt(root.cursor, "$snapshot.cursor"),
    group: {
      id: stringAt(group.id, "$snapshot.group.id"),
      experiment_id: stringAt(group.experiment_id, "$snapshot.group.experiment_id"),
      strategy: stringAt(group.strategy, "$snapshot.group.strategy"),
      status: stringAt(group.status, "$snapshot.group.status"),
      total_cells: numberAt(group.total_cells, "$snapshot.group.total_cells"),
      completed_cells: numberAt(group.completed_cells, "$snapshot.group.completed_cells"),
      partial_cells: numberAt(group.partial_cells, "$snapshot.group.partial_cells"),
      failed_cells: numberAt(group.failed_cells, "$snapshot.group.failed_cells"),
      cancelled_cells: numberAt(group.cancelled_cells, "$snapshot.group.cancelled_cells"),
    },
    cells: root.cells.map(parseCell),
    leaders: Array.isArray(root.leaders) ? root.leaders.map(parseLeader) : [],
  };
}

export function parseRunGroupEvent(value: unknown): RunGroupEvent {
  const root = objectAt(value, "$event");
  if (root.schema_version !== "octagon-run-group-event-v1") {
    throw new ResearchProtocolError("$event.schema_version", "unsupported version");
  }
  const data = objectAt(root.data, "$event.data");
  return {
    schema_version: "octagon-run-group-event-v1",
    sequence: numberAt(root.sequence, "$event.sequence"),
    event_type: stringAt(root.event_type, "$event.event_type"),
    ...(typeof root.created_at === "string" ? { created_at: root.created_at } : {}),
    data,
  };
}
