import { useEffect, useMemo, useState } from "react";

import { researchGet } from "../api/researchClient";
import { mutatorLabel, statusLabel } from "../researchUi";
import type { Lang } from "../researchUi";
import { useI18n } from "../i18n";

type TFn = (key: string, vars?: Record<string, string | number>) => string;

type EvidenceRecord = Record<string, unknown> & {
  category: string;
  source?: string;
  attempt_id?: string;
  anchor?: string;
  status?: string;
  resolution?: { status?: string; [key: string]: unknown };
};

type EvidenceIndex = {
  schema_version: "octagon-evidence-index-v1";
  items: EvidenceRecord[];
  anchors: Record<string, Record<string, unknown>>;
  total: number;
  offset: number;
  limit: number;
  next_offset: number | null;
  selection_manifest: {
    selected_records: number;
    selected_by_category: Record<string, number>;
    omitted_by_category: Record<string, number>;
    truncated: boolean;
  };
  capture_policy: { payload_included: false; allowed_sources: string[] };
};

// label 为 i18n key（无 key 的固定英文专有名词直接给字面量）；渲染处按需 t()。
const SOURCES = [
  ["", "evidenceWorkspace.source.all"],
  ["trace", "Trace"],
  ["events", "Events"],
  ["conversation", "Conversation"],
  ["scores", "Scores"],
  ["wire", "Wire"],
  ["artifacts", "Artifacts"],
] as const;

// value 为 i18n key；专有名词（source 名等）无对应 key 时按原样回落。
const CATEGORY_LABELS: Record<string, string> = {
  aggregate: "evidenceWorkspace.category.aggregate",
  result: "evidenceWorkspace.category.result",
  score: "evidenceWorkspace.category.score",
  error: "evidenceWorkspace.category.error",
  security: "evidenceWorkspace.category.security",
  completeness: "evidenceWorkspace.category.completeness",
  trajectory: "evidenceWorkspace.category.trajectory",
  artifact: "evidenceWorkspace.category.artifact",
};

// 类别标签查表：命中则翻译，否则回落到原始类别名。
const categoryLabel = (t: TFn, name: string): string =>
  CATEGORY_LABELS[name] ? t(CATEGORY_LABELS[name]) : name;

// 选中态的唯一 key。aggregate/result 这类记录没有 anchor，必须带上列表下标，
// 且列表渲染、初始选中、查找选中三处必须用同一个函数——之前三处格式不一致，
// 导致无 anchor 的记录点击后永远匹配不上（检查器一直"未选择"）。
const recordKey = (record: EvidenceRecord, itemIndex: number): string =>
  record.anchor ?? `${record.category}:${record.attempt_id ?? ""}:${itemIndex}`;

// 检查器分层：业务字段放摘要区（中文标签），血缘 ID 折叠到"血缘坐标"，
// 用户点开一条记录首先看到的应该是"发生了什么"，而不是一串 ID。
const COORDINATE_FIELDS = new Set([
  "experiment_id", "group_id", "run_group_id", "run_id", "attempt_id",
  "cell_id", "variant_id", "seed", "source_hash", "content_hash", "record_id",
]);

// value 为 i18n key（Agent 为固定英文专有名词，直接给字面量）；渲染处按需 t()。
const FIELD_LABELS: Record<string, string> = {
  agent: "Agent",
  model: "evidenceWorkspace.field.model",
  status: "evidenceWorkspace.field.status",
  score_total: "evidenceWorkspace.field.scoreTotal",
  mutator_id: "evidenceWorkspace.field.mutatorId",
  mutator_version: "evidenceWorkspace.field.mutatorVersion",
  repeat_index: "evidenceWorkspace.field.repeatIndex",
  source: "evidenceWorkspace.field.source",
  dimension: "evidenceWorkspace.field.dimension",
  value: "evidenceWorkspace.field.value",
  weight: "evidenceWorkspace.field.weight",
  detail: "evidenceWorkspace.field.detail",
  error_code: "evidenceWorkspace.field.errorCode",
  error_message: "evidenceWorkspace.field.errorMessage",
  event_type: "evidenceWorkspace.field.eventType",
  tool_name: "evidenceWorkspace.field.toolName",
  duration_ms: "evidenceWorkspace.field.durationMs",
  path: "evidenceWorkspace.field.path",
  size_bytes: "evidenceWorkspace.field.sizeBytes",
  final_result: "evidenceWorkspace.field.finalResult",
  strategy: "evidenceWorkspace.field.strategy",
  total_cells: "evidenceWorkspace.field.totalCells",
  completed_cells: "evidenceWorkspace.field.completedCells",
  partial_cells: "evidenceWorkspace.field.partialCells",
  failed_cells: "evidenceWorkspace.field.failedCells",
  cancelled_cells: "evidenceWorkspace.field.cancelledCells",
  created_at: "evidenceWorkspace.field.createdAt",
  started_at: "evidenceWorkspace.field.startedAt",
  ended_at: "evidenceWorkspace.field.endedAt",
};

// 字段标签查表：命中则翻译，否则回落到原始字段名。
const fieldLabel = (t: TFn, key: string): string =>
  FIELD_LABELS[key] ? t(FIELD_LABELS[key]) : key;

const formatFieldValue = (t: TFn, lang: Lang, key: string, value: unknown): string => {
  if (key === "status" && typeof value === "string") return statusLabel(value, lang);
  if (key === "mutator_id" && typeof value === "string") return mutatorLabel(value, lang);
  if (key === "repeat_index" && typeof value === "number") return t("evidenceWorkspace.value.repeatNth", { n: value + 1 });
  if (key === "final_result" && value && typeof value === "object" && "state" in value) {
    const state = (value as { state: unknown }).state;
    return state === "captured"
      ? t("evidenceWorkspace.value.captured")
      : state === "missing"
        ? t("evidenceWorkspace.value.missing")
        : String(state);
  }
  return display(value);
};

const display = (value: unknown): string => {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return JSON.stringify(value);
};

export function EvidenceWorkspace({ experimentId }: { experimentId: string }) {
  const { t, lang } = useI18n();
  const [index, setIndex] = useState<EvidenceIndex | null>(null);
  const [source, setSource] = useState("");
  const [category, setCategory] = useState("");
  const [status, setStatus] = useState("");
  const [attemptQuery, setAttemptQuery] = useState("");
  const [attemptId, setAttemptId] = useState("");
  const [offset, setOffset] = useState(0);
  const [selectedKey, setSelectedKey] = useState("");
  const [error, setError] = useState("");
  const [resolveState, setResolveState] = useState<
    { anchor: string; loading?: boolean; data?: unknown; error?: string } | null
  >(null);
  const limit = 50;

  useEffect(() => {
    const params = new URLSearchParams({ offset: String(offset), limit: String(limit) });
    if (source) params.set("source", source);
    if (category) params.set("category", category);
    if (status) params.set("status", status);
    if (attemptId) params.set("attempt_id", attemptId);
    setError("");
    researchGet<EvidenceIndex>(
      `/api/experiments/${encodeURIComponent(experimentId)}/evidence?${params}`,
    ).then((result) => {
      setIndex(result);
      setSelectedKey((current) => {
        const keys = result.items.map((item, itemIndex) => recordKey(item, itemIndex));
        return keys.includes(current) ? current : keys[0] ?? "";
      });
    }).catch((reason) => setError(String(reason)));
  }, [attemptId, category, experimentId, offset, source, status]);

  const selected = useMemo(() => index?.items.find((item, itemIndex) =>
    recordKey(item, itemIndex) === selectedKey,
  ), [index, selectedKey]);
  const selectedResolution = selected?.anchor
    ? index?.anchors[selected.anchor] ?? selected.resolution
    : selected?.resolution;
  const categories = Object.entries(index?.selection_manifest.selected_by_category ?? {});
  const visibleFields = selected ? Object.entries(selected).filter(([key]) =>
    !["resolution", "anchor", "category"].includes(key)) : [];
  const summaryFields = visibleFields.filter(([key]) => !COORDINATE_FIELDS.has(key));
  const coordinateFields = visibleFields.filter(([key]) => COORDINATE_FIELDS.has(key));
  const selectedRunId = typeof selected?.run_id === "string" ? selected.run_id : "";
  // 深链到运行详情的对应板块：优先用记录的来源，来源缺失时按类别推断。
  const selectedView = selected
    ? (typeof selected.source === "string" && selected.source)
      || ({ score: "scores", trajectory: "trace", artifact: "artifacts", security: "events" } as Record<string, string>)[selected.category]
      || ""
    : "";
  const detailHref = selectedRunId
    ? `/runs/${encodeURIComponent(selectedRunId)}${
      typeof selected?.attempt_id === "string" && selected.attempt_id
        ? `?attempt=${encodeURIComponent(selected.attempt_id)}${selectedView ? `&view=${encodeURIComponent(selectedView)}` : ""}`
        : ""
    }`
    : "";

  const applyAttempt = () => {
    setOffset(0);
    setAttemptId(attemptQuery.trim());
  };

  const resolveAnchor = async (anchor: string) => {
    setResolveState({ anchor, loading: true });
    try {
      const data = await researchGet<unknown>(
        `/api/experiments/${encodeURIComponent(experimentId)}/evidence/resolve?anchor=${encodeURIComponent(anchor)}`,
      );
      setResolveState({ anchor, data });
    } catch (reason) {
      setResolveState({ anchor, error: String(reason) });
    }
  };

  return <div className="evidence-workspace">
    <details className="evidence-help">
      <summary>{t("evidenceWorkspace.help.summary")}</summary>
      <div className="evidence-help-body">
        <p>{t("evidenceWorkspace.help.intro")}</p>
        <ul>
          <li><strong>{t("evidenceWorkspace.help.sourceLabel")}</strong>{t("evidenceWorkspace.help.sourceBody")}</li>
          <li><strong>{t("evidenceWorkspace.help.typeLabel")}</strong>{t("evidenceWorkspace.help.typeBody")}</li>
          <li><strong>{t("evidenceWorkspace.help.statusLabel")}</strong>{t("evidenceWorkspace.help.statusBody")}</li>
          <li><strong>{t("evidenceWorkspace.help.anchorLabel")}</strong>{t("evidenceWorkspace.help.anchorBody", { uri: "octagon://…" })}</li>
          <li><strong>{t("evidenceWorkspace.help.truncateLabel")}</strong>{t("evidenceWorkspace.help.truncateBody")}</li>
        </ul>
      </div>
    </details>
    <div className="evidence-filterbar">
      <label>{t("evidenceWorkspace.filter.type")}<select value={category} onChange={(event) => { setOffset(0); setCategory(event.target.value); }}>
        <option value="">{t("evidenceWorkspace.filter.all")}</option>
        {categories.map(([name, count]) => <option key={name} value={name}>{categoryLabel(t, name)} ({count})</option>)}
      </select></label>
      <label>{t("evidenceWorkspace.filter.status")}<select value={status} onChange={(event) => { setOffset(0); setStatus(event.target.value); }}>
        <option value="">{t("evidenceWorkspace.filter.all")}</option><option value="resolved">{t("evidenceWorkspace.status.resolved")}</option><option value="redacted">{t("evidenceWorkspace.status.redacted")}</option>
        <option value="missing">{t("evidenceWorkspace.status.missing")}</option><option value="unsupported">{t("evidenceWorkspace.status.unsupported")}</option><option value="failed">{t("evidenceWorkspace.status.failed")}</option>
      </select></label>
      <label className="evidence-attempt-filter">Attempt
        <span><input value={attemptQuery} onChange={(event) => setAttemptQuery(event.target.value)} placeholder="att_…" onKeyDown={(event) => {
          if (event.key === "Enter") applyAttempt();
        }} /><button type="button" onClick={applyAttempt}>{t("evidenceWorkspace.filter.apply")}</button></span>
      </label>
      {(source || category || status || attemptId) && <button className="evidence-clear" type="button" onClick={() => {
        setSource(""); setCategory(""); setStatus(""); setAttemptId(""); setAttemptQuery(""); setOffset(0);
      }}>{t("evidenceWorkspace.filter.clear")}</button>}
      <div className="evidence-policy"><span className="signal-dot" />{t("evidenceWorkspace.policy.metadataOnly")}</div>
    </div>
    {error && <div className="error" role="alert">{error}</div>}
    <div className="evidence-columns">
      <aside className="evidence-source-rail" aria-label={t("evidenceWorkspace.column.sourceAria")}>
        <div className="evidence-column-head">{t("evidenceWorkspace.column.source")}</div>
        {SOURCES.map(([value, label]) => <button
          type="button"
          key={value}
          className={source === value ? "is-active" : ""}
          onClick={() => { setSource(value); setOffset(0); }}
        ><span>{value === "" ? t(label) : label}</span>{value && <small>{index?.capture_policy.allowed_sources.includes(value) ? t("evidenceWorkspace.source.indexable") : t("evidenceWorkspace.source.notCaptured")}</small>}</button>)}
        {index?.selection_manifest.truncated && <div className="evidence-truncated">
          {t("evidenceWorkspace.truncated.note")}
          {Object.entries(index.selection_manifest.omitted_by_category).map(([name, count]) =>
            <span key={name}>{categoryLabel(t, name)} +{count}</span>)}
        </div>}
      </aside>

      <section className="evidence-timeline-column">
        <div className="evidence-column-head"><span>{t("evidenceWorkspace.column.timeline")}</span><small>{t("evidenceWorkspace.timeline.matchCount", { n: index?.total ?? 0 })}</small></div>
        <div className="evidence-record-list">
          {index?.items.map((record, itemIndex) => {
            const key = recordKey(record, itemIndex);
            const recordStatus = record.resolution?.status ?? record.status ?? "metadata";
            return <button
              type="button"
              key={key}
              className={selectedKey === key ? "is-selected" : ""}
              onClick={() => setSelectedKey(key)}
            >
              <span className={`evidence-record-dot status-${recordStatus}`} />
              <span><strong>{categoryLabel(t, record.category)}
                {record.source ? ` · ${record.source}` : ""}</strong>
                <small>{record.attempt_id ?? display(record.run_group_id ?? record.group_id)}</small></span>
              <span><em>{recordStatus}</em><small>#{offset + itemIndex + 1}</small></span>
            </button>;
          })}
          {index?.items.length === 0 && <div className="results-empty">{t("evidenceWorkspace.timeline.empty")}</div>}
        </div>
        <div className="evidence-pagination">
          <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - limit))}>{t("evidenceWorkspace.page.prev")}</button>
          <span>{index ? `${index.items.length ? index.offset + 1 : 0}–${Math.min(index.offset + index.items.length, index.total)} / ${index.total}` : t("evidenceWorkspace.page.loading")}</span>
          <button disabled={index?.next_offset === null || !index} onClick={() => {
            if (index?.next_offset !== null && index?.next_offset !== undefined) {
              setOffset(index.next_offset);
            }
          }}>{t("evidenceWorkspace.page.next")}</button>
        </div>
      </section>

      <aside className="evidence-inspector">
        <div className="evidence-column-head"><span>{t("evidenceWorkspace.column.inspector")}</span><small>{selected ? categoryLabel(t, selected.category) : t("evidenceWorkspace.inspector.unselected")}</small></div>
        {selected ? <div className="evidence-inspector-body">
          <div className="evidence-inspector-title"><span className={`evidence-record-dot status-${selectedResolution?.status ?? selected.status ?? "metadata"}`} />
            <div><strong>{categoryLabel(t, selected.category)}</strong>
              <small>{display(selected.source)} · {display(selectedResolution?.status ?? selected.status)}</small></div></div>
          <dl>{summaryFields.map(([key, value]) => <div key={key}>
            <dt>{fieldLabel(t, key)}</dt><dd>{formatFieldValue(t, lang, key, value)}</dd>
          </div>)}</dl>
          {detailHref && <a className="evidence-detail-link" href={detailHref}>
            {t("evidenceWorkspace.detail.open")}<small>{t("evidenceWorkspace.detail.autoLocate", { view:
              { trace: t("evidenceWorkspace.detail.view.trace"), events: t("evidenceWorkspace.detail.view.events"),
                conversation: t("evidenceWorkspace.detail.view.conversation"), scores: t("evidenceWorkspace.detail.view.scores"),
                wire: t("evidenceWorkspace.detail.view.wire"), artifacts: t("evidenceWorkspace.detail.view.artifacts") }[selectedView]
                ?? t("evidenceWorkspace.detail.view.fallback")
            })}</small>
          </a>}
          {coordinateFields.length > 0 && <details className="evidence-resolution">
            <summary>{t("evidenceWorkspace.coordinate.summary")}</summary>
            <dl>{coordinateFields.map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{display(value)}</dd></div>)}</dl>
          </details>}
          {selected.anchor && <div className="evidence-anchor-card">
            <span>Evidence Anchor</span><code>{selected.anchor}</code>
            <div><button type="button" onClick={() => void navigator.clipboard?.writeText(selected.anchor ?? "")}>{t("evidenceWorkspace.anchor.copy")}</button>
              <button type="button" disabled={resolveState?.anchor === selected.anchor && resolveState.loading}
                onClick={() => void resolveAnchor(selected.anchor ?? "")}>
                {resolveState?.anchor === selected.anchor && resolveState.loading ? t("evidenceWorkspace.anchor.resolving") : t("evidenceWorkspace.anchor.resolve")}
              </button></div>
            {resolveState?.anchor === selected.anchor && resolveState.error
              && <div className="error" role="alert">{t("evidenceWorkspace.anchor.resolveError", { message: resolveState.error })}</div>}
            {resolveState?.anchor === selected.anchor && resolveState.data !== undefined
              && <pre className="evidence-resolve-result">{JSON.stringify(resolveState.data, null, 2)}</pre>}
          </div>}
          {selectedResolution && <details className="evidence-resolution"><summary>{t("evidenceWorkspace.resolution.metadata")}</summary><pre>{JSON.stringify(selectedResolution, null, 2)}</pre></details>}
        </div> : <div className="results-empty">{t("evidenceWorkspace.inspector.pickFromTimeline")}</div>}
      </aside>
    </div>
  </div>;
}
