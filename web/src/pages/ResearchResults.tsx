import { useEffect, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";

import { discoverCapabilities, getRunGroup, researchGet, researchMutation } from "../api/researchClient";
import type { ResearchCapabilities } from "../api/researchTypes";
import { mutatorLabel, statusLabel } from "../researchUi";
import type { Lang } from "../researchUi";
import { useI18n } from "../i18n";
import { EvidenceWorkspace } from "./EvidenceWorkspace";
import { CompareWorkspace } from "./CompareWorkspace";
import { ArtifactsWorkspace } from "./ArtifactsWorkspace";
import { ExperimentInsights } from "./ExperimentInsights";

type Rate = { numerator: number; denominator: number; value: number | null };
type Aggregate = {
  sample_count: number;
  expected_count: number;
  mean: number | null;
  minimum: number | null;
  maximum: number | null;
  sample_variance: number | null;
  rates: Record<string, Rate>;
  status_counts: Record<string, number>;
};
type RobustnessSlice = {
  variant_id: string;
  mutator_id: string;
  agent: string;
  model: string | null;
  aggregate: Aggregate;
  labels: string[];
};
type Robustness = { slices: RobustnessSlice[] };
type SafetySlice = {
  attack_family: string;
  polarity: string;
  agent: string;
  model: string | null;
  expected: number;
  passed: number;
  failed: number;
  unsupported: number;
  error: number;
};
type Coverage = {
  slices: SafetySlice[];
  failures: Array<{ attempt_id: string; anchors: string[] }>;
};

type Translate = (key: string, vars?: Record<string, string | number>) => string;
const metric = (value: number | null, t: Translate): string => value === null ? t("researchResults.missing") : String(value);
const RESULT_TABS = [
  ["overview", "researchResults.tab.overview"],
  ["robustness", "researchResults.tab.robustness"],
  ["compare", "researchResults.tab.compare"],
  ["evidence", "researchResults.tab.evidence"],
  ["insights", "researchResults.tab.insights"],
  ["safety", "researchResults.tab.safety"],
  ["artifacts", "researchResults.tab.artifacts"],
] as const;
type ResultTab = typeof RESULT_TABS[number][0];
const TERMINAL_GROUP_STATUSES = new Set(["completed", "partial", "failed", "cancelled"]);

function passRate(slice: RobustnessSlice): Rate | undefined {
  return slice.aggregate.rates.end_to_end_pass_rate;
}

function candidateConclusion(slice: RobustnessSlice, t: Translate): string {
  const rate = passRate(slice);
  const passed = rate?.numerator ?? 0;
  const expected = rate?.denominator ?? slice.aggregate.expected_count;
  const observed = slice.aggregate.sample_count;
  if (observed < slice.aggregate.expected_count) return t("researchResults.conclusion.partial", { observed, expected: slice.aggregate.expected_count });
  if (expected < 2) {
    return passed === expected && expected > 0
      ? t("researchResults.conclusion.singlePass")
      : t("researchResults.conclusion.singleFail");
  }
  if (passed === expected && expected > 0) return t("researchResults.conclusion.allPass");
  if (passed > 0) return t("researchResults.conclusion.unstable", { passed, expected });
  if (slice.labels.includes("brittle")
      || slice.aggregate.minimum !== slice.aggregate.maximum) {
    return t("researchResults.conclusion.inconsistentFail");
  }
  return t("researchResults.conclusion.consistentFail");
}

function statusSummary(slice: RobustnessSlice, t: Translate, lang: Lang): string {
  return Object.entries(slice.aggregate.status_counts)
    .filter(([, count]) => count > 0)
    .map(([status, count]) => t("researchResults.statusCount", { count, status: statusLabel(status, lang) }))
    .join(t("researchResults.listSeparator"));
}

export function ResearchResults() {
  const { t, lang } = useI18n();
  const { experimentId = "", groupId = "" } = useParams();
  const [capabilities, setCapabilities] = useState<ResearchCapabilities | null>(null);
  const [robustness, setRobustness] = useState<Robustness | null>(null);
  const [coverage, setCoverage] = useState<Coverage | null>(null);
  const [groupStatus, setGroupStatus] = useState<string | null>(null);
  const [loadingResults, setLoadingResults] = useState(true);
  const [rerun, setRerun] = useState<string[]>([]);
  const [rerunPreviewed, setRerunPreviewed] = useState(false);
  const [rerunLoading, setRerunLoading] = useState(false);
  const [rerunError, setRerunError] = useState("");
  const [error, setError] = useState("");
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedTab = searchParams.get("tab");
  const visibleTabs = RESULT_TABS.filter(([name]) =>
    name !== "safety" || (coverage?.slices.length ?? 0) > 0);
  const activeTab: ResultTab = visibleTabs.some(([name]) => name === requestedTab)
    ? requestedTab as ResultTab
    : "overview";

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = async () => {
      try {
        const result = await discoverCapabilities();
        if (!result.ok) {
          if (alive) setError(result.error.message);
          return;
        }
        if (!alive) return;
        setCapabilities(result.value);

        const refresh = async () => {
          try {
            const snapshot = await getRunGroup(experimentId, groupId);
            if (!snapshot.ok) throw snapshot.error;
            if (!alive) return;
            const status = snapshot.value.group.status;
            setGroupStatus(status);
            if (!TERMINAL_GROUP_STATUSES.has(status)) {
              setLoadingResults(true);
              timer = setTimeout(() => void refresh(), 2000);
              return;
            }

            const requests: Promise<void>[] = [];
            if (result.value.features.robustness) {
              requests.push(researchGet<Robustness>(
                `/api/experiments/${encodeURIComponent(experimentId)}/groups/${encodeURIComponent(groupId)}/robustness?limit=1000`,
              ).then((value) => { if (alive) setRobustness(value); }));
            }
            if (result.value.features.attack_coverage) {
              requests.push(researchGet<Coverage>(
                `/api/experiments/${encodeURIComponent(experimentId)}/attack-coverage`,
              ).then((value) => { if (alive) setCoverage(value); }));
            }
            await Promise.all(requests);
            if (alive) {
              setLoadingResults(false);
              setError("");
            }
          } catch (reason) {
            if (alive) {
              setLoadingResults(false);
              setError(reason instanceof Error ? reason.message : String(reason));
            }
          }
        };
        await refresh();
      } catch (reason) {
        if (alive) {
          setLoadingResults(false);
          setError(reason instanceof Error ? reason.message : String(reason));
        }
      }
    };
    void load();
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
  }, [experimentId, groupId]);

  const previewForensic = async () => {
    if (!capabilities || rerunLoading) return;
    setRerunLoading(true);
    setRerunError("");
    try {
      const preview = await researchMutation<{ selected_cell_ids: string[] }>(
        capabilities,
        "attack_coverage",
        `/api/experiments/${encodeURIComponent(experimentId)}/attack-coverage/rerun-preview`,
      );
      setRerun(preview.selected_cell_ids);
      setRerunPreviewed(true);
    } catch (reason) {
      setRerunError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setRerunLoading(false);
    }
  };

  const observed = robustness?.slices.filter((slice) => slice.aggregate.mean !== null) ?? [];
  const ranked = [...observed].sort((left, right) =>
    (right.aggregate.mean ?? Number.NEGATIVE_INFINITY)
      - (left.aggregate.mean ?? Number.NEGATIVE_INFINITY));
  const totalSamples = observed.reduce((sum, slice) => sum + slice.aggregate.sample_count, 0);
  const totalExpected = observed.reduce((sum, slice) => sum + slice.aggregate.expected_count, 0);
  const totalPasses = observed.reduce((sum, slice) =>
    sum + (passRate(slice)?.numerator ?? 0), 0);
  const highest = ranked[0];
  const noCandidatePassed = totalExpected > 0 && totalPasses === 0;

  return <div className="results-workspace">
    <nav className="results-tabs" aria-label={t("researchResults.aria.workspace")}>
      {visibleTabs.map(([name, label]) => <button
        type="button"
        key={name}
        className={activeTab === name ? "is-active" : ""}
        onClick={() => setSearchParams({ tab: name })}
      >{t(label)}</button>)}
    </nav>
    {error && <div className="error results-error" role="alert">{error}</div>}

    {activeTab === "overview" && <div className="results-body">
      <div className="results-heading"><div><span>RESULTS OVERVIEW</span><h2>{t("researchResults.overview.title")}</h2>
        <p>{t("researchResults.overview.subtitle")}</p></div></div>
      <section className={`results-conclusion ${noCandidatePassed ? "is-danger" : ""}`}>
        {loadingResults
          ? <><strong>{t("researchResults.stillRunning")}</strong><p>{t("researchResults.stillRunningNote")}</p></>
          : ranked.length === 0
            ? <><strong>{t("researchResults.noScorable")}</strong><p>{t("researchResults.noScorableNote", { status: statusLabel(groupStatus ?? "unavailable", lang) })}</p></>
            : noCandidatePassed
              ? <><strong>{t("researchResults.noCandidatePassed")}</strong><p>{t("researchResults.noCandidatePassedNote", { agent: highest.agent, mean: metric(highest.aggregate.mean, t) })}</p></>
              : <><strong>{t("researchResults.passedSummary", { passes: totalPasses, expected: totalExpected })}</strong><p>{t("researchResults.passedSummaryNote", { agent: highest.agent, mean: metric(highest.aggregate.mean, t) })}</p></>}
      </section>
      {ranked.length > 0 && <>
        <div className="results-stat-grid">
          <article><span>{t("researchResults.stat.candidates")}</span><strong>{ranked.length}</strong><small>{t("researchResults.stat.candidatesNote")}</small></article>
          <article><span>{t("researchResults.stat.scored")}</span><strong>{totalSamples}/{totalExpected}</strong><small>{t("researchResults.stat.scoredNote")}</small></article>
          <article><span>{t("researchResults.stat.topMean")}</span><strong>{metric(highest.aggregate.mean, t)}</strong><small>{highest.agent}</small></article>
          <article className={noCandidatePassed ? "is-danger" : ""}><span>{t("researchResults.stat.passed")}</span><strong>{totalPasses}/{totalExpected}</strong><small>{t("researchResults.stat.passedNote")}</small></article>
        </div>
        <section className="results-panel">
          <div className="results-panel-head"><strong>{t("researchResults.ranking.title")}</strong><button onClick={() => setSearchParams({ tab: "robustness" })}>{t("researchResults.ranking.viewRobustness")}</button></div>
          <div className="results-ranking" role="table" aria-label={t("researchResults.ranking.title")}>
            <div className="results-ranking-row is-head" role="row">
              <span role="columnheader">{t("researchResults.col.candidate")}</span><span role="columnheader">{t("researchResults.col.mean")}</span>
              <span role="columnheader">{t("researchResults.col.range")}</span><span role="columnheader">{t("researchResults.col.runStatus")}</span>
              <span role="columnheader">{t("researchResults.col.conclusion")}</span>
            </div>
            {ranked.map((slice, index) => <div className="results-ranking-row" role="row" key={`${slice.variant_id}:${slice.agent}:${slice.model}`}>
              <span role="cell"><strong>{index + 1}. {slice.agent}</strong><small>{slice.model ?? t("researchResults.defaultModel")}</small></span>
              <span className="results-ranking-score" role="cell">{metric(slice.aggregate.mean, t)}</span>
              <span role="cell">{metric(slice.aggregate.minimum, t)}–{metric(slice.aggregate.maximum, t)}</span>
              <span role="cell">{statusSummary(slice, t, lang)}</span>
              <span className={passRate(slice)?.numerator === 0 ? "danger" : ""} role="cell">{candidateConclusion(slice, t)}</span>
            </div>)}
          </div>
        </section>
      </>}
    </div>}

    {activeTab === "robustness" && <div className="results-body">
      <div className="results-heading"><div><span>ROBUSTNESS</span><h2>{t("researchResults.robustness.title")}</h2><p>{t("researchResults.robustness.subtitle")}</p></div></div>
      {!capabilities?.features.robustness && <div className="results-empty">{t("researchResults.robustness.disabled")}</div>}
      {robustness && <section className="results-panel">
        <div className="results-panel-head"><strong>{t("researchResults.robustness.qualityDetail")}</strong><span>{t("researchResults.robustness.sliceCount", { n: robustness.slices.length })}</span></div>
        <div className="robustness-grid" role="table" aria-label={t("researchResults.robustness.resultsAria")}>
          <div className="robustness-row is-head" role="row"><span role="columnheader">{t("researchResults.col.variantCandidate")}</span><span role="columnheader">{t("researchResults.col.mean")}</span><span role="columnheader">{t("researchResults.col.range")}</span><span role="columnheader">{t("researchResults.col.samples")}</span><span role="columnheader">{t("researchResults.col.conclusion")}</span></div>
          {robustness.slices.map((slice) => {
            const value = slice.aggregate.mean;
            return <div className="robustness-row" role="row" key={`${slice.variant_id}:${slice.agent}:${slice.model}`}>
              <span role="cell"><strong>{mutatorLabel(slice.mutator_id, lang)}</strong><small>{slice.agent} / {slice.model ?? t("researchResults.defaultModel")}</small></span>
              <span className="robustness-score" role="cell" style={{ ["--score" as string]: value === null ? 0 : Math.max(0, Math.min(100, value)) }}>{metric(value, t)}</span>
              <span role="cell">{metric(slice.aggregate.minimum, t)}–{metric(slice.aggregate.maximum, t)}</span>
              <span role="cell">{slice.aggregate.sample_count}/{slice.aggregate.expected_count}</span>
              <span role="cell">{candidateConclusion(slice, t)}</span>
            </div>;
          })}
        </div>
      </section>}
    </div>}

    {activeTab === "safety" && <div className="results-body">
      <div className="results-heading"><div><span>SAFETY COVERAGE</span><h2>{t("researchResults.safety.title")}</h2><p>{t("researchResults.safety.subtitle")}</p></div></div>
      {!capabilities?.features.attack_coverage && <div className="results-empty">{t("researchResults.safety.disabled")}</div>}
      {coverage && <section className="results-panel">
        <div className="results-panel-head"><strong>{t("researchResults.safety.coverage")}</strong><span>{coverage.slices.length} slices</span></div>
        <div className="results-table-scroll"><table aria-label={t("researchResults.safety.resultsAria")}>
          <thead><tr><th>{t("researchResults.safety.col.attackFamily")}</th><th>{t("researchResults.safety.col.sampleType")}</th><th>{t("researchResults.safety.col.agentModel")}</th><th>{t("researchResults.safety.col.passed")}</th><th>{t("researchResults.safety.col.failed")}</th><th>{t("researchResults.safety.col.unsupported")}</th><th>{t("researchResults.safety.col.error")}</th></tr></thead>
          <tbody>{coverage.slices.map((slice) => <tr key={`${slice.attack_family}:${slice.polarity}:${slice.agent}:${slice.model}`}>
            <td>{slice.attack_family}</td><td>{slice.polarity === "attack" ? t("researchResults.safety.attackSample") : slice.polarity === "benign" ? t("researchResults.safety.benignSample") : slice.polarity}</td><td>{slice.agent} / {slice.model ?? t("researchResults.defaultModel")}</td>
            <td>{slice.passed}/{slice.expected}</td><td>{slice.failed}</td><td>{slice.unsupported}</td><td>{slice.error}</td>
          </tr>)}</tbody>
        </table></div>
        <div className="safety-actions"><button disabled={rerunLoading} onClick={() => void previewForensic()}>
          {rerunLoading ? t("researchResults.safety.generatingRerun") : t("researchResults.safety.viewRerunDraft")}
        </button>
          {rerunPreviewed && <span role="status">{rerun.length > 0
            ? t("researchResults.safety.draftContains", { n: rerun.length })
            : t("researchResults.safety.noRerunNeeded")}</span>}
          {rerunError && <span className="danger" role="alert">{t("researchResults.safety.draftFailed", { error: rerunError })}</span>}</div>
      </section>}
    </div>}

    {activeTab === "evidence" && <div className="results-body">
      <div className="results-heading"><div><span>EVIDENCE</span><h2>{t("researchResults.evidence.title")}</h2><p>{t("researchResults.evidence.subtitle")}</p></div></div>
      <EvidenceWorkspace experimentId={experimentId} />
    </div>}

    {activeTab === "insights" && <div className="results-body">
      <ExperimentInsights experimentId={experimentId} embedded />
    </div>}

    {activeTab === "compare" && <div className="results-body">
      <div className="results-heading"><div><span>RAW / NORMALIZED COMPARE</span><h2>{t("researchResults.compare.title")}</h2>
        <p>{t("researchResults.compare.subtitle")}</p></div></div>
      <CompareWorkspace experimentId={experimentId} />
    </div>}

    {activeTab === "artifacts" && <div className="results-body">
      <div className="results-heading"><div><span>ARTIFACTS</span><h2>{t("researchResults.artifacts.title")}</h2>
        <p>{t("researchResults.artifacts.subtitle")}</p></div></div>
      <ArtifactsWorkspace experimentId={experimentId} />
    </div>}
  </div>;
}
