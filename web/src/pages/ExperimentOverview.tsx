import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { api, type EnvMeta } from "../api/client";
import { researchGet } from "../api/researchClient";
import { statusLabel } from "../researchUi";
import { useI18n } from "../i18n";

type Protocol = {
  compare_mode?: string;
  agents?: Array<{ agent: string; model: string | null }>;
  repeats?: number;
  execution?: string;
  timeout_seconds?: number | null;
  notify_model_of_timeout?: boolean;
  capture_policy?: string;
};

type Bundle = {
  experiment: {
    id: string; title: string; env_name: string; question: string;
    status: string; protocol_hash: string;
    source_task_id?: string | null; protocol_json?: string;
  };
  variants: Array<{ id: string; mutator_id: string; status: string }>;
  groups: Array<{ id: string; status: string; total_cells: number }>;
};

type TFn = (key: string, vars?: Record<string, string | number>) => string;

function formatTimeout(seconds: number | null | undefined, t: TFn): string {
  if (seconds == null) return t("experimentOverview.timeout.unlimited");
  if (seconds % 60 === 0) return t("experimentOverview.timeout.minutes", { n: seconds / 60 });
  return t("experimentOverview.timeout.minutesSeconds", {
    m: Math.floor(seconds / 60),
    s: seconds % 60,
  });
}

export function ExperimentOverview() {
  const { t, lang } = useI18n();
  const { experimentId = "" } = useParams();
  const [bundle, setBundle] = useState<Bundle | null>(null);
  const [envMeta, setEnvMeta] = useState<EnvMeta | null>(null);
  const [showYaml, setShowYaml] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    researchGet<Bundle>(`/api/experiments/${encodeURIComponent(experimentId)}`)
      .then(setBundle).catch((reason) => setError(String(reason)));
  }, [experimentId]);

  // 场景 meta 是实验的另一半上下文：评分维度、及格线、运行前置都在这里。
  // 只看协议看不出「这个分数是怎么算出来的」。加载失败不阻断整页。
  useEffect(() => {
    const name = bundle?.experiment.env_name;
    if (!name) return;
    api.getEnvMeta(name).then(setEnvMeta).catch(() => setEnvMeta(null));
  }, [bundle?.experiment.env_name]);

  if (error) return <div className="error" role="alert">{error}</div>;
  if (!bundle) return <p>{t("experimentOverview.loading")}</p>;

  let protocol: Protocol = {};
  try {
    protocol = JSON.parse(bundle.experiment.protocol_json ?? "{}") as Protocol;
  } catch { /* 协议不可解析时降级展示，不阻断整页 */ }

  const meta = (envMeta?.meta ?? {}) as {
    pass_threshold?: number;
    dimensions?: Array<{ name: string; weight: number; description?: string }>;
    prerequisites?: { summary?: string };
    test_focus?: string;
  };
  const dimensions = meta.dimensions ?? [];

  return <div><h2>{bundle.experiment.title}</h2>
    <p>{bundle.experiment.question}</p>

    <div className="overview-grid">
      <section className="card"><h3>{t("experimentOverview.config")}</h3><dl>
        <dt>{t("experimentOverview.status")}</dt><dd>{statusLabel(bundle.experiment.status, lang)}</dd>
        <dt>{t("experimentOverview.scenario")}</dt><dd>
          <Link to={`/scenarios/${encodeURIComponent(bundle.experiment.env_name)}`}>
            {bundle.experiment.env_name}
          </Link>
        </dd>
        {bundle.experiment.source_task_id && <>
          <dt>{t("experimentOverview.task")}</dt><dd><code>{bundle.experiment.source_task_id}</code></dd>
        </>}
        <dt>{t("experimentOverview.compareMode")}</dt><dd>{protocol.compare_mode ?? "—"}</dd>
        {/* 超时最常被追问：它只覆盖 Agent 执行，不含排队与评分 */}
        <dt>{t("experimentOverview.taskTimeout")}</dt><dd>
          {formatTimeout(protocol.timeout_seconds, t)}
          <small className="muted">
            {protocol.notify_model_of_timeout
              ? t("experimentOverview.timeout.modelNotified")
              : t("experimentOverview.timeout.modelNotNotified")}
          </small>
        </dd>
        <dt>{t("experimentOverview.repeats")}</dt><dd>{protocol.repeats ?? "—"}</dd>
        <dt>{t("experimentOverview.execution")}</dt><dd>{protocol.execution ?? "—"}</dd>
        <dt>{t("experimentOverview.capturePolicy")}</dt><dd>{protocol.capture_policy ?? "—"}</dd>
        <dt>{t("experimentOverview.protocol")}</dt><dd><code>{bundle.experiment.protocol_hash}</code></dd>
      </dl></section>

      <section className="card"><h3>{t("experimentOverview.platforms")}</h3>
        {(protocol.agents ?? []).length === 0
          ? <p className="muted">{t("experimentOverview.noPlatforms")}</p>
          : <dl>{(protocol.agents ?? []).map((item) => <div key={item.agent}>
              <dt>{item.agent}</dt><dd><code>{item.model ?? t("experimentOverview.defaultModel")}</code></dd>
            </div>)}</dl>}
      </section>
    </div>

    <section className="card"><h3>{t("experimentOverview.scenarioDef")}</h3>
      {envMeta === null
        ? <p className="muted">{t("experimentOverview.metaLoading")}</p>
        : <>
            {meta.test_focus && <p>{meta.test_focus}</p>}
            <dl>
              <dt>{t("experimentOverview.passThreshold")}</dt><dd>{meta.pass_threshold ?? "—"}</dd>
              {meta.prerequisites?.summary && <>
                <dt>{t("experimentOverview.prerequisites")}</dt><dd>{meta.prerequisites.summary}</dd>
              </>}
            </dl>
            {dimensions.length > 0 && <div className="scenario-inputs">
              {dimensions.map((dim) => <div className="scenario-input-row" key={dim.name}>
                <code>{dim.name}</code>
                <small>{t("experimentOverview.weight")} {dim.weight}{dim.description ? ` · ${dim.description}` : ""}</small>
              </div>)}
            </div>}
            <button className="tab" onClick={() => setShowYaml((v) => !v)}>
              {showYaml ? t("experimentOverview.hideYaml") : t("experimentOverview.showYaml")}
            </button>
            {showYaml && <pre className="scenario-input-body">{envMeta.meta_yaml}</pre>}
          </>}
    </section>

    <section className="card"><h3>{t("experimentOverview.variants")}</h3>
      {bundle.variants.map((item) => <div key={item.id}>
        {item.mutator_id} · {statusLabel(item.status, lang)}
      </div>)}
    </section>

    <section><h3>{t("experimentOverview.groups")}</h3>{bundle.groups.map((group) => <div className="card" key={group.id}>
      <strong>{statusLabel(group.status, lang)}</strong> · {t("experimentOverview.cells", { n: group.total_cells })} · <Link to={`/experiments/${experimentId}/groups/${group.id}`}>{t("experimentOverview.liveProgress")}</Link> · <Link to={`/experiments/${experimentId}/groups/${group.id}/results`}>{t("experimentOverview.results")}</Link>
    </div>)}</section>
    <Link to={`/experiments/${experimentId}/insights`}>{t("experimentOverview.insights")}</Link>
  </div>;
}
