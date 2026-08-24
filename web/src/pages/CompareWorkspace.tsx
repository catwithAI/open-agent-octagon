import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { api, type AttemptDetail } from "../api/client";
import { discoverCapabilities, researchGet } from "../api/researchClient";
import { mutatorLabel, statusLabel } from "../researchUi";
import type { Lang } from "../researchUi";
import { useI18n } from "../i18n";

type TFn = (key: string, vars?: Record<string, string | number>) => string;

type AttemptOption = {
  category: "result";
  run_id: string;
  attempt_id: string;
  agent: string;
  model: string | null;
  status: string;
  score_total: number | null;
  variant_id?: string | null;
  mutator_id?: string | null;
  repeat_index?: number | null;
  final_result?: {
    state: "captured" | "missing" | string;
  };
};

type EvidenceResultIndex = {
  items: AttemptOption[];
};

type NormalizedResponse = {
  status: "not_generated" | "current" | "stale" | string;
  raw_ref: string;
  normalized: {
    output?: unknown;
    content?: unknown;
    producer_version?: string;
    warnings?: string[];
  } | null;
};

type LoadedAttempt = {
  option: AttemptOption;
  raw: AttemptDetail | null;
  normalized: NormalizedResponse | null;
  error: string;
};

const emptyLoaded = (option: AttemptOption): LoadedAttempt => ({
  option,
  raw: null,
  normalized: null,
  error: "",
});

function hasOutput(value: unknown): boolean {
  if (value === null || value === undefined) return false;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === "object") return Object.keys(value).length > 0;
  return String(value).trim().length > 0;
}

function attemptGroupLabel(option: AttemptOption, t: TFn, lang: Lang): string {
  const variant = mutatorLabel(option.mutator_id ?? "baseline", lang);
  return option.repeat_index === null || option.repeat_index === undefined
    ? variant
    : `${variant} · ${t("compareWorkspace.repeatIndex", { n: option.repeat_index + 1 })}`;
}

export function CompareWorkspace({ experimentId }: { experimentId: string }) {
  const { t, lang } = useI18n();
  const [options, setOptions] = useState<AttemptOption[]>([]);
  const [leftId, setLeftId] = useState("");
  const [rightId, setRightId] = useState("");
  const [mode, setMode] = useState<"raw" | "normalized">("raw");
  const [normalizedEnabled, setNormalizedEnabled] = useState<boolean | null>(null);
  const [loaded, setLoaded] = useState<Record<string, LoadedAttempt>>({});
  const [error, setError] = useState("");
  const [indexLoaded, setIndexLoaded] = useState(false);

  useEffect(() => {
    setIndexLoaded(false);
    setError("");
    Promise.all([
      researchGet<EvidenceResultIndex>(
        `/api/experiments/${encodeURIComponent(experimentId)}/evidence?category=result&limit=200`,
      ),
      discoverCapabilities(),
    ]).then(([result, capabilities]) => {
      setOptions(result.items);
      const comparable = result.items.filter((item) => item.final_result?.state === "captured");
      setLeftId(comparable[0]?.attempt_id || "");
      setRightId(comparable[1]?.attempt_id || "");
      setNormalizedEnabled(capabilities.ok && capabilities.value.features.normalized_output === true);
      setIndexLoaded(true);
    }).catch((reason) => {
      setError(String(reason));
      setIndexLoaded(true);
    });
  }, [experimentId]);

  const comparableOptions = useMemo(
    () => options.filter((option) => option.final_result?.state === "captured"),
    [options],
  );
  const selectedOptions = useMemo(() => [leftId, rightId].map((attemptId) =>
    comparableOptions.find((option) => option.attempt_id === attemptId)).filter(
    (option): option is AttemptOption => Boolean(option),
  ), [comparableOptions, leftId, rightId]);
  const optionGroups = useMemo(() => {
    const groups = new Map<string, { key: string; label: string; items: AttemptOption[] }>();
    for (const option of comparableOptions) {
      const key = `${option.variant_id ?? option.mutator_id ?? "baseline"}:${option.repeat_index ?? "unknown"}`;
      const group = groups.get(key) ?? { key, label: attemptGroupLabel(option, t, lang), items: [] };
      group.items.push(option);
      groups.set(key, group);
    }
    return [...groups.values()];
  }, [comparableOptions, t, lang]);

  useEffect(() => {
    for (const option of selectedOptions) {
      const existing = loaded[option.attempt_id];
      if (existing?.raw && (mode === "raw" || existing.normalized)) continue;
      setLoaded((current) => ({
        ...current,
        [option.attempt_id]: current[option.attempt_id] ?? emptyLoaded(option),
      }));
      const rawRequest = existing?.raw
        ? Promise.resolve(existing.raw)
        : api.getAttempt(option.run_id, option.attempt_id, false);
      const normalizedRequest = mode === "normalized" && normalizedEnabled
        ? researchGet<NormalizedResponse>(
          `/api/attempts/${encodeURIComponent(option.attempt_id)}/normalized`,
        )
        : Promise.resolve(existing?.normalized ?? null);
      Promise.all([rawRequest, normalizedRequest]).then(([raw, normalized]) => {
        setLoaded((current) => ({
          ...current,
          [option.attempt_id]: { option, raw, normalized, error: "" },
        }));
      }).catch((reason) => {
        setLoaded((current) => ({
          ...current,
          [option.attempt_id]: {
            ...(current[option.attempt_id] ?? emptyLoaded(option)),
            error: String(reason),
          },
        }));
      });
    }
  // loaded is intentionally read as a cache and updated by this effect.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, normalizedEnabled, selectedOptions]);

  const select = (side: "left" | "right", value: string) => {
    if (side === "left") setLeftId(value);
    else setRightId(value);
  };

  return <div className="compare-workspace">
    <div className="compare-toolbar">
      <div role="tablist" aria-label={t("compareWorkspace.outputForm")}>
        <button role="tab" aria-selected={mode === "raw"} className={mode === "raw" ? "is-active" : ""} onClick={() => setMode("raw")}>{t("compareWorkspace.tab.raw")}</button>
        <button role="tab" aria-selected={mode === "normalized"} className={mode === "normalized" ? "is-active" : ""} disabled={normalizedEnabled !== true} onClick={() => setMode("normalized")}>{t("compareWorkspace.tab.normalized")}</button>
      </div>
      <span>{mode === "raw" ? t("compareWorkspace.hint.raw") : t("compareWorkspace.hint.normalized")}</span>
    </div>
    {error && <div className="error" role="alert">{error}</div>}
    {normalizedEnabled === null && mode === "raw" && <div className="compare-capability-note">{t("compareWorkspace.confirmingNormalized")}</div>}
    {normalizedEnabled === false && mode === "raw" && <div className="compare-capability-note">{t("compareWorkspace.normalizedDisabled")}</div>}
    {indexLoaded && !error && comparableOptions.length < 2 && <div className="compare-unavailable">
      <strong>{comparableOptions.length === 0
        ? t("compareWorkspace.noCandidates")
        : t("compareWorkspace.onlyOneCandidate")}</strong>
      <p>{t("compareWorkspace.insufficientResults")}</p>
      <div>
        {options.map((option) => <Link key={option.attempt_id} to={`/runs/${option.run_id}`}>
          <span>{option.agent} · {option.model ?? t("compareWorkspace.defaultModel")}</span>
          <small>{attemptGroupLabel(option, t, lang)} · {statusLabel(option.status, lang)} · {t("compareWorkspace.viewEvidence")}</small>
        </Link>)}
      </div>
    </div>}
    {(!indexLoaded || comparableOptions.length >= 2) && <div className="compare-columns">
      {(["left", "right"] as const).map((side, index) => {
        const attemptId = side === "left" ? leftId : rightId;
        const item = loaded[attemptId];
        const option = comparableOptions.find((candidate) => candidate.attempt_id === attemptId);
        const normalized = item?.normalized;
        const normalizedContent = normalized?.normalized?.output ?? normalized?.normalized?.content;
        return <section className="compare-column" key={side}>
          <div className="compare-selector">
            <label>{index === 0 ? t("compareWorkspace.candidateA") : t("compareWorkspace.candidateB")}<select value={attemptId} onChange={(event) => select(side, event.target.value)}>
              {optionGroups.map((group) => <optgroup key={group.key} label={group.label}>
                {group.items.map((candidate) => <option key={candidate.attempt_id} value={candidate.attempt_id}>
                  {candidate.agent} · {candidate.model ?? t("compareWorkspace.defaultModel")} · {statusLabel(candidate.status, lang)}
                  {candidate.score_total === null ? ` · ${t("compareWorkspace.noScore")}` : ` · ${t("compareWorkspace.scorePoints", { n: candidate.score_total })}`}
                </option>)}
              </optgroup>)}
            </select></label>
          </div>
          {option && <div className="compare-attempt-head">
            <span><strong>{option.agent}</strong><small>{attemptGroupLabel(option, t, lang)} · {option.model ?? t("compareWorkspace.defaultModel")}</small></span>
            <span><strong>{option.score_total ?? "—"}</strong><small>{statusLabel(option.status, lang)}</small></span>
          </div>}
          {item?.error && <div className="error" role="alert">{item.error}</div>}
          {!item?.raw && !item?.error && <div className="results-empty">{t("compareWorkspace.loadingAttempt")}</div>}
          {mode === "raw" && item?.raw && <div className="compare-output">
            <div className="compare-output-head"><span>RAW / final_state.json</span><strong>{t("compareWorkspace.authoritativeSource")}</strong></div>
            {hasOutput(item.raw.final_state)
              ? <pre>{JSON.stringify(item.raw.final_state, null, 2)}</pre>
              : <div className="compare-output-empty">
                <strong>{t("compareWorkspace.noComparableOutput", { status: statusLabel(option?.status ?? item.raw.status, lang) })}</strong>
                <p>{t("compareWorkspace.emptyObjectNote")}</p>
              </div>}
          </div>}
          {mode === "normalized" && item?.raw && <div className="compare-output">
            <div className="compare-output-head"><span>NORMALIZED</span><strong className="derived">{t("compareWorkspace.derivedData")}</strong></div>
            {normalized?.status === "current" && <>
              <div className="compare-derived-meta">{t("compareWorkspace.producer")} {normalized.normalized?.producer_version ?? t("compareWorkspace.unknownVersion")}</div>
              <pre>{JSON.stringify(normalizedContent, null, 2)}</pre>
            </>}
            {normalized?.status === "stale" && <div className="results-empty">{t("compareWorkspace.staleNormalized")}</div>}
            {normalized?.status === "not_generated" && <div className="results-empty">{t("compareWorkspace.notGeneratedNormalized")}</div>}
            {!normalized && <div className="results-empty">{t("compareWorkspace.loadingDerived")}</div>}
          </div>}
          {option && <Link className="compare-deep-link" to={`/runs/${option.run_id}`}>{t("compareWorkspace.openRunDetail")}</Link>}
        </section>;
      })}
      {!indexLoaded && <div className="results-empty">{t("compareWorkspace.loadingComparable")}</div>}
    </div>}
  </div>;
}
