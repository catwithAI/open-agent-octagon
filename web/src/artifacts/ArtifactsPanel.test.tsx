import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useState } from "react";

const { preview } = vi.hoisted(() => ({ preview: vi.fn() }));
vi.mock("../api/client", async (orig) => {
  const actual = await orig<typeof import("../api/client")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      getArtifactPreview: preview,
      artifactUrl: (_run: string, _attempt: string, path: string) => `/download/${path}`,
    },
  };
});

import { ArtifactsPanel } from "../pages/RunDetail";

// 这些用例关心的都是预览外壳，产物结构一律是「根目录若干文件」。
// 后端现在返回目录树，用 helper 保持用例本身的意图不变。
function rootTree(files: Array<{ name: string; size: number; type: string; media_type: string }>) {
  return {
    name: "", path: "", dirs: [],
    files: files.map((f) => ({ ...f, path: f.name })),
    file_count: files.length,
    total_size: files.reduce((sum, f) => sum + f.size, 0),
  } as never;
}



afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("W7 artifact preview shell", () => {
  it("shows a safe Office fallback and original download", async () => {
    preview.mockResolvedValue({
      version: "octagon-artifact-preview-v1",
      artifact: {
        ref: "./deck.pptm", name: "deck.pptm", size: 123,
        media_type: "application/vnd.ms-powerpoint.presentation.macroEnabled.12",
        type: "presentation",
      },
      status: "unsupported",
      counts: { slides: 3, pages: null, sheets: null },
      renderer: { name: "octagon-ooxml-scanner", version: "1" },
      error: { code: "renderer_unavailable", message: "renderer 尚未接入" },
      cache_key: "sha256:x",
      poll_after_ms: null,
      security: { macros_present: true, macros_executed: false },
      capability_gaps: ["macros-disabled", "external-resources-blocked"],
    });
    render(<ArtifactsPanel
      runId="run" attemptId="attempt"
      tree={rootTree([{
        name: "deck.pptm", size: 123, type: "presentation",
        media_type: "application/vnd.ms-powerpoint.presentation.macroEnabled.12",
      }])}
    />);
    fireEvent.click(screen.getByText("deck.pptm"));
    expect(screen.getByText(/正在安全检查预览/)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("内容预览尚未接入")).toBeInTheDocument());
    expect(screen.getByText(/3 页幻灯片/)).toBeInTheDocument();
    expect(screen.getByText(/macros-disabled/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "下载原文件" })).toHaveAttribute(
      "href", "/download/deck.pptm",
    );
  });

  it("renders a bounded XLSX workbook with tabs, cached formula values, search and zoom", async () => {
    preview.mockResolvedValue({
      version: "octagon-artifact-preview-v1",
      artifact: { ref: "./metrics.xlsx", name: "metrics.xlsx", size: 321,
        media_type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type: "spreadsheet" },
      status: "ready", counts: { sheets: 2 },
      renderer: { name: "octagon-xlsx-structural", version: "1" },
      error: null, cache_key: "sha256:workbook", poll_after_ms: null,
      security: {}, capability_gaps: ["formulas-not-evaluated"],
      content: {
        kind: "workbook", truncated: false, formulas_evaluated: false,
        limits: { sheets: 32, rows_per_sheet: 500, columns: 100, cells_total: 10000 },
        sheets: [{
          name: "Summary", dimension: "A1:B2", merges: ["A1:A2"], columns: [],
          frozen: { top_left_cell: "A2", x_split: 0, y_split: 1 }, truncated: false,
          rows: [{ index: 1, hidden: false, height: null, cells: [
            { ref: "A1", row: 1, column: 1, value: "收入", raw_value: "0",
              value_type: "s", formula: null, number_format: null },
          ] }, { index: 2, hidden: false, height: null, cells: [
            { ref: "A2", row: 2, column: 1, value: 0, raw_value: "0",
              value_type: "n", formula: null, number_format: "General" },
            { ref: "B2", row: 2, column: 2, value: 0, raw_value: "0",
              value_type: "n", formula: "SUM(A2:A2)", number_format: "General" },
          ] }],
        }, {
          name: "Data", dimension: "A1", merges: [], columns: [], frozen: null, truncated: false,
          rows: [{ index: 1, hidden: false, height: null, cells: [
            { ref: "A1", row: 1, column: 1, value: "second", raw_value: "second",
              value_type: "str", formula: null, number_format: null },
          ] }],
        }],
      },
    });
    render(<ArtifactsPanel runId="run" attemptId="attempt" tree={rootTree([{
      name: "metrics.xlsx", size: 321, type: "spreadsheet",
      media_type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }])} />);
    fireEvent.click(screen.getByText("metrics.xlsx"));
    await waitFor(() => expect(screen.getByRole("tab", { name: "Summary" })).toBeInTheDocument());
    expect(screen.getByText("=SUM(A2:A2)")).toBeInTheDocument();
    expect(screen.getAllByText("0").length).toBeGreaterThan(0);
    expect(screen.getByText(/公式只展示，不执行/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "放大" }));
    expect(screen.getByText("110%")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Data" }));
    expect(screen.getByText("second")).toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText("值或公式"), { target: { value: "missing" } });
    expect(screen.getByText("没有匹配的单元格。")).toBeInTheDocument();
  });

  it("aborts a stale preview request when another file is selected", async () => {
    const signals: AbortSignal[] = [];
    preview.mockImplementation((_r, _a, path: string, signal: AbortSignal) => {
      signals.push(signal);
      if (path.endsWith("second.xlsx")) {
        return Promise.resolve({
          version: "octagon-artifact-preview-v1",
          artifact: { ref: path, name: "second.xlsx", size: 2, media_type: "application/x", type: "spreadsheet" },
          status: "unsupported", counts: { sheets: 2 }, renderer: { name: "x", version: "1" },
          error: { code: "renderer_unavailable" }, cache_key: "sha256:y", poll_after_ms: null,
          security: {}, capability_gaps: [],
        });
      }
      return new Promise(() => undefined);
    });
    render(<ArtifactsPanel runId="run" attemptId="attempt" tree={rootTree([
      { name: "first.docx", size: 1, type: "document", media_type: "application/x" },
      { name: "second.xlsx", size: 2, type: "spreadsheet", media_type: "application/x" },
    ])} />);
    fireEvent.click(screen.getByText("first.docx"));
    fireEvent.click(screen.getByText("second.xlsx"));
    await waitFor(() => expect(screen.getByText(/2 个工作表/)).toBeInTheDocument());
    expect(signals[0].aborted).toBe(true);
    expect(signals[1].aborted).toBe(false);
  });

  it("polls a background preview until ready", async () => {
    preview
      .mockResolvedValueOnce({
        version: "octagon-artifact-preview-v1",
        artifact: { ref: "./large.xlsx", name: "large.xlsx", size: 30_000_000,
          media_type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
          type: "spreadsheet" },
        status: "rendering", counts: { sheets: 1 },
        renderer: { name: "octagon-xlsx-structural", version: "1" },
        error: null, cache_key: "sha256:large", poll_after_ms: 1,
        security: {}, capability_gaps: ["background-rendering"], content: null,
      })
      .mockResolvedValueOnce({
        version: "octagon-artifact-preview-v1",
        artifact: { ref: "./large.xlsx", name: "large.xlsx", size: 30_000_000,
          media_type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
          type: "spreadsheet" },
        status: "ready", counts: { sheets: 1 },
        renderer: { name: "octagon-xlsx-structural", version: "1" },
        error: null, cache_key: "sha256:large", poll_after_ms: null,
        security: {}, capability_gaps: [],
        content: { kind: "workbook", sheets: [], truncated: false,
          limits: { sheets: 32, rows_per_sheet: 500, columns: 100, cells_total: 10000 },
          formulas_evaluated: false },
      });
    render(<ArtifactsPanel runId="run" attemptId="attempt" tree={rootTree([{
      name: "large.xlsx", size: 30_000_000, type: "spreadsheet",
      media_type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }])} />);
    fireEvent.click(screen.getByText("large.xlsx"));
    await waitFor(() => expect(screen.getByText(/正在后台生成预览/)).toBeInTheDocument());
    await waitFor(() => expect(preview).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByText(/工作簿没有可显示的工作表/)).toBeInTheDocument());
  });

  it("renders PPTX slides with navigation, zoom and bounded static content", async () => {
    preview.mockResolvedValue({
      version: "octagon-artifact-preview-v1",
      artifact: { ref: "./deck.pptx", name: "deck.pptx", size: 100,
        media_type: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        type: "presentation" },
      status: "ready", counts: { slides: 2 },
      renderer: { name: "octagon-pptx-static", version: "1" }, error: null,
      cache_key: "sha256:pptx", poll_after_ms: null, security: {},
      capability_gaps: ["charts-not-rendered"],
      content: { kind: "presentation", width: 100, height: 56, aspect_ratio: 1.78,
        truncated: false, limits: { slides: 100, elements_per_slide: 500 },
        active_content_executed: false,
        slides: [
          { number: 1, elements: [{ kind: "text", x: .1, y: .1, width: .8, height: .2,
            text: "第一页标题", role: "title" }] },
          { number: 2, elements: [{ kind: "text", x: .1, y: .1, width: .8, height: .2,
            text: "第二页内容" }] },
        ] },
    });
    render(<ArtifactsPanel runId="run" attemptId="attempt" tree={rootTree([{
      name: "deck.pptx", size: 100, type: "presentation", media_type: "application/x",
    }])} />);
    fireEvent.click(screen.getByText("deck.pptx"));
    await waitFor(() => expect(screen.getByLabelText("幻灯片预览")).toBeInTheDocument());
    expect(screen.getAllByText("第一页标题").length).toBeGreaterThan(0);
    expect(screen.getByRole("note")).toHaveTextContent("charts-not-rendered");
    fireEvent.click(screen.getByText("下一页"));
    expect(screen.getAllByText("第二页内容").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByText("放大"));
    expect(screen.getByText("110%")).toBeInTheDocument();
  });

  it("renders evaluator-produced PPT pages as full-slide images", async () => {
    preview.mockResolvedValue({
      version: "octagon-artifact-preview-v1",
      artifact: { ref: "./deck.pptx", name: "deck.pptx", size: 100,
        media_type: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        type: "presentation" },
      status: "ready", counts: { slides: 1 },
      renderer: { name: "octagon-pptx-rendered-pages", version: "1" }, error: null,
      cache_key: "sha256:rendered", poll_after_ms: null, security: {}, capability_gaps: [],
      content: { kind: "presentation", width: 1600, height: 900, aspect_ratio: 16 / 9,
        render_mode: "rendered-pages", truncated: false,
        limits: { slides: 100, elements_per_slide: 0 }, active_content_executed: false,
        slides: [{ number: 1, elements: [], rendered_image_data_uri: "data:image/png;base64,cG5n" }],
      },
    });
    render(<ArtifactsPanel runId="run" attemptId="attempt" tree={rootTree([{
      name: "deck.pptx", size: 100, type: "presentation", media_type: "application/x",
    }])} />);
    fireEvent.click(screen.getByText("deck.pptx"));

    await waitFor(() => expect(screen.getByLabelText("幻灯片预览")).toBeInTheDocument());
    const images = screen.getAllByRole("img", { name: "第 1 页" });
    expect(images).toHaveLength(2);
    expect(images[1]).toHaveClass("pptx-rendered-slide");
    expect(images[1]).toHaveAttribute("src", "data:image/png;base64,cG5n");
  });

  it("renders DOCX headings, tables, safe links and search", async () => {
    preview.mockResolvedValue({
      version: "octagon-artifact-preview-v1",
      artifact: { ref: "./report.docx", name: "report.docx", size: 100,
        media_type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        type: "document" },
      status: "ready", counts: { pages: null },
      renderer: { name: "octagon-docx-structural", version: "1" }, error: null,
      cache_key: "sha256:docx", poll_after_ms: null, security: {}, capability_gaps: [],
      content: { kind: "document", page: { width_pt: 612, height_pt: 792 }, truncated: false,
        images_omitted: 0, external_links: 1, active_content_executed: false,
        limits: { blocks: 5000, table_cells: 10000 }, blocks: [
          { kind: "paragraph", text: "执行摘要", heading: 1, list_item: false,
            runs: [{ text: "执行摘要", bold: true }] },
          { kind: "paragraph", text: "安全链接", heading: null, list_item: false,
            runs: [{ text: "安全链接", href: "https://example.test/" }] },
          { kind: "table", rows: [[{ text: "指标", blocks: [] }, { text: "42", blocks: [] }]] },
        ] },
    });
    render(<ArtifactsPanel runId="run" attemptId="attempt" tree={rootTree([{
      name: "report.docx", size: 100, type: "document", media_type: "application/x",
    }])} />);
    fireEvent.click(screen.getByText("report.docx"));
    await waitFor(() => expect(screen.getByRole("heading", { name: "执行摘要" })).toBeInTheDocument());
    expect(screen.getByRole("link", { name: "安全链接" })).toHaveAttribute("href", "https://example.test/");
    expect(screen.getByText("42")).toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText("文档文字"), { target: { value: "摘要" } });
    expect(screen.getByText("1 个匹配块")).toBeInTheDocument();
  });

  it("virtualizes large XLSX row windows", async () => {
    preview.mockResolvedValue({
      version: "octagon-artifact-preview-v1",
      artifact: { ref: "./large-grid.xlsx", name: "large-grid.xlsx", size: 100,
        media_type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type: "spreadsheet" },
      status: "ready", counts: { sheets: 1 },
      renderer: { name: "octagon-xlsx-structural", version: "1" }, error: null,
      cache_key: "sha256:grid", poll_after_ms: null, security: {}, capability_gaps: [],
      content: { kind: "workbook", truncated: true, formulas_evaluated: false,
        limits: { sheets: 32, rows_per_sheet: 500, columns: 100, cells_total: 10000 },
        sheets: [{ name: "Large", dimension: "A1:A200", merges: [], columns: [], frozen: null,
          truncated: true, rows: Array.from({ length: 200 }, (_, index) => ({
            index: index + 1, hidden: false, height: null,
            cells: [{ ref: `A${index + 1}`, row: index + 1, column: 1,
              value: `row-${index + 1}`, display_value: `row-${index + 1}`,
              raw_value: `row-${index + 1}`, value_type: "str", formula: null, number_format: null }],
          })) }] },
    });
    const { container } = render(<ArtifactsPanel runId="run" attemptId="attempt" tree={rootTree([{
      name: "large-grid.xlsx", size: 100, type: "spreadsheet", media_type: "application/x",
    }])} />);
    fireEvent.click(screen.getByText("large-grid.xlsx"));
    await waitFor(() => expect(screen.getByText("row-1")).toBeInTheDocument());
    expect(screen.queryByText("row-150")).not.toBeInTheDocument();
    const scroller = container.querySelector(".xlsx-grid-scroll") as HTMLDivElement;
    Object.defineProperty(scroller, "scrollTop", { value: 4000, configurable: true });
    fireEvent.scroll(scroller);
    expect(screen.getByText("row-150")).toBeInTheDocument();
    expect(screen.queryByText("row-1")).not.toBeInTheDocument();
  });

  it("keeps agent viewer state isolated and synchronizes pages only when locked", async () => {
    const descriptor = {
      version: "octagon-artifact-preview-v1" as const,
      artifact: { ref: "./deck.pptx", name: "deck.pptx", size: 100,
        media_type: "application/x", type: "presentation" as const },
      status: "ready" as const, counts: { slides: 2 },
      renderer: { name: "octagon-pptx-static", version: "1" }, error: null,
      cache_key: "sha256:sync", poll_after_ms: null, security: {}, capability_gaps: [],
      content: { kind: "presentation" as const, width: 100, height: 56, aspect_ratio: 1.78,
        truncated: false, limits: { slides: 100, elements_per_slide: 500 },
        active_content_executed: false as const,
        slides: [{ number: 1, elements: [] }, { number: 2, elements: [] }] },
    };
    preview.mockImplementation((_run, attempt: string) => Promise.resolve(
      attempt === "b"
        ? { ...descriptor, cache_key: "sha256:sync-b",
            content: { ...descriptor.content, slides: descriptor.content.slides.slice(0, 1) } }
        : descriptor,
    ));
    function Harness() {
      const [enabled, setEnabled] = useState(false);
      const [page, setPage] = useState(0);
      const [sheet, setSheet] = useState(0);
      const sync = { enabled, page, sheet, setPage, setSheet };
      const steps = [{ name: "deck.pptx", size: 100,
        type: "presentation" as const, media_type: "application/x" }];
      return <><label><input aria-label="同步" type="checkbox" checked={enabled}
        onChange={(event) => setEnabled(event.target.checked)} />同步</label>
        <ArtifactsPanel runId="run" attemptId="a" tree={rootTree(steps)} officeSync={sync} />
        <ArtifactsPanel runId="run" attemptId="b" tree={rootTree(steps)} officeSync={sync} /></>;
    }
    const { container } = render(<Harness />);
    const panels = Array.from(container.querySelectorAll(".artifacts-panel"));
    for (const panel of panels) {
      fireEvent.click(within(panel as HTMLElement).getByText("deck.pptx"));
    }
    await waitFor(() => expect(screen.getAllByLabelText("幻灯片预览")).toHaveLength(2));
    const viewers = screen.getAllByLabelText("幻灯片预览");
    fireEvent.click(within(viewers[0]).getByText("下一页"));
    expect(within(viewers[0]).getByText("2 / 2")).toBeInTheDocument();
    expect(within(viewers[1]).getByText("1 / 1")).toBeInTheDocument();

    fireEvent.click(screen.getByLabelText("同步"));
    expect(within(viewers[0]).getByText("1 / 2")).toBeInTheDocument();
    fireEvent.click(within(viewers[0]).getByText("下一页"));
    expect(within(viewers[0]).getByText("2 / 2")).toBeInTheDocument();
    expect(screen.getByText(/同步页 2 在此演示文稿中不存在/)).toBeInTheDocument();
  });

  it("resets viewer-local state when switching between files of the same type", async () => {
    const content = { kind: "presentation" as const, width: 100, height: 56, aspect_ratio: 1.78,
      truncated: false, limits: { slides: 100, elements_per_slide: 500 },
      active_content_executed: false as const,
      slides: [{ number: 1, elements: [] }, { number: 2, elements: [] }] };
    preview.mockImplementation((_run, _attempt, path: string) => Promise.resolve({
      version: "octagon-artifact-preview-v1", status: "ready", counts: { slides: path.includes("first") ? 2 : 1 },
      artifact: { ref: path, name: path, size: 1, media_type: "application/x", type: "presentation" },
      renderer: { name: "octagon-pptx-static", version: "1" }, error: null,
      cache_key: `sha256:${path}`, poll_after_ms: null, security: {}, capability_gaps: [],
      content: { ...content, slides: path.includes("first") ? content.slides : content.slides.slice(0, 1) },
    }));
    render(<ArtifactsPanel runId="run" attemptId="attempt" tree={rootTree([
      { name: "first.pptx", size: 1, type: "presentation", media_type: "application/x" },
      { name: "second.pptx", size: 1, type: "presentation", media_type: "application/x" },
    ])} />);
    fireEvent.click(screen.getByText("first.pptx"));
    await waitFor(() => expect(screen.getByText("1 / 2")).toBeInTheDocument());
    fireEvent.click(screen.getByText("下一页"));
    expect(screen.getByText("2 / 2")).toBeInTheDocument();
    fireEvent.click(screen.getByText("second.pptx"));
    await waitFor(() => expect(screen.getByText("1 / 1")).toBeInTheDocument());
    expect(screen.queryByText("2 / 1")).not.toBeInTheDocument();
  });
});

describe("HTML 产物预览（frontend-vfx 场景）", () => {
  const HTML = "<!DOCTYPE html><html><body><script>renderVolcano()</script></body></html>";

  function renderHtmlArtifact() {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true, status: 200, text: async () => HTML,
    }));
    return render(<ArtifactsPanel runId="run" attemptId="attempt" tree={rootTree([{
      name: "index.html", size: HTML.length, type: "html", media_type: "text/html",
    }])} />);
  }

  it("默认显示源码，不自动执行", async () => {
    renderHtmlArtifact();
    fireEvent.click(screen.getByText("index.html"));
    await waitFor(() => expect(screen.getByText(/renderVolcano/)).toBeInTheDocument());
    // 打开产物这个动作本身不应触发执行
    expect(document.querySelector("iframe")).toBeNull();
    expect(screen.getByRole("button", { name: "运行预览" })).toBeInTheDocument();
  });

  it("运行预览用 sandbox 隔离，且不给 allow-same-origin", async () => {
    renderHtmlArtifact();
    fireEvent.click(screen.getByText("index.html"));
    await waitFor(() => expect(screen.getByRole("button", { name: "运行预览" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "运行预览" }));

    const frame = await waitFor(() => {
      const el = document.querySelector("iframe");
      expect(el).not.toBeNull();
      return el as HTMLIFrameElement;
    });
    // 安全核心：allow-scripts 让 Three.js 跑得起来，但**不给** allow-same-origin。
    // 两者必须同时存在才有同源权限；只给前者时 iframe 是不透明源，读不到
    // Octagon 的 cookie/localStorage，也无法以当前用户身份调 /api/*。
    expect(frame.getAttribute("sandbox")).toBe("allow-scripts");
    expect(frame.getAttribute("sandbox")).not.toContain("allow-same-origin");
    // 内容经 srcDoc 注入，不是指向同源 URL 的 src
    expect(frame.getAttribute("srcdoc")).toContain("renderVolcano");
    expect(frame.getAttribute("src")).toBeNull();
  });

  it("可以切回源码视图", async () => {
    renderHtmlArtifact();
    fireEvent.click(screen.getByText("index.html"));
    await waitFor(() => expect(screen.getByRole("button", { name: "运行预览" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "运行预览" }));
    await waitFor(() => expect(document.querySelector("iframe")).not.toBeNull());
    fireEvent.click(screen.getByRole("button", { name: "查看源码" }));
    await waitFor(() => expect(document.querySelector("iframe")).toBeNull());
  });
});

describe("产物目录树", () => {
  type TreeDir = {
    name: string; path: string;
    dirs: TreeDir[];
    files: Array<{ name: string; path: string; size: number; type: "text"; media_type: string }>;
    file_count: number; total_size: number;
  };
  function dir(name: string, path: string, children: {
    dirs?: TreeDir[];
    files?: Array<{ name: string; size?: number }>;
  }): TreeDir {
    const dirs = children.dirs ?? [];
    const files = (children.files ?? []).map((f) => ({
      name: f.name, path: path ? `${path}/${f.name}` : f.name,
      size: f.size ?? 10, type: "text" as const, media_type: "text/plain",
    }));
    const file_count = files.length + dirs.reduce((sum, d) => sum + d.file_count, 0);
    const total_size = files.reduce((sum, f) => sum + f.size, 0)
      + dirs.reduce((sum, d) => sum + d.total_size, 0);
    return { name, path, dirs, files, file_count, total_size };
  }

  it("显示的是整棵子树的文件数，不是当层直属文件数", () => {
    // 回归：产物面板曾按目录的**直属**文件计数，六个交付规模迥异的工作区
    // 因此在首屏一律显示「十几个文件」。
    const tree = dir("", "", {
      dirs: [dir("backend", "backend", {
        files: [{ name: "requirements.txt" }],
        dirs: [dir("app", "backend/app", {
          dirs: [dir("routers", "backend/app/routers", {
            files: [{ name: "talent.py" }, { name: "auth.py" }],
          })],
        })],
      })],
    });
    render(<ArtifactsPanel runId="run" attemptId="attempt" tree={tree as never} />);
    expect(screen.getByText(/产物 · 3 个文件/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /backend.*3 个文件/ })).toBeInTheDocument();
  });

  it("单通道目录链折成一行，避免逐层点开", () => {
    const tree = dir("", "", {
      dirs: [dir("frontend", "frontend", {
        dirs: [dir("src", "frontend/src", {
          dirs: [dir("views", "frontend/src/views", { files: [{ name: "Login.vue" }] })],
        })],
      })],
    });
    render(<ArtifactsPanel runId="run" attemptId="attempt" tree={tree as never} />);
    expect(screen.getByText("frontend/src/views")).toBeInTheDocument();
  });

  it("默认展开到第二层，更深的层级保持折叠", () => {
    // backend 有两个子目录，不触发单通道折叠——所以 app/ 是第二层（展开），
    // routers/ 是第三层（折叠）。
    const tree = dir("", "", {
      dirs: [dir("backend", "backend", {
        dirs: [
          dir("app", "backend/app", {
            files: [{ name: "main.py" }],
            dirs: [dir("routers", "backend/app/routers", { files: [{ name: "deep.py" }] })],
          }),
          dir("alembic", "backend/alembic", { files: [{ name: "env.py" }] }),
        ],
      })],
    });
    render(<ArtifactsPanel runId="run" attemptId="attempt" tree={tree as never} />);
    expect(screen.getByText("main.py")).toBeInTheDocument();
    expect(screen.queryByText("deep.py")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /routers/ }));
    expect(screen.getByText("deep.py")).toBeInTheDocument();
  });

  it("折叠成一行的目录链只算一层深度", () => {
    // `backend/app` 折成一行后，routers/ 是它视觉上的直接子级，
    // 因此仍在自动展开范围内——这是折叠的意图，不是漏折。
    const tree = dir("", "", {
      dirs: [dir("backend", "backend", {
        dirs: [dir("app", "backend/app", {
          dirs: [dir("routers", "backend/app/routers", { files: [{ name: "deep.py" }] })],
        })],
      })],
    });
    render(<ArtifactsPanel runId="run" attemptId="attempt" tree={tree as never} />);
    // 一路单通道会折到底：backend → app → routers 合成一行。
    expect(screen.getByText("backend/app/routers")).toBeInTheDocument();
    expect(screen.getByText("deep.py")).toBeInTheDocument();
  });

  it("嵌套文件按完整路径下载，不是拼出来的目录名", () => {
    const tree = dir("", "", {
      dirs: [dir("backend", "backend", {
        dirs: [dir("app", "backend/app", { files: [{ name: "main.py" }] })],
      })],
    });
    render(<ArtifactsPanel runId="run" attemptId="attempt" tree={tree as never} />);
    fireEvent.click(screen.getByText("main.py"));
    expect(screen.getByRole("link", { name: "下载原文件" }))
      .toHaveAttribute("href", "/download/backend/app/main.py");
  });
});
