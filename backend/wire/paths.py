"""attempt wire 目录布局与 blob ref 安全解析。

单一出口：所有 wire 相关文件路径都从这里解析，API/finalizer/spool writer
不各自拼路径。两个安全边界：

1. attempt_id 只允许保守字符集——路径分量来自 URL，必须挡目录穿越；
2. blob ref 只接受 ``sha256-<64hex>.json.<codec>`` 白名单文件名（
   "API 只通过 ref 查找，不接受任意 path"），解析后再做一次 resolved-path
   containment 双保险。
"""

from __future__ import annotations

import re
from pathlib import Path

# 路径分量白名单：attempt id / source instance 等进入文件名的值。
_SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9._-]+\Z")

# blob 文件名唯一合法形状。codec 首期 gzip，保留 zstd。
# 用 \Z 而非 $：$ 会接受尾部换行，给注入留缝。
BLOB_REF_RE = re.compile(r"^sha256-[0-9a-f]{64}\.json\.(?:gz|zst)\Z")

WIRE_FILE = "wire.jsonl"
MANIFEST_FILE = "wire-manifest.json"
BLOBS_DIR = "wire-blobs"
SOURCES_DIR = "wire-sources"


class WirePathError(ValueError):
    """非法路径分量或 blob ref——调用方（API 层）应映射为 404，不泄漏细节。"""


def _check_component(value: str, what: str) -> str:
    if not _SAFE_COMPONENT_RE.match(value) or value in (".", ".."):
        raise WirePathError(f"非法{what}: {value!r}")
    return value


def attempt_dir(data_path: Path, attempt_id: str) -> Path:
    """attempt 根目录（与 db.py 的 attempts 布局一致），先过分量白名单。"""
    _check_component(attempt_id, " attempt_id")
    return Path(data_path) / "attempts" / attempt_id


def wire_file(data_path: Path, attempt_id: str) -> Path:
    return attempt_dir(data_path, attempt_id) / WIRE_FILE


def manifest_file(data_path: Path, attempt_id: str) -> Path:
    return attempt_dir(data_path, attempt_id) / MANIFEST_FILE


def blobs_dir(data_path: Path, attempt_id: str) -> Path:
    return attempt_dir(data_path, attempt_id) / BLOBS_DIR


def sources_dir(data_path: Path, attempt_id: str) -> Path:
    return attempt_dir(data_path, attempt_id) / SOURCES_DIR


def phase_state_file(data_path: Path, attempt_id: str) -> Path:
    """phase-state 文件：lifecycle 原子写、独立进程只读。"""
    return sources_dir(data_path, attempt_id) / "phase-state.json"


def source_spool_file(data_path: Path, attempt_id: str, kind: str, instance: str | None = None) -> Path:
    """source spool 文件：``wire-sources/<kind>[@<instance>].jsonl``。

    分隔符用 ``@``：它不在分量白名单字符集内，因此 ``kind@instance`` 的拼接
    无歧义。design 早期草案的 ``<kind>-<instance>`` 有确定性碰撞
    （``a-b``+``c`` 与 ``a``+``b-c`` 同路径），已改。
    """
    _check_component(kind, " source kind")
    name = kind
    if instance is not None:
        _check_component(instance, " source instance")
        name = f"{kind}@{instance}"
    return sources_dir(data_path, attempt_id) / f"{name}.jsonl"


def blob_ref_for(sha256_hex: str, codec: str) -> str:
    """由 hash + codec 构造合法 blob ref；写入侧也走白名单，读写同一形状。"""
    ref = f"sha256-{sha256_hex}.json.{codec}"
    if not BLOB_REF_RE.match(ref):
        raise WirePathError(f"非法 blob ref 构成: hash={sha256_hex!r} codec={codec!r}")
    return ref


def resolve_blob_path(data_path: Path, attempt_id: str, ref: str) -> Path:
    """blob ref → 磁盘路径，带目录穿越双重防护。

    第一重：ref 必须整体匹配白名单正则（不含任何路径分隔符）；
    第二重：resolve 后必须仍在该 attempt 的 wire-blobs 目录内。
    """
    if not BLOB_REF_RE.match(ref):
        raise WirePathError(f"非法 blob ref: {ref!r}")
    base = blobs_dir(data_path, attempt_id)
    candidate = (base / ref).resolve()
    if candidate.parent != base.resolve():
        raise WirePathError(f"blob ref 越界: {ref!r}")
    return candidate
