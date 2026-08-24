import { useEffect, useMemo, useState } from "react";

import { discoverCapabilities } from "../api/researchClient";
import type { ResearchCapabilities } from "../api/researchTypes";
import { capabilityLabel, reasonLabel } from "../researchUi";
import { useI18n } from "../i18n";

export function SystemCapabilities() {
  const { t, lang } = useI18n();
  const [caps, setCaps] = useState<ResearchCapabilities | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    discoverCapabilities().then((result) =>
      result.ok ? setCaps(result.value) : setError(result.error.message),
    ).catch((reason) => setError(String(reason)));
  }, []);
  const entries = useMemo(() => Object.entries(caps?.features ?? {}), [caps]);
  const enabled = entries.filter(([, value]) => value).length;

  return <div className="system-page">
    <div className="resource-heading"><span>SYSTEM STATUS</span><h2>{t("systemCapabilities.title")}</h2>
      <p>{t("systemCapabilities.description")}</p></div>
    {error && <div className="error">{error}</div>}
    <div className="system-summary">
      <article><span>{t("systemCapabilities.contract")}</span><strong>{caps?.schema_version ?? t("systemCapabilities.loading")}</strong><small>{t("systemCapabilities.contractDesc")}</small></article>
      <article><span>{t("systemCapabilities.enabled")}</span><strong>{caps ? `${enabled}/${entries.length}` : "—"}</strong><small>{t("systemCapabilities.enabledDesc")}</small></article>
      <article className={caps && enabled < entries.length ? "is-warning" : ""}><span>{t("systemCapabilities.limited")}</span><strong>{caps ? entries.length - enabled : "—"}</strong><small>{t("systemCapabilities.limitedDesc")}</small></article>
    </div>
    <div className="capability-grid">{entries.map(([name, available]) => {
      const detail = caps?.details[name];
      return <article key={name} className={available ? "is-enabled" : "is-disabled"}>
        <div className="capability-card-head"><span className="shell-status-dot" /><div><strong>{capabilityLabel(name, lang)}</strong><code>{name}</code></div>
          <em>{available ? t("systemCapabilities.available") : t("systemCapabilities.unavailable")}</em></div>
        <dl>
          <div><dt>Database Schema</dt><dd>{detail?.schema_ready ? t("systemCapabilities.ready") : t("systemCapabilities.notReady")}</dd></div>
          <div><dt>Dependencies</dt><dd>{detail?.dependencies_ready ? t("systemCapabilities.ready") : t("systemCapabilities.missing")}</dd></div>
        </dl>
        {!available && <div className="capability-reasons">{detail?.unavailable_reasons.map((reason) =>
          <span key={reason}>{reasonLabel(reason, lang)}</span>) || <span>{t("systemCapabilities.noReason")}</span>}</div>}
      </article>;
    })}</div>
  </div>;
}
