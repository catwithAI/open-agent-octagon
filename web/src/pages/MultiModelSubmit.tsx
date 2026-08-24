import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { api, bladeModelsMaybeAnthropic, type BladeModelOption, type EnvSummary, type TaskJson } from "../api/client";
import { ModalityChip, modalityOptionMark } from "../components/ModalityChips";
import { useI18n } from "../i18n";

type UploadedFile = { name: string; path: string; size: number };

const AGENT = "blade-agent";

export function MultiModelSubmit() {
  const { t } = useI18n();
  const nav = useNavigate();
  const [envs, setEnvs] = useState<EnvSummary[]>([]);
  const [envName, setEnvName] = useState("");
  const [tasks, setTasks] = useState<TaskJson[]>([]);
  const [taskId, setTaskId] = useState("");
  const [freePrompt, setFreePrompt] = useState("");
  const [usePrompt, setUsePrompt] = useState(false);
  const [bladeModels, setBladeModels] = useState<BladeModelOption[]>([]);
  const [bladeDefaultModel, setBladeDefaultModel] = useState<string | null>(null);
  const [bladeModelsError, setBladeModelsError] = useState("");
  const [selectedModels, setSelectedModels] = useState<Set<string>>(new Set());
  const [modelSearch, setModelSearch] = useState("");
  const [bladeEnableThinking, setBladeEnableThinking] = useState(true);
  // 任务超时（分钟）：空=不限时。设值时告知 agent 时限并强制超时（测单位时间能力上限）。
  const [timeoutMinutes, setTimeoutMinutes] = useState("");
  const [files, setFiles] = useState<UploadedFile[]>([]);
  const [uploading, setUploading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    api.listEnvs().then((rows) => {
      setEnvs(rows);
      if (rows.length > 0) setEnvName(rows[0].name);
    }).catch((e) => setErr(String(e)));

    api.bladeModels().then((config) => {
      setBladeModels(config.models ?? []);
      setBladeDefaultModel(config.default ?? null);
      setBladeModelsError(config.error ?? "");
    }).catch((e) => setBladeModelsError(String(e)));
  }, []);

  useEffect(() => {
    if (!envName) return;
    api.listEnvTasks(envName).then((t) => {
      setTasks(t);
      setTaskId(t[0]?.id ?? "");
    }).catch((e) => setErr(String(e)));
  }, [envName]);

  const toggleModel = (id: string) => {
    setSelectedModels((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const addModel = (modelId?: string) => {
    const id = (modelId ?? modelSearch).trim();
    if (!id) return;
    setSelectedModels((prev) => new Set(prev).add(id));
    setModelSearch("");
  };

  const uploadFiles = async (fileList: FileList) => {
    setUploading(true);
    try {
      const form = new FormData();
      for (let i = 0; i < fileList.length; i++) {
        form.append("file_" + i, fileList[i]);
      }
      const resp = await fetch("/api/upload", { method: "POST", body: form });
      if (!resp.ok) throw new Error(t("multiModel.uploadFailed", { status: resp.status }));
      const data = await resp.json();
      setFiles((prev) => [...prev, ...(data.files as UploadedFile[])]);
    } catch (e) {
      setErr(String(e));
    } finally {
      setUploading(false);
    }
  };

  const removeFile = (name: string) => {
    setFiles((prev) => prev.filter((f) => f.name !== name));
  };

  const formatSize = (bytes: number) => {
    if (bytes < 1024) return `${bytes}B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)}MB`;
  };

  const canSubmit = envName && selectedModels.size >= 2 && !submitting
    && (usePrompt || !!taskId);

  const modelQuery = modelSearch.trim();
  const filteredModels = useMemo(() => {
    if (!modelQuery) return [];
    const normalized = modelQuery.toLowerCase();
    return bladeModels.filter((model) =>
      model.id.toLowerCase().includes(normalized)
      || (model.label ?? "").toLowerCase().includes(normalized)
    );
  }, [bladeModels, modelQuery]);
  const hasExactModel = bladeModels.some((model) =>
    model.id.toLowerCase() === modelQuery.toLowerCase()
  );

  const handleModelSearchKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key !== "Enter" || !modelQuery) return;
    event.preventDefault();
    const exactModel = bladeModels.find((model) =>
      model.id.toLowerCase() === modelQuery.toLowerCase()
      || (model.label ?? "").toLowerCase() === modelQuery.toLowerCase()
    );
    addModel(exactModel?.id);
  };

  // 多轮 + 选中模型里存在 Anthropic 系才置灰 blade 思考（multi-model 每个模型都是
  // 独立 blade attempt；只要有一个 Anthropic 模型，那个 attempt 多轮就会 signature
  // 400，故保守按整组置灰）。非 Anthropic 多轮可保留 thinking（后端 dispatch 同样
  // 收窄兜底）。task 级判定：env 级会误伤单轮 task；prompt 自由输入模式是单轮，不置灰。
  const selectedTaskMultiTurn = !usePrompt
    && !!tasks.find((t) => t.id === taskId)?.multi_turn;
  const bladeThinkingWouldBreak = selectedTaskMultiTurn
    && bladeModelsMaybeAnthropic([...selectedModels]);
  const effectiveBladeThinking = bladeThinkingWouldBreak ? false : bladeEnableThinking;

  const submit = async () => {
    setSubmitting(true);
    setErr("");
    try {
      const context: Record<string, unknown> = {};
      if (files.length > 0) {
        context.uploaded_files = files.map((f) => ({ name: f.name, path: f.path }));
      }
      const resp = await api.createRun({
        env_name: envName,
        agents: [AGENT],
        task_id: usePrompt ? undefined : taskId,
        prompt: usePrompt ? freePrompt : undefined,
        context: Object.keys(context).length > 0 ? context : undefined,
        compare_mode: "multi-model",
        models: [...selectedModels],
        capture_policy: "full",
        blade_enable_thinking: effectiveBladeThinking,
        timeout_seconds:
          timeoutMinutes.trim() === ""
            ? null
            : Math.round(Number(timeoutMinutes) * 60),
      });
      nav(`/runs/${resp.run_id}`);
    } catch (e) {
      setErr(String(e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="submit-page">
      <h2>{t("multiModel.title")}</h2>

      <div className="card" style={{ marginBottom: 16 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13, color: "var(--text-2)" }}>
          <span style={{ background: "var(--surface-2)", padding: "2px 8px", borderRadius: 4, fontSize: 12 }}>{t("multiModel.parallelBadge")}</span>
          <span>{t("multiModel.parallelHint")}</span>
        </div>
        <div style={{ marginTop: 8, display: "flex", gap: 8 }}>
          <span className="agent-toggle selected" style={{ pointerEvents: "none" }}>
            <span className="agent-dot" />
            <span>{AGENT}</span>
          </span>
        </div>
      </div>

      <label>{t("multiModel.modelsLabel")}</label>
      <div className="card" style={{ marginBottom: 12 }}>
        <div className="multi-model-search">
          <input
            value={modelSearch}
            onChange={(event) => setModelSearch(event.target.value)}
            onKeyDown={handleModelSearchKeyDown}
            placeholder={bladeModels.length > 0
              ? t("multiModel.searchPlaceholder", { n: bladeModels.length })
              : t("multiModel.searchPlaceholderEmpty")}
            aria-label={t("multiModel.searchAria")}
          />
          {modelQuery && (
            <div className="model-picker-options multi-model-search-results">
              {!hasExactModel && (
                <button
                  type="button"
                  className="model-picker-option custom"
                  onClick={() => addModel()}
                >
                  <span className="model-picker-option-main">{t("multiModel.addModelId", { query: modelQuery })}</span>
                </button>
              )}
              {filteredModels.slice(0, 50).map((model) => (
                <button
                  key={model.id}
                  type="button"
                  className={`model-picker-option${selectedModels.has(model.id) ? " selected" : ""}`}
                  onClick={() => addModel(model.id)}
                  title={model.label && model.label !== model.id ? `${model.label} (${model.id})` : model.id}
                >
                  <span className="model-picker-option-main">
                    {model.label || model.id}
                    {bladeDefaultModel === model.id && <span className="muted">{t("multiModel.defaultMark")}</span>}
                  </span>
                  {model.label && model.label !== model.id && (
                    <span className="model-picker-option-sub">{model.id}</span>
                  )}
                </button>
              ))}
              {filteredModels.length === 0 && hasExactModel && (
                <div className="model-picker-empty">{t("multiModel.noMatch")}</div>
              )}
              {filteredModels.length > 50 && (
                <div className="model-picker-empty">
                  {t("multiModel.moreResults", { n: filteredModels.length - 50 })}
                </div>
              )}
            </div>
          )}
        </div>
        {bladeModels.length === 0 && (
          <p className="muted" style={{ fontSize: 12 }}>
            {t("multiModel.modelsUnavailable")}{bladeModelsError ? t("multiModel.modelsUnavailableSuffix", { error: bladeModelsError }) : ""}{t("multiModel.modelsUnavailableTail")}
          </p>
        )}
        {selectedModels.size > 0 && (
          <div className="selected-models" aria-label={t("multiModel.selectedAria")}>
            {[...selectedModels].map((id) => (
              <button
                key={id}
                type="button"
                className="selected-model-chip"
                onClick={() => toggleModel(id)}
                aria-label={t("multiModel.deselectAria", { id })}
                title={t("multiModel.deselectTitle")}
              >
                <span>{id}</span>
                <span aria-hidden="true">&#x2715;</span>
              </button>
            ))}
          </div>
        )}
        {selectedModels.size === 1 && (
          <p className="warning mt-sm">{t("multiModel.needTwo")}</p>
        )}
      </div>

      <label style={{ flexDirection: "row", display: "flex", alignItems: "center", gap: 6, textTransform: "none", opacity: bladeThinkingWouldBreak ? 0.5 : 1 }}>
        <input
          type="checkbox"
          checked={effectiveBladeThinking}
          disabled={bladeThinkingWouldBreak}
          onChange={(e) => setBladeEnableThinking(e.target.checked)}
        />
        <span style={{ fontSize: 13, color: "var(--text-1)" }}>
          {t("multiModel.enableThinking")}
          {bladeThinkingWouldBreak && (
            <span style={{ fontSize: 11, color: "var(--text-2)", marginLeft: 6 }}>
              {t("multiModel.thinkingDisabled")}
            </span>
          )}
        </span>
      </label>

      <label>{t("multiModel.envLabel")}</label>
      <select value={envName} onChange={(e) => setEnvName(e.target.value)}>
        {envs.map((e) => (
          <option key={e.name} value={e.name}>
            {((e.prerequisite_warnings?.length ?? 0) > 0 ? `⚠ ${e.name}` : e.name)
              + modalityOptionMark(e.agent_modalities)}
          </option>
        ))}
      </select>
      {(envs.find((e) => e.name === envName)?.agent_modalities?.length ?? 0) > 0 && (
        <div style={{ marginTop: 4, display: "flex", alignItems: "center", gap: 6, fontSize: 12 }}>
          <span className="muted">{t("multiModel.scenarioNeeds")}</span>
          {envs.find((e) => e.name === envName)!.agent_modalities!.map((m) => (
            <ModalityChip key={m} modality={m} />
          ))}
          <span className="muted">{t("multiModel.inputCapability")}</span>
        </div>
      )}
      {(envs.find((e) => e.name === envName)?.prerequisite_warnings?.length ?? 0) > 0 && (
        <div className="prereq-warn" role="alert">
          {envs.find((e) => e.name === envName)!.prerequisite_warnings!.map((w) => (
            <div key={w}>⚠ {t("multiModel.prereqMissing", { warning: w })}</div>
          ))}
        </div>
      )}

      <label style={{ flexDirection: "row", display: "flex", alignItems: "center", gap: 6, textTransform: "none" }}>
        <input type="checkbox" checked={usePrompt} onChange={(e) => setUsePrompt(e.target.checked)} />
        <span style={{ fontSize: 13, color: "var(--text-1)" }}>{t("multiModel.useFreePrompt")}</span>
      </label>

      {usePrompt ? (
        <>
          <label>{t("multiModel.promptLabel")}</label>
          <textarea rows={4} value={freePrompt} onChange={(e) => setFreePrompt(e.target.value)}
            placeholder={t("multiModel.promptPlaceholder")} />
        </>
      ) : (
        <>
          <label>{t("multiModel.taskLabel")}</label>
          <select value={taskId} onChange={(e) => setTaskId(e.target.value)}>
            {tasks.map((t) => (
              <option key={t.id} value={t.id}>{t.id}</option>
            ))}
          </select>
        </>
      )}

      <label>{t("multiModel.timeoutLabel")}</label>
      <input
        type="number"
        min={0}
        step={0.5}
        value={timeoutMinutes}
        onChange={(e) => setTimeoutMinutes(e.target.value)}
        placeholder={t("multiModel.timeoutPlaceholder")}
      />
      <p className="muted" style={{ fontSize: 12, marginTop: -4 }}>
        {timeoutMinutes.trim() === ""
          ? t("multiModel.timeoutHintNone")
          : t("multiModel.timeoutHintSet", { minutes: timeoutMinutes })}
      </p>

      <label>{t("multiModel.attachmentsLabel")}</label>
      <div className="upload-area">
        <input
          ref={fileRef}
          type="file"
          multiple
          style={{ display: "none" }}
          onChange={(e) => e.target.files && uploadFiles(e.target.files)}
        />
        <button
          type="button"
          className="btn-ghost"
          onClick={() => fileRef.current?.click()}
          disabled={uploading}
        >
          {uploading ? t("multiModel.uploading") : t("multiModel.chooseFile")}
        </button>
        <span className="muted" style={{ fontSize: 12 }}>{t("multiModel.attachmentHint")}</span>
      </div>

      {files.length > 0 && (
        <div className="upload-list">
          {files.map((f) => (
            <div key={f.name} className="upload-item">
              <span className="upload-item-name">{f.name}</span>
              <span className="muted" style={{ fontSize: 11 }}>{formatSize(f.size)}</span>
              <span className="upload-item-remove" onClick={() => removeFile(f.name)}>&#x2715;</span>
            </div>
          ))}
        </div>
      )}

      <div className="mt-md">
        <button onClick={submit} disabled={!canSubmit}>
          {submitting ? t("multiModel.submitting") : t("multiModel.runComparison")}
        </button>
      </div>

      {err && <p className="warning mt-sm">{err}</p>}
    </div>
  );
}
