import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ExperimentInsights } from "./ExperimentInsights";

const capabilities = {
  schema_version: "octagon-capabilities-v1",
  features: { insights: true, research_feedback: true },
  details: {},
};

const detail = {
  id: "insight_fixture",
  version: 1,
  status: "ready",
  producer_version: "octagon-insight-v1",
  report: {
    sections: {
      conclusion: [{
        id: "statement_1",
        kind: "evidence-backed",
        text: "Blade Agent 的重复结果一致，但没有达到通过标准。",
        anchors: ["octagon://experiments/exp_fixture/result/1"],
      }],
    },
    limitations: ["本实验只有两次重复运行。"],
  },
};

afterEach(() => vi.restoreAllMocks());

describe("experiment insights", () => {
  it("generates the first insight from an empty workspace and loads its report", async () => {
    let generated = false;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/api/capabilities") return new Response(JSON.stringify(capabilities));
      if (url.endsWith("/insights?limit=20")) {
        return new Response(JSON.stringify({
          items: generated ? [{ version: 1, status: "ready" }] : [],
        }));
      }
      if (url.endsWith("/insights/generate") && init?.method === "POST") {
        generated = true;
        return new Response(JSON.stringify({ version: 1, status: "ready" }));
      }
      if (url.endsWith("/insights/1")) return new Response(JSON.stringify(detail));
      throw new Error(`unexpected request: ${url}`);
    });

    render(<MemoryRouter><ExperimentInsights experimentId="exp_fixture" embedded /></MemoryRouter>);

    expect(await screen.findByText(/当前实验尚未生成洞察/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "生成研究洞察" }));

    expect(await screen.findByText(/Blade Agent 的重复结果一致/)).toBeInTheDocument();
    expect(screen.getByText("本实验只有两次重复运行。")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("洞察 v1 已生成");
    expect(screen.getByRole("button", { name: "重新生成洞察" })).toBeInTheDocument();
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalledWith(
      "/api/experiments/exp_fixture/insights/generate",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({ "Idempotency-Key": expect.any(String) }),
      }),
    ));
  });

  it("blocks generation and explains eligibility while run groups are active", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/api/capabilities") return new Response(JSON.stringify(capabilities));
      if (url === "/api/experiments/exp_fixture") {
        return new Response(JSON.stringify({ experiment: { status: "running" } }));
      }
      if (url.endsWith("/groups")) {
        return new Response(JSON.stringify({ items: [{ group: { id: "grp_live", status: "running" } }] }));
      }
      if (url.endsWith("/insights?limit=20")) return new Response(JSON.stringify({ items: [] }));
      throw new Error(`unexpected request: ${url}`);
    });

    render(<MemoryRouter><ExperimentInsights experimentId="exp_fixture" embedded /></MemoryRouter>);

    expect(await screen.findByText("当前还不能生成")).toBeInTheDocument();
    expect(screen.getByText(/仍在进行的运行组/)).toBeInTheDocument();
    expect(screen.getByText(/grp_live/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "等待运行结束" })).toBeDisabled();
  });

  it("marks a terminal experiment as eligible for insight generation", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/api/capabilities") return new Response(JSON.stringify(capabilities));
      if (url === "/api/experiments/exp_fixture") {
        return new Response(JSON.stringify({ experiment: { status: "partial" } }));
      }
      if (url.endsWith("/groups")) {
        return new Response(JSON.stringify({
          items: [{ group: { id: "grp_done", status: "completed" } }, { group: { id: "grp_part", status: "partial" } }],
        }));
      }
      if (url.endsWith("/insights?limit=20")) return new Response(JSON.stringify({ items: [] }));
      throw new Error(`unexpected request: ${url}`);
    });

    render(<MemoryRouter><ExperimentInsights experimentId="exp_fixture" embedded /></MemoryRouter>);

    expect(await screen.findByText("本实验可以生成洞察")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "生成研究洞察" })).toBeEnabled();
  });

  it("renders a failed insight version without crashing and offers regeneration", async () => {
    const failedDetail = {
      id: "insight_failed",
      version: 1,
      status: "failed",
      producer_version: "octagon-insight-generator-v1",
      report: {
        error_code: "model_output_invalid",
        error_message: "model output did not satisfy the Insight JSON schema (ValidationError)",
        schema_version: "octagon-insight-failure-v1",
      },
    };
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/api/capabilities") return new Response(JSON.stringify(capabilities));
      if (url.endsWith("/insights?limit=20")) {
        return new Response(JSON.stringify({ items: [{ version: 1, status: "failed" }] }));
      }
      if (url.endsWith("/insights/1")) return new Response(JSON.stringify(failedDetail));
      throw new Error(`unexpected request: ${url}`);
    });

    render(<MemoryRouter><ExperimentInsights experimentId="exp_fixture" embedded /></MemoryRouter>);

    expect(await screen.findByText("本版本生成失败")).toBeInTheDocument();
    expect(screen.getByText("model_output_invalid")).toBeInTheDocument();
    expect(screen.getByText(/model output did not satisfy/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新生成洞察" })).toBeInTheDocument();
    expect(screen.queryByText("局限性")).not.toBeInTheDocument();
  });
});
