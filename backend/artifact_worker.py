"""Isolated renderer worker for untrusted Office artifacts.

The API process invokes this module in a fresh Python subprocess with a
minimal environment and an output-file contract.  Renderer failures therefore
cannot corrupt API process state or leak a partial descriptor to callers.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path
from typing import Any


def _install_limits() -> None:
    """Apply best-effort POSIX limits before opening the artifact."""
    try:
        import resource
    except ImportError:  # pragma: no cover - Windows fallback
        return

    limits = (
        ("RLIMIT_CPU", 12),
        ("RLIMIT_AS", 1024 * 1024 * 1024),
        ("RLIMIT_FSIZE", 64 * 1024 * 1024),
        ("RLIMIT_NOFILE", 64),
    )
    for name, value in limits:
        kind = getattr(resource, name, None)
        if kind is None:
            continue
        try:
            resource.setrlimit(kind, (value, value))
        except (OSError, ValueError):
            # Deployment sandboxes may impose an even stricter immutable limit.
            pass


def _disable_network() -> None:
    """Fail closed if a future renderer accidentally tries to open a socket."""

    class NetworkDisabledError(OSError):
        pass

    def blocked(*_args: Any, **_kwargs: Any):
        raise NetworkDisabledError("artifact renderer network access is disabled")

    socket.socket = blocked  # type: ignore[assignment]
    socket.create_connection = blocked  # type: ignore[assignment]


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 3:
        return 64
    renderer, source_raw, output_raw = argv
    source = Path(source_raw).resolve()
    output = Path(output_raw).resolve()

    _install_limits()
    _disable_network()
    os.umask(0o077)

    # Running a file with ``python -I`` intentionally removes the repository
    # from sys.path. Add only this known source root, never cwd/PYTHONPATH.
    repo_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo_root))

    try:
        if renderer == "xlsx-structural":
            from backend.artifact_xlsx import WorkbookPreviewError, render_workbook
            render = render_workbook
            expected_error = WorkbookPreviewError
        elif renderer == "pptx-static":
            from backend.artifact_pptx import PresentationPreviewError, render_presentation
            render = render_presentation
            expected_error = PresentationPreviewError
        elif renderer == "docx-structural":
            from backend.artifact_docx import DocumentPreviewError, render_document
            render = render_document
            expected_error = DocumentPreviewError
        else:
            _write_json(output, {
                "ok": False,
                "error": {"code": "renderer_unknown", "message": "未知 Office renderer"},
            })
            return 2

        try:
            content = render(source)
        except expected_error as exc:
            _write_json(output, {
                "ok": False,
                "error": {"code": exc.code, "message": exc.message},
            })
            return 2
        _write_json(output, {"ok": True, "content": content})
        return 0
    except BaseException:
        # Do not serialize tracebacks or environment details into a public
        # descriptor. The parent maps a non-zero/crashed worker stably.
        return 70


if __name__ == "__main__":  # pragma: no cover - exercised through subprocess
    raise SystemExit(main())
