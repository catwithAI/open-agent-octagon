import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { MemoryRouter } from "react-router-dom";

import { ExperimentList } from "./ExperimentList";

afterEach(() => vi.restoreAllMocks());

describe("ExperimentList", () => {
  it("renders each agent's best score across repeats, and marks unscored agents distinctly", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/api/capabilities") {
        return new Response(JSON.stringify({
          schema_version: "octagon-capabilities-v1",
          features: { experiments: true },
          details: {},
        }));
      }
      if (url === "/api/experiments") {
        return new Response(JSON.stringify({
          items: [{
            id: "exp_fixture",
            title: "Fixture experiment",
            question: "Does it hold up?",
            env_name: "travel-planner",
            status: "partial",
            protocol_hash: "sha256:abc",
            created_at: "2026-07-24T06:50:59Z",
            best_scores: [
              { agent: "claude-code", best_score: 100 },
              { agent: "codex", best_score: 100 },
              { agent: "blade-agent", best_score: null },
            ],
          }],
        }));
      }
      throw new Error(`unexpected request: ${url}`);
    });

    render(<MemoryRouter><ExperimentList /></MemoryRouter>);

    const claudeChip = await screen.findByText("claude-code 100");
    const codexChip = screen.getByText("codex 100");
    const bladeChip = screen.getByText("blade-agent");

    expect(claudeChip).toHaveAttribute("data-status", "completed");
    expect(codexChip).toHaveAttribute("data-status", "completed");
    expect(bladeChip).toHaveAttribute("data-status", "timeout");

    expect(screen.queryByRole("columnheader", { name: "协议" })).not.toBeInTheDocument();
    expect(screen.getByText("最佳分数")).toBeInTheDocument();
  });

  it("shows a dash when an experiment has no agents or scores yet", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/api/capabilities") {
        return new Response(JSON.stringify({
          schema_version: "octagon-capabilities-v1",
          features: { experiments: true },
          details: {},
        }));
      }
      if (url === "/api/experiments") {
        return new Response(JSON.stringify({
          items: [{
            id: "exp_empty",
            title: "Just queued",
            env_name: "travel-planner",
            status: "queued",
            protocol_hash: "sha256:abc",
            created_at: "2026-07-24T06:50:59Z",
            best_scores: [],
          }],
        }));
      }
      throw new Error(`unexpected request: ${url}`);
    });

    render(<MemoryRouter><ExperimentList /></MemoryRouter>);

    await screen.findByText("Just queued");
    // question is omitted (renders "—") and best_scores is empty (also "—") —
    // two dashes are expected on this row, not a bug.
    expect(screen.getAllByText("—")).toHaveLength(2);
  });
});
