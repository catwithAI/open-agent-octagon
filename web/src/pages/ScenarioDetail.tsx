import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { api, type EnvInputFile, type EnvSummary, type TaskJson } from "../api/client";
import { useI18n } from "../i18n";

// 输入物料查看器：task 的 files[].path 指向场景的 inputs/，只看任务 prompt
// 看不到 agent 实际拿到的输入全貌（prompt 常常只是「请阅读 REQUIREMENT.md」）。
// 默认折叠、点选才拉正文——场景页首屏不该被大段规格文档淹没。
function InputMaterials({ envName }: { envName: string }) {
  const { t } = useI18n();
  const [files, setFiles] = useState<EnvInputFile[]>([]);
  const [active, setActive] = useState<string | null>(null);
  const [content, setContent] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    setFiles([]);
    setActive(null);
    setContent("");
    api.listEnvInputs(envName)
      .then((result) => setFiles(result.files))
      .catch(() => setFiles([]));
  }, [envName]);

  const open = async (file: EnvInputFile) => {
    if (active === file.path) {
      setActive(null);
      return;
    }
    setActive(file.path);
    setContent("");
    setError("");
    if (file.too_large) {
      setError(t("scenarioDetail.fileTooLarge"));
      return;
    }
    try {
      const resp = await fetch(api.envInputUrl(envName, file.path));
      if (!resp.ok) throw new Error(`input -> ${resp.status}`);
      setContent(await resp.text());
    } catch (reason) {
      setError(String(reason));
    }
  };

  if (files.length === 0) return null;
  return (
    <section className="results-panel">
      <div className="results-panel-head">
        <strong>{t("scenarioDetail.inputMaterials")}</strong>
        <span>{t("scenarioDetail.inputFilesDesc", { n: files.length })}</span>
      </div>
      <div className="scenario-inputs">
        {files.map((file) => (
          <div key={file.path}>
            <button className="scenario-input-row" onClick={() => open(file)}>
              <code>{file.path}</code>
              <small>{file.size.toLocaleString()} B{file.too_large ? ` · ${t("scenarioDetail.tooLarge")}` : ""}</small>
            </button>
            {active === file.path && (
              error
                ? <div className="error">{error}</div>
                : <pre className="scenario-input-body">{content || t("scenarioDetail.loading")}</pre>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}

export function ScenarioDetail() {
  const { t } = useI18n();
  const { envName = "" } = useParams();
  const [env, setEnv] = useState<EnvSummary | null>(null);
  const [tasks, setTasks] = useState<TaskJson[]>([]);
  const [error, setError] = useState("");
  useEffect(() => {
    Promise.all([api.listEnvs(), api.listEnvTasks(envName)]).then(([envs, loadedTasks]) => {
      setEnv(envs.find((item) => item.name === envName) ?? null);
      setTasks(loadedTasks);
    }).catch((reason) => setError(String(reason)));
  }, [envName]);
  return <div className="scenario-detail-page">
    <div className="resource-heading"><span>SCENARIO DETAIL</span><h2>{env?.name ?? envName}</h2>
      <p>{env?.test_focus || env?.description || t("scenarioDetail.loadingDef")}</p></div>
    {error && <div className="error">{error}</div>}
    {env && <>
      <div className="system-summary">
        <article><span>{t("scenarioDetail.tasks")}</span><strong>{env.task_count}</strong><small>fixture tasks</small></article>
        <article><span>{t("scenarioDetail.tools")}</span><strong>{env.tool_count}</strong><small>{t("scenarioDetail.allowedTools")}</small></article>
        <article><span>{t("scenarioDetail.passThreshold")}</span><strong>{env.pass_threshold ?? "—"}</strong><small>{env.available === false ? t("scenarioDetail.unavailable") : t("scenarioDetail.loaded")}</small></article>
      </div>
      <div className="scenario-detail-grid">
        <section className="results-panel"><div className="results-panel-head"><strong>{t("scenarioDetail.dimensions")}</strong></div>
          {env.dimensions.map((dimension) => <div className="scenario-dimension-row" key={dimension.name}>
            <span><strong>{dimension.name}</strong><small>{dimension.description}</small></span><em>{dimension.weight}</em>
          </div>)}</section>
        <section className="results-panel"><div className="results-panel-head"><strong>{t("scenarioDetail.runConditions")}</strong></div>
          <div className="scenario-prerequisites"><div><span>{t("scenarioDetail.category")}</span><strong>{env.category}</strong></div>
            <div><span>Skill</span><strong>{env.skill_id}</strong></div>
            <div><span>{t("scenarioDetail.inputModality")}</span><strong>{env.agent_modalities?.join(" / ") || "text"}</strong></div>
            {env.prerequisite_warnings?.map((warning) => <p className="warning" key={warning}>{warning}</p>)}</div></section>
      </div>
      <section className="results-panel scenario-task-table"><div className="results-panel-head"><strong>{t("scenarioDetail.taskCatalog")}</strong><span>{tasks.length} tasks</span></div>
        <div className="results-table-scroll"><table><thead><tr><th>{t("scenarioDetail.taskId")}</th><th>{t("scenarioDetail.prompt")}</th><th>{t("scenarioDetail.timeout")}</th></tr></thead>
          <tbody>{tasks.map((task) => <tr key={task.id}><td><code>{task.id}</code></td><td>{task.prompt}</td><td>{task.timeout_seconds}s</td></tr>)}</tbody></table></div></section>
      <InputMaterials envName={env.name} />
      <Link className="btn" to={`/experiments/new?env=${encodeURIComponent(env.name)}`}>{t("scenarioDetail.newExperiment")}</Link>
    </>}
  </div>;
}
