// 验收：用 Testing Library 渲染真实 WirePanel，固定 API fixture
// 验证空态/曲线/降级 banner/aggregate-only 文案的生产 DOM——不测 helper 死代码。
import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { render, screen, waitFor, cleanup, fireEvent } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { MemoryRouter } from "react-router-dom";
import type { WireManifest, WirePage, WireTrajectory } from "../api/client";

// mock api：getWireManifest/getWire/getWireBlob 返回固定 fixture。
const manifestRef: { value: WireManifest } = { value: { status: "not_available" } };
const pageRef: { llm: WirePage; http: WirePage } = {
  llm: { items: [], next_cursor: null, manifest_status: null },
  http: { items: [], next_cursor: null, manifest_status: null },
};
const blobRef: { value: { status: "ok"; body: unknown } | { status: "unavailable" } } = {
  value: { status: "ok", body: { model: "m", messages: [] } },
};
const trajectoryRef: { value: WireTrajectory; reject: boolean } = {
  value: { status: "not_available", steps: [] },
  reject: false,
};
vi.mock("../api/client", async (orig) => {
  const actual = await orig<typeof import("../api/client")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      getWireManifest: vi.fn(async () => manifestRef.value),
      getWireTrajectory: vi.fn(async () => {
        if (trajectoryRef.reject) throw new Error("GET wire trajectory -> 404");
        return trajectoryRef.value;
      }),
      getWire: vi.fn(async (_r: string, _a: string, p?: { record_type?: string }) =>
        p?.record_type === "http_exchange" ? pageRef.http : pageRef.llm),
      getWireBlob: vi.fn(async () => blobRef.value),
    },
  };
});

import { WirePanel } from "../pages/RunDetail";

function renderPanel() {
  return render(
    <MemoryRouter><WirePanel runId="r" attemptId="a" label="claude" /></MemoryRouter>);
}

beforeEach(() => {
  manifestRef.value = { status: "not_available" };
  pageRef.llm = { items: [], next_cursor: null, manifest_status: null };
  pageRef.http = { items: [], next_cursor: null, manifest_status: null };
  trajectoryRef.value = { status: "not_available", steps: [] };
  trajectoryRef.reject = false;
  blobRef.value = { status: "ok", body: { model: "m", messages: [] } };
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe("WirePanel 生产 DOM", () => {
  it("空态：manifest not_available", async () => {
    manifestRef.value = { status: "not_available" };
    renderPanel();
    await waitFor(() => expect(screen.getByText(/无通信采集/)).toBeInTheDocument());
  });

  it("有曲线：matched llm_call 渲染曲线标题", async () => {
    manifestRef.value = { status: "complete", sources: [], coverage: { correlated_calls: 1 } };
    pageRef.llm = { items: [{
      record_id: "1", record_type: "llm_call", phase: "agent_run",
      correlation: { confidence: "explicit" },
      data: { usage: { input_tokens: 100, output_tokens: 20 } },
      time: { timestamp: "2026-07-14T00:00:01.000Z" },
    }], next_cursor: null, manifest_status: "complete" };
    renderPanel();
    await waitFor(() => expect(screen.getByText(/调用级 token 曲线/)).toBeInTheDocument());
    expect(screen.getByText(/^调用级 token 曲线 · 1 次调用$/)).toBeInTheDocument();
  });

  it("24 条调用逐条读数，未知不伪装成 0，曲线可定位表格行", async () => {
    manifestRef.value = { status: "complete", sources: [], coverage: { correlated_calls: 24 } };
    pageRef.http = { items: [], next_cursor: null, manifest_status: "complete" };
    pageRef.llm = {
      items: Array.from({ length: 24 }, (_, i) => ({
        record_id: `call-${i + 1}`, record_type: "llm_call", phase: "agent_run",
        correlation: { confidence: "explicit", logical_call_id: `lc-${i + 1}` },
        data: { model_resolved: "claude-opus", call_role: "main",
          finish_reason: i === 0 ? "end_turn" : null,
          usage: { input_tokens: i + 1, output_tokens: i === 0 ? null : i * 2,
            cache_read_tokens: null, cache_write_tokens: null, reasoning_tokens: null } },
        time: { timestamp: `2026-07-14T00:00:${String(i).padStart(2, "0")}.000Z` },
      })),
      next_cursor: null, manifest_status: "complete",
    };
    renderPanel();
    await screen.findByText(/逐调用检查器 · 24 条/);
    expect(screen.getAllByText("未知").length).toBeGreaterThan(0);
    const point = screen.getByRole("button", { name: /第 1 次调用 · 输入 1 token/ });
    fireEvent.click(point);
    const row = document.getElementById("wire-call-call-1");
    expect(row).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(point, { key: "Enter" });
    expect(row).toHaveClass("is-selected");
  });

  it("降级 banner：phase degraded + source failure_reason + gap field 可见（评审 M2）", async () => {
    manifestRef.value = {
      status: "partial", phase_attribution: "degraded",
      sources: [{ kind: "env-inbound", instance: "env-inbound", status: "failed",
        failure_reason: "source_start_failed" }],
      gaps: [{ field: "token_usage", reason: "adapter_native_mismatch" }],
      coverage: {},
    };
    pageRef.llm = { items: [], next_cursor: null, manifest_status: "partial" };
    renderPanel();
    await waitFor(() => expect(screen.getByText(/phase 归属降级/)).toBeInTheDocument());
    // 保留「哪个采集器、为什么失败」明细
    expect(screen.getByText(/env-inbound · failed（source_start_failed）/)).toBeInTheDocument();
    // 保留 manifest gap 的具体 field
    expect(screen.getByText(/token_usage/)).toBeInTheDocument();
  });

  it("截断：仅 http 截断时独立标注（评审 R6）", async () => {
    manifestRef.value = { status: "partial", sources: [], coverage: {} };
    pageRef.llm = { items: [], next_cursor: null, manifest_status: "partial" };
    pageRef.http = { items: [{
      record_id: "h1", record_type: "http_exchange", phase: "agent_run",
      correlation: { confidence: "unmatched" }, data: { direction: "outbound", status_code: 200 },
      time: { timestamp: "x" },
    } as never], next_cursor: "CURSOR_MORE", manifest_status: "partial" };
    renderPanel();
    await waitFor(() => expect(screen.getByText(/http_exchange 超上限已截断/)).toBeInTheDocument());
    expect(screen.queryByText(/llm_call 超上限已截断/)).not.toBeInTheDocument();
  });

  it("aggregate-only：per-source 文案，有 llm_call 仍画曲线", async () => {
    manifestRef.value = {
      status: "partial", coverage: {},
      sources: [{ kind: "native-event", instance: "native-event", status: "complete",
        capabilities: { call_boundary: "aggregate-only" } }],
      aggregates: [{ scope: "attempt", usage: { input_tokens: 5000, output_tokens: null } }],
    };
    pageRef.llm = { items: [{
      record_id: "1", record_type: "llm_call", phase: "agent_run",
      correlation: { confidence: "explicit" },
      data: { usage: { input_tokens: 100, output_tokens: 20 } }, time: { timestamp: "x" },
    }], next_cursor: null, manifest_status: "partial" };
    renderPanel();
    await waitFor(() => expect(screen.getByText(/aggregate-only/)).toBeInTheDocument());
    // null output 显「未知」
    expect(screen.getByLabelText("Attempt 累计用量")).toHaveTextContent(/输出 未知/);
    // 仍画曲线
    expect(screen.getByText(/调用级 token 曲线/)).toBeInTheDocument();
  });

  it("aggregate-only 是累计卡片且 conflict 展示 native/result/adapter 三方", async () => {
    manifestRef.value = {
      status: "partial", coverage: {}, totals: { conflicts: 1 },
      sources: [{ kind: "native-event", instance: "native-event", status: "complete",
        capabilities: { call_boundary: "aggregate-only" } }],
      aggregates: [
        { scope: "attempt", producer_event_type: "result",
          usage: { input_tokens: 1131205, output_tokens: 9584, cache_read_tokens: 1028480,
            reasoning_tokens: 2330 } },
        { scope: "adapter", producer_event_type: "adapter_result",
          usage: { input_tokens: 1511, output_tokens: 24148 } },
        { scope: "reconciliation", producer_event_type: "usage_reconciliation",
          conflict: { native: { input_tokens: 48, output_tokens: 222 },
            adapter: { input_tokens: 1511, output_tokens: 24148 } } },
      ],
    };
    pageRef.llm = { items: [], next_cursor: null, manifest_status: "partial" };
    pageRef.http = { items: [], next_cursor: null, manifest_status: "partial" };
    renderPanel();
    await waitFor(() => expect(screen.getAllByText("1,131,205").length).toBeGreaterThan(0));
    expect(screen.getByText(/不是一次调用/)).toBeInTheDocument();
    expect(screen.queryByText(/调用级 token 曲线/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByText(/Token 对账冲突/));
    expect(screen.getByText(/逐调用求和/)).toBeInTheDocument();
    expect(screen.getByText(/Producer result aggregate/)).toBeInTheDocument();
    expect(screen.getByText(/Adapter aggregate/)).toBeInTheDocument();
  });

  it("hop 时间线展示耗时/bytes/source，并与 call/trajectory 双向关联", async () => {
    manifestRef.value = { status: "complete", coverage: { correlated_calls: 2 },
      policy: { effective: "metadata" } };
    pageRef.llm = { items: [{
      record_id: "call-linked", record_type: "llm_call", phase: "agent_run",
      correlation: { confidence: "explicit", logical_call_id: "lc-linked" },
      data: { usage: { input_tokens: 10, output_tokens: 2 } }, time: { timestamp: "1" },
    }], next_cursor: null, manifest_status: "complete" };
    pageRef.http = { items: [{
      record_id: "hop-linked", record_type: "http_exchange", phase: "agent_run",
      source: { kind: "env-inbound", instance: "env-inbound" },
      correlation: { confidence: "explicit", logical_call_id: "lc-linked", hop_id: "hop-1" },
      data: { direction: "inbound", method: "POST", path: "/tools/task_brief",
        status_code: 200, request_bytes: 2, response_bytes: 1253, streamed: false, partial: false },
      time: { timestamp: "2", duration_ms: 3.5 },
    }, {
      record_id: "hop-unmatched", record_type: "http_exchange", phase: "agent_run",
      source: { kind: "env-inbound", instance: "env-inbound" },
      correlation: { confidence: "unmatched", hop_id: "hop-2" },
      data: { direction: "inbound", method: "POST", path: "/tools/workspace_status",
        status_code: 200, request_bytes: 2, response_bytes: 325 },
      time: { timestamp: "3", duration_ms: 1 },
    }], next_cursor: null, manifest_status: "complete" };
    trajectoryRef.value = { status: "complete", steps: [{
      step_id: "ts-linked", sequence: 7, kind: "tool_call",
      logical_call_id: "lc-linked", tool_call_id: "tool-7",
    }] };
    renderPanel();
    // hop 明细现在收在"详细数据"折叠区（时间轴泳道图是主视图）——先展开。
    const fold = await screen.findByText(/详细数据 · /);
    fireEvent.click(fold);
    await screen.findByText(/全部 transport hop · 2/);
    expect(screen.getByText(/3.5ms/)).toBeInTheDocument();
    // 时间轴泳道图也把 hop 渲染成条（同名 title），故用折叠区里含"状态"文案的
    // HopBody 切换按钮（.wire-hop-toggle）精确定位，避开泳道条。
    const hopToggle = (name: RegExp) => screen.getAllByRole("button", { name })
      .find((el) => el.classList.contains("wire-hop-toggle"))!;
    fireEvent.click(hopToggle(/POST \/tools\/task_brief/));
    expect(screen.getByText(/source env-inbound\/env-inbound/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /轨迹 #7/ })).toHaveAttribute("href", "#trajectory-ts-linked");
    fireEvent.click(hopToggle(/POST \/tools\/workspace_status/));
    expect(screen.getByText(/缺少可用 call anchor/)).toBeInTheDocument();
    fireEvent.click(screen.getByText(/Trajectory 语义步骤/));
    expect(screen.getByRole("link", { name: "返回调用" })).toHaveAttribute("href", "#wire-call-call-linked");
  });

  // ---------- payload/blob 展示 policy 门控 ----------

  function _httpHop(over: Record<string, unknown> = {}) {
    return {
      record_id: "h1", record_type: "http_exchange", phase: "agent_run",
      correlation: { confidence: "unmatched" },
      data: { direction: "outbound", method: "POST", path: "/v1/messages",
              status_code: 200, request_bytes: 12, response_bytes: 34, ...over },
      time: { timestamp: "x" },
    } as never;
  }

  it("metadata 档：展开只提示无正文，不显示 body", async () => {
    manifestRef.value = { status: "complete", coverage: {},
      policy: { effective: "metadata" } };
    pageRef.llm = { items: [], next_cursor: null, manifest_status: "complete" };
    // metadata 档：finalizer 不会写 body_ref
    pageRef.http = { items: [_httpHop()], next_cursor: null, manifest_status: "complete" };
    renderPanel();
    const toggle = await screen.findByRole("button", { name: /POST \/v1\/messages/ });
    fireEvent.click(toggle);
    expect(screen.getByText(/不采集报文正文/)).toBeInTheDocument();
    expect(screen.queryByText(/请求正文/)).not.toBeInTheDocument();
  });

  it("full 档：展开拉取并显示请求/响应 blob", async () => {
    manifestRef.value = { status: "complete", coverage: {},
      policy: { effective: "full" } };
    pageRef.llm = { items: [], next_cursor: null, manifest_status: "complete" };
    pageRef.http = { items: [_httpHop({
      request_body_ref: "sha256-aaa.json.gz", response_body_ref: "sha256-bbb.json.gz",
    })], next_cursor: null, manifest_status: "complete" };
    blobRef.value = { status: "ok", body: { model: "m", messages: [] } };
    renderPanel();
    const toggle = await screen.findByRole("button", { name: /POST \/v1\/messages/ });
    fireEvent.click(toggle);
    await waitFor(() => expect(screen.getByText(/请求正文/)).toBeInTheDocument());
    expect(screen.getByText(/响应正文/)).toBeInTheDocument();
    // blob 内容渲染
    await waitFor(() =>
      expect(screen.getAllByText(/"model": "m"/).length).toBeGreaterThan(0));
  });

  it("parsed 档：只有解析视图，无原文切换", async () => {
    manifestRef.value = { status: "complete", coverage: {},
      policy: { effective: "parsed" } };
    pageRef.llm = { items: [], next_cursor: null, manifest_status: "complete" };
    pageRef.http = { items: [_httpHop({
      response_body_ref: "sha256-bbb.json.gz",
    })], next_cursor: null, manifest_status: "complete" };
    blobRef.value = { status: "ok", body: { model: "m" } };
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: /POST \/v1\/messages/ }));
    await waitFor(() => expect(screen.getByText(/解析视图（已脱敏）/)).toBeInTheDocument());
    // parsed 档不给「原文」切换
    expect(screen.queryByRole("tab", { name: "原文" })).not.toBeInTheDocument();
  });

  it("full 档：解析/原文可切换", async () => {
    manifestRef.value = { status: "complete", coverage: {},
      policy: { effective: "full" } };
    pageRef.llm = { items: [], next_cursor: null, manifest_status: "complete" };
    pageRef.http = { items: [_httpHop({
      response_body_ref: "sha256-bbb.json.gz",
    })], next_cursor: null, manifest_status: "complete" };
    blobRef.value = { status: "ok", body: { a: 1 } };
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: /POST \/v1\/messages/ }));
    await waitFor(() => expect(screen.getByText(/协议原文（已脱敏落盘）/)).toBeInTheDocument());
    // full 档有解析/原文两个切换
    const rawTab = screen.getByRole("tab", { name: "原文" });
    const parsedTab = screen.getByRole("tab", { name: "解析" });
    expect(parsedTab).toHaveAttribute("aria-selected", "true");  // 默认解析
    // 默认 pretty JSON（多行缩进）
    expect(screen.getByText(/"a": 1/)).toBeInTheDocument();
    fireEvent.click(rawTab);
    expect(rawTab).toHaveAttribute("aria-selected", "true");
    // 原文模式保留 compact/pretty 两种合法 JSON 空白形式。
    expect(screen.getByText(/"a"\s*:\s*1/)).toBeInTheDocument();
  });

  it("#2 截断 blob 显示「内容已截断」警告，不当完整正文", async () => {
    manifestRef.value = { status: "complete", coverage: {},
      policy: { effective: "full" } };
    pageRef.llm = { items: [], next_cursor: null, manifest_status: "complete" };
    pageRef.http = { items: [_httpHop({
      response_body_ref: "sha256-bbb.json.gz",
      response_body_truncated: true,  // 后端标记响应正文被截断
    })], next_cursor: null, manifest_status: "complete" };
    blobRef.value = { status: "ok", body: { partial: "prefix" } };
    renderPanel();
    const toggle = await screen.findByRole("button", { name: /POST \/v1\/messages/ });
    fireEvent.click(toggle);
    // 截断警告出现，明确非完整正文
    await waitFor(() => expect(screen.getByText(/内容已截断/)).toBeInTheDocument());
    expect(screen.getByText(/连续前缀/)).toBeInTheDocument();
  });

  it("full 档但 blob API 404：明确显示 policy 门控降级，不报错", async () => {
    manifestRef.value = { status: "complete", coverage: {},
      policy: { effective: "full" } };
    pageRef.llm = { items: [], next_cursor: null, manifest_status: "complete" };
    pageRef.http = { items: [_httpHop({
      request_body_ref: "sha256-aaa.json.gz",
    })], next_cursor: null, manifest_status: "complete" };
    blobRef.value = { status: "unavailable" };
    renderPanel();
    const toggle = await screen.findByRole("button", { name: /POST \/v1\/messages/ });
    fireEvent.click(toggle);
    await waitFor(() =>
      expect(screen.getByText(/内容不可用（policy 门控/)).toBeInTheDocument());
  });

  // ---------- #4：trajectory 404 独立降级，不拖垮通信面板 ----------

  it("#4 trajectory 404 时曲线/hop 仍正常渲染，面板不消失", async () => {
    manifestRef.value = { status: "complete", sources: [], coverage: { correlated_calls: 1 } };
    pageRef.llm = { items: [{
      record_id: "1", record_type: "llm_call", phase: "agent_run",
      correlation: { confidence: "explicit" },
      data: { usage: { input_tokens: 100, output_tokens: 20 } },
      time: { timestamp: "2026-07-14T00:00:01.000Z" },
    }], next_cursor: null, manifest_status: "complete" };
    trajectoryRef.reject = true;  // trajectory 接口 404
    renderPanel();
    // 曲线仍渲染（trajectory 失败不影响核心面板）。
    await waitFor(() => expect(screen.getByText(/调用级 token 曲线/)).toBeInTheDocument());
    // 不出现整体加载失败文案。
    expect(screen.queryByText(/通信数据加载失败/)).not.toBeInTheDocument();
  });
});
