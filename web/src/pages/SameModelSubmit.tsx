import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import {
  api,
  bladeModelsMaybeAnthropic,
  type AgentInfo,
  type BladeModelOption,
  type EnvSummary,
  type OpenRouterModel,
  type TaskJson,
} from "../api/client";
import { ModalityBadges, ModalityChip, missingModalities, modalityOptionMark } from "../components/ModalityChips";
import { bladeBareId, sameModelForAgent } from "../experiments/builder";
import { AGENT_NAMES } from "../agents";
import { useI18n } from "../i18n";

type UploadedFile = { name: string; path: string; size: number };

export function SameModelSubmit() {
  const { t } = useI18n();
  const nav = useNavigate();
  const [envs, setEnvs] = useState<EnvSummary[]>([]);
  const [envName, setEnvName] = useState("");
  const [tasks, setTasks] = useState<TaskJson[]>([]);
  const [taskId, setTaskId] = useState("");
  const [freePrompt, setFreePrompt] = useState("");
  const [usePrompt, setUsePrompt] = useState(false);
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [selected, setSelected] = useState<Set<string>>(
    new Set(["blade-agent", "claude-code"]),
  );
  // bareModel：选中的裸模型名（如 anthropic/claude-sonnet-5）。提交时按选中 agent
  // 拼 provider 前缀（cc → or-cc/…、codex → or-codex/…），blade 用裸名。
  const [bareModel, setBareModel] = useState("");
  const [orModels, setOrModels] = useState<OpenRouterModel[]>([]);
  const [bladeModels, setBladeModels] = useState<BladeModelOption[]>([]);
  const [bladeDefaultModel, setBladeDefaultModel] = useState<string | null>(null);
  const [bladeModelsError, setBladeModelsError] = useState("");
  const [bladeFilter, setBladeFilter] = useState("");
  const [bladePickerOpen, setBladePickerOpen] = useState(false);
  const [orFilter, setOrFilter] = useState("");
  const [agentPrefix, setAgentPrefix] = useState<Record<string, string>>({});
  const execution = "parallel" as const;
  const [suggested, setSuggested] = useState<string[]>([]);
  const [bladeEnableThinking, setBladeEnableThinking] = useState(true);
  // 任务超时（分钟）：空=不限时（不注入时间预算文案）。设值时告知 agent 时限
  // 并强制超时，用于测「单位时间能力上限」。
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

    api.listAgents().then(setAgents).catch(() => setAgents([]));

    api.listModelProviders().then((config) => {
      setSuggested(config.suggested ?? []);
      setAgentPrefix(config.agent_prefix ?? {});
    }).catch(() => setSuggested([]));

    api.openrouterModels().then((config) => {
      setOrModels(config.models ?? []);
    }).catch(() => setOrModels([]));
    api.bladeModels().then((config) => {
      setBladeModels(config.models ?? []);
      setBladeDefaultModel(config.default ?? null);
      setBladeModelsError(config.error ?? "");
    }).catch(() => setBladeModels([]));
  }, []);

  useEffect(() => {
    if (!envName) return;
    api.listEnvTasks(envName).then((t) => {
      setTasks(t);
      setTaskId(t[0]?.id ?? "");
    }).catch((e) => setErr(String(e)));
  }, [envName]);

  const toggleAgent = (name: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  const agentStatus = (name: string): string | null => {
    const info = agents.find((a) => a.name === name);
    return info ? info.status : null;
  };

  const uploadFiles = async (fileList: FileList) => {
    setUploading(true);
    try {
      const form = new FormData();
      for (let i = 0; i < fileList.length; i++) {
        form.append("file_" + i, fileList[i]);
      }
      const resp = await fetch("/api/upload", { method: "POST", body: form });
      if (!resp.ok) throw new Error(t("sameModel.uploadFailed", { status: resp.status }));
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

  const selectedList = AGENT_NAMES.filter((a) => selected.has(a));

  // 按 agent 把裸模型名拼成最终 model 串：blade 用裸名（后端会剥前缀，直接给裸名
  // 最干净）；cc/codex 拼各自 provider 前缀（or-cc/…、or-codex/…）。
  const modelForAgent = (agent: string, bare: string): string => {
    return sameModelForAgent(
      agent,
      bare,
      agentPrefix,
      bladeModels.map((item) => item.id),
    );
  };
  // 缺前缀的非 blade agent（provider 未配 agent 字段）→ 无法正确拼前缀，禁止提交。
  const missingPrefix = selectedList.filter(
    (a) => a !== "blade-agent" && !agentPrefix[a],
  );

  // bareModel 是否命中 blade 可用目录（含 provider-<hex>:: / blade/ 前缀形态）。
  // 命中 → blade 走目录 id；未命中 → 回退 upstream/ 直传，blade 可能 422。
  const bladeCatalogHasModel = (bare: string): boolean => {
    const b = bare.trim();
    if (!b) return false;
    return bladeModels.some(
      (m) => m.id === b || m.id.endsWith(`/${b}`) || m.id.endsWith(`::${b}`),
    );
  };
  const bladeFiltered = (() => {
    const q = bladeFilter.trim().toLowerCase();
    if (!q) return bladeModels;
    return bladeModels.filter(
      (m) => m.id.toLowerCase().includes(q) || (m.label ?? "").toLowerCase().includes(q),
    );
  })();
  const canSubmit = envName && selectedList.length >= 2 && !!bareModel.trim()
    && missingPrefix.length === 0
    && !submitting && (usePrompt || !!taskId);

  // 多轮 + Anthropic 系模型才置灰 blade 思考——signature 400 是 Anthropic extended
  // thinking 独有，非 Anthropic 多轮可保留 thinking（后端 dispatch 同样收窄兜底）。
  // 判定落到「选中的 task」而非整个 env（env 级会误伤单轮 task）；prompt 自由输入
  // 模式无选中 task，是单轮，不置灰。
  const selectedTaskMultiTurn = !usePrompt
    && !!tasks.find((t) => t.id === taskId)?.multi_turn;
  const bladeThinkingWouldBreak = selectedTaskMultiTurn
    && bladeModelsMaybeAnthropic([bareModel]);
  const effectiveBladeThinking = bladeThinkingWouldBreak ? false : bladeEnableThinking;

  const submit = async () => {
    setSubmitting(true);
    setErr("");
    try {
      const context: Record<string, unknown> = {};
      if (files.length > 0) {
        context.uploaded_files = files.map((f) => ({ name: f.name, path: f.path }));
      }
      const bare = bareModel.trim();
      const models: Record<string, string> = {};
      for (const a of selectedList) models[a] = modelForAgent(a, bare);
      const resp = await api.createRun({
        env_name: envName,
        agents: selectedList,
        task_id: usePrompt ? undefined : taskId,
        prompt: usePrompt ? freePrompt : undefined,
        context: Object.keys(context).length > 0 ? context : undefined,
        compare_mode: "same-model",
        models,
        execution,
        capture_policy: "full",
        blade_enable_thinking: selected.has("blade-agent") ? effectiveBladeThinking : undefined,
        // 空 → null（不限时）；有值 → 分钟转秒
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
      <h2>{t("sameModel.title")}</h2>

      <div className="card" style={{ marginBottom: 16 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12, fontSize: 13, color: "var(--text-2)", flexWrap: "wrap" }}>
          <span style={{ fontSize: 12, color: "var(--text-1)" }}>{t("sameModel.execution")}</span>
          <strong style={{ color: "var(--accent)" }}>{t("sameModel.parallel")}</strong>
          <span>{t("sameModel.parallelNote")}</span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 12, fontSize: 13, color: "var(--text-2)", flexWrap: "wrap", marginTop: 12, paddingTop: 12, borderTop: "1px solid var(--border-subtle)" }}>
          <span style={{ fontSize: 12, color: "var(--text-1)" }}>{t("sameModel.timeout")}</span>
          <input
            type="number"
            min={0}
            step={0.5}
            value={timeoutMinutes}
            onChange={(e) => setTimeoutMinutes(e.target.value)}
            placeholder={t("sameModel.noTimeout")}
            style={{ width: 80, fontSize: 13 }}
          />
          <span style={{ fontSize: 13, color: "var(--text-1)" }}>{t("sameModel.minutes")}</span>
          <span>
            {timeoutMinutes.trim() === ""
              ? t("sameModel.timeoutEmptyNote")
              : t("sameModel.timeoutSetNote", { minutes: timeoutMinutes })}
          </span>
        </div>
        <div style={{ marginTop: 8, display: "flex", gap: 8 }}>
          {AGENT_NAMES.map((a) => {
            const status = agentStatus(a);
            const unavailable = status !== null && status !== "available";
            return (
              <span
                key={a}
                className={`agent-toggle${selected.has(a) ? " selected" : ""}`}
                style={unavailable ? { opacity: 0.5 } : { cursor: "pointer" }}
                onClick={() => !unavailable && toggleAgent(a)}
              >
                <span className="agent-dot" />
                <span>{a}</span>
                {unavailable && <span className="muted">{t("sameModel.unavailable")}</span>}
              </span>
            );
          })}
        </div>
        {selectedList.length < 2 && <p className="warning mt-sm">{t("sameModel.selectAtLeastTwo")}</p>}
      </div>

      <div className="card" style={{ marginBottom: 12 }}>
        <h3>{t("sameModel.modelShared")}</h3>
        {/* 裸模型名输入：直接编辑或从下方 OpenRouter 列表点选。提交时按选中 agent
            自动拼 provider 前缀。 */}
        <input
          value={bareModel}
          onChange={(e) => setBareModel(e.target.value)}
          placeholder={t("sameModel.modelIdPlaceholder")}
        />
        {(() => {
          const model = orModels.find((m) => m.id === bareModel);
          const env = envs.find((e) => e.name === envName);
          const missing = missingModalities(env?.agent_modalities, model?.input_modalities);
          return (
            <>
              {model && (
                <div style={{ marginTop: 4, display: "flex", alignItems: "center", gap: 6, fontSize: 12 }}>
                  <span className="muted">MODALITIES</span>
                  <ModalityBadges input={model.input_modalities} output={model.output_modalities} />
                </div>
              )}
              {missing.length > 0 && (
                <div className="modality-warn" role="alert">
                  {t("sameModel.modalityWarn", {
                    env: envName,
                    missing: missing.join("/"),
                    model: bareModel,
                    input: (model?.input_modalities ?? []).join("/") || t("sameModel.unknown"),
                  })}
                </div>
              )}
            </>
          );
        })()}
        {/* OpenRouter 全量搜索：342 个模型，输入过滤，截断显示前 50。 */}
        <input
          value={orFilter}
          onChange={(e) => setOrFilter(e.target.value)}
          placeholder={t("sameModel.searchOrModels", { n: orModels.length })}
          style={{ marginTop: 6 }}
        />
        {orFilter.trim() && (
          <div
            style={{
              maxHeight: 220,
              overflowY: "auto",
              border: "1px solid var(--border)",
              borderRadius: 6,
              marginTop: 4,
            }}
          >
            {(() => {
              const q = orFilter.trim().toLowerCase();
              const hits = orModels.filter(
                (m) => m.id.toLowerCase().includes(q) || m.name.toLowerCase().includes(q),
              );
              const shown = hits.slice(0, 50);
              return (
                <>
                  {shown.map((m) => (
                    <div
                      key={m.id}
                      onClick={() => { setBareModel(m.id); setOrFilter(""); }}
                      style={{
                        padding: "6px 10px",
                        cursor: "pointer",
                        fontSize: 13,
                        background: m.id === bareModel ? "var(--accent-soft, #eef)" : undefined,
                      }}
                    >
                      <code>{m.id}</code>
                      <span style={{ marginLeft: 8 }}>
                        <ModalityBadges input={m.input_modalities} output={m.output_modalities} />
                      </span>
                      {m.name !== m.id && (
                        <span className="muted" style={{ marginLeft: 8, fontSize: 12 }}>{m.name}</span>
                      )}
                    </div>
                  ))}
                  {hits.length === 0 && (
                    <div className="muted" style={{ padding: "6px 10px", fontSize: 12 }}>{t("sameModel.noMatch")}</div>
                  )}
                  {hits.length > shown.length && (
                    <div className="muted" style={{ padding: "6px 10px", fontSize: 12 }}>
                      {t("sameModel.moreResults", { n: hits.length - shown.length })}
                    </div>
                  )}
                </>
              );
            })()}
          </div>
        )}
        {/* 快捷候选：octagon.yaml suggestions（剥掉已知 provider 前缀转裸名）。 */}
        {suggested.length > 0 && (
          <div style={{ marginTop: 6, display: "flex", flexWrap: "wrap", gap: 6 }}>
            {suggested.map((s) => {
              // suggestion 带 provider 前缀（or-cc/…），剥成裸名统一心智。
              const parts = s.split("/");
              const bare = parts.length > 1 && Object.values(agentPrefix).includes(parts[0])
                ? parts.slice(1).join("/")
                : s;
              return (
                <button key={s} type="button" onClick={() => setBareModel(bare)}
                  style={{ fontSize: 12, padding: "2px 8px" }}>{bare}</button>
              );
            })}
          </div>
        )}
        {/* Blade 可用模型（自动发现）：独立下拉区块，与 OpenRouter 列表分开。
            选中即填 bareModel（剥 transport 前缀）；blade 走修好的路由回目录 id。 */}
        <div style={{ marginTop: 10, borderTop: "1px solid var(--border-subtle)", paddingTop: 10 }}>
          <p style={{ fontSize: 12, fontWeight: 600, marginBottom: 4, color: "var(--text-1)" }}>
            {t("sameModel.bladeModelsTitle")}
          </p>
          <div className="multi-model-search">
            <input
              value={bladeFilter}
              onChange={(e) => setBladeFilter(e.target.value)}
              onFocus={() => setBladePickerOpen(true)}
              onBlur={() => setBladePickerOpen(false)}
              placeholder={bladeModels.length > 0
                ? t("sameModel.bladeModelsSearch", { n: bladeModels.length })
                : t("sameModel.bladeModelsSearchEmpty")}
            />
            {bladePickerOpen && bladeModels.length > 0 && (
              <div className="model-picker-options multi-model-search-results">
                {bladeFiltered.slice(0, 50).map((m) => (
                  <button
                    key={m.id}
                    type="button"
                    className={`model-picker-option${bareModel === bladeBareId(m.id) ? " selected" : ""}`}
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={() => { setBareModel(bladeBareId(m.id)); setBladeFilter(""); setBladePickerOpen(false); }}
                    title={m.label && m.label !== m.id ? `${m.label} (${m.id})` : m.id}
                  >
                    <span className="model-picker-option-main">
                      {m.label || m.id}
                      {bladeDefaultModel === m.id && (
                        <span className="muted" style={{ marginLeft: 6, fontSize: 11 }}>
                          {t("sameModel.bladeDefault")}
                        </span>
                      )}
                    </span>
                    {m.label && m.label !== m.id && (
                      <span className="model-picker-option-sub">{m.id}</span>
                    )}
                  </button>
                ))}
                {bladeFiltered.length === 0 && (
                  <div className="model-picker-empty">{t("sameModel.noMatch")}</div>
                )}
                {bladeFiltered.length > 50 && (
                  <div className="model-picker-empty">
                    {t("sameModel.moreResults", { n: bladeFiltered.length - 50 })}
                  </div>
                )}
              </div>
            )}
          </div>
          {bladeModels.length === 0 && (
            <p className="muted" style={{ fontSize: 12, marginTop: 4 }}>
              {t("sameModel.bladeModelsUnavailable")}
              {bladeModelsError && `：${bladeModelsError}`}
            </p>
          )}
          {/* 仅在拿到 blade 目录时才告警未命中；目录拉取失败时不误报（可能只是
              暂时不可达，执行层仍允许 off-catalog 直传）。 */}
          {bladeModels.length > 0 && selected.has("blade-agent") && bareModel.trim()
            && !bladeCatalogHasModel(bareModel.trim()) && (
            <p className="warning" style={{ fontSize: 12, marginTop: 6 }} role="alert">
              {t("sameModel.bladeNotInCatalog", { model: bareModel.trim() })}
            </p>
          )}
        </div>
        {/* 提交预览：让用户看清每个 agent 实际会用什么 model 串。 */}
        {bareModel.trim() && (
          <p className="muted" style={{ fontSize: 12, marginTop: 6 }}>
            {t("sameModel.submitPreview", { preview: selectedList.map((a) => `${a} → ${modelForAgent(a, bareModel.trim())}`).join("；") })}
          </p>
        )}
        {missingPrefix.length > 0 && (
          <p className="warning" style={{ fontSize: 12 }}>
            {t("sameModel.missingPrefix", { agents: missingPrefix.join("、") })}
          </p>
        )}
        <p className="muted" style={{ fontSize: 12 }}>
          {t("sameModel.prefixHint")}
        </p>
        {selected.has("blade-agent") && (
          <label style={{ flexDirection: "row", display: "flex", alignItems: "center", gap: 6, textTransform: "none", opacity: bladeThinkingWouldBreak ? 0.5 : 1 }}>
            <input
              type="checkbox"
              checked={effectiveBladeThinking}
              disabled={bladeThinkingWouldBreak}
              onChange={(e) => setBladeEnableThinking(e.target.checked)}
            />
            <span style={{ fontSize: 13, color: "var(--text-1)" }}>
              {t("sameModel.bladeThinking")}
              {bladeThinkingWouldBreak && (
                <span style={{ fontSize: 11, color: "var(--text-2)", marginLeft: 6 }}>
                  {t("sameModel.bladeThinkingDisabled")}
                </span>
              )}
            </span>
          </label>
        )}
      </div>

      <label>{t("sameModel.env")}</label>
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
          <span className="muted">{t("sameModel.scenarioNeeds")}</span>
          {envs.find((e) => e.name === envName)!.agent_modalities!.map((m) => (
            <ModalityChip key={m} modality={m} />
          ))}
          <span className="muted">{t("sameModel.inputCapability")}</span>
        </div>
      )}
      {(envs.find((e) => e.name === envName)?.prerequisite_warnings?.length ?? 0) > 0 && (
        <div className="prereq-warn" role="alert">
          {envs.find((e) => e.name === envName)!.prerequisite_warnings!.map((w) => (
            <div key={w}>{t("sameModel.missingDependency", { warning: w })}</div>
          ))}
        </div>
      )}

      <label style={{ flexDirection: "row", display: "flex", alignItems: "center", gap: 6, textTransform: "none" }}>
        <input type="checkbox" checked={usePrompt} onChange={(e) => setUsePrompt(e.target.checked)} />
        <span style={{ fontSize: 13, color: "var(--text-1)" }}>{t("sameModel.useFreePrompt")}</span>
      </label>

      {usePrompt ? (
        <>
          <label>{t("sameModel.prompt")}</label>
          <textarea rows={4} value={freePrompt} onChange={(e) => setFreePrompt(e.target.value)}
            placeholder={t("sameModel.promptPlaceholder")} />
        </>
      ) : (
        <>
          <label>{t("sameModel.task")}</label>
          <select value={taskId} onChange={(e) => setTaskId(e.target.value)}>
            {tasks.map((tk) => (
              <option key={tk.id} value={tk.id}>{tk.id}</option>
            ))}
          </select>
        </>
      )}

      <label>{t("sameModel.attachments")}</label>
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
          {uploading ? t("sameModel.uploading") : t("sameModel.chooseFile")}
        </button>
        <span className="muted" style={{ fontSize: 12 }}>{t("sameModel.attachmentHint")}</span>
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
          {submitting ? t("sameModel.submitting") : t("sameModel.runSameModel")}
        </button>
      </div>

      {err && <p className="warning mt-sm">{err}</p>}
    </div>
  );
}
