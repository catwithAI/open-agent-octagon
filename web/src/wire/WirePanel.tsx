import { useEffect, useState } from "react";

import {
  api,
  type AttemptSummary,
  type WireCallSummary,
  type WireManifest,
  type WireRecord,
  type WireTrajectory,
  type WireTrajectoryStep,
  type WireUsage,
} from "../api/client";
import { useI18n } from "../i18n";
import { curveSegments, deriveWireView, splitMatched, usageValue, type WireGap } from "./curve";

type TFn = (key: string, vars?: Record<string, string | number>) => string;

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

export function WireBadge({ att }: { att: AttemptSummary }) {
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
