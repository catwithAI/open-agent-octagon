// 验收：run 级成本卡片的五种状态呈现。
//
// 核心不变式：**settling 绝不出现 $0**、**upper_bound 必须标不可审计**、
// **failed 不回落为估算真值**。这三条一旦破了，读者会把未结算或含背景流量的
// 数字当成这次 run 的实际花费。
import { describe, it, expect, afterEach, vi } from "vitest";
import { render, screen, cleanup, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import type { RunCost } from "../api/client";
import { api } from "../api/client";
import { RunCostCard } from "./RunDetail";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function cost(over: Partial<RunCost> = {}): RunCost {
  return {
    run_id: "run1",
    provider: "openrouter",
    api_key_hash: "hash-run",
    attribution_mode: "ephemeral_run_key",
    status: "final",
    auditable: true,
    upstream: {
      execution_cost_usd: 0.8421,
      scoring_cost_usd: 0.1932,
      total_cost_usd: 1.0353,
      key_limit_usd: 5.0,
      usage_start: 0,
      usage_after_execution: 0.8421,
      usage_final: 1.0353,
    },
    estimate: {
      total_cost_usd: 0.9612,
      priced: true,
      attempts_missing_cost: 0,
      attempt_count: 3,
    },
    divergence_ratio: 0.0716,
    derived: {
      session_count: 3,
      avg_cost_per_session_usd: 0.3451,
      avg_cost_per_session_is_amortized: true,
      scoring_cost_ratio: 0.1866,
      budget_consumed_ratio: 0.2071,
      by_model: null,
      by_provider: null,
      requests: null,
      avg_cost_per_request_usd: null,
    },
    error_code: null,
    ...over,
  };
}

function mount(payload: RunCost) {
  vi.spyOn(api, "getRunCost").mockResolvedValue(payload);
  render(<RunCostCard runId="run1" />);
}

describe("RunCostCard", () => {
  it("final 展示上游实扣为主值，本地估算为对照", async () => {
    mount(cost());
    await waitFor(() => expect(screen.getByText("已审计")).toBeInTheDocument());
    expect(screen.getByText("$1.0353")).toBeInTheDocument();
    expect(screen.getByText("本地估算（对照）")).toBeInTheDocument();
    expect(screen.getByText("$0.9612")).toBeInTheDocument();
    expect(screen.getByText(/差异 7\.2%/)).toBeInTheDocument();
  });

  it("final 展示执行/评分拆分与预算消耗", async () => {
    mount(cost());
    await waitFor(() => expect(screen.getByText("执行 / 评分")).toBeInTheDocument());
    expect(screen.getByText("$0.8421 / $0.1932")).toBeInTheDocument();
    expect(screen.getByText(/评分占 18\.7%/)).toBeInTheDocument();
    expect(screen.getByText(/20\.7%/)).toBeInTheDocument();
  });

  it("均摊成本必须标注为均摊，不是逐 session 实耗", async () => {
    mount(cost());
    await waitFor(() =>
      expect(screen.getByText(/均摊值，非逐 session 实耗/)).toBeInTheDocument(),
    );
  });

  it("逐 attempt 展示 OpenRouter 实际计费模型和费用", async () => {
    mount(
      cost({
        granularity: "per_attempt",
        by_attempt: [
          {
            attempt_id: "att-codex",
            agent_name: "codex",
            cost_usd: 0.9817,
            status: "final",
            auditable: true,
            error_code: null,
            activity: {
              usage_usd: 0.9441,
              requests: 9,
              prompt_tokens: 216_415,
              completion_tokens: 50_544,
              reasoning_tokens: 3_343,
              by_model: [
                {
                  model: "openai/gpt-5.4",
                  usage_usd: 0.9441,
                  requests: 9,
                  prompt_tokens: 216_415,
                  completion_tokens: 50_544,
                  reasoning_tokens: 3_343,
                  providers: ["openai"],
                  endpoints: ["responses"],
                },
              ],
              by_provider: [],
            },
          },
        ],
      }),
    );
    await waitFor(() =>
      expect(screen.getByText("openai/gpt-5.4")).toBeInTheDocument(),
    );
    expect(screen.getByText(/\$0\.9441 · 9 请求/)).toBeInTheDocument();
  });

  it("settling 不展示临时 0", async () => {
    mount(
      cost({
        status: "settling",
        auditable: false,
        upstream: {
          execution_cost_usd: null,
          scoring_cost_usd: null,
          total_cost_usd: null,
          key_limit_usd: 5.0,
          usage_start: 0,
          usage_after_execution: null,
          usage_final: null,
        },
        divergence_ratio: null,
      }),
    );
    await waitFor(() => expect(screen.getByText("等待上游结算")).toBeInTheDocument());
    // 关键断言：整张卡里不能出现看起来像"已结算 0 元"的数字
    expect(screen.queryByText("$0.0000")).not.toBeInTheDocument();
    expect(screen.queryByText("$0.00000")).not.toBeInTheDocument();
  });

  it("pending 显示成本统计中且不给金额", async () => {
    mount(
      cost({
        status: "pending",
        auditable: false,
        upstream: {
          execution_cost_usd: null,
          scoring_cost_usd: null,
          total_cost_usd: null,
          key_limit_usd: null,
          usage_start: 0,
          usage_after_execution: null,
          usage_final: null,
        },
        divergence_ratio: null,
      }),
    );
    await waitFor(() => expect(screen.getByText("成本统计中")).toBeInTheDocument());
    expect(screen.queryByText("执行 / 评分")).not.toBeInTheDocument();
  });

  it("upper_bound 明确标记不可审计", async () => {
    mount(
      cost({
        status: "upper_bound",
        auditable: false,
        attribution_mode: "shared_key_upper_bound",
      }),
    );
    await waitFor(() =>
      expect(screen.getByText(/上界 · 含背景流量 · 不可审计/)).toBeInTheDocument(),
    );
    expect(screen.getByText(/不可用于资金归因/)).toBeInTheDocument();
  });

  it("failed 不回落为估算真值", async () => {
    mount(
      cost({
        status: "failed",
        auditable: false,
        error_code: "usage_regression",
        upstream: {
          execution_cost_usd: null,
          scoring_cost_usd: null,
          total_cost_usd: null,
          key_limit_usd: null,
          usage_start: 5.0,
          usage_after_execution: null,
          usage_final: null,
        },
        divergence_ratio: null,
      }),
    );
    await waitFor(() => expect(screen.getByText("结算失败")).toBeInTheDocument());
    expect(screen.getByText(/不回落为本地估算/)).toBeInTheDocument();
    expect(screen.getByText(/usage_regression/)).toBeInTheDocument();
    // 估算值仍可作为对照展示，但主值位置不能拿它顶替
    expect(screen.getByText("$0.9612")).toBeInTheDocument();
  });

  it("估算是下界时保留 ≥ 前缀", async () => {
    mount(cost({ estimate: { ...cost().estimate, priced: false } }));
    await waitFor(() => expect(screen.getByText("≥$0.9612")).toBeInTheDocument());
    // 上游实扣行不加 ≥——那是估算特有的语义
    expect(screen.getByText("$1.0353")).toBeInTheDocument();
  });

  it("提示估算链路缺口", async () => {
    mount(
      cost({
        estimate: { ...cost().estimate, attempts_missing_cost: 2 },
      }),
    );
    await waitFor(() =>
      expect(screen.getByText(/2 个 attempt 有令牌但算不出估算成本/)).toBeInTheDocument(),
    );
  });

  it("未启用上游成本核算且无估算时不渲染空面板", async () => {
    mount(
      cost({
        status: "unavailable",
        auditable: false,
        upstream: null,
        estimate: {
          total_cost_usd: null,
          priced: null,
          attempts_missing_cost: 0,
          attempt_count: 0,
        },
        divergence_ratio: null,
        derived: null,
      }),
    );
    await waitFor(() => expect(api.getRunCost).toHaveBeenCalled());
    expect(screen.queryByText("本次 run 成本")).not.toBeInTheDocument();
  });

  it("接口失败时静默不渲染，不影响 run 详情页其余部分", async () => {
    vi.spyOn(api, "getRunCost").mockRejectedValue(new Error("boom"));
    render(<RunCostCard runId="run1" />);
    await waitFor(() => expect(api.getRunCost).toHaveBeenCalled());
    expect(screen.queryByText("本次 run 成本")).not.toBeInTheDocument();
  });
});
