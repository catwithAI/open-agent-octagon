// 验收：attempt 统计行的「费用」只能是上游临时 key 实扣。
//
// 旧实现在这里回落展示 `att.cost_usd`（token × 本地价表估算），使同一个 run
// 的 attempt 卡片与成本卡片给出相差一个数量级的两个金额——Talent / Luna 的
// Kimi 上游实扣 $0.1866，本地估算 $4.25，报告据后者写出 $13.56 的总成本。
//
// 核心不变式：**估算永不出现在费用位**、**缺账显示原因而不是数字**。
import { describe, it, expect, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import type { AttemptMoney } from "../api/client";
import { MoneyStat } from "./RunDetail";

afterEach(cleanup);

function money(over: Partial<AttemptMoney> = {}): AttemptMoney {
  return {
    audited_cost_usd: 0.1866,
    auditable: true,
    status: "final",
    attribution_mode: "ephemeral_run_key",
    error_code: null,
    unaudited_reason: null,
    unaudited_is_structural: false,
    estimate: { estimate_cost_usd: 4.25, estimate_priced: true },
    ...over,
  };
}

describe("MoneyStat", () => {
  it("展示上游实扣，而不是高出一个数量级的本地估算", () => {
    render(<MoneyStat money={money()} />);
    expect(screen.getByText(/实扣 \$0\.1866/)).toBeInTheDocument();
    // $4.25 是估算，绝不能出现在费用位
    expect(screen.queryByText(/4\.25/)).not.toBeInTheDocument();
  });

  it("结算中不显示金额，更不显示 $0", () => {
    render(
      <MoneyStat
        money={money({
          audited_cost_usd: null,
          auditable: false,
          status: "settling",
          unaudited_reason: "结算中",
        })}
      />,
    );
    expect(screen.getByText(/费用未入账 · 结算中/)).toBeInTheDocument();
    expect(screen.queryByText(/\$0/)).not.toBeInTheDocument();
    expect(screen.queryByText(/4\.25/)).not.toBeInTheDocument();
  });

  it("经网关无法归因时说明是结构性缺账", () => {
    render(
      <MoneyStat
        money={money({
          audited_cost_usd: null,
          auditable: false,
          status: "failed",
          attribution_mode: "ephemeral_key_unbased",
          error_code: "gateway_bound_agent",
          unaudited_reason: "经网关调用上游，无法按 attempt 归因",
          unaudited_is_structural: true,
        })}
      />,
    );
    expect(
      screen.getByText(/费用未入账 · 经网关调用上游，无法按 attempt 归因/),
    ).toBeInTheDocument();
  });

  it("upper_bound 不得作为实扣展示", () => {
    render(
      <MoneyStat
        money={money({
          audited_cost_usd: null,
          auditable: false,
          status: "upper_bound",
          attribution_mode: "shared_key_upper_bound",
          unaudited_reason: "共享 key，仅为上界",
        })}
      />,
    );
    expect(screen.queryByText(/实扣/)).not.toBeInTheDocument();
    expect(screen.getByText(/共享 key，仅为上界/)).toBeInTheDocument();
  });

  it("没有成本账时什么金额都不渲染", () => {
    const { container } = render(
      <MoneyStat
        money={money({
          audited_cost_usd: null,
          auditable: false,
          status: "unavailable",
          attribution_mode: null,
          unaudited_reason: "无成本账",
          estimate: { estimate_cost_usd: 4.25, estimate_priced: true },
        })}
      />,
    );
    expect(container.textContent).not.toMatch(/4\.25/);
    expect(screen.getByText(/费用未入账 · 无成本账/)).toBeInTheDocument();
  });

  it("money 缺失时不渲染任何东西（旧 API 响应）", () => {
    const { container } = render(<MoneyStat money={undefined} />);
    expect(container.textContent).toBe("");
  });
});
