import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { discoverCapabilities, researchGet } from "../api/researchClient";
import { reasonLabel, statusLabel } from "../researchUi";
import { useI18n } from "../i18n";

type BestScore = { agent: string; best_score: number | null };

type ExperimentRow = {
  id: string;
  title: string;
  question?: string;
  env_name: string;
  status: string;
  protocol_hash: string;
  created_at: string;
  best_scores: BestScore[];
};

function formatCreatedAt(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString("zh-CN", { hour12: false });
}

export function ExperimentList() {
  const { t, lang } = useI18n();
  const [items, setItems] = useState<ExperimentRow[] | null>(null);
  const [unavailable, setUnavailable] = useState<string[]>([]);
  const [error, setError] = useState("");
  useEffect(() => {
    discoverCapabilities().then(async (result) => {
      if (!result.ok) { setError(result.error.message); return; }
      if (!result.value.features.experiments) {
        setUnavailable(result.value.details.experiments?.unavailable_reasons ?? ["unavailable"]);
        setItems([]);
        return;
      }
      setItems((await researchGet<{ items: ExperimentRow[] }>("/api/experiments")).items);
    }).catch((reason) => setError(String(reason)));
  }, []);
  return <div>
    <div className="page-heading"><div><h2>{t("experimentList.title")}</h2><p>{t("experimentList.subtitle")}</p></div>
      <Link className="btn" to="/experiments/new">{t("experimentList.newExperiment")}</Link></div>
    {error && <div className="error" role="alert">{error}</div>}
    {unavailable.length > 0 && <div className="card" role="status">{t("experimentList.unavailable", { reasons: unavailable.map((r) => reasonLabel(r, lang)).join(lang === "en" ? ", " : "、") })}</div>}
    {items === null && <p>{t("experimentList.loading")}</p>}
    {items?.length === 0 && unavailable.length === 0 && <div className="empty">{t("experimentList.empty")}</div>}
    {items && items.length > 0 && <table><thead><tr><th>{t("experimentList.col.experiment")}</th><th>{t("experimentList.col.question")}</th><th>{t("experimentList.col.scenario")}</th><th>{t("experimentList.col.status")}</th><th>{t("experimentList.col.bestScore")}</th><th>{t("experimentList.col.submittedAt")}</th></tr></thead>
      <tbody>{items.map((item) => <tr key={item.id}><td><Link to={`/experiments/${item.id}`}>{item.title}</Link></td>
        <td className="muted">{item.question ?? "—"}</td>
        <td>{item.env_name}</td><td>{statusLabel(item.status, lang)}</td>
        <td>
          {item.best_scores.map((s) => (
            <span key={s.agent} className="agent-chip" data-status={s.best_score == null ? "timeout" : "completed"}>
              {s.agent}{s.best_score != null ? ` ${s.best_score}` : ""}
            </span>
          ))}
          {item.best_scores.length === 0 && <span className="muted">—</span>}
        </td>
        <td>{formatCreatedAt(item.created_at)}</td></tr>)}</tbody></table>}
  </div>;
}
