"""净产物边界：什么算 agent 交付给用户的东西。

原先这套规则在三处各写一份且内容不一致（blade_service 的回收清单没有
`dist`/`build`，两个 programming scorer 又各有一份），而 API 的产物扫描和
评分快照干脆没有排除。后果是同一个 attempt 在不同链路上"有多少产物"给出
不同答案：实测 2026-07-28 的六方对比里，净产物都是 46-63 个文件，但 kimi
的原始文件数是 13533、codex 是 1816——差异全部来自 `npm install` 与构建，
不是交付内容。

排除口径按「是否是用户拿到的交付物」判断，而不是「是否由 agent 生成」：

- 依赖与缓存（node_modules/.venv/__pycache__/...）：不是交付物，重装即得。
- 构建产物（dist/build/.next/target）：在这些评测场景里交付的是可运行的源码
  工程，构建产物是过程副产品。**注意这个判断依赖场景**——纯前端静态站点的
  交付物可能恰恰就是 `dist/`，届时需要按场景放开而不是改这里的默认。
- 版本库元数据（.git）：不是交付物，且体量常远超源码本身。
"""

from __future__ import annotations

from pathlib import Path

# 依赖、缓存、构建产物、版本库元数据——都不是交付给用户的东西。
ARTIFACT_SKIP_DIRS = frozenset({
    # 依赖树
    # 2026-09-14：去掉 "vendor"——django 等仓库把第三方 JS 放在源码树的
    # vendor/ 下，跳过会让评分快照缺基线文件、repository_discipline 全员扣分。
    "node_modules", ".venv", "venv",
    # 语言/工具缓存
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".cache", ".gradle", ".tox",
    # 构建产物
    "dist", "build", ".next", "target", ".output", ".turbo",
    # 版本库与运行时元数据
    ".git", ".svn", ".hg",
    # blade 运行时内部目录
    ".agents", ".blade", ".octagon",
})


def is_excluded_relpath(relative: Path) -> bool:
    """Whether a workspace-relative path falls inside an excluded directory."""
    return any(part in ARTIFACT_SKIP_DIRS for part in relative.parts)


def iter_artifact_files(root: Path, *, max_files: int | None = None):
    """Yield workspace-relative paths of net deliverable files under ``root``.

    Prunes excluded directories during the walk rather than filtering after a
    full ``rglob``: a polluted workspace can hold 13k+ files whose only purpose
    is to be skipped, and descending into them costs I/O on every caller.

    Symlinks are never followed and never yielded. Following them would let a
    workspace link out to arbitrary filesystem locations; yielding them without
    following would report paths whose content is not part of the attempt.
    """
    count = 0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            children = sorted(current.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_symlink():
                continue
            relative = child.relative_to(root)
            if child.is_dir():
                if child.name not in ARTIFACT_SKIP_DIRS:
                    stack.append(child)
                continue
            # 目录已在上面剪枝，这里不再按名字过滤：排除的是目录，而一个
            # 名叫 `build` 的**文件**（脚本、Makefile 目标）是交付物。
            if not child.is_file():
                continue
            if max_files is not None and count >= max_files:
                return
            count += 1
            yield relative
