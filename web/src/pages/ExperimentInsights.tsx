import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";

import { createIdempotencyKey } from "../api/idempotency";
import { discoverCapabilities, researchGet, researchMutation } from "../api/researchClient";
import type { ResearchCapabilities } from "../api/researchTypes";
import { statusLabel } from "../researchUi";
import { useI18n } from "../i18n";

type Statement = { id: string; kind: "evidence-backed" | "inference" | "unavailable"; text: string; anchors: string[] };
// 失败版本（生成失败保留为独立版本）的 report 只有 error_code/error_message，
// 没有 sections/limitations——渲染前必须区分，否则整页崩溃成白屏。
type InsightReport = {
  sections?: Record<string, Statement[]>;
  limitations?: string[];
  error_code?: string;
  error_message?: string;
};
type Insight = {
  id: string;
  version: number;
  status: string;
  producer_version: string;
  report: InsightReport | null;
};
type InsightVersion = { version: number; status: string };
type GenerationReceipt = { version: number; status: string };
// 与服务端 generate 的前置校验保持一致（research_api.generate_insight_response）：
// 实验状态终态 + 所有运行组终态，否则 409 insight_inputs_not_finalized。
const TERMINAL_STATUSES = new Set(["completed", "partial", "failed", "cancelled"]);
const SECTION_LABEL_KEYS: Record<string, string> = {
  consensus: "experimentInsights.section.consensus",
  divergence: "experimentInsights.section.divergence",
  first_deviation: "experimentInsights.section.firstDeviation",
  recovery: "experimentInsights.section.recovery",
  suggested_probes: "experimentInsights.section.suggestedProbes",
};
type Readiness = { experimentStatus: string; groups: Array<{ id: string; status: string }> };

export function ExperimentInsights(props: { experimentId?: string; embedded?: boolean } = {}) {
  const { t, lang } = useI18n();
  const params = useParams();
  const experimentId = props.experimentId ?? params.experimentId ?? "";
  const [caps, setCaps] = useState<ResearchCapabilities | null>(null);
  const [versions, setVersions] = useState<InsightVersion[]>([]);
  const [insight, setInsight] = useState<Insight | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [readiness, setReadiness] = useState<Readiness | null>(null);

  const loadInsight = async (version: number) => {
    const value = await researchGet<Insight>(
      `/api/experiments/${encodeURIComponent(experimentId)}/insights/${version}`,
    );
    setInsight(value);
  };

  const loadVersions = async (preferredVersion?: number) => {
    const list = await researchGet<{ items: InsightVersion[] }>(
      `/api/experiments/${encodeURIComponent(experimentId)}/insights?limit=20`,
    );
    setVersions(list.items);
    const version = preferredVersion ?? list.items[0]?.version;
    if (version !== undefined) await loadInsight(version);
    else setInsight(null);
  };

  useEffect(() => {
    let alive = true;
    discoverCapabilities().then(async (result) => {
      if (!result.ok) { setError(result.error.message); return; }
      if (!alive) return;
      setCaps(result.value);
      if (!result.value.features.insights) return;
      void (async () => {
        try {
          const detail = await researchGet<{ experiment: { status: string } }>(
            `/api/experiments/${encodeURIComponent(experimentId)}`,
          );
          const groups = await researchGet<{ items: Array<{ group: { id: string; status: string } }> }>(
            `/api/experiments/${encodeURIComponent(experimentId)}/groups`,
          );
          if (alive) setReadiness({
            experimentStatus: detail.experiment.status,
            groups: groups.items.map((item) => ({ id: item.group.id, status: item.group.status })),
          });
        } catch {
          // 可生成性信息拿不到时不阻塞生成入口，交给服务端校验兜底。
        }
      })();
      const list = await researchGet<{ items: InsightVersion[] }>(
        `/api/experiments/${encodeURIComponent(experimentId)}/insights?limit=20`,
      );
      if (!alive) return;
      setVersions(list.items);
      if (list.items[0]) {
        const value = await researchGet<Insight>(
          `/api/experiments/${encodeURIComponent(experimentId)}/insights/${list.items[0].version}`,
        );
        if (alive) setInsight(value);
      }
    }).catch((reason) => setError(String(reason)));
    return () => { alive = false; };
  }, [experimentId]);

  const generate = async (sourceVersion?: number) => {
    if (!caps) return;
    setBusy(true);
    setError("");
    setNotice(sourceVersion === undefined ? t("experimentInsights.generating") : t("experimentInsights.regenerating"));
    try {
      const path = sourceVersion === undefined
        ? `/api/experiments/${encodeURIComponent(experimentId)}/insights/generate`
        : `/api/experiments/${encodeURIComponent(experimentId)}/insights/${sourceVersion}/regenerate`;
      const receipt = await researchMutation<GenerationReceipt>(
        caps,
        "insights",
        path,
        undefined,
        { "Idempotency-Key": createIdempotencyKey() },
      );
      await loadVersions(receipt.version);
      setNotice(t("experimentInsights.generated", { version: receipt.version }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
      setNotice("");
    } finally {
      setBusy(false);
    }
  };

  const feedback = async (signal: "helpful" | "unhelpful") => {
    if (!caps || !insight) return;
    setBusy(true);
    setError("");
    try {
      await researchMutation(
        caps,
        "research_feedback",
        `/api/experiments/${encodeURIComponent(experimentId)}/insights/${insight.version}/feedback`,
        { signal },
        { "Idempotency-Key": createIdempotencyKey() },
      );
      setNotice(signal === "helpful" ? t("experimentInsights.feedbackHelpful") : t("experimentInsights.feedbackUnhelpful"));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };
  const pendingGroups = readiness?.groups.filter((group) => !TERMINAL_STATUSES.has(group.status)) ?? [];
  const ready = readiness === null
    ? null
    : TERMINAL_STATUSES.has(readiness.experimentStatus) && pendingGroups.length === 0;

  return <div className={`insights-workspace ${props.embedded ? "is-embedded" : ""}`}>
    <div className="results-heading"><div><span>{t("experimentInsights.eyebrow")}</span><h2>{t("experimentInsights.title")}</h2>
      <p>{t("experimentInsights.subtitle")}</p></div></div>
    {error && <div className="error">{error}</div>}
    {caps && !caps.features.insights && <p>{t("experimentInsights.notEnabled")}</p>}
    {caps?.features.insights && readiness && <section className="results-panel insights-eligibility">
      <div className="results-panel-head"><strong>{t("experimentInsights.eligibilityHead")}</strong>
        <span>{ready ? t("experimentInsights.eligibilityReady") : t("experimentInsights.eligibilityNotReady")}</span></div>
      {ready
        ? <p>{t("experimentInsights.readyBody", { status: statusLabel(readiness.experimentStatus, lang), count: readiness.groups.length })}</p>
        : <p>{t("experimentInsights.notReadyBody", { status: statusLabel(readiness.experimentStatus, lang) })}
          {pendingGroups.length > 0 && t("experimentInsights.pendingGroups", { groups: pendingGroups.map((group) =>
            t("experimentInsights.pendingGroupItem", { id: group.id, status: statusLabel(group.status, lang) })).join(lang === "en" ? ", " : "、") })}
          {t("experimentInsights.notReadyTail")}</p>}
      <p><small>{t("experimentInsights.eligibilityNote")}</small></p>
    </section>}
    {caps?.features.insights && <div className="insights-toolbar">
      {versions.length > 0 && <label>{t("experimentInsights.versionLabel")} <select value={insight?.version ?? ""} disabled={busy} onChange={(event) => void loadInsight(Number(event.target.value))}>
        {versions.map((item) => <option key={item.version} value={item.version}>v{item.version} · {statusLabel(item.status, lang)}</option>)}
      </select></label>}
      <button className="btn" disabled={busy || ready === false} onClick={() => void generate(insight?.version)}>
        {busy ? t("experimentInsights.processing") : ready === false ? t("experimentInsights.waitForRuns") : insight ? t("experimentInsights.regenerate") : t("experimentInsights.generate")}
      </button>
      {notice && <span role="status">{notice}</span>}
    </div>}
    {caps?.features.insights && versions.length === 0 && !busy && <div className="results-empty">
      {t("experimentInsights.emptyState")}
    </div>}
    {insight && <>
      <div className="insights-version">v{insight.version} · {insight.producer_version} · {statusLabel(insight.status, lang)}</div>
      {insight.status === "failed" || !insight.report?.sections
        ? <section className="results-panel insights-failure">
          <div className="results-panel-head"><strong>{t("experimentInsights.failureHead")}</strong><span>{insight.report?.error_code ?? t("experimentInsights.unknownError")}</span></div>
          <p>{insight.report?.error_message ?? t("experimentInsights.failureBody")}</p>
          <p>{t("experimentInsights.failureNote")}</p>
        </section>
        : <>
          <div className="insights-sections">{Object.entries(insight.report.sections).map(([section, statements]) => <section className="results-panel" key={section}>
            <div className="results-panel-head"><strong>{SECTION_LABEL_KEYS[section] ? t(SECTION_LABEL_KEYS[section]) : section}</strong><span>{t("experimentInsights.statementCount", { n: statements.length })}</span></div>
            <div className="insights-statements">{statements.map((statement) => <article key={statement.id}>
              <strong>{statement.kind === "evidence-backed" ? t("experimentInsights.kind.evidenceBacked") : statement.kind === "inference" ? t("experimentInsights.kind.inference") : t("experimentInsights.kind.unavailable")}</strong> — {statement.text}
              {statement.anchors.map((anchor) => <a key={anchor} href={`/api/experiments/${encodeURIComponent(experimentId)}/evidence/resolve?anchor=${encodeURIComponent(anchor)}`}> {t("experimentInsights.viewEvidence")} </a>)}
            </article>)}</div>
          </section>)}</div>
          <section className="results-panel insights-limitations">
            <div className="results-panel-head"><strong>{t("experimentInsights.limitations")}</strong><span>{t("experimentInsights.statementCount", { n: insight.report.limitations?.length ?? 0 })}</span></div>
            {insight.report.limitations?.length
              ? insight.report.limitations.map((item) => <p key={item}>{item}</p>)
              : <div className="results-empty">{t("experimentInsights.noExtraLimitations")}</div>}
          </section>
          <div className="insights-feedback"><span>{t("experimentInsights.feedbackPrompt")}</span>
            <button disabled={busy || !caps?.features.research_feedback} onClick={() => void feedback("helpful")}>{t("experimentInsights.helpful")}</button>
            <button disabled={busy || !caps?.features.research_feedback} onClick={() => void feedback("unhelpful")}>{t("experimentInsights.unhelpful")}</button>
          </div>
        </>}
    </>}
  </div>;
}
