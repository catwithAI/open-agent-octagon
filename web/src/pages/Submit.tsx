import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { api, bladeModelsMaybeAnthropic, type AgentInfo, type BladeModelOption, type EnvSummary, type TaskJson } from "../api/client";
import { ModalityChip, modalityOptionMark } from "../components/ModalityChips";
import { useI18n } from "../i18n";

type UploadedFile = { name: string; path: string; size: number };

export function Submit() {
  const { t } = useI18n();
  const nav = useNavigate();
  const [envs, setEnvs] = useState<EnvSummary[]>([]);
  const [envName, setEnvName] = useState("");
  const [tasks, setTasks] = useState<TaskJson[]>([]);
  const [taskId, setTaskId] = useState("");
  const [freePrompt, setFreePrompt] = useState("");
  const [usePrompt, setUsePrompt] = useState(false);
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bladeModel, setBladeModel] = useState("");
  const [bladeModelSearch, setBladeModelSearch] = useState("");
  const [bladeModelPickerOpen, setBladeModelPickerOpen] = useState(false);
  const [bladeModels, setBladeModels] = useState<BladeModelOption[]>([]);
  const [bladeDefaultModel, setBladeDefaultModel] = useState<string | null>(null);
  const [bladeModelsError, setBladeModelsError] = useState("");
  const [bladeEnableThinking, setBladeEnableThinking] = useState(true);
  // 任务超时（分钟）：空=不限时。设值时告知 agent 时限并强制超时（测单位时间能力上限）。
  const [timeoutMinutes, setTimeoutMinutes] = useState("");
  const [files, setFiles] = useState<UploadedFile[]>([]);
  const [uploading, setUploading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);
  const bladeModelPickerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    api.listEnvs().then((rows) => {
      setEnvs(rows);
      if (rows.length > 0) setEnvName(rows[0].name);
    }).catch((e) => setErr(String(e)));

    api.listAgents().then((list) => {
      setAgents(list);
      const avail = list.filter((a) => a.status === "available").map((a) => a.name);
      setSelected(new Set(avail));
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

  useEffect(() => {
    if (!bladeModelPickerOpen) return;
    const closeOnPointerDown = (event: PointerEvent) => {
      if (!bladeModelPickerRef.current?.contains(event.target as Node)) {
        setBladeModelPickerOpen(false);
      }
    };
    window.addEventListener("pointerdown", closeOnPointerDown);
    return () => window.removeEventListener("pointerdown", closeOnPointerDown);
  }, [bladeModelPickerOpen]);

  const toggle = (name: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name); else next.add(name);
      return next;
    });
  };

  const uploadFiles = async (fileList: FileList) => {
    setUploading(true);
    try {
      const form = new FormData();
      for (let i = 0; i < fileList.length; i++) {
        form.append("file_" + i, fileList[i]);
      }
      const resp = await fetch("/api/upload", { method: "POST", body: form });
      if (!resp.ok) throw new Error(t("submit.uploadFailed", { status: resp.status }));
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

  const canSubmit = envName && selected.size > 0 && !submitting && (
    usePrompt ? freePrompt.trim().length > 0 : !!taskId
  );

  // 多轮 + Anthropic 系模型才置灰 blade「开启思考」：signature 400 是 Anthropic
  // extended thinking 独有，非 Anthropic 多轮可保留 thinking（后端 dispatch 同样
  // 收窄兜底）。task 级判定：同一 env 可同时含多轮与单轮 task，env 级会误伤单轮
  // task；prompt 自由输入模式无选中 task，是单轮，不置灰。
  const selectedTaskMultiTurn = !usePrompt
    && !!tasks.find((t) => t.id === taskId)?.multi_turn;
  const bladeThinkingWouldBreak = selectedTaskMultiTurn
    && bladeModelsMaybeAnthropic([bladeModel]);
  const effectiveBladeThinking = bladeThinkingWouldBreak ? false : bladeEnableThinking;

  const submit = async () => {
    setSubmitting(true);
    setErr("");
    try {
      const context: Record<string, unknown> = {};
      if (files.length > 0) {
        context.uploaded_files = files.map((f) => ({ name: f.name, path: f.path }));
      }
      const bladeModelOverride = bladeModel.trim();
      const resp = await api.createRun({
        env_name: envName,
        agents: [...selected],
        task_id: usePrompt ? undefined : taskId,
        prompt: usePrompt ? freePrompt : undefined,
        context: Object.keys(context).length > 0 ? context : undefined,
        capture_policy: "full",
        blade_model: selected.has("blade-agent") && bladeModelOverride ? bladeModelOverride : undefined,
        blade_enable_thinking: selected.has("blade-agent") ? effectiveBladeThinking : undefined,
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

  const formatSize = (bytes: number) => {
    if (bytes < 1024) return `${bytes}B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)}MB`;
  };

  const modelQuery = bladeModelSearch.trim();
  const filteredBladeModels = useMemo(() => {
    const normalized = modelQuery.toLowerCase();
    if (!normalized) return bladeModels;
    return bladeModels.filter((m) =>
      m.id.toLowerCase().startsWith(normalized)
      || (m.label ?? "").toLowerCase().startsWith(normalized)
    );
  }, [bladeModels, modelQuery]);
  const selectedBladeModel = bladeModel
    ? bladeModels.find((m) => m.id === bladeModel)
    : undefined;
  const selectedBladeModelLabel = selectedBladeModel?.label || bladeModel;
  const canUseModelQuery = modelQuery.length > 0
    && !bladeModels.some((m) => m.id.toLowerCase() === modelQuery.toLowerCase());
  const selectBladeModel = (modelId: string) => {
    setBladeModel(modelId);
    setBladeModelSearch("");
    setBladeModelPickerOpen(false);
  };

  return (
    <div className="submit-page">
      <h2>{t("submit.title")}</h2>

      <div className="card">
        <h3>{t("submit.agents")}</h3>
        <div className="agent-selector">
          {agents.map((a) => {
            const avail = a.status === "available";
            const on = selected.has(a.name);
            return (
              <div
                key={a.name}
                className={`agent-toggle${on ? " selected" : ""}${!avail ? " disabled" : ""}`}
                onClick={() => avail && toggle(a.name)}
              >
                <span className="agent-dot" />
                <span>{a.name}</span>
                {!avail && <span className="muted">({a.detail ?? a.status})</span>}
              </div>
            );
          })}
        </div>
        {selected.size === 0 && <p className="warning mt-sm">{t("submit.needOneAgent")}</p>}
      </div>

      {selected.has("blade-agent") && (
        <div className="card">
          <h3>Blade Agent</h3>
          <label>{t("submit.bladeModelId")}</label>
          {bladeModels.length > 0 ? (
            <div className="model-picker" ref={bladeModelPickerRef}>
              <button
                type="button"
                className="model-picker-trigger"
                onClick={() => setBladeModelPickerOpen((current) => !current)}
              >
                <span className="model-picker-label">
                  {bladeModel
                    ? selectedBladeModelLabel
                    : bladeDefaultModel
                      ? t("submit.bladeDefaultModel", { model: bladeDefaultModel })
                      : t("submit.bladeNoOverride")}
                </span>
                <span className="model-picker-chevron">▾</span>
              </button>
              {bladeModelPickerOpen && (
                <div className="model-picker-menu">
                  <div className="model-picker-search">
                    <input
                      value={bladeModelSearch}
                      onChange={(e) => setBladeModelSearch(e.target.value)}
                      placeholder={t("submit.bladeSearchPlaceholder")}
                      autoFocus
                    />
                  </div>
                  <div className="model-picker-options">
                    {!modelQuery && (
                      <button
                        type="button"
                        className={`model-picker-option${bladeModel ? "" : " selected"}`}
                        onClick={() => selectBladeModel("")}
                      >
                        <span className="model-picker-option-main">
                          {bladeDefaultModel ? t("submit.bladeDefaultModel", { model: bladeDefaultModel }) : t("submit.bladeNoOverride")}
                        </span>
                      </button>
                    )}
                    {canUseModelQuery && (
                      <button
                        type="button"
                        className="model-picker-option custom"
                        onClick={() => selectBladeModel(modelQuery)}
                      >
                        <span className="model-picker-option-main">{t("submit.useModelId", { query: modelQuery })}</span>
                      </button>
                    )}
                    {filteredBladeModels.length === 0 && !canUseModelQuery ? (
                      <div className="model-picker-empty">{t("submit.noMatch")}</div>
                    ) : (
                      filteredBladeModels.map((m) => (
                        <button
                          key={m.id}
                          type="button"
                          className={`model-picker-option${m.id === bladeModel ? " selected" : ""}`}
                          onClick={() => selectBladeModel(m.id)}
                          title={m.id === m.label ? m.id : `${m.label} (${m.id})`}
                        >
                          <span className="model-picker-option-main">{m.label || m.id}</span>
                          {m.label && m.label !== m.id && (
                            <span className="model-picker-option-sub">{m.id}</span>
                          )}
                        </button>
                      ))
                    )}
                  </div>
                </div>
              )}
            </div>
          ) : (
            <input
              value={bladeModel}
              onChange={(e) => setBladeModel(e.target.value)}
              placeholder={t("submit.bladeInputPlaceholder")}
            />
          )}
          {bladeModelsError && (
            <p className="muted mt-sm" style={{ fontSize: 12 }}>
              {t("submit.modelsUnavailable", { error: bladeModelsError })}
            </p>
          )}
          <label style={{ flexDirection: "row", display: "flex", alignItems: "center", gap: 6, textTransform: "none", opacity: bladeThinkingWouldBreak ? 0.5 : 1 }}>
            <input
              type="checkbox"
              checked={effectiveBladeThinking}
              disabled={bladeThinkingWouldBreak}
              onChange={(e) => setBladeEnableThinking(e.target.checked)}
            />
            <span style={{ fontSize: 13, color: "var(--text-1)" }}>
              {t("submit.enableThinking")}
              {bladeThinkingWouldBreak && (
                <span style={{ fontSize: 11, color: "var(--text-2)", marginLeft: 6 }}>
                  {t("submit.thinkingDisabled")}
                </span>
              )}
            </span>
          </label>
        </div>
      )}

      <label>{t("submit.envLabel")}</label>
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
          <span className="muted">{t("submit.scenarioNeeds")}</span>
          {envs.find((e) => e.name === envName)!.agent_modalities!.map((m) => (
            <ModalityChip key={m} modality={m} />
          ))}
          <span className="muted">{t("submit.inputCapability")}</span>
        </div>
      )}
      {(envs.find((e) => e.name === envName)?.prerequisite_warnings?.length ?? 0) > 0 && (
        <div className="prereq-warn" role="alert">
          {envs.find((e) => e.name === envName)!.prerequisite_warnings!.map((w) => (
            <div key={w}>⚠ {t("submit.prereqMissing", { warning: w })}</div>
          ))}
        </div>
      )}

      <label style={{ flexDirection: "row", display: "flex", alignItems: "center", gap: 6, textTransform: "none" }}>
        <input type="checkbox" checked={usePrompt} onChange={(e) => setUsePrompt(e.target.checked)} />
        <span style={{ fontSize: 13, color: "var(--text-1)" }}>{t("submit.useFreePrompt")}</span>
      </label>

      {usePrompt ? (
        <>
          <label>{t("submit.promptLabel")}</label>
          <textarea rows={4} value={freePrompt} onChange={(e) => setFreePrompt(e.target.value)}
            placeholder={t("submit.promptPlaceholder")} />
        </>
      ) : (
        <>
          <label>{t("submit.taskLabel")}</label>
          <select value={taskId} onChange={(e) => setTaskId(e.target.value)}>
            {tasks.map((t) => (
              <option key={t.id} value={t.id}>{t.id}</option>
            ))}
          </select>
        </>
      )}

      <label>{t("submit.timeoutLabel")}</label>
      <input
        type="number"
        min={0}
        step={0.5}
        value={timeoutMinutes}
        onChange={(e) => setTimeoutMinutes(e.target.value)}
        placeholder={t("submit.timeoutPlaceholder")}
      />
      <p className="muted" style={{ fontSize: 12, marginTop: -4 }}>
        {timeoutMinutes.trim() === ""
          ? t("submit.timeoutHintNone")
          : t("submit.timeoutHintSet", { minutes: timeoutMinutes })}
      </p>

      {/* 文件上传 */}
      <label>{t("submit.attachmentsLabel")}</label>
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
          {uploading ? t("submit.uploading") : t("submit.chooseFile")}
        </button>
        <span className="muted" style={{ fontSize: 12 }}>{t("submit.attachmentHint")}</span>
      </div>

      {files.length > 0 && (
        <div className="upload-list">
          {files.map((f) => (
            <div key={f.name} className="upload-item">
              <span className="upload-item-name">{f.name}</span>
              <span className="muted" style={{ fontSize: 11 }}>{formatSize(f.size)}</span>
              <span className="upload-item-remove" onClick={() => removeFile(f.name)}>✕</span>
            </div>
          ))}
        </div>
      )}

      <div className="mt-md">
        <button onClick={submit} disabled={!canSubmit}>
          {submitting ? t("submit.submitting") : t("submit.runAgents", { n: selected.size })}
        </button>
      </div>

      {err && <p className="warning mt-sm">{err}</p>}
    </div>
  );
}
