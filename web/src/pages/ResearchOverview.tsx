import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { discoverCapabilities, researchGet } from "../api/researchClient";
import type { ResearchCapabilities } from "../api/researchTypes";
import { statusLabel } from "../researchUi";
import { useI18n } from "../i18n";

type ExperimentSummary = {
  id: string;
  title: string;
  env_name: string;
  status: string;
  created_at: string;
};

const ACTIVE_STATUSES = new Set(["planned", "queued", "running", "cancelling"]);
const FINISHED_STATUSES = new Set(["completed", "partial", "failed", "cancelled"]);

export function ResearchOverview() {
  const { t, lang } = useI18n();
  const [caps, setCaps] = useState<ResearchCapabilities | null>(null);
  const [items, setItems] = useState<ExperimentSummary[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    discoverCapabilities().then(async (result) => {
      if (!result.ok) {
        setError(result.error.message);
        return;
      }
      setCaps(result.value);
      if (result.value.features.experiments) {
        const list = await researchGet<{ items: ExperimentSummary[] }>("/api/experiments?limit=20");
        setItems(list.items);
      }
    }).catch((reason) => setError(String(reason)));
  }, []);

  const active = useMemo(() => items.filter((item) => ACTIVE_STATUSES.has(item.status)), [items]);
  const completed = useMemo(() => items.filter((item) => FINISHED_STATUSES.has(item.status)), [items]);
  const enabledFeatures = caps ? Object.values(caps.features).filter(Boolean).length : 0;
  const featureCount = caps ? Object.keys(caps.features).length : 0;

  return <div className="research-overview">
    <div className="research-stat-grid" aria-label={t("researchOverview.summaryAria")}>
      <article className="research-stat">
        <span>{t("researchOverview.runningExperiments")}</span><strong>{active.length}</strong><small>{t("researchOverview.runningDesc")}</small>
      </article>
      <article className="research-stat">
        <span>{t("researchOverview.recentlyCompleted")}</span><strong>{completed.length}</strong><small>{t("researchOverview.recentlyCompletedDesc")}</small>
      </article>
      <article className="research-stat">
        <span>{t("researchOverview.securityEvents")}</span><strong>—</strong><small>{t("researchOverview.securityEventsDesc")}</small>
      </article>
      <article className="research-stat">
        <span>{t("researchOverview.systemCapabilities")}</span><strong>{caps ? `${enabledFeatures}/${featureCount}` : "…"}</strong>
        <small>{caps?.features.experiments ? t("researchOverview.workspaceAvailable") : t("researchOverview.workspaceLimited")}</small>
      </article>
    </div>

    {error && <div className="error" role="alert">{error}</div>}

    <section className="research-continue">
      <div className="research-panel-head">
        <div><span className="signal-dot" />{t("researchOverview.continue")}</div>
        <Link to="/experiments">{t("researchOverview.viewAll")}</Link>
      </div>
      {active[0] ? <div className="research-continue-body">
        <div><small>{active[0].env_name}</small><h2>{active[0].title}</h2>
          <p>{statusLabel(active[0].status, lang)} · {t("researchOverview.continueHint")}</p></div>
        <Link className="btn" to={`/experiments/${active[0].id}`}>{t("researchOverview.openExperiment")}</Link>
      </div> : <div className="research-empty-row">
        <span>{t("researchOverview.noActive")}</span><Link className="btn" to="/experiments/new">{t("researchOverview.newExperiment")}</Link>
      </div>}
    </section>

    <div className="research-overview-columns">
      <section className="research-panel">
        <div className="research-panel-head"><div>{t("researchOverview.recentExperiments")}</div><Link to="/experiments">{t("researchOverview.experimentList")}</Link></div>
        <div className="research-list">
          {items.slice(0, 5).map((item) => <Link key={item.id} to={`/experiments/${item.id}`}>
            <span><strong>{item.title}</strong><small>{item.env_name}</small></span>
            <span>{statusLabel(item.status, lang)}</span>
          </Link>)}
          {items.length === 0 && <div className="research-empty-row">{t("researchOverview.noExperiments")}</div>}
        </div>
      </section>
      <section className="research-panel">
        <div className="research-panel-head"><div>{t("researchOverview.quickLinks")}</div></div>
        <div className="research-quick-links">
          <Link to="/experiments/new"><strong>{t("researchOverview.newExperiment")}</strong><span>{t("researchOverview.newExperimentDesc")}</span></Link>
          <Link to="/scenarios"><strong>{t("researchOverview.browseScenarios")}</strong><span>{t("researchOverview.browseScenariosDesc")}</span></Link>
        </div>
      </section>
    </div>
  </div>;
}
