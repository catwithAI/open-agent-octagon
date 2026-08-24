import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api/client";
import { researchGet } from "../api/researchClient";
import { useI18n } from "../i18n";

type ArtifactRecord = {
  category: "artifact";
  run_id: string;
  attempt_id: string;
  agent: string;
  model: string | null;
  anchor: string;
  resolution?: {
    status?: string;
    metadata?: { relative_ref?: string; size?: number; [key: string]: unknown };
  };
};

type ArtifactIndex = {
  items: ArtifactRecord[];
  total: number;
  selection_manifest: {
    truncated: boolean;
    omitted_by_category: Record<string, number>;
  };
};

const extension = (path: string) => path.includes(".")
  ? path.split(".").pop()?.toUpperCase() ?? "FILE"
  : "FILE";

export function ArtifactsWorkspace({ experimentId }: { experimentId: string }) {
  const { t } = useI18n();
  const [index, setIndex] = useState<ArtifactIndex | null>(null);
  const [query, setQuery] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    researchGet<ArtifactIndex>(
      `/api/experiments/${encodeURIComponent(experimentId)}/evidence?category=artifact&limit=200`,
    ).then(setIndex).catch((reason) => setError(String(reason)));
  }, [experimentId]);

  const items = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    if (!normalized) return index?.items ?? [];
    return (index?.items ?? []).filter((item) => {
      const path = item.resolution?.metadata?.relative_ref ?? "";
      return path.toLowerCase().includes(normalized)
        || item.agent.toLowerCase().includes(normalized)
        || item.attempt_id.toLowerCase().includes(normalized);
    });
  }, [index, query]);

  return <div className="artifacts-workspace">
    <div className="artifacts-toolbar">
      <label>{t("artifactsWorkspace.searchArtifacts")}<input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={t("artifactsWorkspace.searchPlaceholder")} /></label>
      <span>{t("artifactsWorkspace.itemCount", { shown: items.length, total: index?.total ?? 0 })}</span>
    </div>
    {error && <div className="error" role="alert">{error}</div>}
    {index?.selection_manifest.truncated && <div className="compare-capability-note">
      {t("artifactsWorkspace.truncated", { n: index.selection_manifest.omitted_by_category.artifact ?? 0 })}
    </div>}
    <div className="artifact-index-grid">
      {items.map((item) => {
        const path = item.resolution?.metadata?.relative_ref;
        const status = item.resolution?.status ?? "metadata";
        return <article key={item.anchor}>
          <div className="artifact-type">{extension(path ?? "")}</div>
          <div className="artifact-index-main">
            <strong>{path ?? t("artifactsWorkspace.unresolvedRef")}</strong>
            <small>{item.agent} · {item.model ?? t("artifactsWorkspace.defaultModel")}</small>
            <code>{item.attempt_id}</code>
          </div>
          <div className="artifact-index-meta">
            <span className={`artifact-status status-${status}`}>{status}</span>
            {typeof item.resolution?.metadata?.size === "number" && <small>{item.resolution.metadata.size} bytes</small>}
          </div>
          <div className="artifact-index-actions">
            {path && <a href={api.artifactUrl(item.run_id, item.attempt_id, path)}>{t("artifactsWorkspace.openArtifact")}</a>}
            <Link to={`/runs/${item.run_id}`}>RunDetail →</Link>
          </div>
        </article>;
      })}
      {index && items.length === 0 && <div className="results-empty">{t("artifactsWorkspace.noArtifacts")}</div>}
      {!index && !error && <div className="results-empty">{t("artifactsWorkspace.loadingIndex")}</div>}
    </div>
  </div>;
}
