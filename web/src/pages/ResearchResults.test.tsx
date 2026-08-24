import { fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ResearchResults } from "./ResearchResults";

const capabilities = {
  schema_version: "octagon-capabilities-v1",
  features: {
    experiments: true,
    robustness: true,
    attack_coverage: true,
  },
  details: {},
};

const completedGroup = {
  schema_version: "octagon-run-group-snapshot-v1",
  cursor: 12,
  group: {
    id: "grp_fixture",
    experiment_id: "exp_fixture",
    strategy: "full-matrix",
    status: "completed",
    total_cells: 3,
    completed_cells: 3,
    partial_cells: 0,
    failed_cells: 0,
    cancelled_cells: 0,
  },
  cells: [],
  leaders: [],
};

const aggregate = (
  mean: number,
  minimum: number,
  maximum: number,
  statuses: Record<string, number>,
  labels: string[] = [],
) => ({
  sample_count: 2,
  expected_count: 2,
  mean,
  minimum,
  maximum,
  sample_variance: minimum === maximum ? 0 : 648,
  rates: {
    end_to_end_pass_rate: { numerator: 0, denominator: 2, value: 0 },
  },
  status_counts: statuses,
  labels,
});

const robustness = {
  slices: [
    {
      variant_id: "var_base",
      mutator_id: "baseline",
      agent: "blade-agent",
      model: "deepseek/deepseek-v4-flash",
      aggregate: aggregate(36, 36, 36, { gave_up: 2 }),
      labels: ["best", "worst"],
    },
    {
      variant_id: "var_base",
      mutator_id: "baseline",
      agent: "claude-code",
      model: "or-cc/deepseek/deepseek-v4-flash",
      aggregate: aggregate(18, 0, 36, { gave_up: 2 }, ["brittle"]),
      labels: ["brittle"],
    },
    {
      variant_id: "var_base",
      mutator_id: "baseline",
      agent: "codex",
      model: "or-codex/deepseek/deepseek-v4-flash",
      aggregate: aggregate(0, 0, 0, { gave_up: 2 }),
      labels: [],
    },
  ],
};

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("research results overview", () => {
  it("shows an actionable conclusion and omits irrelevant safety sections", async () => {
    const requested: string[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      requested.push(url);
      if (url === "/api/capabilities") return new Response(JSON.stringify(capabilities));
      if (url.endsWith("/groups/grp_fixture")) {
        return new Response(JSON.stringify(completedGroup));
      }
      if (url.includes("/robustness")) return new Response(JSON.stringify(robustness));
      if (url.endsWith("/attack-coverage")) {
        return new Response(JSON.stringify({ slices: [], failures: [] }));
      }
      throw new Error(`unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/experiments/exp_fixture/groups/grp_fixture/results"]}>
        <Routes>
          <Route
            path="/experiments/:experimentId/groups/:groupId/results"
            element={<ResearchResults />}
          />
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByText("本次实验没有候选通过")).toBeInTheDocument();
    expect(screen.getByText(/相对最高的是 blade-agent，平均 36 分/)).toBeInTheDocument();
    expect(screen.getByText("36–36")).toBeInTheDocument();
    expect(screen.getByText("0–36")).toBeInTheDocument();
    expect(screen.getByText("重复结果不一致，且均未通过")).toBeInTheDocument();
    expect(screen.getAllByText("2 次未通过")).toHaveLength(3);
    expect(screen.queryByRole("button", { name: "安全" })).not.toBeInTheDocument();
    expect(screen.queryByText("安全概况")).not.toBeInTheDocument();
    expect(requested.findIndex((url) => url.endsWith("/groups/grp_fixture")))
      .toBeLessThan(requested.findIndex((url) => url.includes("/robustness")));
  });

  it("automatically loads final results when a running group becomes terminal", async () => {
    vi.useFakeTimers();
    let groupRequests = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/api/capabilities") return new Response(JSON.stringify(capabilities));
      if (url.endsWith("/groups/grp_fixture")) {
        groupRequests += 1;
        return new Response(JSON.stringify(groupRequests === 1
          ? { ...completedGroup, group: { ...completedGroup.group, status: "running", completed_cells: 0 } }
          : completedGroup));
      }
      if (url.includes("/robustness")) return new Response(JSON.stringify(robustness));
      if (url.endsWith("/attack-coverage")) {
        return new Response(JSON.stringify({ slices: [], failures: [] }));
      }
      throw new Error(`unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/experiments/exp_fixture/groups/grp_fixture/results"]}>
        <Routes>
          <Route
            path="/experiments/:experimentId/groups/:groupId/results"
            element={<ResearchResults />}
          />
        </Routes>
      </MemoryRouter>,
    );

    await vi.waitFor(() => expect(groupRequests).toBe(1));
    expect(screen.getByText("实验仍在运行")).toBeInTheDocument();
    await vi.advanceTimersByTimeAsync(2000);
    await vi.waitFor(() =>
      expect(screen.getByText("本次实验没有候选通过")).toBeInTheDocument());
    expect(groupRequests).toBe(2);
  });

  it("shows a visible result when the forensic rerun preview is empty", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/api/capabilities") return new Response(JSON.stringify(capabilities));
      if (url.endsWith("/groups/grp_fixture")) {
        return new Response(JSON.stringify(completedGroup));
      }
      if (url.includes("/robustness")) return new Response(JSON.stringify(robustness));
      if (url.endsWith("/attack-coverage") && init?.method !== "POST") {
        return new Response(JSON.stringify({
          slices: [{
            attack_family: "prompt-injection",
            polarity: "attack",
            agent: "codex",
            model: "fixture",
            expected: 1,
            passed: 0,
            failed: 0,
            unsupported: 0,
            error: 1,
          }],
          failures: [],
        }));
      }
      if (url.endsWith("/attack-coverage/rerun-preview") && init?.method === "POST") {
        return new Response(JSON.stringify({ selected_cell_ids: [] }));
      }
      throw new Error(`unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={[
        "/experiments/exp_fixture/groups/grp_fixture/results?tab=safety",
      ]}>
        <Routes>
          <Route
            path="/experiments/:experimentId/groups/:groupId/results"
            element={<ResearchResults />}
          />
        </Routes>
      </MemoryRouter>,
    );

    fireEvent.click(await screen.findByRole(
      "button", { name: "查看失败样本复跑草案" },
    ));
    expect(await screen.findByText("没有需要复跑的失败实验单元。")).toBeInTheDocument();
  });
});
