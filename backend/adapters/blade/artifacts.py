"""Workspace artifact recovery and skill synchronization helpers for Blade."""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ...artifact_scope import ARTIFACT_SKIP_DIRS
from ..base import AdapterResult, AdapterRunInput
from ..edited_paths import agent_edited_paths

logger = logging.getLogger(__name__)

# 产物回收时跳过的目录。与 API 扫描、评分快照共用同一份净产物边界——
# 这里原本自带一份清单且漏了 dist/build，导致「回收到的产物」与「判分看到的
# 产物」口径不一致。
_ARTIFACT_SKIP_DIRS = ARTIFACT_SKIP_DIRS
_ARTIFACT_MAX_FILES = 500
_ARTIFACT_MAX_DEPTH = 5

# 从 events.jsonl 里抠出 agent 用 Edit/Write 类工具写过的 workspace 相对路径。
# 事件里的工具参数可能是原始 JSON，也可能是被再次 JSON 转义的字符串
# （recovered messages），所以两种写法都匹配。只认 file_path / path 两个键。
_EDIT_PATH_PATTERNS = [
    re.compile(r'\\"(?:file_path|path)\\":\s*\\"([^\\"]+)\\"'),
    re.compile(r'"(?:file_path|path)":\s*"([^"]+)"'),
]
_EDIT_TOOL_MARKERS = (
    "Edit",
    "Write",
    "write_file",
    "edit_file",
    "MultiEdit",
    "create_file",
)

# 优先回收时试探的远端前缀：日志里的路径未必与远端工作区同根。
_REMOTE_PATH_PREFIXES = ("", "workspace/", "skill_workspace/", "output/")


def _agent_edited_paths(attempt_dir: Path) -> list[str]:
    """agent 在远端 workspace 里写过的文件（相对路径），用于产物回收优先拉取。

    2026-09-14 复盘：django 仓库 6000+ 文件，BFS 回收 500 个就截断，
    agent 改的 django/utils/numberformat.py 根本轮不到，本地 workspace
    停在基线 → functional 0 分，而 agent 其实已改完并跑绿测试。
    """
    events_path = attempt_dir / "events.jsonl"
    if not events_path.is_file():
        return []
    found: list[str] = []
    seen: set[str] = set()
    try:
        for line in events_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not any(marker in line for marker in _EDIT_TOOL_MARKERS):
                continue
            for pattern in _EDIT_PATH_PATTERNS:
                for match in pattern.finditer(line):
                    raw = match.group(1).strip()
                    if not raw or raw.startswith("/") or ".." in raw.split("/"):
                        continue
                    rel = raw[2:] if raw.startswith("./") else raw
                    if rel and rel not in seen:
                        seen.add(rel)
                        found.append(rel)
    except OSError:
        return found
    return found


def _digest_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _digest_of(path: Path) -> str | None:
    """文件当前内容的摘要；不存在返回 None。"""
    try:
        return _digest_bytes(path.read_bytes())
    except OSError:
        return None


def _candidate_remote_paths(rel: str, *, project_workspace: str = "") -> list[str]:
    """一个相对路径在远端可能的几种写法，按可能性排序。"""
    # `lstrip("./")` 按字符集剥，会把 `.hidden/report.md` 削成
    # `hidden/report.md`，每个候选前缀都指向不存在的路径。
    clean = rel
    while clean.startswith("./"):
        clean = clean[2:]
    seen: dict[str, None] = {}
    # software_factory 场景：agent 的 cwd 是项目工作区绝对路径，而文件接口的
    # "." 指向另一份带基线物料的目录——两边都有 repo/…，只有前者带 agent 的
    # 改动。所以它必须排在最前（2026-09-14 现场结论）。
    if project_workspace:
        seen.setdefault(f"{project_workspace.rstrip('/')}/{clean}", None)
    for prefix in _REMOTE_PATH_PREFIXES:
        seen.setdefault(f"{prefix}{clean}", None)
    # 只剩文件名的写法：agent 在工作区根上直接写文件的常见情形。
    name = Path(clean).name
    if name != clean:
        seen.setdefault(name, None)
    return list(seen)


def _iterative_artifact_sync_error(result: AdapterResult) -> str | None:
    sync = result.external_refs.get("artifact_sync")
    if not isinstance(sync, dict):
        return "产物同步结果缺失"
    if sync.get("error"):
        return str(sync["error"])
    errors = sync.get("errors")
    if isinstance(errors, list) and errors:
        return "; ".join(str(item) for item in errors[:10])
    if sync.get("truncated_at") is not None:
        return f"产物同步在 {sync['truncated_at']} 个文件处截断"
    return None


def artifact_recovery_failed(external_refs: dict[str, Any]) -> bool:
    """优先路径一个都没落地 —— 产物没回收到，分数不可用。

    这是 2026-09-18 横评里 blade-agent 27 个 attempt 全部低分的形状：attempt
    正常跑完、拿到一个真实的低分，而那个分数衡量的是空工作区。把它标成
    infrastructure，统计时才能与「agent 确实没做好」分开（需求 6.3）。

    只有「有依据、且全部落空」才算失败：`priority_source == "none"` 表示压根
    没提取到路径，那是另一种情况（需求 4.4），不在这里判。
    """
    sync = external_refs.get("artifact_sync")
    if not isinstance(sync, dict):
        return False
    missing = sync.get("priority_missing")
    if not isinstance(missing, list) or not missing:
        return False
    got = sync.get("priority_downloaded")
    return not (isinstance(got, list) and got)


def _fail_incomplete_iterative_artifact_sync(result: AdapterResult) -> bool:
    if result.status != "completed":
        return False
    detail = _iterative_artifact_sync_error(result)
    if detail is None:
        return False
    result.status = "chat_failed"
    result.error_code = "iteration_artifact_sync_failed"
    result.error_message = detail
    return True


async def download_priority_paths(
    client: Any,
    session_id: str,
    paths: list[str],
    *,
    download_root: Path,
    baseline_root: Path,
    errors: list[str],
    project_workspace: str = "",
) -> tuple[list[str], list[str]]:
    """定向下载 agent 编辑过的路径，并校验落地内容确实变了。

    「`download_file` 没抛异常」不等于「拿到了产物」：实测下载成功但文件
    内容仍是基线（mtime 停在物料拷贝时间）。远端路径前缀可能与本地不同，
    请求到的其实是另一个位置的同名文件。所以下载后比对 hash——与基线一致
    就视为未命中，换候选前缀再试（需求 5.1/5.2）。
    """
    got: list[str] = []
    missing: list[str] = []
    for rel in paths:
        target = download_root / rel
        # 基线必须取自**活的 workspace**，不能取 download_root：续聊/恢复
        # 时 download_root 是新建的空 staging 目录，摘要恒为 None，校验就
        # 完全失效——而那恰恰是这个校验要守的路径。
        # 文件不存在时为 None：那时任何内容都算命中（本来就没有）。
        baseline = _digest_of(baseline_root / rel)
        landed = False
        for candidate in _candidate_remote_paths(
            rel, project_workspace=project_workspace
        ):
            try:
                content = await client.download_file(session_id, candidate)
            except Exception:
                # 候选路径猜错会 404，这是预期内的试探，不记进 errors——
                # errors 非空会让迭代评审路径把 attempt 判成 chat_failed。
                continue
            if baseline is not None and _digest_bytes(content) == baseline:
                # 内容没变 = 没真正拿到，继续试下一个候选。
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            except OSError as exc:
                errors.append(f"write {rel}: {exc}")
                break
            got.append(rel)
            landed = True
            break
        if not landed:
            missing.append(rel)
    return got, missing


async def recover_workspace_artifacts(
    client: Any,
    session_id: str,
    attempt_dir: Path,
    task: AdapterRunInput,
    *,
    overwrite_existing_files: bool = False,
    download_priority_fn: Callable[..., Awaitable[tuple[list[str], list[str]]]] | None = None,
) -> dict[str, Any]:
    """递归拉取 blade workspace 到本地 skill_workspace。

    不再依赖硬编码文件名：能拉的全拉（跳过运行时内部目录），scorer 自己判定。
    首轮已存在且非空的本地文件默认跳过（uploaded 物料本来就在本地）。
    Session 续聊或恢复时远端工作区是当前版本权威来源，必须覆盖旧轮本地文件。
    """
    workspace_dir = attempt_dir / "skill_workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    staging_dir = (
        Path(tempfile.mkdtemp(prefix=".skill_workspace.sync-", dir=attempt_dir))
        if overwrite_existing_files
        else None
    )
    download_root = staging_dir or workspace_dir
    overwrite_existing = {
        str(x)
        for x in (task.task_context.get("_agent_material_files") or [])
        if isinstance(x, str)
    }
    # Task uploads are copied locally before the Blade session starts. They
    # are seed inputs, not final artifacts: the agent may edit the same file
    # in the remote workspace (for example amortization.db). Allow the
    # remote copy to replace the local seed during recovery.
    for item in task.task_context.get("uploaded_files") or []:
        if isinstance(item, str):
            name = Path(item).name
        elif isinstance(item, dict):
            name = str(item.get("name") or Path(str(item.get("path") or "")).name)
        else:
            continue
        if name and Path(name).name == name:
            overwrite_existing.add(name)

    listing: list[dict[str, Any]] = []
    downloaded: list[str] = []
    skipped_existing: list[str] = []
    errors: list[str] = []
    truncated = False

    # 优先回收：先按 agent 实际编辑过的路径定向下载，再进 BFS 兜底。
    # BFS 的 500 上限本身是合理的防爆保护，但它会把交付物挤掉——
    # 优先路径不受这个上限约束。
    # workspace_root 留空是故意的：blade 在沙盒内按 session 选定工作区
    # （/root/智能助手工作空间/<session>），adapter 事前不知具体子目录。
    # `normalize_path` 用工作区标记反查前缀，与安全扫描的做法一致。
    priority_paths, priority_source = agent_edited_paths(attempt_dir)
    priority_downloaded: list[str] = []
    priority_missing: list[str] = []
    priority_downloader = download_priority_fn or download_priority_paths
    if priority_paths:
        got, missing = await priority_downloader(
            client,
            session_id,
            priority_paths,
            download_root=download_root,
            baseline_root=workspace_dir,
            errors=errors,
        )
        priority_downloaded.extend(got)
        priority_missing.extend(missing)
        downloaded.extend(got)

    # 优先阶段已落地的路径，BFS 阶段跳过（list 查找是 O(n)，500 个文件
    # 逐个扫会变成 O(n²)，这里固化成 set）。
    priority_done = set(priority_downloaded)

    queue: list[tuple[str, int]] = [(".", 0)]
    seen_files = 0
    while queue:
        dir_path, depth = queue.pop(0)
        try:
            entries = await client.list_dir(session_id, dir_path)
        except Exception as exc:
            errors.append(f"list {dir_path}: {exc}")
            continue
        for entry in entries:
            rel = entry.path or (
                entry.name if dir_path == "." else f"{dir_path}/{entry.name}"
            )
            if entry.is_dir:
                if entry.name in _ARTIFACT_SKIP_DIRS or entry.name.startswith("."):
                    continue
                if depth + 1 <= _ARTIFACT_MAX_DEPTH:
                    queue.append((rel, depth + 1))
                continue
            if entry.name.startswith("."):
                continue
            seen_files += 1
            if seen_files > _ARTIFACT_MAX_FILES:
                truncated = True
                break
            listing.append({"path": rel})
            target = download_root / rel
            # 优先阶段已经拉到的不再重拉：BFS 是兜底，重复下载既浪费
            # 带宽，又可能用一个「候选前缀猜错」的版本覆盖掉优先阶段
            # 已经校验过内容的正确版本。
            if rel in priority_done:
                continue
            if (
                staging_dir is None
                and target.exists()
                and target.stat().st_size > 0
                and rel not in overwrite_existing
                and not overwrite_existing_files
            ):
                skipped_existing.append(rel)
                continue
            try:
                content = await client.download_file(session_id, rel)
            except Exception as exc:
                errors.append(f"download {rel}: {exc}")
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            except OSError as exc:
                errors.append(f"write {rel}: {exc}")
                continue
            downloaded.append(rel)
        if truncated:
            break

    result: dict[str, Any] = {
        "downloaded": downloaded,
        "skipped_existing": skipped_existing,
        "total_listed": len(listing),
        "errors": errors,
        # 这四个字段恒存在（需求 6.2）：排查低分时要一眼看出是
        # 「agent 没做」还是「没回收到」。truncated_at 为 None 表示没截断。
        "truncated_at": _ARTIFACT_MAX_FILES if truncated else None,
        "priority_downloaded": priority_downloaded,
        "priority_missing": priority_missing,
        "priority_source": "+".join(priority_source) if priority_source else "none",
    }
    if truncated:
        logger.warning(
            "artifact recovery truncated at %d files (session %s)",
            _ARTIFACT_MAX_FILES,
            session_id,
        )
    if staging_dir is not None:
        if errors or truncated:
            # staging 被整个丢弃 —— 包括优先阶段已经拉到的那些文件。
            # 若仍把它们记在 priority_downloaded 里，`artifact_recovery_failed`
            # 会判定回收成功，于是 attempt 拿着一个「对空工作区打出的分数」
            # 进矩阵，且不带 infrastructure 标记。产物确实没落地，记账就得
            # 照实说：全部转入 priority_missing。
            shutil.rmtree(staging_dir, ignore_errors=True)
            if priority_downloaded:
                logger.warning(
                    "staging 丢弃使 %d 个优先产物未落地 (session %s)",
                    len(priority_downloaded),
                    session_id,
                )
                for rel in priority_downloaded:
                    if rel in downloaded:
                        downloaded.remove(rel)
                priority_missing.extend(priority_downloaded)
                priority_downloaded.clear()
                result["priority_downloaded"] = priority_downloaded
                result["priority_missing"] = priority_missing
                result["downloaded"] = downloaded
                result["staging_discarded"] = True
        else:
            backup_dir = attempt_dir / f".skill_workspace.previous-{time.time_ns()}"
            try:
                workspace_dir.rename(backup_dir)
                staging_dir.rename(workspace_dir)
            except Exception:
                if not workspace_dir.exists() and backup_dir.exists():
                    backup_dir.rename(workspace_dir)
                shutil.rmtree(staging_dir, ignore_errors=True)
                raise
            else:
                shutil.rmtree(backup_dir, ignore_errors=True)
                result["workspace_replaced"] = True
    return result


def sync_blade_skills(envs_path: Path, skills_path: Path) -> list[str]:
    """把所有 env 的 blade_skill/ 同步到 <skills_path>/octagon/<env-name>/。

    路线 A 下这是旧 env 薄壳的注册通道：blade server 启动时把 <skills_path>
    加进 BLADE_SKILL_PATHS，薄壳即成为注册 skill（id = "octagon/<env-name>"），
    运行期不再向 session 上传。
    """
    envs_path = Path(envs_path)
    skills_path = Path(skills_path)
    target = skills_path / "octagon"
    tmp = skills_path / ".octagon.tmp"
    skills_path.mkdir(parents=True, exist_ok=True)
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)

    synced: list[str] = []
    if envs_path.is_dir():
        for env_dir in sorted(p for p in envs_path.iterdir() if p.is_dir()):
            blade_skill = env_dir / "blade_skill"
            if not blade_skill.is_dir():
                continue
            shutil.copytree(blade_skill, tmp / env_dir.name)
            synced.append(env_dir.name)

    if target.exists():
        shutil.rmtree(target)
    tmp.rename(target)
    logger.info("sync_blade_skills: %s envs -> %s", len(synced), target)
    return synced
