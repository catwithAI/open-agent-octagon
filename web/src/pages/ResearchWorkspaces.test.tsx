import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { MemoryRouter } from "react-router-dom";

import { api } from "../api/client";
import { CompareWorkspace } from "./CompareWorkspace";
import { EvidenceWorkspace } from "./EvidenceWorkspace";

const evidenceIndex = {
  schema_version: "octagon-evidence-index-v1",
  experiment_id: "exp_fixture",
  items: [{
    category: "trajectory",
    source: "trace",
    attempt_id: "att_left",
    run_id: "run_left",
    anchor: "octagon://experiments/exp_fixture/groups/grp/runs/run_left/attempts/att_left?source=trace&record=trace%3A1",
    resolution: { status: "redacted", metadata: { line_index: 0 } },
  }],
  anchors: {},
  total: 1,
  offset: 0,
  limit: 50,
  next_offset: null,
  selection_manifest: {
    selected_records: 1,
    selected_by_category: { trajectory: 1 },
    omitted_by_category: {},
    truncated: false,
  },
  capture_policy: { payload_included: false, allowed_sources: ["trace"] },
};

const resultIndex = {
  ...evidenceIndex,
  items: [
    { category: "result", run_id: "run_left", attempt_id: "att_left", agent: "codex", model: "fixture-a", status: "completed", score_total: 90, variant_id: "var_base", mutator_id: "baseline", repeat_index: 0, final_result: { state: "captured" } },
    { category: "result", run_id: "run_right", attempt_id: "att_right", agent: "claude-code", model: "fixture-b", status: "completed", score_total: 80, variant_id: "var_base", mutator_id: "baseline", repeat_index: 1, final_result: { state: "captured" } },
  ],
  total: 2,
};

const capabilities = {
  schema_version: "octagon-capabilities-v1",
  features: { experiments: true, normalized_output: false },
  details: {},
};

afterEach(() => vi.restoreAllMocks());

describe("research result workspaces", () => {
  it("paginates metadata-only evidence and exposes the selected Anchor", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      new Response(JSON.stringify(evidenceIndex)));
    render(<EvidenceWorkspace experimentId="exp_fixture" />);

    expect(await screen.findByText("轨迹 · trace")).toBeInTheDocument();
    expect(screen.getByText("仅元数据 · payload 未包含")).toBeInTheDocument();
    expect(screen.getAllByText("Evidence Anchor").length).toBeGreaterThan(0);
    expect(screen.getByText(/octagon:\/\/experiments/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "下一页" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: /Trace/ }));
    await waitFor(() => expect(globalThis.fetch).toHaveBeenLastCalledWith(
      expect.stringContaining("source=trace"),
    ));
  });

  it("selects anchor-less records (aggregate/result) in the inspector on click", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      new Response(JSON.stringify(resultIndex)));
    render(<EvidenceWorkspace experimentId="exp_fixture" />);

    // 第一条无 anchor 记录默认选中，检查器展示其字段而不是"未选择"。
    // （att_left 会同时出现在时间线行和检查器里，所以断言 agent 字段。）
    expect(await screen.findByText("codex")).toBeInTheDocument();
    expect(screen.queryByText("从时间线选择一条记录。")).not.toBeInTheDocument();

    // 点击第二条无 anchor 记录后，检查器切换到该记录，并给出运行详情入口。
    fireEvent.click(screen.getByRole("button", { name: /att_right/ }));
    await waitFor(() => expect(screen.getByText("claude-code")).toBeInTheDocument());
    expect(screen.getByText("fixture-b")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /打开运行详情/ })).toHaveAttribute(
      "href", "/runs/run_right?attempt=att_right");
    // ID 坐标收进折叠区，摘要区不直接平铺。
    expect(screen.getByText("血缘坐标（实验矩阵定位 ID）")).toBeInTheDocument();
    // 页面自带概念说明。
    expect(screen.getByText("这个页面是什么？（概念说明）")).toBeInTheDocument();
  });

  it("resolves an anchor inline instead of navigating to raw API JSON", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("/evidence/resolve")) {
        return new Response(JSON.stringify({ status: "redacted", metadata: { line_index: 0 } }));
      }
      return new Response(JSON.stringify(evidenceIndex));
    });
    render(<EvidenceWorkspace experimentId="exp_fixture" />);

    fireEvent.click(await screen.findByRole("button", { name: "解析记录" }));
    expect(await screen.findByText(/"line_index": 0/)).toBeInTheDocument();
    expect(vi.mocked(globalThis.fetch).mock.calls.some(([input]) =>
      String(input).includes("/evidence/resolve?anchor="))).toBe(true);
  });

  it("keeps Raw authoritative by default and does not request normalized output", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("/capabilities")) return new Response(JSON.stringify(capabilities));
      if (url.includes("/evidence")) return new Response(JSON.stringify(resultIndex));
      throw new Error(`unexpected request: ${url}`);
    });
    vi.spyOn(api, "getAttempt").mockImplementation(async (_runId, attemptId) => ({
      id: attemptId,
      agent_name: attemptId === "att_left" ? "codex" : "claude-code",
      status: "completed",
      transport_status: "complete",
      score_total: attemptId === "att_left" ? 90 : 80,
      thinking_count: 0,
      tool_call_count: 0,
      token_usage: {},
      cost_estimate: null,
      duration_ms: 1,
      scores: [],
      tool_calls: [],
      events: [],
      final_state: { answer: attemptId },
      progress: {},
      external_refs: {},
      error_code: null,
      error_message: null,
    }));

    render(<MemoryRouter><CompareWorkspace experimentId="exp_fixture" /></MemoryRouter>);
    expect(await screen.findAllByText("权威来源")).toHaveLength(2);
    expect(screen.getByRole("tab", { name: "Raw · 权威原始数据" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Normalized · 派生数据" })).toBeDisabled();
    expect(screen.getAllByText(/att_left|att_right/).length).toBeGreaterThan(0);
    expect(document.querySelector('optgroup[label="原始对照 · 第 1 次"]')).toBeInTheDocument();
    expect(document.querySelector('optgroup[label="原始对照 · 第 2 次"]')).toBeInTheDocument();
    expect(api.getAttempt).toHaveBeenCalledWith("run_left", "att_left", false);
    expect(api.getAttempt).toHaveBeenCalledWith("run_right", "att_right", false);
    expect(vi.mocked(globalThis.fetch).mock.calls.some(([input]) =>
      String(input).includes("/normalized"))).toBe(false);
  });

  it("does not render empty compare columns when attempts have no final output", async () => {
    const failedIndex = {
      ...resultIndex,
      items: [{
        category: "result",
        run_id: "run_failed",
        attempt_id: "att_failed",
        agent: "claude-code",
        model: "fixture-model",
        status: "gave_up",
        score_total: 0,
        variant_id: "var_base",
        mutator_id: "baseline",
        repeat_index: 1,
        final_result: { state: "missing" },
      }],
      total: 1,
    };
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("/capabilities")) return new Response(JSON.stringify(capabilities));
      if (url.includes("/evidence")) return new Response(JSON.stringify(failedIndex));
      throw new Error(`unexpected request: ${url}`);
    });
    vi.spyOn(api, "getAttempt").mockResolvedValue({
      id: "att_failed",
      agent_name: "claude-code",
      status: "gave_up",
      final_state: {},
    } as never);

    render(<MemoryRouter><CompareWorkspace experimentId="exp_fixture" /></MemoryRouter>);

    expect(await screen.findByText("本次实验没有候选提交最终输出，无法进行输出对比")).toBeInTheDocument();
    expect(screen.getByText(/查看运行证据/)).toBeInTheDocument();
    expect(screen.queryByText("{}")).not.toBeInTheDocument();
    expect(api.getAttempt).not.toHaveBeenCalled();
  });
});
