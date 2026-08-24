import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { MemoryRouter } from "react-router-dom";

import { api } from "../api/client";
import { ExperimentBuilder } from "./ExperimentBuilder";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("ExperimentBuilder create flow", () => {
  it("only renders variants allowed by the selected env and task", async () => {
    vi.spyOn(api, "listEnvs").mockResolvedValue([{
      name: "fixture-env",
      supported_mutators: ["baseline", "letter-case"],
      conditional_mutators: {
        "instruction-position": { requires: ["structured_instructions"] },
      },
    }] as never);
    vi.spyOn(api, "listEnvTasks").mockResolvedValue([{
      id: "fixture-task",
      context: { _mutation: { capabilities: ["structured_instructions"] } },
    }] as never);
    vi.spyOn(api, "getEnvMeta").mockResolvedValue({
      name: "fixture-env", meta: {}, meta_yaml: "name: fixture-env",
    });
    vi.spyOn(api, "listAgents").mockResolvedValue([] as never);
    vi.spyOn(api, "openrouterModels").mockResolvedValue({ models: [] } as never);
    vi.spyOn(api, "bladeModels").mockResolvedValue({ models: [] } as never);
    vi.spyOn(api, "listModelProviders").mockResolvedValue({ agent_prefix: {} } as never);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      schema_version: "octagon-capabilities-v1",
      features: { experiments: true, task_variants: true, run_groups: true },
      details: {},
    })));

    render(<MemoryRouter><ExperimentBuilder /></MemoryRouter>);
    await screen.findByDisplayValue("fixture-env");
    await screen.findByDisplayValue("fixture-task");

    expect(screen.getByText("字母大小写")).toBeInTheDocument();
    expect(screen.getByText("指令位置")).toBeInTheDocument();
    expect(screen.queryByText("空格扰动")).not.toBeInTheDocument();
    expect(screen.queryByText("相似字符替换")).not.toBeInTheDocument();
  });

  it("posts the experiment after preview on an insecure HTTP browser context", async () => {
    vi.spyOn(api, "listEnvs").mockResolvedValue([{ name: "fixture-env" }] as never);
    vi.spyOn(api, "listEnvTasks").mockResolvedValue([{
      id: "fixture-task",
      prompt: "Fixture prompt",
      timeout_seconds: 1800,
    }] as never);
    vi.spyOn(api, "getEnvMeta").mockResolvedValue({
      name: "fixture-env",
      meta: {},
      meta_yaml: "name: fixture-env\ndescription: Fixture",
    });
    vi.spyOn(api, "listAgents").mockResolvedValue([
      { name: "blade-agent", status: "available" },
      { name: "claude-code", status: "available" },
      { name: "codex", status: "available" },
    ] as never);
    vi.spyOn(api, "openrouterModels").mockResolvedValue({
      models: [{ id: "vendor/model-b", name: "Model B" }],
    } as never);
    vi.spyOn(api, "bladeModels").mockResolvedValue({
      models: [{ id: "upstream/vendor/model-a", label: "Blade A" }],
    } as never);
    vi.spyOn(api, "listModelProviders").mockResolvedValue({
      agent_prefix: { "claude-code": "or-cc", codex: "or-codex" },
    } as never);
    vi.stubGlobal("crypto", {
      getRandomValues: (bytes: Uint8Array) => {
        bytes.fill(0xab);
        return bytes;
      },
    });

    const requests: Array<{ url: string; init?: RequestInit }> = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      requests.push({ url, init });
      if (url === "/api/capabilities") {
        return new Response(JSON.stringify({
          schema_version: "octagon-capabilities-v1",
          features: { experiments: true, task_variants: true, run_groups: true },
          details: {},
        }));
      }
      if (url === "/api/experiments/preview") {
        return new Response(JSON.stringify({
          schema_version: "octagon-variant-preview-v1",
          source_hash: "sha256:source",
          protocol_hash: "sha256:protocol",
          preview_token: "sha256:protocol",
          cells: 1,
          attempts: 2,
          variants: [{
            id: "var_fixture",
            mutator_id: "baseline",
            mutator_version: "1",
            status: "ready",
            diff: null,
            warnings: [],
            error_code: null,
            error_message: null,
          }],
          blocking_warnings: [],
          advisory_warnings: [],
        }));
      }
      if (url === "/api/experiments") {
        return new Response(JSON.stringify({
          experiment_id: "exp_fixture",
          run_group_id: "grp_fixture",
          variant_ids: ["var_fixture"],
          cell_ids: ["cell_fixture"],
          replayed: false,
          execution_scheduled: true,
        }));
      }
      throw new Error(`unexpected request: ${url}`);
    });

    render(<MemoryRouter><ExperimentBuilder /></MemoryRouter>);
    await screen.findByDisplayValue("fixture-env");
    expect(await screen.findByLabelText("场景 meta.yaml")).toHaveTextContent(
      "description: Fixture",
    );
    expect(screen.getByLabelText("模型可见任务提示")).not.toHaveTextContent("本任务限时");
    const timeoutDisclosure = screen.getByRole("checkbox", {
      name: /告知模型超时机制/,
    });
    fireEvent.click(timeoutDisclosure);
    expect(screen.getByLabelText("模型可见任务提示"))
      .toHaveTextContent("本任务限时 30 分钟");
    fireEvent.click(timeoutDisclosure);
    fireEvent.change(screen.getByPlaceholderText("例如：指令鲁棒性深跑"), {
      target: { value: "Fixture experiment" },
    });
    fireEvent.change(screen.getByPlaceholderText("不同 Agent 在确定性变体下表现是否稳定？"), {
      target: { value: "Is it stable?" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "搜索 blade-agent 模型" }), {
      target: { value: "Blade A" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Blade A/ }));
    fireEvent.change(screen.getByRole("textbox", { name: "搜索 claude-code 模型" }), {
      target: { value: "Model B" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Model B/ }));

    fireEvent.click(screen.getByRole("button", { name: "检查配置" }));
    const start = await screen.findByRole("button", { name: "创建并开始实验" });
    fireEvent.click(start);

    await waitFor(() => expect(requests.some((request) =>
      request.url === "/api/experiments" && request.init?.method === "POST")).toBe(true));
    const createRequest = requests.find((request) => request.url === "/api/experiments");
    expect(new Headers(createRequest?.init?.headers).get("Idempotency-Key"))
      .toBe("abababab-abab-4bab-abab-abababababab");

    // Regression: the builder must default to the selected task's own
    // timeout_seconds (there is no standalone UI entry to set it otherwise
    // than by picking a task, and it must reach both preview and create).
    const previewRequest = requests.find((request) => request.url === "/api/experiments/preview");
    expect(JSON.parse(String(previewRequest?.init?.body))).toMatchObject({ timeout_seconds: 1800 });
    expect(JSON.parse(String(createRequest?.init?.body))).toMatchObject({ timeout_seconds: 1800 });
    expect(JSON.parse(String(createRequest?.init?.body))).toMatchObject({
      protocol: { notify_model_of_timeout: false },
    });
  });

  it("allows overriding Blade's final upstream model ID in same-model mode", async () => {
    vi.spyOn(api, "listEnvs").mockResolvedValue([{ name: "fixture-env" }] as never);
    vi.spyOn(api, "listEnvTasks").mockResolvedValue([{ id: "fixture-task" }] as never);
    vi.spyOn(api, "getEnvMeta").mockResolvedValue({
      name: "fixture-env", meta: {}, meta_yaml: "name: fixture-env",
    });
    vi.spyOn(api, "listAgents").mockResolvedValue([
      { name: "blade-agent", status: "available" },
      { name: "claude-code", status: "available" },
    ] as never);
    vi.spyOn(api, "openrouterModels").mockResolvedValue({
      models: [{ id: "vendor/model-a", name: "Model A" }],
    } as never);
    vi.spyOn(api, "bladeModels").mockResolvedValue({
      models: [], error: "401 Unauthorized",
    } as never);
    vi.spyOn(api, "listModelProviders").mockResolvedValue({
      agent_prefix: { "claude-code": "or-cc" },
    } as never);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      schema_version: "octagon-capabilities-v1",
      features: { experiments: true, task_variants: true, run_groups: true },
      details: {},
    })));

    render(<MemoryRouter><ExperimentBuilder /></MemoryRouter>);
    await screen.findByDisplayValue("fixture-env");
    fireEvent.click(screen.getByRole("button", { name: /^同模型多个 Agent/ }));
    const search = screen.getByPlaceholderText("输入厂商或模型名称搜索");
    fireEvent.change(search, { target: { value: "Model A" } });
    fireEvent.click(screen.getByRole("button", { name: /Model A/ }));

    const override = screen.getByRole("textbox", { name: "Blade 最终模型 ID" });
    expect(override).toHaveValue("upstream/vendor/model-a");
    expect(screen.getByText(/模型目录暂不可用/)).toBeInTheDocument();

    fireEvent.change(override, {
      target: { value: "upstream/vendor/model-a-custom" },
    });
    expect(screen.getByText(
      "blade-agent → upstream/vendor/model-a-custom",
    )).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "恢复自动映射" }));
    expect(override).toHaveValue("upstream/vendor/model-a");
  });

  it("selects one agent and multiple searchable models without a text protocol", async () => {
    vi.spyOn(api, "listEnvs").mockResolvedValue([{ name: "fixture-env" }] as never);
    vi.spyOn(api, "listEnvTasks").mockResolvedValue([{ id: "fixture-task" }] as never);
    vi.spyOn(api, "getEnvMeta").mockResolvedValue({
      name: "fixture-env", meta: {}, meta_yaml: "name: fixture-env",
    });
    vi.spyOn(api, "listAgents").mockResolvedValue([
      { name: "blade-agent", status: "available" },
      { name: "claude-code", status: "available" },
      { name: "codex", status: "available" },
    ] as never);
    vi.spyOn(api, "openrouterModels").mockResolvedValue({
      models: [
        { id: "vendor/model-a", name: "Model A" },
        { id: "vendor/model-b", name: "Model B" },
      ],
    } as never);
    vi.spyOn(api, "bladeModels").mockResolvedValue({ models: [] } as never);
    vi.spyOn(api, "listModelProviders").mockResolvedValue({
      agent_prefix: { codex: "or-codex", "claude-code": "or-cc" },
    } as never);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      schema_version: "octagon-capabilities-v1",
      features: { experiments: true, task_variants: true, run_groups: true },
      details: {},
    })));

    render(<MemoryRouter><ExperimentBuilder /></MemoryRouter>);
    await screen.findByDisplayValue("fixture-env");
    fireEvent.click(screen.getByRole("button", { name: /多模型/ }));
    fireEvent.click(screen.getByRole("button", { name: /codex/i }));

    const search = screen.getByRole("textbox", { name: "搜索候选模型" });
    fireEvent.change(search, { target: { value: "model" } });
    fireEvent.click(screen.getByRole("button", { name: /Model A/ }));
    fireEvent.change(search, { target: { value: "model" } });
    fireEvent.click(screen.getByRole("button", { name: /Model B/ }));

    const selected = screen.getByLabelText("已选候选模型");
    expect(selected).toHaveTextContent("or-codex/vendor/model-a");
    expect(selected).toHaveTextContent("or-codex/vendor/model-b");
    expect(screen.queryByText("每行 Agent:模型 ID")).not.toBeInTheDocument();
  });

  it("keeps exactly one independently searched model for each agent", async () => {
    vi.spyOn(api, "listEnvs").mockResolvedValue([{ name: "fixture-env" }] as never);
    vi.spyOn(api, "listEnvTasks").mockResolvedValue([{ id: "fixture-task" }] as never);
    vi.spyOn(api, "getEnvMeta").mockResolvedValue({
      name: "fixture-env", meta: {}, meta_yaml: "name: fixture-env",
    });
    vi.spyOn(api, "listAgents").mockResolvedValue([
      { name: "blade-agent", status: "available" },
      { name: "claude-code", status: "available" },
      { name: "codex", status: "available" },
    ] as never);
    vi.spyOn(api, "openrouterModels").mockResolvedValue({
      models: [
        { id: "vendor/model-a", name: "Model A" },
        { id: "vendor/model-b", name: "Model B" },
      ],
    } as never);
    vi.spyOn(api, "bladeModels").mockResolvedValue({
      models: [{ id: "upstream/vendor/blade-model", label: "Blade Model" }],
    } as never);
    vi.spyOn(api, "listModelProviders").mockResolvedValue({
      agent_prefix: { codex: "or-codex", "claude-code": "or-cc" },
    } as never);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      schema_version: "octagon-capabilities-v1",
      features: { experiments: true, task_variants: true, run_groups: true },
      details: {},
    })));

    render(<MemoryRouter><ExperimentBuilder /></MemoryRouter>);
    await screen.findByDisplayValue("fixture-env");

    const claudeSearch = screen.getByRole("textbox", { name: "搜索 claude-code 模型" });
    fireEvent.change(claudeSearch, { target: { value: "Model A" } });
    fireEvent.click(screen.getByRole("button", { name: /Model A/ }));
    expect(screen.getByText("or-cc/vendor/model-a")).toBeInTheDocument();

    fireEvent.change(claudeSearch, { target: { value: "Model B" } });
    fireEvent.click(screen.getByRole("button", { name: /Model B/ }));

    expect(screen.getByText("or-cc/vendor/model-b")).toBeInTheDocument();
    expect(screen.queryByText("or-cc/vendor/model-a")).not.toBeInTheDocument();
    expect(screen.getAllByText("请选择该 Agent 使用的模型")).toHaveLength(1);
  });
});
