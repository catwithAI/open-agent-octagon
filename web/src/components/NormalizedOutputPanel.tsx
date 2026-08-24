import { useEffect, useState } from "react";

import { createIdempotencyKey } from "../api/idempotency";
import { discoverCapabilities, researchGet, researchMutation } from "../api/researchClient";
import { useI18n } from "../i18n";

type TFn = (key: string, vars?: Record<string, string | number>) => string;

type NormalizedResponse = {
  status: "not_generated" | "current" | "stale" | string;
  raw_ref: string;
  generation_available: boolean;
  generation_unavailable_reason: string | null;
  normalized: { output: unknown; producer_version: string; warnings: string[] } | null;
};

const unavailableLabel = (reason: string | null, t: TFn): string =>
  reason === "source_not_finalized"
    ? t("normalizedOutput.unavailable.notFinalized")
    : reason === "final_output_unavailable"
      ? t("normalizedOutput.unavailable.noFinalOutput")
      : t("normalizedOutput.unavailable.nothingToNormalize");

export function NormalizedOutputPanel({
  attemptId,
  rawOutput,
}: {
  attemptId: string;
  rawOutput?: unknown;
}) {
  const { t } = useI18n();
  const [mode, setMode] = useState<"raw" | "normalized">("raw");
  const [data, setData] = useState<NormalizedResponse | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const hasRawOutput = rawOutput !== undefined
    && rawOutput !== null
    && (typeof rawOutput !== "object" || Object.keys(rawOutput as object).length > 0);

  const load = async () => {
    try {
      setData(await researchGet<NormalizedResponse>(`/api/attempts/${encodeURIComponent(attemptId)}/normalized`));
    } catch (reason) { setError(String(reason)); }
  };

  useEffect(() => {
    void load();
  }, [attemptId]);

  const generate = async () => {
    setBusy(true);
    setError("");
    try {
      const caps = await discoverCapabilities();
      if (!caps.ok || !caps.value.features.normalized_output) {
        setError(t("normalizedOutput.notEnabled"));
        return;
      }
      await researchMutation(
        caps.value,
        "normalized_output",
        `/api/attempts/${encodeURIComponent(attemptId)}/normalized/generate`,
        undefined,
        { "Idempotency-Key": createIdempotencyKey() },
      );
      const value = await researchGet<NormalizedResponse>(
        `/api/attempts/${encodeURIComponent(attemptId)}/normalized`,
      );
      setData(value);
      if (value.status === "current") setMode("normalized");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };

  return <details>
    <summary>{t("normalizedOutput.summary")}</summary>
    <div role="tablist" aria-label={t("normalizedOutput.outputForm")}>
      <button role="tab" aria-selected={mode === "raw"} onClick={() => setMode("raw")}>{t("normalizedOutput.tab.raw")}</button>
      {data?.status === "current" && <button role="tab" aria-selected={mode === "normalized"} onClick={() => setMode("normalized")}>{t("normalizedOutput.tab.normalized")}</button>}
    </div>
    {mode === "raw" && <div>
      <p>{t("normalizedOutput.rawAuthoritative")}</p>
      {hasRawOutput
        ? <pre>{JSON.stringify(rawOutput, null, 2)}</pre>
        : <p role="status">{t("normalizedOutput.noFinalOutput")}</p>}
      {data?.status === "not_generated" && data.generation_available && <button disabled={busy} onClick={() => void generate()}>
        {busy ? t("normalizedOutput.generating") : t("normalizedOutput.generate")}
      </button>}
      {data?.status === "not_generated" && !data.generation_available && <p className="muted">
        {unavailableLabel(data.generation_unavailable_reason, t)}
      </p>}
      {data?.status === "stale" && data.generation_available && <button disabled={busy} onClick={() => void generate()}>
        {busy ? t("normalizedOutput.regenerating") : t("normalizedOutput.regenerate")}
      </button>}
      {error && <div className="error" role="alert">{error}</div>}
    </div>}
    {mode === "normalized" && <div>
      {error && <div className="error" role="alert">{error}</div>}
      {data?.status === "stale" && <p role="status">{t("normalizedOutput.staleNote")}</p>}
      {data?.status === "current" && <>
        <p>{t("normalizedOutput.derivedVersion", { version: data.normalized?.producer_version ?? "" })}</p>
        <pre>{JSON.stringify(data.normalized?.output, null, 2)}</pre>
      </>}
    </div>}
  </details>;
}
