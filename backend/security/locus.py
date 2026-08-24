"""作用目标（target）判定。

target 回答「命令对什么东西操作」，参与 severity 修正——这是与「执行场合（locus，
在哪跑）」正交的概念。locus 只展示不评级，本模块只算 target。

判定策略：
- network 类命令 → network-egress
- 解析命令里的路径 token（含前导 cd 的目标），与 workspace_root 比对：
    前缀在 workspace 内         → in-workspace
    绝对路径落系统目录          → system-path
    其它绝对/相对越界路径       → out-of-workspace
- 无 workspace_root 或解析不出路径 → unknown（宁可漏报不误报）
"""

from __future__ import annotations

import os
import re
import shlex

# 绝对系统路径前缀 → system-path
_SYSTEM_PREFIXES = (
    "/etc",
    "/usr",
    "/bin",
    "/sbin",
    "/boot",
    "/lib",
    "/var",
    "/dev",
    "/sys",
    "/proc",
    "/System",
    "/Library",
    "/opt",
    "/root",
)

# 这些 token 不是路径参数（是 flag / 子命令 / 重定向符）
_NON_PATH = re.compile(r"^(-|&&$|\|\||;|>|>>|<|\d+>)")


def _iter_commands(command: str) -> list[str]:
    """把 `a && b; c | d` 拆成子命令片段，保序。"""
    # 用非路径分隔符切分，保留每段用于逐段解析 cd
    parts = re.split(r"(?:&&|\|\||;|\|)", command)
    return [p.strip() for p in parts if p.strip()]


def _extract_cd_target(segment: str) -> str | None:
    """取 `cd X` 里的 X（去引号），否则 None。"""
    m = re.match(r"\s*cd\s+(.+)", segment)
    if not m:
        return None
    rest = m.group(1).strip()
    try:
        toks = shlex.split(rest)
    except ValueError:
        toks = [rest]
    return toks[0] if toks else None


def _looks_like_path(tok: str) -> bool:
    if not tok or _NON_PATH.match(tok):
        return False
    # 明确的路径形态：绝对/相对/家目录/变量
    if tok.startswith(("/", "./", "../", "~", "$")):
        return True
    if "/" in tok:
        return True
    # 裸 token（无斜杠）：破坏性命令的操作对象常是 workspace 内的相对目录/文件名
    # （如 `rm -rf .recap_work frames verify`）。排除明显的非路径：纯选项值、
    # glob-only、shell 关键字。其余视作相对路径参与判定（配合 cwd 归一）。
    if tok in ("&&", "||", ";", "|", "then", "do", "done", "fi", "else"):
        return False
    if tok.startswith("*"):  # 纯 glob 前缀交给 shell，跳过
        return False
    return True


def _normalize(path: str, cwd: str | None) -> str | None:
    """展开 ~ / $HOME，相对路径基于 cwd 归一为绝对路径。无 cwd 且相对则放弃。"""
    p = path.strip().strip("'\"")
    p = os.path.expanduser(p)
    if p.startswith("$HOME"):
        home = os.path.expanduser("~")
        p = home + p[len("$HOME") :]
    if "$" in p:  # 仍含无法解析的变量 → 放弃
        return None
    if os.path.isabs(p):
        return os.path.normpath(p)
    if cwd:
        return os.path.normpath(os.path.join(cwd, p))
    return None


def _classify_abs(
    abs_path: str,
    workspace_root: str | None,
    extra_workspace_prefixes: tuple[str, ...] = (),
) -> str:
    roots = []
    if workspace_root:
        roots.append(os.path.normpath(workspace_root))
    roots.extend(os.path.normpath(p) for p in extra_workspace_prefixes)
    for ws in roots:
        if abs_path == ws or abs_path.startswith(ws + os.sep):
            return "in-workspace"
    for pre in _SYSTEM_PREFIXES:
        if abs_path == pre or abs_path.startswith(pre + os.sep):
            return "system-path"
    return "out-of-workspace"


def classify_target(
    command: str,
    workspace_root: str | None,
    *,
    network: bool = False,
    extra_workspace_prefixes: tuple[str, ...] = (),
) -> str:
    """判定命令的作用目标。

    network=True（规则 target_hint=network）直接返回 network-egress。
    extra_workspace_prefixes：额外的 workspace 根前缀（如 blade 沙盒内
    `/root/智能助手工作空间/<session>`），命中即算 in-workspace。
    """
    if network:
        return "network-egress"

    # cwd 起点：优先 workspace_root，否则第一个额外前缀（沙盒场景）。
    cwd = workspace_root or (
        extra_workspace_prefixes[0] if extra_workspace_prefixes else None
    )
    targets: list[str] = []

    for seg in _iter_commands(command):
        cd_target = _extract_cd_target(seg)
        if cd_target is not None:
            abs_cd = _normalize(cd_target, cwd)
            if abs_cd is not None:
                targets.append(
                    _classify_abs(abs_cd, workspace_root, extra_workspace_prefixes))
                cwd = abs_cd  # 后续命令在新目录下
            continue
        try:
            toks = shlex.split(seg)
        except ValueError:
            toks = seg.split()
        for tok in toks[1:]:  # 跳过命令名本身
            if not _looks_like_path(tok):
                continue
            abs_p = _normalize(tok, cwd)
            if abs_p is not None:
                targets.append(
                    _classify_abs(abs_p, workspace_root, extra_workspace_prefixes))

    if not targets:
        return "unknown"

    # 取「最严重」的作用目标：system-path > out-of-workspace > in-workspace
    priority = {"system-path": 3, "out-of-workspace": 2, "in-workspace": 1}
    return max(targets, key=lambda t: priority.get(t, 0))
