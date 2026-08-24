import { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import {
  api,
  type AgentInfo,
  type BladeModelOption,
  type EnvSummary,
  type OpenRouterModel,
  type TaskJson,
} from "../api/client";
import {
  createExperiment,
  discoverCapabilities,
  previewExperiment,
  type VariantPreviewDto,
} from "../api/researchClient";
import { createIdempotencyKey } from "../api/idempotency";
import type { ResearchCapabilities } from "../api/researchTypes";
import { mutatorLabel, previewMessageLabel, statusLabel } from "../researchUi";
import {
  availableMutators,
  cardinality,
  legacyProtocol,
  modelVisiblePrompt,
  sameModelForAgent,
  type BuilderVariant,
  type CompareMode,
} from "../experiments/builder";


import { AGENT_NAMES } from "../agents";
import { useI18n } from "../i18n";

type VariantControl = {
  mutator: string;
  label: string;
  icon: string;
  description: string;
  applicability: string;
  settingLabel: string;
  defaultSetting: string;
  settings: readonly { value: string; label: string }[];
  settingKind: "intensity" | "position";
};

const VARIANT_CONTROLS: readonly VariantControl[] = [{
  mutator: "spacing",
  label: "experimentBuilder.variant.spacing.label",
  icon: "↔",
  description: "experimentBuilder.variant.spacing.description",
  applicability: "experimentBuilder.variant.spacing.applicability",
  settingLabel: "experimentBuilder.variant.setting.intensity",
  defaultSetting: "low",
  settings: [
    { value: "low", label: "experimentBuilder.variant.intensity.low" },
    { value: "medium", label: "experimentBuilder.variant.intensity.medium" },
    { value: "high", label: "experimentBuilder.variant.intensity.high" },
  ],
  settingKind: "intensity",
}, {
  mutator: "letter-case",
  label: "experimentBuilder.variant.letterCase.label",
  icon: "Aa",
  description: "experimentBuilder.variant.letterCase.description",
  applicability: "experimentBuilder.variant.letterCase.applicability",
  settingLabel: "experimentBuilder.variant.setting.intensity",
  defaultSetting: "low",
  settings: [
    { value: "low", label: "experimentBuilder.variant.intensity.low" },
    { value: "medium", label: "experimentBuilder.variant.intensity.medium" },
    { value: "high", label: "experimentBuilder.variant.intensity.high" },
  ],
  settingKind: "intensity",
}, {
  mutator: "unicode-homoglyph",
  label: "experimentBuilder.variant.homoglyph.label",
  icon: "АA",
  description: "experimentBuilder.variant.homoglyph.description",
  applicability: "experimentBuilder.variant.homoglyph.applicability",
  settingLabel: "experimentBuilder.variant.setting.substitution",
  defaultSetting: "low",
  settings: [
    { value: "low", label: "experimentBuilder.variant.intensity.low" },
    { value: "medium", label: "experimentBuilder.variant.intensity.medium" },
    { value: "high", label: "experimentBuilder.variant.intensity.high" },
  ],
  settingKind: "intensity",
}, {
  mutator: "instruction-position",
  label: "experimentBuilder.variant.instructionPosition.label",
  icon: "⇅",
  description: "experimentBuilder.variant.instructionPosition.description",
  applicability: "experimentBuilder.variant.instructionPosition.applicability",
  settingLabel: "experimentBuilder.variant.setting.arrangement",
  defaultSetting: "tail",
  settings: [
    { value: "head", label: "experimentBuilder.variant.position.head" },
    { value: "tail", label: "experimentBuilder.variant.position.tail" },
    { value: "shuffle", label: "experimentBuilder.variant.position.shuffle" },
  ],
  settingKind: "position",
}];

export type VariantSelections = Record<string, string | null>;

export function buildVariantSpecs(selections: VariantSelections): BuilderVariant[] {
  const selected = VARIANT_CONTROLS.filter(
    (control) => typeof selections[control.mutator] === "string",
  );
  return [{
    schema_version: "octagon-variant-spec-v1",
    mutator: "baseline",
    version: "1",
    seed: 0,
    intensity: "identity",
    params: {},
  }, ...selected.map((control, index) => {
    const setting = selections[control.mutator] ?? control.defaultSetting;
    return {
      schema_version: "octagon-variant-spec-v1" as const,
      mutator: control.mutator,
      version: "1",
      seed: index + 1,
      intensity: control.settingKind === "intensity" ? setting : "default",
      params: control.settingKind === "position" ? { position: setting } : {},
    };
  })];
}

export function ExperimentBuilder() {
  const { t, lang } = useI18n();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [capabilities, setCapabilities] = useState<ResearchCapabilities | null>(null);
  const [envs, setEnvs] = useState<EnvSummary[]>([]);
  const [tasks, setTasks] = useState<TaskJson[]>([]);
  const [metaYaml, setMetaYaml] = useState("");
  const [metaError, setMetaError] = useState("");
  const [envName, setEnvName] = useState(searchParams.get("env") ?? "");
  const [taskId, setTaskId] = useState("");
  const [freePrompt, setFreePrompt] = useState("");
  const [usePrompt, setUsePrompt] = useState(false);
  const [title, setTitle] = useState("");
  const [question, setQuestion] = useState("");
  const [mode, setMode] = useState<CompareMode>("multi-agent");
  const [availableAgents, setAvailableAgents] = useState<AgentInfo[]>([]);
  const [multiAgentSelectedAgents, setMultiAgentSelectedAgents] = useState<Set<string>>(
    new Set(["blade-agent", "claude-code"]),
  );
  const [multiAgentModels, setMultiAgentModels] = useState<Record<string, string>>({});
  const [multiAgentQueries, setMultiAgentQueries] = useState<Record<string, string>>({});
  const [selectedAgents, setSelectedAgents] = useState<Set<string>>(
    new Set(["blade-agent", "claude-code"]),
  );
  const [bareModel, setBareModel] = useState("");
  const [modelQuery, setModelQuery] = useState("");
  const [bladeModelOverride, setBladeModelOverride] = useState("");
  const [multiModelAgent, setMultiModelAgent] = useState<string>("blade-agent");
  const [multiModelQuery, setMultiModelQuery] = useState("");
  const [multiModelSelections, setMultiModelSelections] = useState<string[]>([]);
  const [openRouterModels, setOpenRouterModels] = useState<OpenRouterModel[]>([]);
  const [bladeModels, setBladeModels] = useState<BladeModelOption[]>([]);
  const [bladeModelsError, setBladeModelsError] = useState("");
  const [agentPrefix, setAgentPrefix] = useState<Record<string, string>>({});
  const [variantSelections, setVariantSelections] = useState<VariantSelections>(() =>
    Object.fromEntries(VARIANT_CONTROLS.map((control) => [control.mutator, null]))
  );
  const [repeats, setRepeats] = useState(1);
  // null = 跟随所选任务的 timeout_seconds（自由 prompt 时跟随后端默认值 600s）；
  // 用户一旦手动改过就固定为该值，不再随切换任务自动跳变。
  const [timeoutSecondsOverride, setTimeoutSecondsOverride] = useState<number | null>(null);
  const [notifyModelOfTimeout, setNotifyModelOfTimeout] = useState(false);
  const [profileId, setProfileId] = useState(searchParams.get("profile") ?? "");
  const [preview, setPreview] = useState<VariantPreviewDto | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    discoverCapabilities().then((result) => {
      if (result.ok) setCapabilities(result.value);
      else setError(t("experimentBuilder.error.capability", { message: result.error.message }));
    }).catch((reason) => setError(String(reason)));
    api.listEnvs().then((items) => {
      setEnvs(items);
      setEnvName((current) => items.some((item) => item.name === current) ? current : items[0]?.name ?? "");
    }).catch((reason) => setError(String(reason)));
    api.listAgents().then(setAvailableAgents).catch(() => setAvailableAgents([]));
    api.openrouterModels().then((result) => {
      setOpenRouterModels(result.models);
    }).catch(() => setOpenRouterModels([]));
    api.bladeModels().then((result) => {
      setBladeModels(result.models);
      setBladeModelsError(result.error ?? "");
    }).catch((reason) => {
      setBladeModels([]);
      setBladeModelsError(String(reason));
    });
    api.listModelProviders().then((result) => {
      setAgentPrefix(result.agent_prefix ?? {});
    }).catch(() => setAgentPrefix({}));
  }, []);

  useEffect(() => {
    if (!envName) return;
    api.listEnvTasks(envName).then((items) => {
      setTasks(items);
      setTaskId(items[0]?.id ?? "");
    }).catch((reason) => setError(String(reason)));
    setMetaYaml("");
    setMetaError("");
    api.getEnvMeta(envName).then((result) => {
      setMetaYaml(result.meta_yaml);
    }).catch((reason) => setMetaError(String(reason)));
  }, [envName]);

  const selectedEnv = envs.find((env) => env.name === envName);
  const selectedTask = tasks.find((task) => task.id === taskId);
  const defaultTimeoutSeconds = usePrompt ? 600 : (selectedTask?.timeout_seconds ?? 600);
  const timeoutSeconds = timeoutSecondsOverride ?? defaultTimeoutSeconds;
  const sourcePrompt = usePrompt ? freePrompt : (selectedTask?.prompt ?? "");
  const sourceContext = usePrompt ? {} : (selectedTask?.context ?? {});
  const taskPromptPreview = modelVisiblePrompt(
    sourcePrompt,
    sourceContext,
    timeoutSeconds,
    notifyModelOfTimeout,
  );
  const allowedMutators = useMemo(
    () => availableMutators(selectedEnv, usePrompt ? {} : selectedTask?.context),
    [selectedEnv, selectedTask, usePrompt],
  );
  const availableVariantControls = useMemo(
    () => VARIANT_CONTROLS.filter((control) => allowedMutators.has(control.mutator)),
    [allowedMutators],
  );
  const activeVariantSelections = useMemo(
    () => Object.fromEntries(VARIANT_CONTROLS.map((control) => [
      control.mutator,
      allowedMutators.has(control.mutator) ? variantSelections[control.mutator] : null,
    ])),
    [allowedMutators, variantSelections],
  );

  const automaticBladeModel = sameModelForAgent(
    "blade-agent",
    bareModel.trim(),
    agentPrefix,
    bladeModels.map((item) => item.id),
  );
  const effectiveBladeModel = bladeModelOverride.trim() || automaticBladeModel;

  const sameModelCandidates = useMemo(() => {
    const bare = bareModel.trim();
    if (!bare) return [];
    return AGENT_NAMES.filter((agent) => selectedAgents.has(agent)).map((agent) => {
      return {
        agent,
        model: agent === "blade-agent"
          ? effectiveBladeModel
          : sameModelForAgent(
            agent,
            bare,
            agentPrefix,
            bladeModels.map((item) => item.id),
          ),
      };
    });
  }, [
    agentPrefix,
    bareModel,
    bladeModels,
    effectiveBladeModel,
    selectedAgents,
  ]);

  const multiAgentCandidates = useMemo(() =>
    AGENT_NAMES.filter((agent) => multiAgentSelectedAgents.has(agent)).map((agent) => {
      const model = multiAgentModels[agent];
      return {
        agent,
        model: model
          ? sameModelForAgent(agent, model, agentPrefix, bladeModels.map((item) => item.id))
          : null,
      };
    }), [
    agentPrefix,
    bladeModels,
    multiAgentModels,
    multiAgentSelectedAgents,
  ]);

  const multiModelCandidates = useMemo(() => multiModelSelections.map((model) => ({
    agent: multiModelAgent,
    model: sameModelForAgent(
      multiModelAgent,
      model,
      agentPrefix,
      bladeModels.map((item) => item.id),
    ),
  })), [agentPrefix, bladeModels, multiModelAgent, multiModelSelections]);

  const protocol = useMemo(() => {
    const candidates = mode === "same-model"
      ? sameModelCandidates
      : mode === "multi-model"
        ? multiModelCandidates
        : multiAgentCandidates;
    const draft = legacyProtocol(mode, candidates);
    draft.variant_specs = buildVariantSpecs(activeVariantSelections);
    draft.repeats = repeats;
    draft.profile = profileId ? { id: profileId } : null;
    draft.notify_model_of_timeout = notifyModelOfTimeout;
    return draft;
  }, [
    mode,
    multiAgentCandidates,
    multiModelCandidates,
    profileId,
    repeats,
    notifyModelOfTimeout,
    sameModelCandidates,
    activeVariantSelections,
  ]);
  const size = cardinality(protocol);
  const enabled = capabilities?.features.experiments === true
    && capabilities.features.task_variants === true
    && capabilities.features.run_groups === true;
  const selectedAgentNames = AGENT_NAMES.filter((agent) => selectedAgents.has(agent));
  const missingProviderPrefixes = selectedAgentNames.filter(
    (agent) => agent !== "blade-agent" && !agentPrefix[agent],
  );
  const bladeModelCatalogMatch = bladeModels.some(
    (item) => item.id === effectiveBladeModel,
  );
  const sameModelReady = mode !== "same-model"
    || (sameModelCandidates.length >= 2
      && bareModel.trim().length > 0
      && missingProviderPrefixes.length === 0);
  const multiAgentReady = mode !== "multi-agent"
    || (multiAgentCandidates.length >= 2
      && multiAgentCandidates.every((item) => item.model)
      && multiAgentCandidates.every((item) =>
        item.agent === "blade-agent" || Boolean(agentPrefix[item.agent])
      ));
  const multiModelReady = mode !== "multi-model"
    || (multiModelCandidates.length >= 2
      && (multiModelAgent === "blade-agent" || Boolean(agentPrefix[multiModelAgent])));
  const modelHits = useMemo(() => {
    const query = modelQuery.trim().toLowerCase();
    if (!query) return [];
    return openRouterModels.filter(
      (item) => item.id.toLowerCase().includes(query)
        || item.name.toLowerCase().includes(query),
    ).slice(0, 50);
  }, [modelQuery, openRouterModels]);
  const multiModelOptions = useMemo(() => multiModelAgent === "blade-agent"
    && bladeModels.length > 0
    ? bladeModels.map((item) => ({ id: item.id, name: item.label || item.id }))
    : openRouterModels, [bladeModels, multiModelAgent, openRouterModels]);
  const multiModelHits = useMemo(() => {
    const query = multiModelQuery.trim().toLowerCase();
    if (!query) return [];
    return multiModelOptions.filter(
      (item) => item.id.toLowerCase().includes(query)
        || item.name.toLowerCase().includes(query),
    ).slice(0, 50);
  }, [multiModelOptions, multiModelQuery]);

  const toggleAgent = (agent: string) => {
    setSelectedAgents((current) => {
      const next = new Set(current);
      if (next.has(agent)) next.delete(agent);
      else next.add(agent);
      return next;
    });
  };

  const toggleMultiAgent = (agent: string) => {
    setMultiAgentSelectedAgents((current) => {
      const next = new Set(current);
      if (next.has(agent)) next.delete(agent);
      else next.add(agent);
      return next;
    });
  };

  const setMultiAgentModel = (agent: string, model: string) => {
    setMultiAgentModels((current) => ({ ...current, [agent]: model }));
    setMultiAgentQueries((current) => ({ ...current, [agent]: "" }));
  };

  const clearMultiAgentModel = (agent: string) => {
    setMultiAgentModels((current) => {
      const next = { ...current };
      delete next[agent];
      return next;
    });
  };

  const multiAgentOptions = (agent: string) => agent === "blade-agent"
    && bladeModels.length > 0
    ? bladeModels.map((item) => ({ id: item.id, name: item.label || item.id }))
    : openRouterModels;

  const multiAgentHits = (agent: string) => {
    const query = (multiAgentQueries[agent] ?? "").trim().toLowerCase();
    if (!query) return [];
    return multiAgentOptions(agent).filter(
      (item) => item.id.toLowerCase().includes(query)
        || item.name.toLowerCase().includes(query),
    ).slice(0, 30);
  };

  const toggleVariant = (control: VariantControl) => {
    setVariantSelections((current) => ({
      ...current,
      [control.mutator]: current[control.mutator] === null ? control.defaultSetting : null,
    }));
  };

  const setVariantSetting = (mutator: string, setting: string) => {
    setVariantSelections((current) => ({ ...current, [mutator]: setting }));
  };

  const selectMultiModelAgent = (agent: string) => {
    setMultiModelAgent(agent);
    setMultiModelSelections([]);
    setMultiModelQuery("");
  };

  const addMultiModel = (model: string) => {
    setMultiModelSelections((current) =>
      current.includes(model) ? current : [...current, model]
    );
    setMultiModelQuery("");
  };

  const removeMultiModel = (model: string) => {
    setMultiModelSelections((current) => current.filter((item) => item !== model));
  };

  useEffect(() => setPreview(null), [envName, taskId, freePrompt, usePrompt, protocol, timeoutSeconds]);

  const source = {
    env_name: envName,
    ...(usePrompt ? { prompt: freePrompt, context: {}, constraints: {} } : { task_id: taskId }),
    timeout_seconds: timeoutSeconds,
    protocol,
  };

  const runPreview = async () => {
    if (!capabilities) return;
    setBusy(true);
    setError("");
    try { setPreview(await previewExperiment(capabilities, source)); }
    catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  };

  const create = async () => {
    if (!capabilities || !preview) return;
    setBusy(true);
    setError("");
    try {
      const created = await createExperiment(capabilities, {
        ...source,
        title,
        question,
        preview_token: preview.preview_token,
      }, createIdempotencyKey());
      navigate(`/experiments/${created.experiment_id}/groups/${created.run_group_id}`);
    } catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  };

  if (capabilities && !enabled) {
    return <div className="card" role="status">
      {t("experimentBuilder.disabled")}
    </div>;
  }
  const candidateCount = mode === "same-model"
    ? sameModelCandidates.length
    : mode === "multi-model"
      ? multiModelCandidates.length
      : multiAgentCandidates.length;
  return <div className="experiment-builder-page">
    <div className="builder-heading">
      <div><span className="builder-eyebrow">EXPERIMENT BUILDER</span><h2>{t("experimentBuilder.title")}</h2>
        <p>{t("experimentBuilder.subtitle")}</p></div>
    </div>
    <div className="builder-layout">
      <aside className="builder-steps" aria-label={t("experimentBuilder.steps.aria")}>
        {[
          ["01", t("experimentBuilder.step.goal"), Boolean(title && question)],
          ["02", t("experimentBuilder.step.source"), Boolean(envName && (usePrompt ? freePrompt : taskId))],
          ["03", t("experimentBuilder.step.matrix"), candidateCount >= 2],
          ["04", t("experimentBuilder.step.variants"), size.blocking.length === 0],
          ["05", t("experimentBuilder.step.review"), Boolean(preview)],
        ].map(([number, label, complete], index) => <div
          className={`builder-step${complete ? " is-complete" : ""}${!complete && index === 0 ? " is-current" : ""}`}
          key={String(number)}
        >
          <span>{complete ? "✓" : number}</span><div><strong>{label}</strong><small>{complete ? t("experimentBuilder.step.configured") : t("experimentBuilder.step.pending")}</small></div>
        </div>)}
      </aside>

      <div className="builder-form">
        <section className="builder-section">
          <div className="builder-section-head"><span>01</span><div><h3>{t("experimentBuilder.section.goal.title")}</h3><p>{t("experimentBuilder.section.goal.hint")}</p></div></div>
          <div className="builder-fields two-columns">
            <label>{t("experimentBuilder.field.title")}<input value={title} onChange={(event) => setTitle(event.target.value)} placeholder={t("experimentBuilder.field.title.placeholder")} /></label>
            <label>{t("experimentBuilder.field.question")}<input value={question} onChange={(event) => setQuestion(event.target.value)} placeholder={t("experimentBuilder.field.question.placeholder")} /></label>
          </div>
        </section>

        <section className="builder-section">
          <div className="builder-section-head"><span>02</span><div><h3>{t("experimentBuilder.section.source.title")}</h3><p>{t("experimentBuilder.section.source.hint")}</p></div></div>
          <div className="builder-fields two-columns">
            <label>{t("experimentBuilder.field.scenario")}<select value={envName} onChange={(event) => setEnvName(event.target.value)}>
              {envs.map((env) => <option key={env.name} value={env.name}>{env.name}</option>)}
            </select></label>
            {!usePrompt && <label>{t("experimentBuilder.field.task")}<select aria-label={t("experimentBuilder.field.task")} value={taskId} onChange={(event) => setTaskId(event.target.value)}>
              {tasks.map((task) => <option key={task.id} value={task.id}>{task.id}</option>)}
            </select></label>}
          </div>
          <label className="builder-check"><input type="checkbox" checked={usePrompt} onChange={(event) => setUsePrompt(event.target.checked)} /> {t("experimentBuilder.usePrompt")}</label>
          {usePrompt && <label>{t("experimentBuilder.field.customPrompt")}<textarea aria-label={t("experimentBuilder.field.customPrompt")} value={freePrompt} onChange={(event) => setFreePrompt(event.target.value)} /></label>}
          <div className="builder-meta-panel">
            <div className="builder-preview-heading">
              <span><strong>{t("experimentBuilder.meta.scenarioDef")}</strong><code>meta.yaml</code></span>
              <small>{selectedEnv?.description || selectedEnv?.test_focus || envName}</small>
            </div>
            {metaError
              ? <p className="warning">{t("experimentBuilder.meta.loadError", { message: metaError })}</p>
              : <pre aria-label={t("experimentBuilder.meta.aria")}>{metaYaml || t("experimentBuilder.meta.loading")}</pre>}
          </div>
        </section>

        <section className="builder-section">
          <div className="builder-section-head"><span>03</span><div><h3>{t("experimentBuilder.section.matrix.title")}</h3><p>{t("experimentBuilder.section.matrix.hint")}</p></div></div>
          <div className="builder-mode-grid">
            {[
              ["multi-agent", t("experimentBuilder.mode.multiAgent"), t("experimentBuilder.mode.multiAgent.desc")],
              ["same-model", t("experimentBuilder.mode.sameModel"), t("experimentBuilder.mode.sameModel.desc")],
              ["multi-model", t("experimentBuilder.mode.multiModel"), t("experimentBuilder.mode.multiModel.desc")],
            ].map(([value, label, description]) => <button
              type="button"
              key={value}
              className={mode === value ? "is-selected" : ""}
              onClick={() => setMode(value as CompareMode)}
            ><strong>{label}</strong><span>{description}</span></button>)}
          </div>
          {mode === "same-model" ? <div className="builder-candidate-panel">
            <div className="builder-subheading"><h4>{t("experimentBuilder.candidate.agents")}</h4><span>{t("experimentBuilder.candidate.atLeastTwo")}</span></div>
            <div className="builder-agent-grid">
              {AGENT_NAMES.map((agent) => {
                const status = availableAgents.find((item) => item.name === agent)?.status;
                const unavailable = status !== undefined && status !== "available";
                return <button
                  type="button"
                  key={agent}
                  className={`agent-toggle${selectedAgents.has(agent) ? " selected" : ""}`}
                  disabled={unavailable}
                  onClick={() => toggleAgent(agent)}
                >
                  <span className="builder-agent-state">{selectedAgents.has(agent) ? "✓" : "+"}</span>
                  <span><strong>{agent}</strong><small>{unavailable ? t("experimentBuilder.agent.unavailable") : selectedAgents.has(agent) ? t("experimentBuilder.agent.inMatrix") : t("experimentBuilder.agent.clickToAdd")}</small></span>
                </button>;
              })}
            </div>
            {selectedAgents.size < 2 && <p className="warning">{t("experimentBuilder.warning.atLeastTwoAgents")}</p>}
            <div className="builder-subheading"><h4>{t("experimentBuilder.candidate.sharedModel")}</h4><span>{t("experimentBuilder.candidate.modelsAvailable", { n: openRouterModels.length })}</span></div>
            <div className="builder-model-picker">
              <input
                value={modelQuery}
                onChange={(event) => setModelQuery(event.target.value)}
                placeholder={t("experimentBuilder.model.searchPlaceholder")}
              />
              {modelQuery.trim() && <div className="model-picker-options multi-model-search-results">
                {modelHits.map((model) => <button
                  type="button"
                  className={`model-picker-option${model.id === bareModel ? " selected" : ""}`}
                  key={model.id}
                  onClick={() => {
                    setBareModel(model.id);
                    setBladeModelOverride("");
                    setModelQuery("");
                  }}
                >
                  <span className="model-picker-option-main">{model.name}</span>
                  <span className="model-picker-option-sub">{model.id}</span>
                </button>)}
                {modelHits.length === 0 && <div className="model-picker-empty">{t("experimentBuilder.model.noMatch")}</div>}
              </div>}
            </div>
            {bareModel && <div className="builder-model-selected">
              <span>{t("experimentBuilder.model.selected")}</span><strong>{openRouterModels.find((item) => item.id === bareModel)?.name ?? bareModel}</strong>
              <code>{bareModel}</code>
              <div>{sameModelCandidates.map((item) => <span key={item.agent}>{item.agent} → {item.model}</span>)}</div>
              {selectedAgents.has("blade-agent") && <label className="builder-blade-model-override">
                <span>{t("experimentBuilder.blade.finalModelId")}</span>
                <span>
                  <input
                    aria-label={t("experimentBuilder.blade.finalModelId")}
                    value={effectiveBladeModel}
                    onChange={(event) => setBladeModelOverride(event.target.value)}
                  />
                  <button
                    type="button"
                    disabled={!bladeModelOverride}
                    onClick={() => setBladeModelOverride("")}
                  >{t("experimentBuilder.blade.restoreAuto")}</button>
                </span>
                <small>{t("experimentBuilder.blade.finalModelHint")}</small>
              </label>}
            </div>}
            {missingProviderPrefixes.length > 0 && <p className="warning">{t("experimentBuilder.warning.missingPrefix", { agents: missingProviderPrefixes.join("、") })}</p>}
            {bareModel && selectedAgents.has("blade-agent") && !bladeModelCatalogMatch
              && <p className="warning">
                {bladeModelsError
                  ? t("experimentBuilder.blade.catalogUnavailable")
                  : t("experimentBuilder.blade.catalogMiss")}
              </p>}
          </div> : mode === "multi-model" ? <div className="builder-candidate-panel">
            <div className="builder-subheading"><h4>{t("experimentBuilder.candidate.execAgent")}</h4><span>{t("experimentBuilder.candidate.selectOne")}</span></div>
            <div className="builder-agent-grid">
              {AGENT_NAMES.map((agent) => {
                const status = availableAgents.find((item) => item.name === agent)?.status;
                const unavailable = status !== undefined && status !== "available";
                const selected = multiModelAgent === agent;
                return <button
                  type="button"
                  key={agent}
                  className={`agent-toggle${selected ? " selected" : ""}`}
                  disabled={unavailable}
                  aria-pressed={selected}
                  onClick={() => selectMultiModelAgent(agent)}
                >
                  <span className="builder-agent-state">{selected ? "✓" : "+"}</span>
                  <span><strong>{agent}</strong><small>{unavailable ? t("experimentBuilder.agent.unavailable") : selected ? t("experimentBuilder.agent.currentExec") : t("experimentBuilder.agent.clickToSelect")}</small></span>
                </button>;
              })}
            </div>
            <div className="builder-subheading">
              <h4>{t("experimentBuilder.candidate.models")}</h4>
              <span>{t("experimentBuilder.candidate.modelsAvailableAtLeastTwo", { n: multiModelOptions.length })}</span>
            </div>
            <div className="builder-model-picker">
              <input
                value={multiModelQuery}
                onChange={(event) => setMultiModelQuery(event.target.value)}
                placeholder={multiModelAgent === "blade-agent"
                  ? t("experimentBuilder.model.searchBladePlaceholder")
                  : t("experimentBuilder.model.searchPlaceholder")}
                aria-label={t("experimentBuilder.model.searchCandidateAria")}
              />
              {multiModelQuery.trim() && <div className="model-picker-options multi-model-search-results">
                {multiModelHits.map((model) => <button
                  type="button"
                  className={`model-picker-option${multiModelSelections.includes(model.id) ? " selected" : ""}`}
                  key={model.id}
                  onClick={() => addMultiModel(model.id)}
                >
                  <span className="model-picker-option-main">{model.name}</span>
                  <span className="model-picker-option-sub">{model.id}</span>
                </button>)}
                {multiModelHits.length === 0 && <div className="model-picker-empty">{t("experimentBuilder.model.noMatchAvailable")}</div>}
              </div>}
            </div>
            <div className="builder-multi-model-selected" aria-label={t("experimentBuilder.model.selectedCandidatesAria")}>
              {multiModelSelections.length === 0
                ? <div className="builder-model-empty">{t("experimentBuilder.model.emptyHint")}</div>
                : multiModelSelections.map((model) => {
                  const resolved = multiModelCandidates.find((item) =>
                    item.model === sameModelForAgent(
                      multiModelAgent,
                      model,
                      agentPrefix,
                      bladeModels.map((item) => item.id),
                    )
                  )?.model;
                  const label = multiModelOptions.find((item) => item.id === model)?.name ?? model;
                  return <article key={model}>
                    <span><strong>{label}</strong><code>{resolved}</code></span>
                    <button
                      type="button"
                      aria-label={t("experimentBuilder.model.removeAria", { label })}
                      onClick={() => removeMultiModel(model)}
                    >×</button>
                  </article>;
                })}
            </div>
            {multiModelSelections.length === 1 && <p className="warning">{t("experimentBuilder.warning.oneMoreModel")}</p>}
            {multiModelAgent !== "blade-agent" && !agentPrefix[multiModelAgent]
              && <p className="warning">{t("experimentBuilder.warning.missingPrefixSingle", { agent: multiModelAgent })}</p>}
          </div> : <div className="builder-candidate-panel">
            <div className="builder-subheading">
              <h4>{t("experimentBuilder.candidate.agentsAndModels")}</h4>
              <span>{t("experimentBuilder.candidate.agentsModelsHint")}</span>
            </div>
            <div className="builder-agent-model-list">
              {AGENT_NAMES.map((agent) => {
                const enabledAgent = multiAgentSelectedAgents.has(agent);
                const status = availableAgents.find((item) => item.name === agent)?.status;
                const unavailable = status !== undefined && status !== "available";
                const selectedModel = multiAgentModels[agent];
                const options = multiAgentOptions(agent);
                const selectedLabel = options.find((item) => item.id === selectedModel)?.name
                  ?? selectedModel;
                const resolvedModel = selectedModel
                  ? sameModelForAgent(
                    agent,
                    selectedModel,
                    agentPrefix,
                    bladeModels.map((item) => item.id),
                  )
                  : null;
                const query = multiAgentQueries[agent] ?? "";
                const hits = multiAgentHits(agent);
                return <article
                  className={`builder-agent-model-row${enabledAgent ? " is-enabled" : ""}`}
                  key={agent}
                >
                  <button
                    type="button"
                    role="switch"
                    aria-checked={enabledAgent}
                    aria-label={`${enabledAgent ? t("experimentBuilder.agent.toggleOff") : t("experimentBuilder.agent.toggleOn")} ${agent}`}
                    className="builder-agent-model-toggle"
                    disabled={unavailable}
                    onClick={() => toggleMultiAgent(agent)}
                  >
                    <span className="builder-agent-state">{enabledAgent ? "✓" : "+"}</span>
                    <span><strong>{agent}</strong><small>{unavailable ? t("experimentBuilder.agent.unavailable") : enabledAgent ? t("experimentBuilder.agent.inComparison") : t("experimentBuilder.agent.notAdded")}</small></span>
                  </button>
                  <div className="builder-agent-model-picker">
                    <div className="builder-model-picker">
                      <input
                        value={query}
                        disabled={!enabledAgent || unavailable}
                        onChange={(event) => setMultiAgentQueries((current) => ({
                          ...current,
                          [agent]: event.target.value,
                        }))}
                        placeholder={agent === "blade-agent"
                          ? t("experimentBuilder.model.searchBladePlaceholder")
                          : t("experimentBuilder.model.searchPlaceholder")}
                        aria-label={t("experimentBuilder.model.searchAgentAria", { agent })}
                      />
                      {enabledAgent && query.trim() && <div className="model-picker-options multi-model-search-results">
                        {hits.map((model) => <button
                          type="button"
                          className={`model-picker-option${selectedModel === model.id ? " selected" : ""}`}
                          key={model.id}
                          onClick={() => setMultiAgentModel(agent, model.id)}
                        >
                          <span className="model-picker-option-main">{model.name}</span>
                          <span className="model-picker-option-sub">{model.id}</span>
                        </button>)}
                        {hits.length === 0 && <div className="model-picker-empty">{t("experimentBuilder.model.noMatchAvailable")}</div>}
                      </div>}
                    </div>
                    {enabledAgent && (selectedModel
                      ? <div className="builder-agent-model-value">
                        <span><strong>{selectedLabel}</strong><code>{resolvedModel}</code></span>
                        <button
                          type="button"
                          aria-label={t("experimentBuilder.model.clearAgentAria", { agent })}
                          onClick={() => clearMultiAgentModel(agent)}
                        >×</button>
                      </div>
                      : <div className="builder-agent-model-missing">{t("experimentBuilder.model.selectForAgent")}</div>)}
                    {enabledAgent && agent !== "blade-agent" && !agentPrefix[agent]
                      && <p className="warning">{t("experimentBuilder.warning.missingPrefixSingle", { agent })}</p>}
                  </div>
                </article>;
              })}
            </div>
            {multiAgentSelectedAgents.size < 2 && <p className="warning">{t("experimentBuilder.warning.atLeastTwoForCompare")}</p>}
          </div>}
        </section>

        <section className="builder-section">
          <div className="builder-section-head"><span>04</span><div><h3>{t("experimentBuilder.section.variants.title")}</h3><p>{t("experimentBuilder.section.variants.hint")}</p></div></div>
          <div className="variant-console">
            <div className="variant-console-heading">
              <div><strong>{t("experimentBuilder.variantConsole.title")}</strong><span>{t("experimentBuilder.variantConsole.hint")}</span></div>
              <small>{t("experimentBuilder.variantConsole.enabledCount", { n: protocol.variant_specs.length })}</small>
            </div>
            <article className="variant-control is-enabled is-baseline">
              <div className="variant-control-icon" aria-hidden="true">◎</div>
              <div className="variant-control-copy">
                <div className="variant-control-title">
                  <strong>{t("experimentBuilder.baseline.title")}</strong>
                  <code>baseline</code>
                  <span className="variant-fixed-badge">{t("experimentBuilder.baseline.fixedBadge")}</span>
                </div>
                <p>{t("experimentBuilder.baseline.description")}</p>
                <small>{t("experimentBuilder.baseline.note")}</small>
              </div>
              <div className="variant-baseline-state"><span>{t("experimentBuilder.baseline.asIs")}</span><i /></div>
            </article>
            {availableVariantControls.length === 0 && <div className="variant-empty">
              {t("experimentBuilder.variant.empty")}
            </div>}
            {availableVariantControls.map((control) => {
              const setting = variantSelections[control.mutator];
              const enabledVariant = setting !== null;
              return <article
                className={`variant-control${enabledVariant ? " is-enabled" : ""}`}
                key={control.mutator}
              >
                <div className="variant-control-icon" aria-hidden="true">{control.icon}</div>
                <div className="variant-control-copy">
                  <div className="variant-control-title">
                    <strong>{t(control.label)}</strong>
                    <code>{control.mutator}</code>
                  </div>
                  <p>{t(control.description)}</p>
                  <small>{t(control.applicability)}</small>
                  <div className="variant-gearbox">
                    <span>{t(control.settingLabel)}</span>
                    <div className="variant-gears" role="group" aria-label={`${t(control.label)}${t(control.settingLabel)}`}>
                      {control.settings.map((option) => <button
                        type="button"
                        key={option.value}
                        disabled={!enabledVariant}
                        className={setting === option.value ? "is-selected" : ""}
                        aria-pressed={setting === option.value}
                        onClick={() => setVariantSetting(control.mutator, option.value)}
                      >{t(option.label)}</button>)}
                    </div>
                  </div>
                </div>
                <button
                  type="button"
                  role="switch"
                  aria-checked={enabledVariant}
                  aria-label={`${enabledVariant ? t("experimentBuilder.agent.toggleOff") : t("experimentBuilder.agent.toggleOn")}${t(control.label)}`}
                  className="variant-switch"
                  onClick={() => toggleVariant(control)}
                ><span /></button>
              </article>;
            })}
          </div>
          <div className="builder-fields two-columns">
            <label>{t("experimentBuilder.field.repeats")}<input type="number" min={1} value={repeats} onChange={(event) => setRepeats(Number(event.target.value))} /></label>
            <label>{t("experimentBuilder.field.timeout")}
              <input
                type="number" min={1} value={timeoutSeconds}
                onChange={(event) => setTimeoutSecondsOverride(Number(event.target.value))}
              />
              {timeoutSecondsOverride === null && !usePrompt && selectedTask && (
                <small className="hint">{t("experimentBuilder.timeout.followTask")}</small>
              )}
            </label>
            <label>
              <span>{t("experimentBuilder.notifyTimeout.label")}</span>
              <span className="builder-checkbox-row">
                <input
                  type="checkbox"
                  checked={notifyModelOfTimeout}
                  onChange={(event) => setNotifyModelOfTimeout(event.target.checked)}
                />
                <span>{t("experimentBuilder.notifyTimeout.row")}</span>
              </span>
              <small className="hint">{t("experimentBuilder.notifyTimeout.hint")}</small>
            </label>
            <label>{t("experimentBuilder.field.profile")}<input value={profileId} onChange={(event) => setProfileId(event.target.value)} placeholder={t("experimentBuilder.field.profile.placeholder")} /></label>
          </div>
          <div className="builder-prompt-preview">
            <div className="builder-preview-heading">
              <span><strong>{t("experimentBuilder.prompt.visibleTitle")}</strong><code>baseline</code></span>
              <small className={notifyModelOfTimeout ? "is-notified" : ""}>
                {notifyModelOfTimeout ? t("experimentBuilder.prompt.timeoutInjected") : t("experimentBuilder.prompt.timeoutNotNotified")}
              </small>
            </div>
            <p>{t("experimentBuilder.prompt.explainer")}</p>
            <pre aria-label={t("experimentBuilder.prompt.visibleTitle")}>
              {taskPromptPreview || t("experimentBuilder.prompt.placeholder")}
            </pre>
          </div>
        </section>
      </div>

      <aside className="builder-summary" aria-live="polite">
        <div className="builder-summary-head"><span>{t("experimentBuilder.summary.title")}</span><small>{preview ? t("experimentBuilder.summary.previewed") : t("experimentBuilder.summary.draft")}</small></div>
        {error && <div className="error" role="alert">{error}</div>}
        <dl>
          <div><dt>{t("experimentBuilder.summary.mode")}</dt><dd>{mode === "same-model" ? t("experimentBuilder.mode.sameModel") : mode === "multi-model" ? t("experimentBuilder.mode.multiModel") : t("experimentBuilder.mode.multiAgent")}</dd></div>
          <div><dt>{t("experimentBuilder.summary.candidates")}</dt><dd>{candidateCount}</dd></div>
          <div><dt>{t("experimentBuilder.summary.variants")}</dt><dd>{protocol.variant_specs.length}</dd></div>
          <div><dt>{t("experimentBuilder.summary.repeats")}</dt><dd>{repeats}</dd></div>
          <div><dt>{t("experimentBuilder.summary.timeout")}</dt><dd>{timeoutSeconds}s</dd></div>
          <div><dt>{t("experimentBuilder.summary.notifyTimeout")}</dt><dd>{notifyModelOfTimeout ? t("experimentBuilder.summary.yes") : t("experimentBuilder.summary.no")}</dd></div>
        </dl>
        <div className="builder-cardinality">
          <div><span>{t("experimentBuilder.summary.cells")}</span><strong>{size.cells}</strong></div>
          <div><span>{t("experimentBuilder.summary.totalAttempts")}</span><strong>{size.attempts}</strong></div>
        </div>
        {size.blocking.map((item) =>
          <div className="error" key={item}>{previewMessageLabel(item, lang)}</div>
        )}
        {!preview ? <button
          className="builder-primary-action"
          disabled={!enabled || !multiAgentReady || !sameModelReady || !multiModelReady || busy || size.blocking.length > 0}
          onClick={runPreview}
        >
          {busy ? t("experimentBuilder.action.checking") : t("experimentBuilder.action.check")}
        </button> : <>
          <button
            className="builder-create-action"
            disabled={busy || !title || !question || preview.blocking_warnings.length > 0}
            onClick={create}
          >
            {busy ? t("experimentBuilder.action.creating") : t("experimentBuilder.action.create")}
          </button>
          <button className="builder-recheck-action" disabled={busy} onClick={runPreview}>
            {t("experimentBuilder.action.recheck")}
          </button>
          {(!title || !question) && <div className="warning">{t("experimentBuilder.warning.fillTitleQuestion")}</div>}
        </>}
        {preview && <div className="builder-review">
          <h3>{preview.blocking_warnings.length > 0 ? t("experimentBuilder.review.failed") : t("experimentBuilder.review.passed")}</h3>
          <span>{t("experimentBuilder.review.protocolHash")}</span><code>{preview.protocol_hash}</code>
          <p>{t("experimentBuilder.review.cellsAttempts", { cells: preview.cells, attempts: preview.attempts })}</p>
          {[...preview.blocking_warnings, ...preview.advisory_warnings].map((warning) =>
            <div className="warning" key={warning}>{previewMessageLabel(warning, lang)}</div>
          )}
          {preview.variants.map((item) => <details key={item.id}>
            <summary>
              {mutatorLabel(item.mutator_id, lang)} <code>{item.mutator_id}</code> · {statusLabel(item.status, lang)}
            </summary>
            {item.error_message
              ? <div className="builder-preview-error">
                <strong>{previewMessageLabel(item.error_message, lang)}</strong>
                {item.error_code && <code>{t("experimentBuilder.review.errorCode", { code: item.error_code })}</code>}
              </div>
              : <pre>{item.diff ?? t("experimentBuilder.review.noDiff")}</pre>}
          </details>)}
        </div>}
      </aside>
    </div>
  </div>;
}
