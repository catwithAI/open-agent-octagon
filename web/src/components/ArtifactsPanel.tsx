import { useEffect, useRef, useState } from "react";

import {
  api,
  type ArtifactDir,
  type ArtifactFile,
  type ArtifactPreviewDescriptor,
  type ArtifactType,
} from "../api/client";
import { useI18n } from "../i18n";
import {
  DocumentViewer,
  HtmlArtifactView,
  PresentationViewer,
  WorkbookViewer,
  type OfficeSyncState,
} from "./viewers/ArtifactViewers";

// 默认展开到第二层：一级目录（backend/frontend）几乎总是脚手架名字，
// 展开一层才看得到 app/、src/ 这种真正区分实现的层级；再深就淹没了。
const ARTIFACT_AUTO_EXPAND_DEPTH = 2;

// 只有一个子目录、且自己没有文件的目录链会折成 `backend/app/routers` 一行。
// 全栈脚手架里这种单通道链很常见，逐层点开只是徒增点击。
function collapseChain(dir: ArtifactDir): { labels: string[]; node: ArtifactDir } {
  const labels = [dir.name];
  let node = dir;
  while (node.files.length === 0 && node.dirs.length === 1) {
    node = node.dirs[0];
    labels.push(node.name);
  }
  return { labels, node };
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} K`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} M`;
}

function ArtifactTreeNode({ dir, depth, expanded, toggle, onOpenFile, fileIcon, activePath }: {
  dir: ArtifactDir;
  depth: number;
  expanded: Set<string>;
  toggle: (path: string) => void;
  onOpenFile: (file: ArtifactFile) => void;
  fileIcon: (type: ArtifactType) => string;
  activePath: string | null;
}) {
  const { t } = useI18n();
  const { labels, node } = collapseChain(dir);
  const open = expanded.has(dir.path);
  return (
    <div className="artifact-node">
      <button
        className="artifact-dir-row"
        style={{ paddingLeft: 8 + depth * 14 }}
        onClick={() => toggle(dir.path)}
        aria-expanded={open}
      >
        <span className="ev-chevron">{open ? "▼" : "▶"}</span>
        <span className="artifact-dir-name">{labels.join("/")}</span>
        <span className="artifact-dir-meta">
          {t("runDetail.artifact.dirMeta", { n: node.file_count, size: formatBytes(node.total_size) })}
        </span>
      </button>
      {open && (
        <>
          {node.dirs.map((child) => (
            <ArtifactTreeNode
              key={child.path} dir={child} depth={depth + 1}
              expanded={expanded} toggle={toggle} onOpenFile={onOpenFile}
              fileIcon={fileIcon} activePath={activePath}
            />
          ))}
          {node.files.map((file) => (
            <button
              key={file.path}
              className={`artifact-file${activePath === file.path ? " is-active" : ""}`}
              style={{ paddingLeft: 8 + (depth + 1) * 14 }}
              onClick={() => onOpenFile(file)}
            >
              <span className="artifact-file-icon">{fileIcon(file.type)}</span>
              <span className="artifact-file-name">{file.name}</span>
              <span className="artifact-file-size">{formatBytes(file.size)}</span>
            </button>
          ))}
        </>
      )}
    </div>
  );
}

export function ArtifactsPanel({ tree, runId, attemptId, officeSync }: { tree: ArtifactDir | null; runId: string; attemptId: string; officeSync?: OfficeSyncState }) {
  const { t } = useI18n();
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [preview, setPreview] = useState<{
    path: string;
    type: ArtifactType;
    state: "loading" | "ready" | "error";
    content?: string;
    descriptor?: ArtifactPreviewDescriptor;
    error?: string;
  } | null>(null);
  const previewRequest = useRef<AbortController | null>(null);
  const initializedAttemptRef = useRef<string | null>(null);

  useEffect(() => () => { previewRequest.current?.abort(); }, []);

  // 换 attempt 时重置展开状态并中止旧预览请求；同一个 attempt 的 SSE 刷新
  // （tree 引用变化但 attemptId 不变）绝不折叠用户手动展开的目录。
  useEffect(() => {
    const attemptKey = `${runId}:${attemptId}`;
    if (initializedAttemptRef.current === attemptKey) return;
    previewRequest.current?.abort();
    previewRequest.current = null;
    setPreview(null);
    if (!tree) {
      initializedAttemptRef.current = null;
      setExpanded(new Set());
      return;
    }
    initializedAttemptRef.current = attemptKey;
    const initial = new Set<string>();
    // 按**折叠后**的视图计深度：collapseChain 把单通道链合成一行，展开状态
    // 存的是链首的 path，跟着原始层级走会往集合里塞永远点不到的中间节点，
    // 而真正的第二层反而没展开。
    const walk = (dir: ArtifactDir, depth: number) => {
      if (depth >= ARTIFACT_AUTO_EXPAND_DEPTH) return;
      for (const child of dir.dirs) {
        initial.add(child.path);
        walk(collapseChain(child).node, depth + 1);
      }
    };
    walk(tree, 0);
    setExpanded(initial);
  }, [runId, attemptId, tree]);

  if (!tree || tree.file_count === 0) return null;
  const waitForPoll = (milliseconds: number, signal: AbortSignal) => new Promise<void>((resolve, reject) => {
    const onAbort = () => {
      window.clearTimeout(timer);
      reject(new DOMException("aborted", "AbortError"));
    };
    const timer = window.setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, Math.max(10, Math.min(milliseconds, 5000)));
    if (signal.aborted) onAbort();
    else signal.addEventListener("abort", onAbort, { once: true });
  });
  const loadPreview = async (file: ArtifactFile) => {
    const path = file.path;
    previewRequest.current?.abort();
    const controller = new AbortController();
    previewRequest.current = controller;
    if (file.type === "image" || file.type === "video" || file.type === "audio") {
      setPreview({ path, type: file.type, state: "ready" });
      return;
    }
    setPreview({ path, type: file.type, state: "loading" });
    try {
      if (file.type === "text" || file.type === "html") {
        // html 与 text 走同一条取内容路径——后端一律以纯文本返回（绝不发
        // text/html，否则直接访问 URL 就是同源页面）。渲染由前端的
        // `<iframe sandbox srcdoc>` 负责，见 HtmlArtifactView。
        const resp = await fetch(api.artifactUrl(runId, attemptId, path), { signal: controller.signal });
        if (!resp.ok) throw new Error(`artifact -> ${resp.status}`);
        const content = await resp.text();
        if (!controller.signal.aborted) setPreview({ path, type: file.type, state: "ready", content });
      } else {
        for (let poll = 0; poll < 120; poll++) {
          const descriptor = await api.getArtifactPreview(runId, attemptId, path, controller.signal);
          if (controller.signal.aborted) return;
          setPreview({ path, type: file.type, state: "ready", descriptor });
          if (descriptor.status !== "rendering") return;
          await waitForPoll(descriptor.poll_after_ms ?? 500, controller.signal);
        }
        throw new Error(t("runDetail.artifact.previewTimeout"));
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        setPreview({ path, type: file.type, state: "error", error: error instanceof Error ? error.message : t("runDetail.artifact.previewFailed") });
      }
    }
  };
  const fileIcon = (type: ArtifactType) => ({
    image: "🖼", video: "🎬", audio: "🔊", presentation: "📊",
    document: "📝", spreadsheet: "📈", html: "🌐", text: "📄", binary: "📦",
  })[type];
  const toggle = (path: string) => setExpanded((current) => {
    const next = new Set(current);
    if (next.has(path)) next.delete(path);
    else next.add(path);
    return next;
  });
  return (
    <div className="artifacts-panel">
      <div className="artifacts-header">
        {t("runDetail.artifact.panelHeader", { n: tree.file_count, size: formatBytes(tree.total_size) })}
        {tree.truncated && <span className="artifact-truncated">{t("runDetail.artifact.truncated")}</span>}
      </div>
      <div className="artifact-tree">
        {tree.dirs.map((child) => (
          <ArtifactTreeNode
            key={child.path} dir={child} depth={0}
            expanded={expanded} toggle={toggle} onOpenFile={loadPreview}
            fileIcon={fileIcon} activePath={preview?.path ?? null}
          />
        ))}
        {/* 根目录直属文件排在目录之后：README 这类总是少数，先看结构再看散文件 */}
        {tree.files.map((file) => (
          <button
            key={file.path}
            className={`artifact-file${preview?.path === file.path ? " is-active" : ""}`}
            style={{ paddingLeft: 8 }}
            onClick={() => loadPreview(file)}
          >
            <span className="artifact-file-icon">{fileIcon(file.type)}</span>
            <span className="artifact-file-name">{file.name}</span>
            <span className="artifact-file-size">{formatBytes(file.size)}</span>
          </button>
        ))}
      </div>
      {preview && (
        <div className="artifact-preview">
          <div className="artifact-preview-header">
            <span>{preview.path}</span>
            <span className="artifact-preview-actions">
              <a href={api.artifactUrl(runId, attemptId, preview.path)} download>{t("runDetail.artifact.download")}</a>
              <span className="artifact-close" onClick={() => { previewRequest.current?.abort(); setPreview(null); }}>✕</span>
            </span>
          </div>
          {preview.state === "loading" && <div className="artifact-preview-state">{t("runDetail.artifact.checking")}</div>}
          {preview.state === "error" && <div className="artifact-preview-state artifact-preview-error">{preview.error}</div>}
          {preview.state === "ready" && preview.type === "image" && (
            <img src={api.artifactUrl(runId, attemptId, preview.path)} alt={preview.path} style={{ maxWidth: "100%" }} />
          )}
          {preview.state === "ready" && preview.type === "video" && (
            <video src={api.artifactUrl(runId, attemptId, preview.path)} controls style={{ maxWidth: "100%" }} />
          )}
          {preview.state === "ready" && preview.type === "audio" && (
            <audio src={api.artifactUrl(runId, attemptId, preview.path)} controls style={{ width: "100%" }} />
          )}
          {preview.state === "ready" && preview.type === "text" && (
            <pre>{preview.content}</pre>
          )}
          {preview.state === "ready" && preview.type === "html" && (
            <HtmlArtifactView html={preview.content ?? ""} />
          )}
          {preview.state === "ready" && preview.descriptor?.status === "rendering" && (
            <div className="artifact-preview-state" role="status">
              <strong>{t("runDetail.artifact.renderingTitle")}</strong>
              <div className="muted">{t("runDetail.artifact.renderingNote")}</div>
            </div>
          )}
          {preview.state === "ready" && preview.descriptor?.content?.kind === "workbook" && (
            <WorkbookViewer key={`${preview.path}:${preview.descriptor.cache_key}`}
              workbook={preview.descriptor.content} officeSync={officeSync} />
          )}
          {preview.state === "ready" && preview.descriptor?.content?.kind === "presentation" && (
            <PresentationViewer key={`${preview.path}:${preview.descriptor.cache_key}`}
              presentation={preview.descriptor.content} officeSync={officeSync} />
          )}
          {preview.state === "ready" && preview.descriptor?.content?.kind === "document" && (
            <DocumentViewer key={`${preview.path}:${preview.descriptor.cache_key}`}
              document={preview.descriptor.content} />
          )}
          {preview.state === "ready" && preview.descriptor?.content &&
            preview.descriptor.capability_gaps.length > 0 && (
            <div className="artifact-capability-gaps" role="note">
              {t("runDetail.artifact.capabilityGaps", { gaps: preview.descriptor.capability_gaps.join(t("runDetail.common.listSep")) })}
            </div>
          )}
          {preview.state === "ready" && preview.descriptor &&
            preview.descriptor.status !== "rendering" && !preview.descriptor.content && (
            <div className="artifact-preview-state">
              <strong>{preview.descriptor.status === "failed" ? t("runDetail.artifact.securityFailed") : t("runDetail.artifact.previewNotWired")}</strong>
              {preview.descriptor.error?.message && <div>{preview.descriptor.error.message}</div>}
              <div className="muted">
                {preview.descriptor.artifact.media_type}
                {preview.descriptor.counts.slides != null && t("runDetail.artifact.slidesCount", { n: preview.descriptor.counts.slides })}
                {preview.descriptor.counts.sheets != null && t("runDetail.artifact.sheetsCount", { n: preview.descriptor.counts.sheets })}
              </div>
              {preview.descriptor.capability_gaps.length > 0 && (
                <div className="artifact-capability-gaps">{t("runDetail.artifact.limitedCapabilities", { gaps: preview.descriptor.capability_gaps.join(t("runDetail.common.listSep")) })}</div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
