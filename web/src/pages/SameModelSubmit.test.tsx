// 同模型提交页隐藏采集技术选项，并固定请求 full 采集。
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, waitFor, cleanup, fireEvent } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { MemoryRouter } from "react-router-dom";
import { I18nProvider } from "../i18n";

const { createRunMock } = vi.hoisted(() => ({
  createRunMock: vi.fn(async () => ({ run_id: "r1", task_id: "t", env_name: "e", agents: [], attempts: [] })),
}));

vi.mock("../api/client", async (orig) => {
  const actual = await orig<typeof import("../api/client")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      listEnvs: vi.fn(async () => [{
        name: "travel-planner", skill_id: "octagon/travel-planner", description: "",
        category: "", test_focus: "", pass_threshold: null, dimensions: [],
        tool_count: 0, task_count: 1, available: true,
      }]),
      listEnvTasks: vi.fn(async () => [{
        id: "task_1", env_name: "travel-planner", prompt: "p",
        context: {}, constraints: {}, timeout_seconds: 600,
      }]),
      listAgents: vi.fn(async () => [
        { name: "claude-code", status: "available" as const },
        { name: "codex", status: "available" as const },
        { name: "blade-agent", status: "available" as const },
      ]),
      bladeModels: vi.fn(async () => ({ default: null, models: [] })),
      // agent_prefix 必填：cc/codex 缺前缀会落入 missingPrefix 而禁止提交。
      listModelProviders: vi.fn(async () => ({
        providers: ["up"],
        suggested: ["up/glm"],
        agent_prefix: { "claude-code": "or-cc", codex: "or-codex" },
      })),
      createRun: createRunMock,
    },
  };
});

import { SameModelSubmit } from "./SameModelSubmit";

afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe("SameModelSubmit capture_policy", () => {
  it("隐藏技术选项并默认以 full 进入请求体", async () => {
    render(
      <I18nProvider>
        <MemoryRouter><SameModelSubmit /></MemoryRouter>
      </I18nProvider>,
    );
    expect(screen.queryByText(/通信采集档/)).not.toBeInTheDocument();
    // 填模型 ID（否则 canSubmit=false 无法提交）。页面现有「模型 ID」与「搜索模型」
    // 两个输入，用精确 placeholder 前缀定位模型 ID 输入，避免宽泛正则命中多个。
    const modelInput = screen.getByPlaceholderText(/^(模型 ID|Model ID)/) as HTMLInputElement;
    fireEvent.change(modelInput, { target: { value: "up/glm" } });
    // 等待 env/task/provider-prefix 三个异步目录都加载完成后再提交；只等待静态标题
    // 会在全量并行测试下偶发点击到 disabled 按钮。
    const submit = screen.getByRole("button", { name: /提交|开始|运行|submit|run/i });
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);
    await waitFor(() => expect(createRunMock).toHaveBeenCalled());
    const body = (createRunMock.mock.calls[0] as unknown[])[0] as {
      capture_policy?: string;
      timeout_seconds?: number | null;
    };
    expect(body.capture_policy).toBe("full");
    expect(body.timeout_seconds).toBeNull();
  });
});
