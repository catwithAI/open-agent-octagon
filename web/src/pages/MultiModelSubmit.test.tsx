import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { MemoryRouter } from "react-router-dom";

vi.mock("../api/client", async (orig) => {
  const actual = await orig<typeof import("../api/client")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      listEnvs: vi.fn(async () => [{
        name: "demo", skill_id: "demo", description: "", category: "", test_focus: "",
        pass_threshold: null, dimensions: [], tool_count: 0, task_count: 1, available: true,
      }]),
      listEnvTasks: vi.fn(async () => [{
        id: "task_1", env_name: "demo", prompt: "p", context: {}, constraints: {},
        timeout_seconds: 600,
      }]),
      bladeModels: vi.fn(async () => ({
        default: "openai/gpt-5",
        models: [
          { id: "openai/gpt-5", label: "GPT 5" },
          { id: "anthropic/claude-opus", label: "Claude Opus" },
          { id: "qwen/qwen3", label: "Qwen 3" },
        ],
      })),
    },
  };
});

import { MultiModelSubmit } from "./MultiModelSubmit";

afterEach(() => cleanup());

describe("MultiModelSubmit model search", () => {
  it("只展示搜索结果，并可从搜索框下方取消已选模型", async () => {
    render(<MemoryRouter><MultiModelSubmit /></MemoryRouter>);

    const search = await screen.findByRole("textbox", { name: "搜索上游模型" });
    expect(search).toHaveAttribute("placeholder", expect.stringContaining("共 3 个"));
    expect(screen.queryByText("Claude Opus")).not.toBeInTheDocument();

    fireEvent.change(search, { target: { value: "claude" } });
    fireEvent.click(screen.getByRole("button", { name: /Claude Opus/ }));

    expect(search).toHaveValue("");
    const remove = screen.getByRole("button", { name: "取消选择 anthropic/claude-opus" });
    expect(remove).toBeInTheDocument();
    fireEvent.click(remove);
    await waitFor(() => expect(remove).not.toBeInTheDocument());
  });
});
