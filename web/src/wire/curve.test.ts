// 验收：固定 fixture 验证曲线点、空态、降级态、截断归因。
import { describe, it, expect } from "vitest";
import { curveSegments, splitMatched, deriveWireView, usageValue } from "./curve";
import type { WireManifest, WireRecord } from "../api/client";

function call(id: string, usage: Record<string, number | null>, confidence = "explicit"): WireRecord {
  return {
    record_id: id, record_type: "llm_call", phase: "agent_run",
    correlation: { confidence }, data: { usage },
    time: { timestamp: `2026-07-14T00:00:0${id}.000Z` },
  } as WireRecord;
}
function hop(id: string, confidence = "unmatched"): WireRecord {
  return {
    record_id: id, record_type: "http_exchange", phase: "agent_run",
    correlation: { confidence }, data: { direction: "outbound", status_code: 200 },
    time: { timestamp: `2026-07-14T00:00:0${id}.000Z` },
  } as unknown as WireRecord;
}

describe("usageValue：null ≠ 0", () => {
  it("数值原样，null/缺失为 null（不当 0）", () => {
    expect(usageValue({ input_tokens: 0 }, "input_tokens")).toBe(0);
    expect(usageValue({ input_tokens: null }, "input_tokens")).toBeNull();
    expect(usageValue({}, "output_tokens")).toBeNull();
  });
});

describe("curveSegments：断点分段", () => {
  it("null 处断开，不画 0（3 点中间缺失 → 两段）", () => {
    const calls = [
      call("1", { output_tokens: 10 }),
      call("2", { output_tokens: null }),  // 断点
      call("3", { output_tokens: 30 }),
    ];
    const segs = curveSegments(calls, "output_tokens");
    expect(segs).toHaveLength(2);
    expect(segs[0]).toEqual([[0, 10]]);
    expect(segs[1]).toEqual([[2, 30]]);
  });
  it("显式 0 是有效点（连续一段）", () => {
    const segs = curveSegments([call("1", { output_tokens: 0 }), call("2", { output_tokens: 5 })], "output_tokens");
    expect(segs).toHaveLength(1);
    expect(segs[0]).toEqual([[0, 0], [1, 5]]);
  });
});

describe("splitMatched：unmatched 覆盖 llm + http", () => {
  it("inferred 进曲线；unmatched llm/http 单独分组", () => {
    const calls = [call("1", { output_tokens: 5 }, "explicit"), call("2", { output_tokens: 3 }, "inferred"),
      call("3", { output_tokens: 1 }, "unmatched")];
    const hops = [hop("9", "unmatched")];
    const { matched, unmatched } = splitMatched(calls, hops);
    expect(matched.map((c) => c.record_id)).toEqual(["1", "2"]);  // inferred 进曲线
    expect(unmatched.map((c) => c.record_id).sort()).toEqual(["3", "9"]);  // 覆盖 http
  });
});

describe("deriveWireView：空态/降级/截断", () => {
  const base: WireManifest = { status: "complete", sources: [], coverage: {}, totals: {} };

  it("空态：manifest not_available", () => {
    expect(deriveWireView(null, [], [], false, false).kind).toBe("not_available");
    expect(deriveWireView({ status: "not_available" }, [], [], false, false).kind).toBe("not_available");
  });

  it("有曲线：matched llm_call", () => {
    const v = deriveWireView(base, [call("1", { input_tokens: 10 })], [], false, false);
    expect(v.kind).toBe("available");
    if (v.kind === "available") { expect(v.hasCurve).toBe(true); expect(v.curveCount).toBe(1); }
  });

  const reasons = (v: ReturnType<typeof deriveWireView>) =>
    v.kind === "available" ? v.gaps.map((g) => g.reason) : [];

  it("降级态：phase_attribution degraded 进 gaps", () => {
    const v = deriveWireView({ ...base, status: "partial", phase_attribution: "degraded" }, [], [], false, false);
    expect(reasons(v)).toContain("phase_degraded");
  });

  it("降级态：保留 source failure_reason 与 manifest gap field（评审 M2）", () => {
    const m: WireManifest = { ...base, status: "partial",
      sources: [{ kind: "env-inbound", instance: "env-inbound", status: "failed",
        failure_reason: "source_start_failed" }],
      gaps: [{ field: "token_usage", reason: "adapter_native_mismatch" }] };
    const v = deriveWireView(m, [], [], false, false);
    if (v.kind === "available") {
      // 结构化 gap 保留明细，不是扁平字符串
      const src = v.gaps.find((g) => g.reason === "source");
      expect(src?.source).toBe("env-inbound");
      expect(src?.status).toBe("failed");
      expect(src?.failureReason).toBe("source_start_failed");
      const mg = v.gaps.find((g) => g.reason === "adapter_native_mismatch");
      expect(mg?.field).toBe("token_usage");
    }
  });

  it("截断归因：call/http 分别标注（评审 R6）", () => {
    expect(reasons(deriveWireView(base, [], [], false, true))).toContain("trunc_hops");
    expect(reasons(deriveWireView(base, [], [], false, true))).not.toContain("trunc_calls");
    expect(reasons(deriveWireView(base, [], [], true, false))).toContain("trunc_calls");
    expect(reasons(deriveWireView(base, [], [], true, false))).not.toContain("trunc_hops");
  });

  it("aggregate-only 是 per-source 标记，有 llm_call 仍画曲线（评审 M8）", () => {
    const m: WireManifest = { ...base, status: "partial",
      sources: [{ kind: "native-event", instance: "native-event", status: "complete",
        capabilities: { call_boundary: "aggregate-only" } }] };
    const v = deriveWireView(m, [call("1", { input_tokens: 5 })], [], false, false);
    if (v.kind === "available") { expect(v.aggregateOnly).toBe(true); expect(v.hasCurve).toBe(true); }
  });
});
