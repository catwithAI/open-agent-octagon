import {
  boundary,
  parseCapabilities,
  parseRunGroupSnapshot,
  type BoundaryResult,
  type ResearchCapabilities,
  type ResearchFeature,
  type RunGroupSnapshot,
} from "./researchTypes";

export class FeatureUnavailableError extends Error {
  constructor(public readonly feature: ResearchFeature) {
    super(`research feature unavailable: ${feature}`);
    this.name = "FeatureUnavailableError";
  }
}

async function json(response: Response): Promise<unknown> {
  if (!response.ok) throw new Error(`research API -> ${response.status}`);
  return response.json() as Promise<unknown>;
}

export async function researchGet<T>(path: string): Promise<T> {
  return json(await fetch(path)) as Promise<T>;
}

export function featureEnabled(
  capabilities: ResearchCapabilities,
  feature: ResearchFeature,
): boolean {
  return capabilities.features[feature] === true;
}

// AppShell 与几乎每个页面组件都各自调用一次 discoverCapabilities（互不知情），
// 未去重时同一次导航会并发打出多份 /api/capabilities 请求。TTL 内的重复调用
// 复用同一个 in-flight/已完成 promise；capabilities 在会话内基本不变，短 TTL
// 只是防止跨导航的陈旧读取。
let capabilitiesCache: { promise: Promise<BoundaryResult<ResearchCapabilities>>; at: number } | null = null;
const CAPABILITIES_CACHE_TTL_MS = 30_000;

export function discoverCapabilities(): Promise<BoundaryResult<ResearchCapabilities>> {
  const now = Date.now();
  if (capabilitiesCache && now - capabilitiesCache.at < CAPABILITIES_CACHE_TTL_MS) {
    return capabilitiesCache.promise;
  }
  // Store the in-flight promise (not just the resolved value) before the
  // network call settles, so concurrent callers within the same tick share
  // one fetch instead of each starting their own.
  const promise = fetch("/api/capabilities")
    .then(json)
    .then((data) => boundary(parseCapabilities, data));
  capabilitiesCache = { promise, at: now };
  promise.catch(() => {
    // A failed fetch must not poison the cache for the retry window.
    if (capabilitiesCache?.promise === promise) capabilitiesCache = null;
  });
  return promise;
}

/** Test-only: force the next discoverCapabilities() call to hit the network. */
export function _resetCapabilitiesCacheForTests(): void {
  capabilitiesCache = null;
}

export async function getRunGroup(
  experimentId: string,
  groupId: string,
): Promise<BoundaryResult<RunGroupSnapshot>> {
  const path = `/api/experiments/${encodeURIComponent(experimentId)}/groups/${encodeURIComponent(groupId)}`;
  return boundary(parseRunGroupSnapshot, await json(await fetch(path)));
}

export async function researchMutation<T>(
  capabilities: ResearchCapabilities,
  feature: ResearchFeature,
  path: string,
  body?: unknown,
  headers?: Record<string, string>,
): Promise<T> {
  if (!featureEnabled(capabilities, feature)) throw new FeatureUnavailableError(feature);
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...headers },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  return json(response) as Promise<T>;
}

export function runGroupStreamUrl(experimentId: string, groupId: string): string {
  return `/api/experiments/${encodeURIComponent(experimentId)}/groups/${encodeURIComponent(groupId)}/stream`;
}

export type VariantPreviewDto = {
  schema_version: "octagon-variant-preview-v1";
  source_hash: string;
  protocol_hash: string;
  preview_token: string;
  cells: number;
  attempts: number;
  variants: Array<{
    id: string;
    mutator_id: string;
    mutator_version: string;
    status: string;
    diff: string | null;
    warnings: string[];
    error_code: string | null;
    error_message: string | null;
  }>;
  blocking_warnings: string[];
  advisory_warnings: string[];
};

export function previewExperiment(
  capabilities: ResearchCapabilities,
  body: unknown,
): Promise<VariantPreviewDto> {
  return researchMutation(capabilities, "experiments", "/api/experiments/preview", body);
}

export function createExperiment(
  capabilities: ResearchCapabilities,
  body: unknown,
  idempotencyKey: string,
): Promise<{
  experiment_id: string;
  run_group_id: string;
  variant_ids: string[];
  cell_ids: string[];
  replayed: boolean;
  execution_scheduled: boolean;
}> {
  return researchMutation(capabilities, "experiments", "/api/experiments", body, {
    "Idempotency-Key": idempotencyKey,
  });
}
