import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, describe, expect, it, vi } from "vitest";

import { NormalizedOutputPanel } from "./NormalizedOutputPanel";

afterEach(() => vi.restoreAllMocks());

describe("normalized output panel", () => {
  it("explains a missing final output instead of offering a no-op generate button", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      status: "not_generated",
      raw_ref: "attempts/att_fixture/final_state.json",
      generation_available: false,
      generation_unavailable_reason: "final_output_unavailable",
      normalized: null,
    })));

    render(<NormalizedOutputPanel attemptId="att_fixture" rawOutput={{}} />);

    expect(await screen.findByText("本次运行没有提交最终输出。")).toBeInTheDocument();
    expect(screen.getByText(/因此无法生成规范化结果/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /生成规范化结果/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: /规范化结果/ })).not.toBeInTheDocument();
  });
});
