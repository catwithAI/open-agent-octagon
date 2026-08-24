"""Safe artifact classification and Office preview descriptors.

This module owns the shared W7 trust boundary and renderer scheduling:
untrusted OOXML is inspected as a bounded ZIP, active content is never
executed, and callers get a stable descriptor instead of accidentally decoding
an OOXML ZIP as UTF-8 text.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal


PREVIEW_CONTRACT_VERSION = "octagon-artifact-preview-v1"
SCANNER_NAME = "octagon-ooxml-scanner"
SCANNER_VERSION = "1"
WORKER_TIMEOUT_SECONDS = 15
WORKER_RESULT_BYTES = 32 * 1024 * 1024
SYNC_PREVIEW_MAX_BYTES = 20 * 1024 * 1024
PREVIEW_POLL_AFTER_MS = 500
MAX_BACKGROUND_JOBS = 256

_BACKGROUND_EXECUTOR = ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="octagon-office-preview"
)
_BACKGROUND_LOCK = threading.Lock()
_BACKGROUND_JOBS: dict[str, Future[dict[str, Any]]] = {}

MAX_SOURCE_BYTES = 128 * 1024 * 1024
MAX_ZIP_ENTRIES = 10_000
MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_SINGLE_ENTRY_BYTES = 64 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200

ArtifactType = Literal[
    "presentation", "document", "spreadsheet", "image", "video", "audio",
    # html 与 text 分开：前端特效类场景（如 frontend-vfx-*）的产物必须**执行**
    # 才有意义，光看源码看不出效果。但它是被测 agent 生成的不可信内容，
    # 只能在 `<iframe sandbox>`（不给 allow-same-origin）里跑——单独一个类型
    # 才能让前端选对渲染路径，而不是把它当纯文本贴出来。
    "html", "text", "binary",
]

_OOXML_MARKERS: dict[str, tuple[ArtifactType, str]] = {
    "ppt/presentation.xml": (
        "presentation",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ),
    "word/document.xml": (
        "document",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    "xl/workbook.xml": (
        "spreadsheet",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
}
_MACRO_PARTS = re.compile(r"(^|/)(vbaProject\.bin|activeX/|embeddings/)", re.IGNORECASE)
_EXTERNAL_REL = re.compile(rb'TargetMode\s*=\s*["\']External["\']', re.IGNORECASE)
@dataclass(slots=True)
class Inspection:
    artifact_type: ArtifactType
    media_type: str
    status: Literal["ready", "rendering", "unsupported", "failed"]
    error_code: str | None = None
    error_message: str | None = None
    counts: dict[str, int | None] = field(default_factory=dict)
    security: dict[str, bool] = field(default_factory=dict)
    capability_gaps: list[str] = field(default_factory=list)


def _plain_type(path: Path) -> tuple[ArtifactType, str]:
    """Classify non-Office content from a bounded byte sample.

    The filename only refines a type after the bytes have established that the
    file is text. This prevents a text file named ``image.png`` from being fed
    to the image viewer and keeps unknown binary out of the UTF-8 endpoint.
    """
    try:
        with path.open("rb") as handle:
            sample = handle.read(4096)
    except OSError:
        return "binary", "application/octet-stream"

    signatures: tuple[tuple[bool, ArtifactType, str], ...] = (
        (sample.startswith(b"\x89PNG\r\n\x1a\n"), "image", "image/png"),
        (sample.startswith(b"\xff\xd8\xff"), "image", "image/jpeg"),
        (sample.startswith((b"GIF87a", b"GIF89a")), "image", "image/gif"),
        (sample.startswith(b"BM"), "image", "image/bmp"),
        (sample.startswith((b"II*\x00", b"MM\x00*")), "image", "image/tiff"),
        (sample.startswith(b"RIFF") and sample[8:12] == b"WEBP", "image", "image/webp"),
        (sample.startswith(b"RIFF") and sample[8:12] == b"WAVE", "audio", "audio/wav"),
        (sample.startswith(b"OggS"), "audio", "audio/ogg"),
        (sample.startswith(b"fLaC"), "audio", "audio/flac"),
        (sample.startswith(b"ID3") or sample[:2] in {b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"},
         "audio", "audio/mpeg"),
        (len(sample) >= 12 and sample[4:8] == b"ftyp", "video", "video/mp4"),
        (sample.startswith(b"\x1aE\xdf\xa3"), "video", "video/webm"),
    )
    for matched, artifact_type, media_type in signatures:
        if matched:
            return artifact_type, media_type
    if sample.startswith(b"%PDF-"):
        return "binary", "application/pdf"

    # SVG is XML text but has its own inert <img> presentation path. Require an
    # actual root tag in the bounded prefix; the extension alone is not enough.
    text_prefix = sample.lstrip(b"\xef\xbb\xbf\x00\t\r\n ")
    if re.search(br"<(?:[A-Za-z_][\w.-]*:)?svg(?:\s|>)", text_prefix, re.IGNORECASE):
        return "image", "image/svg+xml"

    if b"\x00" not in sample:
        try:
            sample.decode("utf-8")
        except UnicodeDecodeError:
            pass
        else:
            # HTML 单独成类（可 sandbox 渲染）。与 SVG 同样的判据顺序：**先**由
            # 字节确认是文本，**再**看内容特征——只信扩展名会让一个叫
            # `x.html` 的纯文本被送进渲染路径。要求在有界前缀里出现真实根标签。
            text_prefix = sample.lstrip(b"\xef\xbb\xbf\x00\t\r\n ")
            if re.search(
                br"<(?:!doctype\s+html|html(?:\s|>)|head(?:\s|>)|body(?:\s|>))",
                text_prefix, re.IGNORECASE,
            ):
                return "html", "text/html"
            guessed = mimetypes.guess_type(path.name)[0] or "text/plain"
            media_type = guessed if guessed.startswith("text/") else "text/plain"
            return "text", media_type
    return "binary", "application/octet-stream"


def _unsafe_member(name: str) -> bool:
    # OOXML member names are POSIX paths regardless of the host OS.
    path = PurePosixPath(name.replace("\\", "/"))
    return path.is_absolute() or ".." in path.parts


def _failed(path: Path, code: str, message: str) -> Inspection:
    artifact_type, media_type = _plain_type(path)
    # A failed Office-container check means the suffix is not evidence of MIME.
    if path.suffix.lower() in {".ppt", ".pptx", ".pptm", ".doc", ".docx", ".docm", ".xls", ".xlsx", ".xlsm"}:
        artifact_type, media_type = "binary", "application/octet-stream"
    return Inspection(artifact_type, media_type, "failed", code, message)


def inspect_artifact(path: Path) -> Inspection:
    """Classify *path* by content and validate OOXML container safety."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        return _failed(path, "artifact_unreadable", str(exc))
    if size > MAX_SOURCE_BYTES:
        return _failed(path, "source_too_large", "文件超过预览大小上限")

    try:
        with path.open("rb") as handle:
            magic = handle.read(8)
    except OSError as exc:
        return _failed(path, "artifact_unreadable", str(exc))

    # Legacy Office is OLE Compound File.  It is download-only in the first
    # phase because parsing it would require a separate sandboxed converter.
    if magic.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        artifact_type, _ = _plain_type(path)
        suffix = path.suffix.lower()
        if suffix == ".ppt":
            artifact_type = "presentation"
        elif suffix == ".doc":
            artifact_type = "document"
        elif suffix == ".xls":
            artifact_type = "spreadsheet"
        return Inspection(
            artifact_type,
            "application/x-ole-storage",
            "unsupported",
            "legacy_office_unsupported",
            "旧版 Office 格式首期仅支持下载",
            capability_gaps=["legacy-office-rendering"],
        )

    if not magic.startswith(b"PK"):
        # An Office extension is only a hint; never decode a fake OOXML file as
        # text or claim it is a valid presentation/document/workbook.
        if path.suffix.lower() in {".pptx", ".pptm", ".docx", ".docm", ".xlsx", ".xlsm"}:
            return _failed(path, "invalid_ooxml_zip", "Office 文件不是有效的 OOXML ZIP")
        artifact_type, media_type = _plain_type(path)
        return Inspection(artifact_type, media_type, "ready" if artifact_type != "binary" else "unsupported")

    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ZIP_ENTRIES:
                return _failed(path, "zip_entry_limit", "ZIP 文件条目数超过安全上限")
            total = 0
            names: set[str] = set()
            external_relationships = False
            macros_present = False
            for info in infos:
                if _unsafe_member(info.filename):
                    return _failed(path, "zip_path_traversal", "ZIP 包含不安全路径")
                names.add(info.filename)
                total += info.file_size
                if info.file_size > MAX_SINGLE_ENTRY_BYTES or total > MAX_UNCOMPRESSED_BYTES:
                    return _failed(path, "zip_uncompressed_limit", "ZIP 解压大小超过安全上限")
                if info.file_size and info.compress_size == 0:
                    return _failed(path, "zip_compression_ratio", "ZIP 压缩比异常")
                if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
                    return _failed(path, "zip_compression_ratio", "ZIP 压缩比超过安全上限")
                macros_present = macros_present or bool(_MACRO_PARTS.search(info.filename))
                if info.filename.lower().endswith(".rels") and info.file_size <= 2 * 1024 * 1024:
                    external_relationships = external_relationships or bool(
                        _EXTERNAL_REL.search(archive.read(info))
                    )

            detected = next((value for marker, value in _OOXML_MARKERS.items() if marker in names), None)
            if detected is None:
                # Generic ZIP, including one renamed to an Office extension.
                if path.suffix.lower() in {".pptx", ".pptm", ".docx", ".docm", ".xlsx", ".xlsm"}:
                    return _failed(path, "ooxml_part_missing", "ZIP 缺少 Office 主文档部件")
                return Inspection("binary", "application/zip", "unsupported", "archive_preview_unsupported")

            artifact_type, media_type = detected
            if macros_present:
                media_type = {
                    "presentation": "application/vnd.ms-powerpoint.presentation.macroEnabled.12",
                    "document": "application/vnd.ms-word.document.macroEnabled.12",
                    "spreadsheet": "application/vnd.ms-excel.sheet.macroEnabled.12",
                }[artifact_type]
            if artifact_type == "presentation":
                count = sum(1 for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n))
                counts = {"slides": count, "pages": None, "sheets": None}
            elif artifact_type == "document":
                counts = {"slides": None, "pages": None, "sheets": None}
            else:
                count = sum(1 for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
                counts = {"slides": None, "pages": None, "sheets": count}
            gaps = ["content-renderer-not-installed"]
            if macros_present:
                gaps.append("macros-disabled")
            if external_relationships:
                gaps.append("external-resources-blocked")
            return Inspection(
                artifact_type,
                media_type,
                "unsupported",
                "renderer_unavailable",
                "文件已通过安全检查，内容 renderer 后续接入",
                counts=counts,
                security={
                    "macros_present": macros_present,
                    "macros_executed": False,
                    "external_relationships_present": external_relationships,
                    "external_resources_loaded": False,
                },
                capability_gaps=gaps,
            )
    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError):
        return _failed(path, "invalid_ooxml_zip", "Office/ZIP 文件损坏或无法读取")


def cache_key(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(PREVIEW_CONTRACT_VERSION.encode("ascii"))
    digest.update(b"\0octagon-office-renderers-v2\0")
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _worker_result(
    path: Path,
    *,
    renderer: str,
    cache_dir: Path | None,
    timeout_seconds: float = WORKER_TIMEOUT_SECONDS,
    expected_cache_key: str | None = None,
) -> dict[str, Any]:
    """Run a renderer in a credential-free subprocess and return its envelope.

    Successful and renderer-level failed results are cached atomically. Worker
    timeout/crash is deliberately not cached so a transient resource problem
    can recover on the next request.
    """
    source_cache_key = expected_cache_key or cache_key(path)
    key = source_cache_key.split(":", 1)[1]
    cache_file: Path | None = None
    if cache_dir is not None:
        # The attempt workspace is agent-writable. Never follow a cache-root
        # symlink that an artifact-producing process could have planted.
        try:
            if cache_dir.is_symlink():
                raise OSError("preview cache root is a symlink")
            cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            if cache_dir.is_symlink():
                raise OSError("preview cache root became a symlink")
            cache_dir.resolve().relative_to(cache_dir.parent.resolve())
        except (OSError, ValueError):
            cache_dir = None
        else:
            candidate_parent = cache_dir / key
            candidate = candidate_parent / f"{renderer}.json"
            try:
                if candidate_parent.is_symlink() or candidate.is_symlink():
                    raise OSError("preview cache entry is a symlink")
                if candidate_parent.exists():
                    candidate_parent.resolve().relative_to(cache_dir.resolve())
            except (OSError, ValueError):
                cache_dir = None
            else:
                cache_file = candidate
    if cache_file is not None:
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and isinstance(cached.get("ok"), bool):
                return cached
        except (OSError, json.JSONDecodeError):
            pass

    worker = Path(__file__).with_name("artifact_worker.py")
    with tempfile.TemporaryDirectory(prefix="octagon-office-") as temp_raw:
        temp = Path(temp_raw)
        result_path = temp / "result.json"
        source_snapshot = temp / f"source{path.suffix.lower()}"
        try:
            # Render a byte-stable snapshot. Without this copy an agent that is
            # still writing the artifact can change it after hashing, causing
            # content B to be cached under content A's key.
            shutil.copyfile(path, source_snapshot)
            if cache_key(source_snapshot) != source_cache_key:
                return {
                    "ok": False,
                    "transient": True,
                    "error": {
                        "code": "artifact_changed",
                        "message": "文件仍在写入，请稍后重试预览",
                    },
                }
        except OSError:
            return {
                "ok": False,
                "transient": True,
                "error": {"code": "artifact_unreadable", "message": "无法读取稳定的预览快照"},
            }
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "HOME": str(temp),
            "TMPDIR": str(temp),
        }
        try:
            completed = subprocess.run(
                [sys.executable, "-I", str(worker), renderer, str(source_snapshot), str(result_path)],
                cwd=temp,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout_seconds,
                check=False,
                start_new_session=True,
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "transient": True,
                "error": {"code": "renderer_timeout", "message": "Office 预览处理超时"},
            }
        if completed.returncode not in (0, 2) or not result_path.is_file():
            return {
                "ok": False,
                "transient": True,
                "error": {"code": "renderer_crashed", "message": "Office 预览进程异常退出"},
            }
        try:
            if result_path.stat().st_size > WORKER_RESULT_BYTES:
                raise ValueError("worker result too large")
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return {
                "ok": False,
                "transient": True,
                "error": {"code": "renderer_invalid_output", "message": "Office 预览结果无效"},
            }
        if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
            return {
                "ok": False,
                "transient": True,
                "error": {"code": "renderer_invalid_output", "message": "Office 预览结果无效"},
            }

    if cache_file is not None and not result.get("transient"):
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            if cache_file.parent.is_symlink() or cache_file.is_symlink():
                raise OSError("preview cache entry became a symlink")
            cache_file.parent.resolve().relative_to(cache_file.parent.parent.resolve())
        except (OSError, ValueError):
            return result
        temporary = cache_file.with_name(
            f".{cache_file.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        temporary.write_text(
            json.dumps(result, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        try:
            os.replace(temporary, cache_file)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return result


def preview_descriptor(
    path: Path,
    artifact_ref: str,
    cache_dir: Path | None = None,
) -> dict[str, Any]:
    inspection = inspect_artifact(path)
    source_cache_key = cache_key(path)
    content: dict[str, Any] | None = None
    renderer = {"name": SCANNER_NAME, "version": SCANNER_VERSION}
    status = inspection.status
    error_code = inspection.error_code
    error_message = inspection.error_message
    gaps = list(inspection.capability_gaps)

    renderer_spec: tuple[str, str, str, list[str]] | None = None
    if inspection.status == "unsupported" and inspection.error_code == "renderer_unavailable":
        if inspection.artifact_type == "spreadsheet":
            from .artifact_xlsx import RENDERER_NAME, RENDERER_VERSION
            renderer_spec = (
                "xlsx-structural", RENDERER_NAME, RENDERER_VERSION,
                ["formulas-not-evaluated", "bounded-structural-preview"],
            )
        elif inspection.artifact_type == "presentation":
            from .artifact_pptx import RENDERER_NAME, RENDERER_VERSION
            renderer_spec = (
                "pptx-static", RENDERER_NAME, RENDERER_VERSION,
                ["theme-fidelity-limited", "group-transform-limited", "bounded-static-preview"],
            )
        elif inspection.artifact_type == "document":
            from .artifact_docx import RENDERER_NAME, RENDERER_VERSION
            renderer_spec = (
                "docx-structural", RENDERER_NAME, RENDERER_VERSION,
                ["pagination-approximate", "bounded-structural-preview"],
            )
    if renderer_spec is not None:
        worker_renderer, renderer_name, renderer_version, renderer_gaps = renderer_spec
        result = _worker_result(
            path,
            renderer=worker_renderer,
            cache_dir=cache_dir,
            expected_cache_key=source_cache_key,
        )
        if not result.get("ok"):
            status = "failed"
            error = result.get("error") if isinstance(result.get("error"), dict) else {}
            error_code = str(error.get("code") or "renderer_failed")
            error_message = str(error.get("message") or "Office 预览失败")
        else:
            content = result.get("content")
            status = "ready"
            error_code = None
            error_message = None
            renderer = {"name": renderer_name, "version": renderer_version}
            gaps = [gap for gap in gaps if gap != "content-renderer-not-installed"]
            gaps.extend(renderer_gaps)
            if isinstance(content, dict) and content.get("images_omitted"):
                gaps.append("embedded-images-omitted")
            if isinstance(content, dict) and isinstance(content.get("features"), list):
                gaps.extend(str(item) for item in content["features"])
    return {
        "version": PREVIEW_CONTRACT_VERSION,
        "artifact": {
            "ref": artifact_ref,
            "name": path.name,
            "size": path.stat().st_size,
            "media_type": inspection.media_type,
            "type": inspection.artifact_type,
        },
        "status": status,
        "counts": inspection.counts,
        "renderer": renderer,
        "error": (
            {"code": error_code, "message": error_message}
            if error_code else None
        ),
        "cache_key": source_cache_key,
        "poll_after_ms": None,
        "security": inspection.security,
        "capability_gaps": gaps,
        "content": content,
    }


def _renderable_office(inspection: Inspection) -> bool:
    return (
        inspection.artifact_type in {"spreadsheet", "presentation", "document"}
        and inspection.status == "unsupported"
        and inspection.error_code == "renderer_unavailable"
    )


def _pending_descriptor(
    path: Path, artifact_ref: str, inspection: Inspection, key: str
) -> dict[str, Any]:
    renderer_by_type = {
        "spreadsheet": ("octagon-xlsx-structural", "1"),
        "presentation": ("octagon-pptx-static", "1"),
        "document": ("octagon-docx-structural", "1"),
    }
    renderer_name, renderer_version = renderer_by_type[inspection.artifact_type]

    gaps = [
        gap for gap in inspection.capability_gaps
        if gap != "content-renderer-not-installed"
    ]
    if "background-rendering" not in gaps:
        gaps.append("background-rendering")
    return {
        "version": PREVIEW_CONTRACT_VERSION,
        "artifact": {
            "ref": artifact_ref,
            "name": path.name,
            "size": path.stat().st_size,
            "media_type": inspection.media_type,
            "type": inspection.artifact_type,
        },
        "status": "rendering",
        "counts": inspection.counts,
        "renderer": {"name": renderer_name, "version": renderer_version},
        "error": None,
        "cache_key": key,
        "poll_after_ms": PREVIEW_POLL_AFTER_MS,
        "security": inspection.security,
        "capability_gaps": gaps,
        "content": None,
    }


def _background_failure(
    path: Path, artifact_ref: str, inspection: Inspection, key: str,
    *, code: str = "renderer_crashed", message: str = "Office 预览后台任务异常退出",
) -> dict[str, Any]:
    value = _pending_descriptor(path, artifact_ref, inspection, key)
    value.update({
        "status": "failed",
        "poll_after_ms": None,
        "error": {"code": code, "message": message},
    })
    return value


def scheduled_preview_descriptor(
    path: Path,
    artifact_ref: str,
    cache_dir: Path | None = None,
) -> dict[str, Any]:
    """Return a descriptor, scheduling large renderer work without blocking API.

    Small files preserve the synchronous contract. Large renderable Office files
    are deduplicated by resolved source path + composite content key and polled
    through the same endpoint until their future completes.
    """
    inspection = inspect_artifact(path)
    try:
        source_size = path.stat().st_size
    except OSError:
        return preview_descriptor(path, artifact_ref, cache_dir)
    if source_size <= SYNC_PREVIEW_MAX_BYTES or not _renderable_office(inspection):
        return preview_descriptor(path, artifact_ref, cache_dir)

    key = cache_key(path)
    job_key = f"{path.resolve()}\0{key}"
    with _BACKGROUND_LOCK:
        future = _BACKGROUND_JOBS.get(job_key)
        if future is None:
            if len(_BACKGROUND_JOBS) >= MAX_BACKGROUND_JOBS:
                for old_key, old_future in list(_BACKGROUND_JOBS.items()):
                    if old_future.done():
                        _BACKGROUND_JOBS.pop(old_key, None)
                    if len(_BACKGROUND_JOBS) < MAX_BACKGROUND_JOBS:
                        break
            if len(_BACKGROUND_JOBS) >= MAX_BACKGROUND_JOBS:
                return _background_failure(
                    path, artifact_ref, inspection, key,
                    code="renderer_queue_full",
                    message="Office 预览任务队列已满，请稍后重试",
                )
            future = _BACKGROUND_EXECUTOR.submit(
                preview_descriptor, path, artifact_ref, cache_dir
            )
            _BACKGROUND_JOBS[job_key] = future

    if not future.done():
        return _pending_descriptor(path, artifact_ref, inspection, key)
    try:
        return future.result()
    except BaseException:
        return _background_failure(path, artifact_ref, inspection, key)
