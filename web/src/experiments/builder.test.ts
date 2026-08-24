import { describe, expect, it } from "vitest";

import {
  acceptRecommendation,
  availableMutators,
  cardinality,
  legacyProtocol,
  modelVisiblePrompt,
  sameModelForAgent,
  unsupportedMutators,
} from "./builder";

describe("experiment builder protocol", () => {
  it("only exposes mutators allowed by the selected env and task capabilities", () => {
    const env = {
      supported_mutators: ["baseline", "spacing"],
      conditional_mutators: {
        "instruction-position": { requires: ["structured_instructions"] },
        "letter-case": { requires: ["latin_text"] },
      },
    };
    expect([...availableMutators(env, {})]).toEqual(["baseline", "spacing"]);
    expect([...availableMutators(env, {
      _mutation: { capabilities: ["structured_instructions"] },
    })]).toEqual(["baseline", "spacing", "instruction-position"]);
    expect([...availableMutators(undefined, {})]).toEqual(["baseline"]);
  });

  it("expands four candidates, three variants and two repeats to 24 attempts", () => {
    const draft = legacyProtocol("multi-agent", [
      { agent: "codex", model: "fixture" },
      { agent: "claude-code", model: "fixture" },
      { agent: "blade-agent", model: "fixture-a" },
      { agent: "blade-agent", model: "fixture-b" },
    ]);
    draft.repeats = 2;
    draft.variant_specs.push(
      { ...draft.variant_specs[0], mutator: "whitespace", seed: 1 },
      { ...draft.variant_specs[0], mutator: "instruction-order", seed: 2 },
    );
    expect(cardinality(draft)).toEqual({ cells: 6, attempts: 24, blocking: [] });
  });

  it("does not apply an auto-profile recommendation without explicit acceptance", () => {
    const draft = legacyProtocol("multi-agent", [{ agent: "codex", model: null }]);
    const recommendation = {
      recommendation_id: "rec_fixture",
      profile_id: "standard",
      profile_version: "1.0",
    };
    expect(acceptRecommendation(draft, recommendation, false)).toBe(draft);
    expect(acceptRecommendation(draft, recommendation, true).profile).toEqual({
      id: "standard",
      version: "1.0",
      recommendation_id: "rec_fixture",
    });
  });

  it("explains unsupported mutators before preview", () => {
    const draft = legacyProtocol("multi-agent", [{ agent: "codex", model: null }]);
    draft.variant_specs.push({ ...draft.variant_specs[0], mutator: "forbidden", seed: 2 });
    expect(unsupportedMutators(draft, new Set(["baseline"]))).toEqual(["forbidden"]);
  });

  it("preserves the three legacy mode mappings", () => {
    const defaultProtocol = legacyProtocol(
      "multi-agent",
      [{ agent: "codex", model: null }],
    );
    expect(defaultProtocol.execution).toBe("parallel");
    expect(defaultProtocol.capture_policy).toBe("full");
    expect(legacyProtocol("same-model", [
      { agent: "codex", model: "same" },
      { agent: "claude-code", model: "same" },
    ]).execution).toBe("parallel");
    const multi = legacyProtocol("multi-model", [
      { agent: "codex", model: "or-codex/one" },
      { agent: "codex", model: "or-codex/two" },
    ]);
    expect(cardinality(multi).blocking).toEqual([]);
    multi.agents[1] = { agent: "claude-code", model: "or-cc/two" };
    expect(cardinality(multi).blocking).toContain(
      "multi-model requires one agent and at least two explicit models",
    );
  });

  it("maps one bare model to each agent's deployable model ID", () => {
    const bare = "deepseek/deepseek-v4-flash";
    const prefixes = { "claude-code": "or-cc", codex: "or-codex" };
    const bladeModels = ["upstream/deepseek/deepseek-v4-flash"];
    expect(sameModelForAgent("blade-agent", bare, prefixes, bladeModels))
      .toBe("upstream/deepseek/deepseek-v4-flash");
    expect(sameModelForAgent("claude-code", bare, prefixes, bladeModels))
      .toBe("or-cc/deepseek/deepseek-v4-flash");
    expect(sameModelForAgent("codex", bare, prefixes, bladeModels))
      .toBe("or-codex/deepseek/deepseek-v4-flash");
  });

  it("maps Blade bare models to upstream IDs when its catalog is unavailable", () => {
    expect(sameModelForAgent(
      "blade-agent",
      "deepseek/deepseek-v4-pro",
      {},
      [],
    )).toBe("upstream/deepseek/deepseek-v4-pro");
    expect(sameModelForAgent(
      "blade-agent",
      "upstream/z-ai/glm-5.2",
      {},
      [],
    )).toBe("upstream/z-ai/glm-5.2");
  });

  it("previews the timeout notice only when disclosure is enabled", () => {
    const hidden = modelVisiblePrompt("完成任务", { input: "fixture" }, 1800, false);
    const disclosed = modelVisiblePrompt("完成任务", { input: "fixture" }, 1800, true);
    expect(hidden).not.toContain("本任务限时");
    expect(disclosed).toContain("本任务限时 30 分钟");
    expect(disclosed).toContain("完成任务");
    expect(disclosed).toContain('"input": "fixture"');
  });

  it("hides internal context keys from the task prompt preview", () => {
    const preview = modelVisiblePrompt("完成任务", {
      _mutation: { capabilities: ["structured_instructions"] },
      uploaded_files: [{ name: "brief.pdf", path: "/host/private/brief.pdf" }],
    }, 600, false);
    expect(preview).not.toContain("_mutation");
    expect(preview).not.toContain("/host/private");
    expect(preview).toContain("brief.pdf");
  });
});
