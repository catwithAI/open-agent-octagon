import { describe, expect, it } from "vitest";

import { leaderReasonLabel, mutatorLabel, previewMessageLabel } from "./researchUi";

describe("research preview localization", () => {
  it("explains an env-contract rejection in Chinese while retaining the mutator id", () => {
    expect(previewMessageLabel("mutator forbidden by env contract: spacing"))
      .toBe("场景契约未允许使用变体：空格扰动（spacing）");
    expect(mutatorLabel("instruction-position")).toBe("指令位置");
  });

  it("keeps the original backend message for the English locale", () => {
    const message = "mutator forbidden by env contract: spacing";
    expect(previewMessageLabel(message, "en")).toBe(message);
    expect(mutatorLabel("spacing", "en")).toBe("Spacing");
  });

  it("localizes known applicability reasons", () => {
    expect(previewMessageLabel("instruction-position: structured instruction_blocks required"))
      .toBe("指令位置：任务没有声明结构化 instruction_blocks");
    expect(previewMessageLabel("attempt limit exceeded")).toBe("总执行次数超过上限");
  });

  it("uses user-facing labels for leader events", () => {
    expect(leaderReasonLabel("scope-finalized")).toBe("本轮优胜候选已确定");
    expect(leaderReasonLabel("unknown-reason")).toBe("优胜候选已更新");
  });
});
