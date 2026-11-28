import React, { useEffect, useRef, useState } from "react";

import type {
  DocumentBlock,
  DocumentPreview,
  PresentationPreview,
  WorkbookPreview,
} from "../../api/client";
import { useI18n } from "../../i18n";

function spreadsheetColumnLabel(column: number): string {
  let current = column;
  let label = "";
  while (current > 0) {
    current -= 1;
    label = String.fromCharCode(65 + (current % 26)) + label;
    current = Math.floor(current / 26);
  }
  return label;
}

export type OfficeSyncState = {
  enabled: boolean;
  page: number;
  sheet: number;
  setPage: (page: number) => void;
  setSheet: (sheet: number) => void;
};

export function PresentationViewer({ presentation, officeSync }: { presentation: PresentationPreview; officeSync?: OfficeSyncState }) {
  const { t } = useI18n();
  const [slideIndex, setSlideIndex] = useState(0);
  const [zoom, setZoom] = useState(100);
  const rootRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (officeSync?.enabled)
      setSlideIndex(Math.max(officeSync.page, 0));
  }, [officeSync?.enabled, officeSync?.page]);
  const slide = presentation.slides[slideIndex];
  if (!slide) return <div className="artifact-preview-state">
    {presentation.slides.length === 0
      ? t("runDetail.pptx.noSlides")
      : t("runDetail.pptx.syncMissing", { index: slideIndex + 1, total: presentation.slides.length })}
  </div>;
  const renderElement = (element: typeof slide.elements[number], index: number, thumbnail = false) => {
    const style = {
      left: `${element.x * 100}%`, top: `${element.y * 100}%`,
      width: `${element.width * 100}%`, height: `${element.height * 100}%`,
      backgroundColor: element.fill ?? undefined,
      fontSize: element.font_size && !thumbnail ? `${Math.max(8, element.font_size * .75)}px` : undefined,
    };
    if (element.kind === "image" && element.data_uri)
      return <img key={index} className="pptx-element pptx-image" style={style} src={element.data_uri} alt="" />;
    if (element.kind === "table")
      return <table key={index} className="pptx-element pptx-table" style={style}><tbody>
        {(element.rows ?? []).map((row, ri) => <tr key={ri}>{row.map((cell, ci) => <td key={ci}>{cell}</td>)}</tr>)}
      </tbody></table>;
    return <div key={index} className={`pptx-element pptx-text${element.role === "title" ? " is-title" : ""}`}
      style={style}>{element.text}</div>;
  };
  const renderSlide = (item: typeof slide, thumbnail = false) => item.rendered_image_data_uri
    ? <img className="pptx-rendered-slide" src={item.rendered_image_data_uri} alt={t("runDetail.pptx.slideAlt", { n: item.number })} />
    : item.elements.map((element, index) => renderElement(element, index, thumbnail));
  const selectSlide = (index: number) => {
    const bounded = Math.min(Math.max(index, 0), presentation.slides.length - 1);
    setSlideIndex(bounded);
    if (officeSync?.enabled) officeSync.setPage(bounded);
  };
  return <div className="pptx-viewer" ref={rootRef} aria-label={t("runDetail.pptx.viewerAria")}>
    <div className="pptx-toolbar">
      <button onClick={() => selectSlide(slideIndex - 1)} disabled={slideIndex === 0}>{t("runDetail.pptx.prev")}</button>
      <span>{slideIndex + 1} / {presentation.slides.length}</span>
      <button onClick={() => selectSlide(slideIndex + 1)}
        disabled={slideIndex >= presentation.slides.length - 1}>{t("runDetail.pptx.next")}</button>
      <button onClick={() => setZoom((value) => Math.max(50, value - 10))}>{t("runDetail.pptx.zoomOut")}</button>
      <span>{zoom}%</span><button onClick={() => setZoom((value) => Math.min(180, value + 10))}>{t("runDetail.pptx.zoomIn")}</button>
      <button onClick={() => setZoom(100)}>{t("runDetail.pptx.fit")}</button>
      <button onClick={() => rootRef.current?.requestFullscreen?.()}>{t("runDetail.pptx.fullscreen")}</button>
    </div>
    {presentation.truncated && <div className="xlsx-warning">{t("runDetail.pptx.truncated")}</div>}
    <div className="pptx-layout">
      <div className="pptx-thumbnails" role="tablist" aria-label={t("runDetail.pptx.thumbsAria")}>
        {presentation.slides.map((item, index) => <button key={item.number} role="tab"
          aria-selected={index === slideIndex} className={index === slideIndex ? "is-active" : ""}
          onClick={() => selectSlide(index)}>
          <span>{item.number}</span><div className="pptx-thumb-canvas" style={{ aspectRatio: presentation.aspect_ratio }}>
            {item.rendered_image_data_uri
              ? renderSlide(item, true)
              : item.elements.filter((element) => element.kind === "text").slice(0, 8)
                .map((element, ei) => renderElement(element, ei, true))}
          </div>
        </button>)}
      </div>
      <div className="pptx-stage-scroll"><div className="pptx-stage" style={{
        aspectRatio: presentation.aspect_ratio,
        width: `${zoom}%`,
      }}>{renderSlide(slide)}</div></div>
    </div>
    {slide.notes && <details className="pptx-notes"><summary>{t("runDetail.pptx.notes")}</summary><pre>{slide.notes}</pre></details>}
  </div>;
}

export function DocumentViewer({ document }: { document: DocumentPreview }) {
  const { t } = useI18n();
  const [query, setQuery] = useState("");
  const [zoom, setZoom] = useState(100);
  const normalized = query.trim().toLocaleLowerCase();
  const blockMatches = (block: DocumentBlock): boolean => {
    if (block.kind === "paragraph") return block.text.toLocaleLowerCase().includes(normalized);
    return block.rows.some((row) => row.some((cell) => cell.text.toLocaleLowerCase().includes(normalized)));
  };
  const matches = normalized ? document.blocks.filter(blockMatches).length : 0;
  const renderBlock = (block: DocumentBlock, index: number): React.ReactNode => {
    const matching = normalized && blockMatches(block);
    if (block.kind === "table") return <table key={index} className={matching ? "docx-match" : ""}><tbody>
      {block.rows.map((row, ri) => <tr key={ri}>{row.map((cell, ci) => <td key={ci}>{cell.text}</td>)}</tr>)}
    </tbody></table>;
    const content = block.runs.length ? block.runs.map((run, ri) => {
      const node = <span style={{ fontWeight: run.bold ? 700 : undefined, fontStyle: run.italic ? "italic" : undefined }}>{run.text}</span>;
      return run.href ? <a key={ri} href={run.href} target="_blank" rel="noreferrer noopener">{node}</a> : <span key={ri}>{node}</span>;
    }) : block.text;
    const className = `${block.list_item ? "docx-list-item " : ""}${matching ? "docx-match" : ""}`.trim();
    if (block.heading) {
      const Tag = `h${Math.max(1, Math.min(6, block.heading))}` as keyof React.JSX.IntrinsicElements;
      return <Tag key={index} className={className}>{content}</Tag>;
    }
    return <p key={index} className={className}>{content || <br />}</p>;
  };
  return <div className="docx-viewer" aria-label={t("runDetail.docx.viewerAria")}>
    <div className="docx-toolbar">
      <label>{t("runDetail.docx.search")} <input placeholder={t("runDetail.docx.searchPlaceholder")} value={query} onChange={(event) => setQuery(event.target.value)} /></label>
      {normalized && <span>{t("runDetail.docx.matches", { n: matches })}</span>}
      <button onClick={() => setZoom((value) => Math.max(60, value - 10))}>{t("runDetail.xlsx.zoomOut")}</button>
      <span>{zoom}%</span><button onClick={() => setZoom((value) => Math.min(180, value + 10))}>{t("runDetail.xlsx.zoomIn")}</button>
    </div>
    {(document.truncated || document.images_omitted > 0) && <div className="xlsx-warning">
      {document.truncated && t("runDetail.docx.truncated")}
      {document.images_omitted > 0 && t("runDetail.docx.imagesOmitted", { n: document.images_omitted })}
    </div>}
    <article className="docx-page" style={{ fontSize: `${zoom}%`,
      aspectRatio: document.page.width_pt && document.page.height_pt
        ? document.page.width_pt / document.page.height_pt : undefined }}>
      {(document.headers?.length ?? 0) > 0 && <header className="docx-header">{document.headers?.map(renderBlock)}</header>}
      {document.blocks.map(renderBlock)}
      {(document.footers?.length ?? 0) > 0 && <footer className="docx-footer">{document.footers?.map(renderBlock)}</footer>}
    </article>
  </div>;
}

export function WorkbookViewer({ workbook, officeSync }: { workbook: WorkbookPreview; officeSync?: OfficeSyncState }) {
  const { t } = useI18n();
  const [sheetIndex, setSheetIndex] = useState(0);
  const [query, setQuery] = useState("");
  const [zoom, setZoom] = useState(100);
  const [scrollTop, setScrollTop] = useState(0);
  const gridRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (officeSync?.enabled)
      setSheetIndex(Math.max(officeSync.sheet, 0));
  }, [officeSync?.enabled, officeSync?.sheet]);
  const sheet = workbook.sheets[sheetIndex];
  if (!sheet) return <div className="artifact-preview-state">
    {workbook.sheets.length === 0
      ? t("runDetail.xlsx.noSheets")
      : t("runDetail.xlsx.syncMissing", { index: sheetIndex + 1, total: workbook.sheets.length })}
  </div>;

  const normalizedQuery = query.trim().toLocaleLowerCase();
  const rows = normalizedQuery
    ? sheet.rows.filter((row) => row.cells.some((cell) =>
      String(cell.value ?? "").toLocaleLowerCase().includes(normalizedQuery)
      || String(cell.formula ?? "").toLocaleLowerCase().includes(normalizedQuery)))
    : sheet.rows;
  const maxColumn = Math.max(1, ...sheet.rows.flatMap((row) => row.cells.map((cell) => cell.column)));
  const rowHeight = 28;
  const virtualized = rows.length > 100;
  const virtualStart = virtualized ? Math.max(0, Math.floor(scrollTop / rowHeight) - 8) : 0;
  const virtualEnd = virtualized ? Math.min(rows.length, virtualStart + 36) : rows.length;
  const visibleRows = rows.slice(virtualStart, virtualEnd);
  const cellByPosition = new Map<string, typeof sheet.rows[number]["cells"][number]>();
  rows.forEach((row) => row.cells.forEach((cell) => cellByPosition.set(`${row.index}:${cell.column}`, cell)));
  const displayValue = (value: string | number | boolean | null) => {
    if (value === null) return <span className="xlsx-empty">∅</span>;
    if (typeof value === "boolean") return value ? "TRUE" : "FALSE";
    return String(value);
  };

  return (
    <div className="xlsx-viewer" aria-label={t("runDetail.xlsx.viewerAria")}>
      <div className="xlsx-toolbar">
        <div className="xlsx-tabs" role="tablist" aria-label={t("runDetail.xlsx.sheetsAria")}>
          {workbook.sheets.map((item, index) => (
            <button key={`${item.name}-${index}`} role="tab" aria-selected={index === sheetIndex}
              className={index === sheetIndex ? "is-active" : ""}
              onClick={() => { setSheetIndex(index); if (officeSync?.enabled) officeSync.setSheet(index); setQuery(""); setScrollTop(0); if (gridRef.current) gridRef.current.scrollTop = 0; }}>
              {item.name}
            </button>
          ))}
        </div>
        <label className="xlsx-search">{t("runDetail.xlsx.search")}
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={t("runDetail.xlsx.searchPlaceholder")} />
        </label>
        <div className="xlsx-zoom" aria-label={t("runDetail.xlsx.zoomAria")}>
          <button onClick={() => setZoom((value) => Math.max(50, value - 10))} aria-label={t("runDetail.xlsx.zoomOut")}>−</button>
          <span>{zoom}%</span>
          <button onClick={() => setZoom((value) => Math.min(180, value + 10))} aria-label={t("runDetail.xlsx.zoomIn")}>＋</button>
        </div>
      </div>
      {(workbook.truncated || sheet.truncated) && (
        <div className="xlsx-warning" role="status">
          {t("runDetail.xlsx.truncated")}
        </div>
      )}
      <div className="xlsx-meta">
        {sheet.dimension && <span>{t("runDetail.xlsx.range", { dim: sheet.dimension })}</span>}
        {sheet.frozen && <span>{t("runDetail.xlsx.frozen", { cell: sheet.frozen.top_left_cell ?? t("runDetail.xlsx.frozenEnabled") })}</span>}
        {sheet.merges.length > 0 && <span>{t("runDetail.xlsx.merges", { n: sheet.merges.length })}</span>}
        <span>{t("runDetail.xlsx.formulaNote")}</span>
      </div>
      <div className="xlsx-grid-scroll" ref={gridRef} onScroll={(event) => setScrollTop(event.currentTarget.scrollTop)}>
        <table className="xlsx-grid" style={{ fontSize: `${zoom}%` }}>
          <thead><tr><th className="xlsx-corner" />
            {Array.from({ length: maxColumn }, (_, index) => (
              <th key={index + 1}>{spreadsheetColumnLabel(index + 1)}</th>
            ))}
          </tr></thead>
          <tbody>
            {virtualized && virtualStart > 0 && <tr className="xlsx-spacer" aria-hidden="true">
              <td colSpan={maxColumn + 1} style={{ height: virtualStart * rowHeight }} />
            </tr>}
            {visibleRows.map((row) => (
              <tr key={row.index} className={row.hidden ? "xlsx-hidden-row" : ""}>
                <th>{row.index}</th>
                {Array.from({ length: maxColumn }, (_, index) => {
                  const cell = cellByPosition.get(`${row.index}:${index + 1}`);
                  return <td key={index + 1} title={cell?.formula ? `=${cell.formula}` : cell?.number_format ?? undefined}>
                    {cell ? displayValue(cell.display_value ?? cell.value) : <span className="xlsx-empty">∅</span>}
                    {cell?.formula && <span className="xlsx-formula">={cell.formula}</span>}
                  </td>;
                })}
              </tr>
            ))}
            {virtualized && virtualEnd < rows.length && <tr className="xlsx-spacer" aria-hidden="true">
              <td colSpan={maxColumn + 1} style={{ height: (rows.length - virtualEnd) * rowHeight }} />
            </tr>}
          </tbody>
        </table>
        {rows.length === 0 && <div className="artifact-preview-state">{t("runDetail.xlsx.noMatch")}</div>}
      </div>
    </div>
  );
}

// HTML 产物预览（frontend-vfx-* 这类场景的产物必须执行才看得出效果）。
//
// **安全模型**：产物是被测 agent 生成的不可信内容。这里用
// `sandbox="allow-scripts"` 且**刻意不给** `allow-same-origin`——两者必须同时
// 存在才能拿到同源权限，只给前者时 iframe 是独立的不透明源：脚本能跑
// （Three.js 需要），但读不到 Octagon 的 cookie/localStorage，也无法以当前
// 用户身份调 /api/*（删 run、读别的 attempt 产物）。
//
// 默认显示源码、点按钮才运行：与本页"默认显示原始数据"的取向一致，不让
// 打开产物这个动作本身就触发执行。
export function HtmlArtifactView({ html }: { html: string }) {
  const { t } = useI18n();
  const [running, setRunning] = useState(false);
  return (
    <div className="html-artifact">
      <div className="html-artifact-bar">
        <button className="tab" onClick={() => setRunning((v) => !v)}>
          {running ? t("runDetail.html.viewSource") : t("runDetail.html.runPreview")}
        </button>
        <span className="muted">
          {running
            ? t("runDetail.html.sandboxNote")
            : t("runDetail.html.chars", { n: html.length.toLocaleString() })}
        </span>
      </div>
      {running
        ? <iframe
            className="html-artifact-frame"
            title={t("runDetail.html.frameTitle")}
            sandbox="allow-scripts"
            srcDoc={html}
          />
        : <pre>{html}</pre>}
    </div>
  );
}
