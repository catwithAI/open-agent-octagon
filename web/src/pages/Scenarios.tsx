import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { api, type EnvSummary, type TaskJson } from "../api/client";
import { ModalityChip } from "../components/ModalityChips";
import { useI18n } from "../i18n";

// 分类展示定义：顺序即页面顺序，颜色取自设计系统语义色。
// label/blurb 为 i18n key，渲染时经 t() 解析。
const CATEGORIES: Array<{ key: string; labelKey: string; color: string; blurbKey: string }> = [
  {
    key: "general-assistant",
    labelKey: "scenarios.cat.generalAssistant.label",
    color: "var(--green)",
    blurbKey: "scenarios.cat.generalAssistant.blurb",
  },
  {
    key: "office-productivity",
    labelKey: "scenarios.cat.officeProductivity.label",
    color: "var(--accent)",
    blurbKey: "scenarios.cat.officeProductivity.blurb",
  },
  {
    key: "real-skill",
    labelKey: "scenarios.cat.realSkill.label",
    color: "var(--blue)",
    blurbKey: "scenarios.cat.realSkill.blurb",
  },
  {
    key: "complex-workflow",
    labelKey: "scenarios.cat.complexWorkflow.label",
    color: "var(--blue)",
    blurbKey: "scenarios.cat.complexWorkflow.blurb",
  },
  {
    key: "coding",
    labelKey: "scenarios.cat.coding.label",
    color: "var(--yellow)",
    blurbKey: "scenarios.cat.coding.blurb",
  },
  {
    key: "user-coding",
    labelKey: "scenarios.cat.userCoding.label",
    color: "var(--yellow)",
    blurbKey: "scenarios.cat.userCoding.blurb",
  },
  {
    key: "agent-system",
    labelKey: "scenarios.cat.agentSystem.label",
    color: "var(--accent)",
    blurbKey: "scenarios.cat.agentSystem.blurb",
  },
  {
    key: "safety-hitl",
    labelKey: "scenarios.cat.safetyHitl.label",
    color: "var(--red)",
    blurbKey: "scenarios.cat.safetyHitl.blurb",
  },
  {
    key: "baseline",
    labelKey: "scenarios.cat.baseline.label",
    color: "var(--text-2)",
    blurbKey: "scenarios.cat.baseline.blurb",
  },
];

const FALLBACK_CATEGORY = { key: "", labelKey: "scenarios.cat.fallback.label", color: "var(--text-2)", blurbKey: "" };

// 同一卡片内维度分段：同色相、递减透明度，保持谱系统一
const SEG_OPACITY = [1, 0.62, 0.38, 0.22, 0.14, 0.1];

function TaskList({ envName }: { envName: string }) {
  const { t } = useI18n();
  const [tasks, setTasks] = useState<TaskJson[] | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    let alive = true;
    api.listEnvTasks(envName)
      .then((t) => { if (alive) setTasks(t); })
      .catch((e) => { if (alive) setErr(String(e)); });
    return () => { alive = false; };
  }, [envName]);

  if (err) return <p className="warning">{err}</p>;
  if (tasks === null) return <p className="muted">{t("scenarios.list.loading")}</p>;
  if (tasks.length === 0) return <p className="muted">{t("scenarios.list.emptyTasks")}</p>;

  return (
    <table>
      <thead>
        <tr>
          <th style={{ width: 140 }}>{t("scenarios.table.taskId")}</th>
          <th>{t("scenarios.table.prompt")}</th>
          <th style={{ width: 70 }}>{t("scenarios.table.timeout")}</th>
        </tr>
      </thead>
      <tbody>
        {tasks.map((t) => (
          <tr key={t.id}>
            <td className="font-mono" style={{ fontSize: 12 }}>{t.id}</td>
            <td style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", fontSize: 12 }}>{t.prompt}</td>
            <td className="muted">{t.timeout_seconds}s</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function EnvCard({ env }: { env: EnvSummary }) {
  const { t } = useI18n();
  const [showTasks, setShowTasks] = useState(false);
  const dimensions = env.dimensions ?? [];
  const totalWeight = dimensions.reduce((s, d) => s + d.weight, 0);
  const threshold = env.pass_threshold;

  return (
    <article className="scn-card">
      <div className="scn-card-head">
        <span className="scn-name">{env.name}</span>
        {(env.agent_modalities?.length ?? 0) > 0 && (
          <span className="mod-badges" title={t("scenarios.card.modalitiesTitle", { mods: env.agent_modalities!.join("/") })}>
            {env.agent_modalities!.map((m) => <ModalityChip key={m} modality={m} />)}
          </span>
        )}
        <span className="scn-stats">
          <span>{t("scenarios.card.tasks", { n: env.task_count })}</span>
          <span>{t("scenarios.card.tools", { n: env.tool_count })}</span>
        </span>
      </div>

      {(env.prerequisite_warnings?.length ?? 0) > 0 && (
        <div className="scn-prereq-warn" title={t("scenarios.card.prereqTitle")}>
          {env.prerequisite_warnings!.map((w) => (
            <div key={w}>⚠ {w}</div>
          ))}
        </div>
      )}

      <div className="scn-eyebrow">{t("scenarios.card.eyebrow")}</div>
      <p className="scn-focus">{env.test_focus || env.description || t("scenarios.card.notFilled")}</p>
      {env.test_focus && env.description && <p className="scn-desc">{env.description}</p>}

      {dimensions.length > 0 && (
        <>
          <div className="scn-score-label">
            <span>{t("scenarios.card.scoreLabel")}</span>
            <span style={{ opacity: 0.7 }}>{t("scenarios.card.maxScore", { total: totalWeight })}{threshold != null ? t("scenarios.card.passSuffix", { threshold }) : ""}</span>
          </div>
          <div className="scn-bar">
            {dimensions.map((d, i) => (
              <div
                key={d.name}
                className="scn-bar-seg"
                style={{ width: `${(d.weight / totalWeight) * 100}%`, opacity: SEG_OPACITY[i % SEG_OPACITY.length] }}
                title={`${d.name} ${d.weight}`}
              />
            ))}
            {threshold != null && totalWeight > 0 && (
              <div
                className="scn-threshold"
                style={{ left: `${(threshold / totalWeight) * 100}%` }}
                title={t("scenarios.card.thresholdTitle", { threshold })}
              />
            )}
          </div>
          <div className="scn-dims">
            {dimensions.map((d, i) => (
              <div key={d.name} className="scn-dim">
                <span className="scn-dim-dot" style={{ opacity: SEG_OPACITY[i % SEG_OPACITY.length] }} />
                <span className="scn-dim-name">{d.name}</span>
                <span className="scn-dim-weight">{d.weight}</span>
                <span className="scn-dim-desc">{d.description}</span>
              </div>
            ))}
          </div>
        </>
      )}

      <button className="scn-tasks-toggle" onClick={() => setShowTasks((v) => !v)}>
        {showTasks ? t("scenarios.card.collapseTasks") : t("scenarios.card.viewTasks", { n: env.task_count })}
      </button>
      <Link className="scn-detail-link" to={`/scenarios/${encodeURIComponent(env.name)}`}>{t("scenarios.card.openDetail")}</Link>
      {showTasks && <div className="mt-sm"><TaskList envName={env.name} /></div>}
    </article>
  );
}

export function Scenarios() {
  const { t } = useI18n();
  const [envs, setEnvs] = useState<EnvSummary[] | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    let alive = true;
    api.listEnvs()
      .then((list) => { if (alive) setEnvs(list); })
      .catch((e) => { if (alive) setErr(String(e)); });
    return () => { alive = false; };
  }, []);

  const known = new Set(CATEGORIES.map((c) => c.key));
  const groups = envs
    ? [...CATEGORIES, FALLBACK_CATEGORY]
        .map((cat) => ({
          cat,
          list: envs.filter((e) =>
            cat.key === "" ? !known.has(e.category) : e.category === cat.key
          ),
        }))
        .filter((g) => g.list.length > 0)
    : [];

  return (
    <div>
      <h2>{t("scenarios.title")}</h2>
      <p className="muted" style={{ marginBottom: 24, maxWidth: 720 }}>
        {t("scenarios.intro")}
      </p>
      {err && <p className="warning">{err}</p>}
      {envs === null && !err && <p className="muted">{t("scenarios.list.loading")}</p>}
      {envs !== null && envs.length === 0 && (
        <div className="empty"><span>{t("scenarios.emptyEnvs")}</span></div>
      )}
      {groups.map(({ cat, list }) => (
        <section key={cat.key || "misc"} className="scn-section" style={{ ["--cat" as string]: cat.color }}>
          <div className="scn-section-head">
            <span className="scn-cat-marker" />
            <h3>{t(cat.labelKey)}</h3>
            {cat.blurbKey && <span className="scn-section-blurb">{t(cat.blurbKey)}</span>}
            <span className="scn-section-count">{t("scenarios.section.count", { n: list.length })}</span>
          </div>
          <div className="scn-grid">
            {list.map((env) => <EnvCard key={env.name} env={env} />)}
          </div>
        </section>
      ))}
    </div>
  );
}
