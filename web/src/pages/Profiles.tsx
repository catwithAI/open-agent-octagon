import { useEffect, useState } from "react";

import { discoverCapabilities, researchGet } from "../api/researchClient";
import { mutatorLabel } from "../researchUi";
import { useI18n } from "../i18n";

type ProfileSummary = {
  id: string;
  version: string;
  label: string;
  description: string;
  applies_to: { categories: string[]; modalities: string[] };
  limits: { max_cells: number; max_attempts: number | null };
  content_hash: string;
};
type ProfileDetail = ProfileSummary & {
  profile: {
    protocol: {
      repeats: number;
      variants: Array<{
        mutator_id: string;
        intensity: string;
        params?: Record<string, unknown>;
      }>;
      timeout_seconds: number;
      execution: string;
      max_concurrency: number | null;
      capture_policy: string;
      leader: { metric: string; tie_break: string };
    };
  };
};

// 各标签函数返回 i18n key（无匹配时回落原值），由组件用 t() 解析。
const executionLabelKey = (value: string): string => ({
  parallel: "profiles.execution.parallel",
  serial: "profiles.execution.serial",
}[value] ?? value);

const captureLabelKey = (value: string): string => ({
  off: "profiles.capture.off",
  metadata: "profiles.capture.metadata",
  parsed: "profiles.capture.parsed",
  full: "profiles.capture.full",
}[value] ?? value);

const metricLabelKey = (value: string): string => ({
  task_score: "profiles.metric.taskScore",
}[value] ?? value);

const tieBreakLabelKey = (value: string): string => ({
  candidate_id: "profiles.tieBreak.candidateId",
  duration: "profiles.tieBreak.duration",
}[value] ?? value);

const intensityLabelKey = (value: string): string => ({
  default: "profiles.intensity.default",
  light: "profiles.intensity.light",
  medium: "profiles.intensity.medium",
  heavy: "profiles.intensity.heavy",
}[value] ?? value);

const variantSettingLabelKey = (
  variant: ProfileDetail["profile"]["protocol"]["variants"][number],
): string => {
  const position = variant.params?.position;
  if (position === "head") return "profiles.variant.head";
  if (position === "tail") return "profiles.variant.tail";
  if (position === "shuffle") return "profiles.variant.shuffle";
  return intensityLabelKey(variant.intensity);
};

const modalityLabelKey = (value: string): string => ({
  text: "profiles.modality.text",
  image: "profiles.modality.image",
  audio: "profiles.modality.audio",
}[value] ?? value);

export function Profiles() {
  const { t, lang } = useI18n();
  const [items, setItems] = useState<ProfileSummary[]>([]);
  const [selected, setSelected] = useState<ProfileDetail | null>(null);
  const [status, setStatus] = useState(t("profiles.loading"));

  useEffect(() => {
    discoverCapabilities().then(async (result) => {
      if (!result.ok || !result.value.features.profiles) {
        setStatus(t("profiles.notEnabled"));
        return;
      }
      const catalog = await researchGet<{ items: ProfileSummary[] }>("/api/profiles");
      setItems(catalog.items);
      setStatus("");
      if (catalog.items[0]) {
        setSelected(await researchGet<ProfileDetail>(
          `/api/profiles/${encodeURIComponent(catalog.items[0].id)}?version=${encodeURIComponent(catalog.items[0].version)}`,
        ));
      }
    }).catch((reason) => setStatus(String(reason)));
  }, []);

  const inspect = async (item: ProfileSummary) => {
    setSelected(await researchGet<ProfileDetail>(
      `/api/profiles/${encodeURIComponent(item.id)}?version=${encodeURIComponent(item.version)}`,
    ));
  };

  return <div className="profiles-page">
    <div className="resource-heading"><span>{t("profiles.eyebrow")}</span>
      <div className="resource-title-line"><h2>{t("profiles.title")}</h2><strong className="development-badge">{t("profiles.developmentBadge")}</strong></div>
      <p>{t("profiles.intro")}</p>
      <p className="development-note">{t("profiles.developmentNote")}</p>
    </div>
    {status && <div className="card" role="status">{status}</div>}
    <div className="profiles-layout">
      <div className="profile-catalog">
        {items.map((item) => <button className={selected?.id === item.id ? "is-selected" : ""} key={`${item.id}:${item.version}`} onClick={() => void inspect(item)}>
          <span><strong>{item.label}</strong><code>{item.id}@{item.version}</code></span>
          <p>{item.description}</p>
          <div><span>{t("profiles.maxCells", { n: item.limits.max_cells })}</span><span>{item.applies_to.modalities.map((m) => t(modalityLabelKey(m))).join(" / ")}</span></div>
        </button>)}
      </div>
      <aside className="profile-inspector">
        <div className="profile-inspector-head"><span>{t("profiles.inspectorHead")}</span>{selected && <code>{selected.id}@{selected.version}</code>}</div>
        {selected ? <div className="profile-inspector-body">
          <h3>{selected.label}</h3><p>{selected.description}</p>
          <dl>
            <div><dt>{t("profiles.repeats")}</dt><dd>{selected.profile.protocol.repeats}</dd></div>
            <div><dt>{t("profiles.executionLabel")}</dt><dd>{t(executionLabelKey(selected.profile.protocol.execution))}</dd></div>
            <div><dt>{t("profiles.maxConcurrency")}</dt><dd>{selected.profile.protocol.max_concurrency ?? t("profiles.systemDecides")}</dd></div>
            <div><dt>{t("profiles.timeout")}</dt><dd>{t("profiles.timeoutSeconds", { n: selected.profile.protocol.timeout_seconds })}</dd></div>
            <div><dt>{t("profiles.captureLabel")}</dt><dd>{t(captureLabelKey(selected.profile.protocol.capture_policy))}</dd></div>
            <div><dt>{t("profiles.leader")}</dt><dd>{t(metricLabelKey(selected.profile.protocol.leader.metric))} / {t(tieBreakLabelKey(selected.profile.protocol.leader.tie_break))}</dd></div>
          </dl>
          <div className="profile-variants"><span>{t("profiles.variants")}</span>{selected.profile.protocol.variants.map((variant, index) =>
            <div key={`${variant.mutator_id}:${index}`}><strong>{mutatorLabel(variant.mutator_id, lang)}</strong><small>{t(variantSettingLabelKey(variant))}</small></div>)}</div>
          <div className="profile-hash"><span>{t("profiles.contentHash")}</span><code>{selected.content_hash}</code></div>
          <button className="btn" disabled title={t("profiles.applyTitle")}>{t("profiles.applyToNew")}</button>
        </div> : <div className="results-empty">{t("profiles.selectToInspect")}</div>}
      </aside>
    </div>
  </div>;
}
