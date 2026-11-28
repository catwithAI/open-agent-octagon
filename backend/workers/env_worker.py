"""Isolated subprocess worker for untrusted environment code execution.

Decouples ``envs/<name>/core.py`` and ``envs/<name>/scorer.py`` from the FastAPI
web process so external scenario code never imports into the API process and
cannot access in-memory ``runtime_state.run_credentials`` or admin environment
variables (``OPENROUTER_MANAGEMENT_KEY``, ``SAME_MODEL_SSH_PASSWORD``, etc.).

Invocation protocol (parent -> child):
    ``python -m backend.workers.env_worker <action> --request <json> --output <json>``

Using dedicated private files inside a ``0700`` temporary directory ensures
stray ``print()`` output from external ``core.py`` / ``scorer.py`` modules never
corrupts the JSON IPC channel.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import inspect
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterable, Mapping
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from octagon.env_api import EnvContext, TraceWriter, clear_current_registry, get_current_registry

_REPO_ROOT = str(Path(__file__).resolve().parents[2])
_STDERR_SNIPPET_LIMIT = 2000
_KILL_GRACE_SECONDS = 2.0

# Minimal OS/Python runtime allowlist for inspect-env and call-tool.
# Provider API keys are intentionally excluded so core.py never sees them.
SAFE_BASELINE_ENV_KEYS: frozenset[str] = frozenset({
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TMPDIR",
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "VIRTUAL_ENV",
    "PYTHONUTF8",
    "PYTHONIOENCODING",
    "PYTHONPATH",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "SELECTED_SKILLS_DIR",
})

# High-privilege secrets that MUST NEVER enter any isolated worker, even via extra_env.
HARD_ADMIN_DENYLIST: frozenset[str] = frozenset({
    "OPENROUTER_MANAGEMENT_KEY",
    "SAME_MODEL_SSH_PASSWORD",
    "SAME_MODEL_BLADE_API_KEY",
    "INSIGHTS_API_KEY",
})

# Sensitive secrets stripped from inherited process env by default.
# Keys outside HARD_ADMIN_DENYLIST (BLADE_API_KEY, OCTAGON_RUN_LLM_API_KEY,
# OCTAGON_RUN_LLM_BASE_URL) may only be injected explicitly per job via extra_env.
SENSITIVE_ENV_DENYLIST: frozenset[str] = HARD_ADMIN_DENYLIST | frozenset({
    "BLADE_API_KEY",
    "OCTAGON_RUN_LLM_API_KEY",
    "OCTAGON_RUN_LLM_BASE_URL",
})

_MANIFEST_FORBIDDEN_KEYS: frozenset[str] = frozenset({
    "api_key",
    "token",
    "secret",
    "password",
    "authorization",
})

_ACTIVE_SCORER_PROCS_LOCK = threading.Lock()
_ACTIVE_SCORER_PROCS: dict[str, set[subprocess.Popen[str]]] = {}


class EnvWorkerError(RuntimeError):
    """Raised when the isolated env worker fails unexpectedly."""


class EnvWorkerTimeoutError(EnvWorkerError, TimeoutError):
    """Raised when an isolated env worker exceeds its hard timeout."""


class EnvToolExecutionError(EnvWorkerError):
    """Raised when an isolated env tool raises an application exception."""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(f"{error_type}: {message}")
        self.error_type = error_type
        self.message = message


def build_isolated_worker_env(
    *,
    action: str,
    base_env: Mapping[str, str] | None = None,
    extra_env: Mapping[str, str] | None = None,
    extra_deny: Iterable[str] = (),
) -> dict[str, str]:
    """Build a scrubbed environment dict for an isolated worker subprocess.

    - ``inspect-env`` / ``call-tool``: strict allowlist of safe OS/Python runtime
      vars (plus ``SELECTED_SKILLS_DIR`` if set), stripping all provider and
      admin credentials.
    - ``run-scorer``: baseline + non-denylisted vars minus
      ``SENSITIVE_ENV_DENYLIST`` and ``extra_deny``, overlaid with job-specific
      ``extra_env`` (e.g. ``judge_env_overlay()``), while still unconditionally
      blocking ``HARD_ADMIN_DENYLIST`` and ``extra_deny``.
    """
    source = os.environ if base_env is None else base_env
    extra_deny_set = {k for k in extra_deny if isinstance(k, str) and k}
    hard_denied = HARD_ADMIN_DENYLIST | extra_deny_set
    inherited_denied = SENSITIVE_ENV_DENYLIST | extra_deny_set

    if action in ("inspect-env", "call-tool"):
        env: dict[str, str] = {
            k: str(v)
            for k, v in source.items()
            if k in SAFE_BASELINE_ENV_KEYS and k not in inherited_denied
        }
    elif action == "run-scorer":
        env = {
            k: str(v)
            for k, v in source.items()
            if k not in inherited_denied
        }
    else:
        raise ValueError(f"unsupported env worker action: {action!r}")

    if extra_env:
        for k, v in extra_env.items():
            if not isinstance(k, str) or not k or v is None:
                continue
            if k in hard_denied:
                continue
            env[k] = str(v)

    env.setdefault("PYTHONUTF8", "1")
    return env


def _terminate_process_group(
    proc: subprocess.Popen[str], *, grace_seconds: float = _KILL_GRACE_SECONDS
) -> None:
    """Terminate a child process group with SIGTERM -> grace -> SIGKILL and reap."""
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.terminate()
            except OSError:
                pass
        try:
            proc.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                pass
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def _register_active_scorer(cancel_key: str, proc: subprocess.Popen[str]) -> None:
    with _ACTIVE_SCORER_PROCS_LOCK:
        _ACTIVE_SCORER_PROCS.setdefault(cancel_key, set()).add(proc)


def _unregister_active_scorer(cancel_key: str, proc: subprocess.Popen[str]) -> None:
    with _ACTIVE_SCORER_PROCS_LOCK:
        procs = _ACTIVE_SCORER_PROCS.get(cancel_key)
        if procs is not None:
            procs.discard(proc)
            if not procs:
                _ACTIVE_SCORER_PROCS.pop(cancel_key, None)


def cancel_active_scorer_processes(attempt_id: str) -> int:
    """Terminate all active isolated scorer subprocesses tracked for ``attempt_id``."""
    with _ACTIVE_SCORER_PROCS_LOCK:
        procs = list(_ACTIVE_SCORER_PROCS.get(attempt_id, ()))
    killed = 0
    for proc in procs:
        if proc.poll() is None:
            _terminate_process_group(proc)
            killed += 1
    return killed


def _ensure_repo_root_on_pythonpath(worker_env: dict[str, str]) -> dict[str, str]:
    env = dict(worker_env)
    existing = env.get("PYTHONPATH", "")
    parts = [p for p in existing.split(os.pathsep) if p] if existing else []
    if _REPO_ROOT not in parts:
        parts.insert(0, _REPO_ROOT)
        env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, raw)
    finally:
        os.close(fd)


def _run_worker_command(
    action: str,
    payload: dict[str, Any],
    *,
    timeout_seconds: float,
    extra_env: Mapping[str, str] | None = None,
    extra_deny: Iterable[str] = (),
    cancel_key: str | None = None,
) -> dict[str, Any]:
    worker_env = _ensure_repo_root_on_pythonpath(
        build_isolated_worker_env(
            action=action,
            extra_env=extra_env,
            extra_deny=extra_deny,
        )
    )
    with tempfile.TemporaryDirectory(prefix="octagon-env-worker-") as tmpdir:
        tmp = Path(tmpdir)
        req_path = tmp / "request.json"
        resp_path = tmp / "response.json"
        _write_private_json(req_path, payload)

        cmd = [
            sys.executable,
            "-m",
            "backend.workers.env_worker",
            action,
            "--request",
            str(req_path),
            "--output",
            str(resp_path),
        ]
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=worker_env,
            start_new_session=True,
            text=True,
        )
        if cancel_key:
            _register_active_scorer(cancel_key, proc)
        try:
            try:
                _stdout, stderr = proc.communicate(timeout=timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                _terminate_process_group(proc)
                raise EnvWorkerTimeoutError(
                    f"env worker {action!r} timed out after {timeout_seconds}s"
                ) from exc
        except BaseException:
            if proc.poll() is None:
                _terminate_process_group(proc)
            raise
        finally:
            if cancel_key:
                _unregister_active_scorer(cancel_key, proc)

        response: dict[str, Any] | None = None
        if resp_path.is_file():
            try:
                parsed = json.loads(resp_path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    response = parsed
            except (OSError, json.JSONDecodeError):
                response = None

        if response is not None and (
            proc.returncode == 0 or (proc.returncode == 2 and response.get("ok") is False)
        ):
            return response

        snippet = (stderr or "").strip()
        if len(snippet) > _STDERR_SNIPPET_LIMIT:
            snippet = snippet[-_STDERR_SNIPPET_LIMIT:]
        raise EnvWorkerError(
            f"env worker {action!r} exited with code {proc.returncode}"
            + (f": {snippet}" if snippet else "")
        )


# ---------- Parent-side public API ----------------------------------------


def inspect_env_in_subprocess(
    env_name: str,
    env_dir: Path,
    *,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Inspect ``core.py`` and ``scorer.py`` inside an isolated subprocess."""
    resp = _run_worker_command(
        "inspect-env",
        {"env_name": env_name, "env_dir": str(Path(env_dir).resolve())},
        timeout_seconds=timeout_seconds,
    )
    if not resp.get("ok"):
        raise EnvWorkerError(
            f"inspect-env failed for {env_name}: {resp.get('message') or resp}"
        )
    return resp


def call_tool_in_subprocess(
    *,
    env_name: str,
    env_dir: Path,
    tool_name: str,
    data_path: Path,
    attempt_id: str,
    env_session_id: str,
    arguments: dict[str, Any],
    timeout_seconds: float = 120.0,
) -> Any:
    """Invoke a single ``@env_tool`` inside an isolated subprocess."""
    resp = _run_worker_command(
        "call-tool",
        {
            "env_name": env_name,
            "env_dir": str(Path(env_dir).resolve()),
            "tool_name": tool_name,
            "data_path": str(Path(data_path).resolve()),
            "attempt_id": attempt_id,
            "env_session_id": env_session_id,
            "arguments": arguments,
        },
        timeout_seconds=timeout_seconds,
    )
    if resp.get("ok") is True:
        return resp.get("result")
    raise EnvToolExecutionError(
        error_type=str(resp.get("error_type") or "RuntimeError"),
        message=str(resp.get("message") or "tool execution failed"),
    )


def _run_scorer_in_subprocess_raw(
    *,
    env_name: str,
    env_dir: Path,
    fn_name: str,
    kwargs: dict[str, Any],
    timeout_seconds: float = 300.0,
    extra_env: dict[str, str] | None = None,
    extra_deny: Iterable[str] = (),
) -> tuple[Any, dict[str, Any] | None]:
    serialized_kwargs: dict[str, Any] = {}
    path_keys: list[str] = []
    for key, value in kwargs.items():
        if isinstance(value, Path):
            serialized_kwargs[key] = str(value)
            path_keys.append(key)
        else:
            serialized_kwargs[key] = value

    attempt_id = kwargs.get("attempt_id")
    cancel_key = str(attempt_id) if isinstance(attempt_id, str) and attempt_id else None

    resp = _run_worker_command(
        "run-scorer",
        {
            "env_name": env_name,
            "env_dir": str(Path(env_dir).resolve()),
            "fn_name": fn_name,
            "kwargs": serialized_kwargs,
            "path_keys": path_keys,
        },
        timeout_seconds=timeout_seconds,
        extra_env=extra_env,
        extra_deny=extra_deny,
        cancel_key=cancel_key,
    )
    if resp.get("ok") is True:
        declared = resp.get("declared_manifest")
        return resp.get("result"), declared if isinstance(declared, dict) else None

    error_type = str(resp.get("error_type") or "RuntimeError")
    message = str(resp.get("message") or "scorer execution failed")
    if error_type == "ScorerUnavailableError":
        from backend.evaluator import ScorerUnavailableError

        raise ScorerUnavailableError(message)
    if error_type == "TypeError":
        raise TypeError(message)
    raise RuntimeError(f"{error_type}: {message}")


def run_scorer_in_subprocess(
    *,
    env_name: str,
    env_dir: Path,
    fn_name: str,
    kwargs: dict[str, Any],
    timeout_seconds: float = 300.0,
    extra_env: dict[str, str] | None = None,
    extra_deny: Iterable[str] = (),
) -> Any:
    """Execute ``scorer.py:<fn_name>`` inside an isolated subprocess."""
    result, _declared = _run_scorer_in_subprocess_raw(
        env_name=env_name,
        env_dir=env_dir,
        fn_name=fn_name,
        kwargs=kwargs,
        timeout_seconds=timeout_seconds,
        extra_env=extra_env,
        extra_deny=extra_deny,
    )
    return result


# ---------- Child-side helpers & action handlers --------------------------


def _module_name_for(env_name: str, kind: str) -> str:
    return f"_octagon_env_{env_name.replace('-', '_')}_{kind}"


def _exec_module(module_name: str, path: Path) -> Any:
    original_sys_path = list(sys.path)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为 {path} 创建 import spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    finally:
        if sys.path != original_sys_path:
            sys.path[:] = original_sys_path
    return module


def _serialize_tool_result(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, dict):
        return {k: _serialize_tool_result(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_serialize_tool_result(v) for v in value]
    if isinstance(value, tuple):
        return [_serialize_tool_result(v) for v in value]
    return value


def _sanitize_manifest(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): (
                "<redacted>"
                if str(key).lower() in _MANIFEST_FORBIDDEN_KEYS
                else _sanitize_manifest(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_manifest(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


def _open_worker_env_db(
    data_path: Path, attempt_id: str, env_dir: Path
) -> sqlite3.Connection:
    attempt_dir = data_path / "attempts" / attempt_id
    attempt_dir.mkdir(parents=True, exist_ok=True)
    env_db_path = attempt_dir / "env.db"
    needs_init = not env_db_path.exists()
    conn = sqlite3.connect(env_db_path, timeout=30.0)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        if needs_init:
            conn.execute("PRAGMA journal_mode=WAL")
            schema_path = env_dir / "schema.sql"
            if schema_path.is_file():
                conn.executescript(schema_path.read_text(encoding="utf-8"))
                conn.commit()
    except BaseException:
        conn.close()
        raise
    return conn


def _handle_inspect_env(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    env_name = str(req["env_name"])
    env_dir = Path(req["env_dir"])

    core_path = env_dir / "core.py"
    core_error: dict[str, str] | None = None
    tools_payload: list[dict[str, Any]] = []

    if not core_path.is_file():
        core_error = {"stage": "core", "detail": f"缺少 {core_path.name}"}
    else:
        clear_current_registry()
        try:
            _exec_module(_module_name_for(env_name, "core"), core_path)
        except Exception as exc:
            core_error = {"stage": "core", "detail": f"import {core_path.name} 失败: {exc}"}
        else:
            tools = get_current_registry()
            tools_payload = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters_schema": tool.parameters,
                    "is_async": bool(tool.is_async),
                }
                for tool in tools.values()
            ]
        finally:
            clear_current_registry()

    scorer_path = env_dir / "scorer.py"
    scorer_error: dict[str, str] | None = None
    scorer_payload: dict[str, Any] | None = None

    if scorer_path.is_file():
        try:
            scorer_mod = _exec_module(_module_name_for(env_name, "scorer"), scorer_path)
        except Exception as exc:
            scorer_error = {
                "stage": "scorer",
                "detail": f"import {scorer_path.name} 失败: {exc}",
            }
        else:
            score_fn = getattr(scorer_mod, "score", None)
            eval_sub_fn = getattr(scorer_mod, "evaluate_submission", None)
            cancel_fn = getattr(scorer_mod, "cancel_scoring", None)
            declared_version = getattr(scorer_mod, "__octagon_scorer_version__", None)
            score_fn_version = getattr(score_fn, "__version__", None) if score_fn is not None else None
            try:
                source_hash = f"sha256:{hashlib.sha256(scorer_path.read_bytes()).hexdigest()}"
            except OSError:
                source_hash = None
            rubric_constants = {
                name: str(value)
                for name, value in sorted(vars(scorer_mod).items())
                if "RUBRIC" in name.upper() and isinstance(value, (str, Path))
            }
            scorer_payload = {
                "present": True,
                "has_score": callable(score_fn),
                "has_evaluate_submission": callable(eval_sub_fn),
                "has_cancel_scoring": callable(cancel_fn),
                "scorer_version": (
                    declared_version if isinstance(declared_version, str) else None
                ),
                "score_fn_version": (
                    score_fn_version if isinstance(score_fn_version, str) else None
                ),
                "source_hash": source_hash,
                "rubric_constants": rubric_constants,
            }

    return 0, {
        "ok": True,
        "core_error": core_error,
        "scorer_error": scorer_error,
        "tools": tools_payload,
        "scorer": scorer_payload,
    }


def _handle_call_tool(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    env_name = str(req["env_name"])
    env_dir = Path(req["env_dir"])
    tool_name = str(req["tool_name"])
    data_path = Path(req["data_path"])
    attempt_id = str(req["attempt_id"])
    env_session_id = str(req["env_session_id"])
    arguments = req.get("arguments") or {}
    if not isinstance(arguments, dict):
        return 2, {
            "ok": False,
            "error_type": "TypeError",
            "message": f"tool arguments must be a dict, got {type(arguments).__name__}",
        }

    core_path = env_dir / "core.py"
    clear_current_registry()
    try:
        _exec_module(_module_name_for(env_name, "core"), core_path)
        tools = get_current_registry()
    except Exception as exc:
        return 2, {
            "ok": False,
            "error_type": exc.__class__.__name__,
            "message": str(exc),
        }
    finally:
        clear_current_registry()

    tool = tools.get(tool_name)
    if tool is None:
        return 2, {
            "ok": False,
            "error_type": "KeyError",
            "message": f"tool not found in env={env_name}: {tool_name}",
        }

    env_db = _open_worker_env_db(data_path, attempt_id, env_dir)
    try:
        ctx = EnvContext(
            attempt_id=attempt_id,
            env_session_id=env_session_id,
            db=env_db,
            trace=TraceWriter(
                data_path=data_path,
                attempt_id=attempt_id,
                env_session_id=env_session_id,
            ),
        )
        try:
            result = asyncio.run(tool.acall(ctx, **arguments))
        except Exception as exc:
            return 2, {
                "ok": False,
                "error_type": exc.__class__.__name__,
                "message": str(exc),
            }
    finally:
        env_db.close()

    return 0, {"ok": True, "result": _serialize_tool_result(result)}


def _handle_run_scorer(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    env_name = str(req["env_name"])
    env_dir = Path(req["env_dir"])
    fn_name = str(req["fn_name"])
    raw_kwargs = req.get("kwargs") or {}
    path_keys = set(req.get("path_keys") or ()) | {"env_db"}

    scorer_path = env_dir / "scorer.py"
    try:
        scorer_mod = _exec_module(_module_name_for(env_name, "scorer"), scorer_path)
    except Exception as exc:
        return 2, {
            "ok": False,
            "error_type": exc.__class__.__name__,
            "message": str(exc),
        }

    fn = getattr(scorer_mod, fn_name, None)
    if not callable(fn):
        return 2, {
            "ok": False,
            "error_type": "AttributeError",
            "message": f"scorer.py in env={env_name} missing callable {fn_name!r}",
        }

    kwargs: dict[str, Any] = {}
    for key, value in raw_kwargs.items():
        if key in path_keys and isinstance(value, str):
            kwargs[key] = Path(value)
        else:
            kwargs[key] = value

    sig = inspect.signature(fn)
    has_var_keyword = any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )
    if has_var_keyword:
        call_kwargs = kwargs
    else:
        call_kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}

    try:
        result = fn(**call_kwargs)
        declared_manifest: dict[str, Any] | None = None
        raw_declared = getattr(fn, "__octagon_evaluation_manifest__", None)
        if callable(raw_declared):
            raw_declared = raw_declared()
        if raw_declared is not None:
            if not isinstance(raw_declared, dict):
                raise TypeError("scorer evaluation manifest hook must return a mapping")
            declared_manifest = _sanitize_manifest(raw_declared)
    except Exception as exc:
        return 2, {
            "ok": False,
            "error_type": exc.__class__.__name__,
            "message": str(exc),
        }

    return 0, {
        "ok": True,
        "result": result,
        "declared_manifest": declared_manifest,
    }


def _write_output(path: Path, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    path.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.workers.env_worker")
    parser.add_argument("action", choices=("inspect-env", "call-tool", "run-scorer"))
    parser.add_argument("--request", required=True, help="Path to input JSON request")
    parser.add_argument("--output", required=True, help="Path to output JSON response")
    args = parser.parse_args(argv)

    req_path = Path(args.request)
    out_path = Path(args.output)
    try:
        req = json.loads(req_path.read_text(encoding="utf-8"))
        if not isinstance(req, dict):
            raise ValueError("request payload must be a JSON object")
        if args.action == "inspect-env":
            code, payload = _handle_inspect_env(req)
        elif args.action == "call-tool":
            code, payload = _handle_call_tool(req)
        else:
            code, payload = _handle_run_scorer(req)
    except Exception as exc:
        _write_output(
            out_path,
            {
                "ok": False,
                "error_type": exc.__class__.__name__,
                "message": str(exc),
            },
        )
        return 2

    _write_output(out_path, payload)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
