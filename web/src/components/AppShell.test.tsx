import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { MemoryRouter } from "react-router-dom";

import { api } from "../api/client";
import { AppShell } from "./AppShell";
import { RunList } from "../pages/RunList";

const disabledCapabilities = {
  schema_version: "octagon-capabilities-v1",
  features: { experiments: false },
  details: {
    experiments: {
      enabled: false,
      schema_ready: false,
      dependencies_ready: true,
      unavailable_reasons: ["schema_missing"],
    },
  },
};

afterEach(() => vi.restoreAllMocks());

describe("compatible AppShell", () => {
  it("keeps every legacy entry and exposes only actionable research navigation", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(disabledCapabilities)),
    );
    render(<MemoryRouter initialEntries={["/experiments"]}><AppShell><div>content</div></AppShell></MemoryRouter>);
    expect(screen.getByRole("link", { name: "多 Agent" })).toHaveAttribute("href", "/");
    expect(screen.getByRole("link", { name: "同模型" })).toHaveAttribute("href", "/same-model");
    expect(screen.getByRole("link", { name: "多模型" })).toHaveAttribute("href", "/multi-model");
    expect(screen.getByRole("link", { name: "运行记录" })).toHaveAttribute("href", "/runs");
    expect(screen.getByRole("link", { name: "总览" })).toHaveAttribute("href", "/overview");
    expect(screen.getByRole("link", { name: "新建实验" })).toHaveAttribute("href", "/experiments/new");
    expect(screen.getByRole("link", { name: "配置模板" })).toHaveAttribute("href", "/profiles");
    expect(screen.queryByRole("link", { name: "系统状态" })).not.toBeInTheDocument();
    expect(screen.getByText("实验列表", { selector: ".shell-crumb" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "跳到主要内容" })).toHaveAttribute("href", "#main-content");
    await waitFor(() => expect(screen.getByText("研究工作区受限")).toBeInTheDocument());
  });

  it("opens and closes the responsive navigation with the keyboard", () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(disabledCapabilities)),
    );
    render(<MemoryRouter><AppShell><div>content</div></AppShell></MemoryRouter>);
    const menu = screen.getByRole("button", { name: "打开导航" });
    fireEvent.click(menu);
    expect(menu).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("complementary")).toHaveClass("is-open");
    fireEvent.keyDown(document, { key: "Escape" });
    expect(menu).toHaveAttribute("aria-expanded", "false");
  });

  it("renders RunList from the batched page without per-row detail requests", async () => {
    vi.spyOn(api, "listRuns").mockResolvedValue({
      total: 1,
      limit: 50,
      offset: 0,
      items: [{
        run_id: "run_fixture",
        task_id: "task_fixture",
        env_name: "fixture",
        run_status: "completed",
        compare_mode: "multi-agent",
        model: null,
        execution: "parallel",
        created_at: new Date().toISOString(),
        attempt_count: 1,
        attempts: [{
          id: "att_fixture",
          agent_name: "codex",
          model: "fixture-model",
          status: "completed",
          score_total: 88,
        }],
      }],
    });
    const getRun = vi.spyOn(api, "getRun");
    render(<MemoryRouter><RunList /></MemoryRouter>);
    expect(await screen.findByText("codex 88")).toBeInTheDocument();
    expect(getRun).not.toHaveBeenCalled();
  });
});
