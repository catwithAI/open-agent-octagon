"""从 attempt 日志里还原「agent 编辑过哪些文件」。

blade-agent 在 2026-09-18 的横评里 27 个 attempt 全部拿到形状一致的低分。
事后确认那不是能力问题：产物没被回收回来，scorer 看到的是未改动的基线。

根因是 BA 的工具调用**分片流式**下发——`{"function":{"name":"Write"}}` 与
`{"arguments":"{\\"file_path\\":\\"...\\"}"}` 落在不同事件行里。任何「同一行
既要有工具名、又要有路径」的提取方式都会几乎全空（实测一个 attempt：15883
行带编辑标记，只提取出 2 个路径）。

所以这里不按「工具名 + 路径同现」提取，而是三个来源取并集：

1. `events.jsonl`：全文件扫 `file_path` / `path`，不要求同行有工具名；
2. `trace.jsonl`：结构化的 `arguments`，比正则可靠；
3. mtime：物料拷贝进来时保留源仓库时间戳（常差几天），agent 的编辑集中在
   attempt 运行那几分钟，按时间窗口就能分开。

宁可多拉几个文件，也不能漏掉真正的交付物——多下载一个文件的成本是几十 KB，
漏掉一个交付物的成本是这个 attempt 的分数整个作废。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# 值得回收的后缀。Read 的入参也会出现在事件里，用后缀白名单把明显不是
# 交付物的东西挡掉（日志、锁文件、二进制缓存）。
DELIVERABLE_SUFFIXES = frozenset(
    {
        # 源码
        ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs", ".c", ".h",
        ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift", ".kt", ".scala", ".sh",
        ".sql", ".r", ".m", ".lua", ".pl",
        # 标记/配置
        ".md", ".rst", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini",
        ".cfg", ".xml", ".html", ".htm", ".css", ".scss", ".env",
        # 文档/表格/演示
        ".csv", ".tsv", ".xlsx", ".xls", ".docx", ".doc", ".pptx", ".ppt",
        ".pdf", ".odt", ".ods", ".odp",
        # 数据
        ".db", ".sqlite", ".sqlite3", ".parquet", ".ipynb",
        # 图像（产物型场景的交付物）
        ".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp",
    }
)

# 运行时内部目录：出现在路径里就不是交付物。
_SKIP_PARTS = frozenset(
    {
        "node_modules", ".venv", "venv", "vendor", "__pycache__", ".git",
        ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache", "dist",
        "build", ".next", "target", ".agents", ".blade", ".octagon",
        "site-packages", ".tox",
    }
)

# 事件行里的路径字段。不要求同一行出现工具名——这正是旧口径失效的地方。
#
# 引号可能被转义任意层：arguments 本身常是「JSON 字符串里再套一层 JSON」，
# 于是同一个字段在原始行里可能写成 `"file_path"`、`\"file_path\"`，甚至
# `\\\"file_path\\\"`。`\\*` 吞掉任意层转义反斜杠，值部分同理。
_ESC = r'\\*"'
_PATH_KEY_RE = re.compile(
    _ESC
    + r"(?:file_path|filePath|path|notebook_path|target_file)"
    + _ESC
    + r"\s*:\s*"
    + _ESC
    + r"((?:[^\"\\]|\\.)*?)"
    + _ESC
)

_MAX_PATH_LEN = 512


_SESSION_DIR_RE = re.compile(
    r"^(?:sess|session|att|attempt|run|task|job)[-_][A-Za-z0-9-]{6,}$|^[0-9a-f]{12,}$",
    re.IGNORECASE,
)


def _looks_like_session_dir(name: str) -> bool:
    """`sess_abc123` / 一长串 hex 这类按次生成的目录名。

    判据保守：宁可留着一层前缀（候选路径里还有不带前缀的写法兜底），
    也不要把 `django`、`src` 这种真实目录误删。
    """
    return bool(_SESSION_DIR_RE.match(name))


def _unescape(raw: str) -> str:
    """把捕获到的字面量还原成真实路径。

    嵌套的 JSON 会留下任意层反斜杠（`a\\\\/b`、`a\\\\\\\\/b`）。逐层用 JSON
    解一次，解不动为止——比手写替换更稳。
    """
    text = raw
    for _ in range(4):
        if "\\" not in text:
            break
        try:
            decoded = json.loads(f'"{text}"')
        except (json.JSONDecodeError, ValueError):
            break
        if decoded == text:
            break
        text = decoded
    return text.replace("\\/", "/").replace("\\", "/") if "\\" in text else text


def _looks_like_deliverable(raw: str) -> bool:
    if not raw or len(raw) > _MAX_PATH_LEN:
        return False
    if "\n" in raw or "\x00" in raw:
        return False
    suffix = Path(raw).suffix.lower()
    return suffix in DELIVERABLE_SUFFIXES


def normalize_path(raw: str, *, workspace_root: str = "") -> str | None:
    """把日志里的路径归一成 workspace 内的相对路径。

    绝对路径不再直接丢弃——BA 记的就是容器内绝对路径，一丢就全空了。
    以 workspace_root 为前缀的剥掉前缀；剥不掉但形似交付物的，退而取其
    相对部分。逃逸（`..`）一律拒绝：这些字符串最终要拼进下载落地路径。
    """
    text = (raw or "").strip().replace("\\/", "/")
    if not text:
        return None
    # 去掉 file:// 之类的协议前缀
    if text.startswith("file://"):
        text = text[len("file://") :]

    if workspace_root:
        root = workspace_root.rstrip("/")
        if text == root:
            return None
        if text.startswith(root + "/"):
            text = text[len(root) + 1 :]

    if text.startswith("/"):
        # 仍是绝对路径：blade 的工作区根不止一种写法（/workspace、
        # /home/agent/workspace…）。取已知工作区标记之后的部分，取不到就
        # 退回最后两段——足以在远端按候选前缀试探。
        parts = [p for p in text.split("/") if p]
        # blade 的工作区是 `/root/智能助手工作空间/<session>/...`；其余 agent
        # 多用 workspace / skill_workspace。标记之后的部分才是工作区内相对路径。
        for marker in ("skill_workspace", "workspace", "智能助手工作空间", "work", "agent"):
            if marker in parts:
                idx = len(parts) - 1 - parts[::-1].index(marker)
                tail = parts[idx + 1 :]
                # blade 在工作区下再按 session 开一层目录，那一层不属于
                # 工作区内的相对路径，带上它远端一定找不到。
                if len(tail) > 1 and _looks_like_session_dir(tail[0]):
                    tail = tail[1:]
                if tail:
                    text = "/".join(tail)
                    break
        else:
            text = "/".join(parts[-2:]) if len(parts) >= 2 else parts[-1]

    # 逃逸检查必须在剥前缀**之前**：`lstrip("./")` 按字符集剥，会把
    # `../../etc/x.py` 直接削成 `etc/x.py`，逃逸痕迹就此消失。
    if any(part == ".." for part in Path(text).parts):
        return None
    # 归一成干净的相对路径：去掉空段与 `.` 段。这个字符串最终要拼进落地
    # 路径与远端请求，`a/./b` 与 `a//b` 会变成两个不同的 key 却指同一文件。
    parts = tuple(p for p in text.split("/") if p not in ("", "."))
    if not parts:
        return None
    text = "/".join(parts)
    if any(part in _SKIP_PARTS for part in parts):
        return None
    if not _looks_like_deliverable(text):
        return None
    return text


def paths_from_events(
    attempt_dir: Path, *, workspace_root: str = "", limit: int = 400
) -> list[str]:
    """全文件扫 events.jsonl 里的路径字段，不要求同行带工具名。"""
    events_path = Path(attempt_dir) / "events.jsonl"
    if not events_path.is_file():
        return []
    found: dict[str, None] = {}
    try:
        with events_path.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                for match in _PATH_KEY_RE.finditer(line):
                    normalized = normalize_path(
                        _unescape(match.group(1)), workspace_root=workspace_root
                    )
                    if normalized:
                        found.setdefault(normalized, None)
                if len(found) >= limit:
                    break
    except OSError:
        return list(found)
    return list(found)


def _walk_arguments(node: Any, out: dict[str, None], workspace_root: str) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("file_path", "filePath", "path", "notebook_path", "target_file") and isinstance(
                value, str
            ):
                normalized = normalize_path(value, workspace_root=workspace_root)
                if normalized:
                    out.setdefault(normalized, None)
            else:
                _walk_arguments(value, out, workspace_root)
    elif isinstance(node, list):
        for item in node:
            _walk_arguments(item, out, workspace_root)
    elif isinstance(node, str) and node.lstrip().startswith("{"):
        # arguments 常被序列化成字符串再塞进行里，解开再看一层。
        try:
            _walk_arguments(json.loads(node), out, workspace_root)
        except json.JSONDecodeError:
            return


def paths_from_trace(
    attempt_dir: Path, *, workspace_root: str = "", limit: int = 400
) -> list[str]:
    """从 trace.jsonl 的结构化 arguments 取路径——比正则可靠。"""
    trace_path = Path(attempt_dir) / "trace.jsonl"
    if not trace_path.is_file():
        return []
    found: dict[str, None] = {}
    try:
        with trace_path.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                _walk_arguments(row, found, workspace_root)
                if len(found) >= limit:
                    break
    except OSError:
        return list(found)
    return list(found)


def paths_from_mtime(
    attempt_dir: Path, *, window_seconds: float = 0.0, limit: int = 400
) -> list[str]:
    """本地 workspace 里明显晚于物料拷贝时间的文件。

    物料从源仓库拷进来时保留原始 mtime（常差几天）；agent 的编辑发生在
    attempt 运行那几分钟。取最新一批即可把两者分开。
    """
    workspace = Path(attempt_dir) / "skill_workspace"
    if not workspace.is_dir():
        return []
    entries: list[tuple[float, str]] = []
    for path in workspace.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        rel = path.relative_to(workspace).as_posix()
        if any(part in _SKIP_PARTS for part in path.relative_to(workspace).parts):
            continue
        if not _looks_like_deliverable(rel):
            continue
        try:
            entries.append((path.stat().st_mtime, rel))
        except OSError:
            continue
    if not entries:
        return []

    if window_seconds > 0:
        newest = max(mtime for mtime, _ in entries)
        cutoff = newest - window_seconds
        picked = [rel for mtime, rel in sorted(entries, reverse=True) if mtime >= cutoff]
    else:
        picked = [rel for _, rel in sorted(entries, reverse=True)]
    return picked[:limit]


def agent_edited_paths(
    attempt_dir: Path,
    *,
    workspace_root: str = "",
    mtime_window_seconds: float = 0.0,
    limit: int = 400,
) -> tuple[list[str], list[str]]:
    """三源合并，返回 (路径列表, 命中的来源名)。

    来源为空时调用方要把 `priority_source: "none"` 记进 artifact_sync——
    这样该 attempt 的低分能被识别成「回收无依据」而不是「能力差」（需求 4.4）。
    """
    attempt_dir = Path(attempt_dir)
    merged: dict[str, None] = {}
    sources: list[str] = []

    for name, found in (
        ("events", paths_from_events(attempt_dir, workspace_root=workspace_root, limit=limit)),
        ("trace", paths_from_trace(attempt_dir, workspace_root=workspace_root, limit=limit)),
        (
            "mtime",
            paths_from_mtime(attempt_dir, window_seconds=mtime_window_seconds, limit=limit),
        ),
    ):
        if found:
            sources.append(name)
        for item in found:
            merged.setdefault(item, None)

    return list(merged)[:limit], sources
