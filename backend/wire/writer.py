"""canonical 原子重写 + content-addressed blob writer。

canonical ``wire.jsonl``/``wire-manifest.json`` 由 finalizer 整体生成——写临时
文件、fsync、原子 rename，绝不 in-place 追加：rebuild 要能反复重写而
读者永远只见完整文件。

blob：payload 序列化为 UTF-8 JSON → **对未压缩 JSON 字节算 SHA-256**（文件名
``sha256-<hex>.json.<codec>`` 中的 hash 指内容而非压缩产物，换 codec 不换身份）
→ gzip 压缩 → 临时文件 + fsync + rename。同 attempt 内容去重：同 hash 已存在
即跳过写入。进 blob 的内容必须已过 redaction（写入方职责，本层不再解析）。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from backend.wire import paths


def atomic_write_bytes(dest: Path, data: bytes) -> None:
    """写临时文件 + fsync + rename 的原子落盘原语。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=dest.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, dest)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def atomic_write_jsonl(dest: Path, records: Iterable[dict[str, Any]]) -> int:
    """canonical wire.jsonl 原子重写，返回行数。"""
    lines = [
        json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in records
    ]
    payload = ("\n".join(lines) + "\n").encode("utf-8") if lines else b""
    atomic_write_bytes(dest, payload)
    return len(lines)


def atomic_write_json(dest: Path, obj: dict[str, Any]) -> None:
    """manifest 等单 JSON 文件的原子重写。"""
    atomic_write_bytes(
        dest, (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )


@dataclass(frozen=True)
class BlobRef:
    """写入结果，供 evidence/manifest 记录（hash、原始/保存大小、codec）。"""

    ref: str
    sha256: str
    raw_bytes: int
    stored_bytes: int
    codec: str
    deduplicated: bool


class BlobWriter:
    """单 attempt 的 content-addressed blob writer。

    首期 codec 固定 gzip（zstd 是否新增依赖留实现期决定，
    manifest/evidence 通过 ``codec`` 字段自描述，之后加 zstd 不破坏读取）。
    """

    CODEC = "gz"

    def __init__(self, data_path: Path, attempt_id: str):
        self._dir = paths.blobs_dir(data_path, attempt_id)

    def write_json(self, payload: Any) -> BlobRef:
        """序列化（UTF-8 JSON）→ hash → 压缩 → 原子写；同内容去重。"""
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        return self.write_bytes(raw)

    def write_bytes(self, raw: bytes) -> BlobRef:
        digest = hashlib.sha256(raw).hexdigest()
        ref = paths.blob_ref_for(digest, self.CODEC)
        dest = self._dir / ref
        if dest.exists():
            return BlobRef(
                ref=ref,
                sha256=digest,
                raw_bytes=len(raw),
                stored_bytes=dest.stat().st_size,
                codec=self.CODEC,
                deduplicated=True,
            )
        # mtime=0 使压缩产物可复现，同内容跨 attempt 也逐字节一致。
        compressed = gzip.compress(raw, mtime=0)
        atomic_write_bytes(dest, compressed)
        return BlobRef(
            ref=ref,
            sha256=digest,
            raw_bytes=len(raw),
            stored_bytes=len(compressed),
            codec=self.CODEC,
            deduplicated=False,
        )

    def read_bytes(self, ref: str) -> bytes:
        """按 ref 回读并解压（API 层的 policy/权限检查在调用前完成）。"""
        blob_path = self._dir / ref
        if not paths.BLOB_REF_RE.match(ref) or not blob_path.exists():
            raise FileNotFoundError(ref)
        return gzip.decompress(blob_path.read_bytes())
