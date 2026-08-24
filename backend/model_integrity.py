"""Attempt-scoped model integrity enforcement.

The model stored on ``attempts.model`` is the experiment contract.  Local HTTP
agents are routed through Octagon's reverse proxy, so the proxy can compare the
model in every LLM request with that contract *before* sending any bytes to the
upstream provider.

Capture and integrity are deliberately separate concerns:

* capture is observational and may be disabled/fail open;
* model integrity is a comparison validity boundary and must fail closed.

No credentials are read or persisted here.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import _now_iso, _open_sync

logger = logging.getLogger(__name__)

STATUS_NOT_OBSERVED = "not_observed"
STATUS_VERIFIED = "verified"
STATUS_VIOLATED = "violated"


@dataclass(frozen=True)
class ModelDecision:
    allowed: bool
    expected: str | None
    observed: str | None
    error_code: str | None = None
    reason: str | None = None
    enforced: bool = False


# attempt models are immutable after creation.  Include the database path in
# cache keys because tests and multi-instance deployments may reuse attempt IDs.
_EXPECTED_CACHE: dict[tuple[str, str], str | None] = {}
_RECORDED_MATCHES: set[tuple[str, str, str]] = set()


def reset_caches() -> None:
    """Clear process-local hot-path caches (primarily for runtime/test reset)."""
    _EXPECTED_CACHE.clear()
    _RECORDED_MATCHES.clear()


def _runtime_db_path() -> Path | None:
    try:
        from . import runtime_state

        return Path(runtime_state.get().db_path)
    except RuntimeError:
        # Direct proxy unit tests intentionally run without a bound runtime.
        return None


def _expected_model(db_path: Path, attempt_id: str) -> tuple[bool, str | None]:
    key = (str(db_path), attempt_id)
    if key in _EXPECTED_CACHE:
        return True, _EXPECTED_CACHE[key]
    with _open_sync(db_path) as conn:
        row = conn.execute(
            "SELECT model FROM attempts WHERE id=?", (attempt_id,)
        ).fetchone()
    if row is None:
        return False, None
    expected = row[0] if isinstance(row[0], str) and row[0].strip() else None
    _EXPECTED_CACHE[key] = expected
    return True, expected


def normalize_expected_model(expected: str, provider: str) -> str:
    """Remove only the configured Octagon provider prefix.

    Model IDs themselves commonly contain ``/`` (for example
    ``deepseek/deepseek-v4-flash``), so generic first-segment stripping would
    make unrelated models compare equal.  The proxy route already knows the
    exact provider name and only that prefix is safe to remove.
    """
    prefix = provider.strip("/") + "/"
    return expected[len(prefix):] if expected.startswith(prefix) else expected


def _short(value: str | None) -> str:
    if value is None:
        return "<missing>"
    return value if len(value) <= 200 else value[:197] + "..."


def _record(
    db_path: Path,
    attempt_id: str,
    *,
    observed: str | None,
    violation: bool,
    error_code: str | None,
    error_message: str | None,
) -> None:
    match_key = (
        str(db_path),
        attempt_id,
        observed or "",
    )
    if not violation and match_key in _RECORDED_MATCHES:
        return

    with _open_sync(db_path) as conn:
        # Serialize read-modify-write of the observed-model set.  Several child
        # agents may issue their first request concurrently.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT model_integrity_status,model_integrity_observed_json,"
            "model_integrity_error_code,model_integrity_error_message "
            "FROM attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()
        if row is None:
            conn.rollback()
            raise LookupError(f"attempt not found: {attempt_id}")
        try:
            parsed = json.loads(row[1] or "[]")
            observed_models = [
                item for item in parsed
                if isinstance(item, str) and item
            ] if isinstance(parsed, list) else []
        except (TypeError, ValueError):
            observed_models = []
        if observed and observed not in observed_models:
            observed_models.append(observed)

        current = row[0] or STATUS_NOT_OBSERVED
        status = (
            STATUS_VIOLATED
            if violation or current == STATUS_VIOLATED
            else STATUS_VERIFIED
        )
        persisted_error_code = (
            error_code if violation
            else row[2] if current == STATUS_VIOLATED
            else None
        )
        persisted_error_message = (
            error_message if violation
            else row[3] if current == STATUS_VIOLATED
            else None
        )
        conn.execute(
            "UPDATE attempts SET model_integrity_status=?,"
            "model_integrity_observed_json=?,"
            "model_integrity_violation_count="
            "model_integrity_violation_count+?,"
            "model_integrity_error_code=?,model_integrity_error_message=?,"
            "model_integrity_checked_at=? WHERE id=?",
            (
                status,
                json.dumps(observed_models, ensure_ascii=False),
                1 if violation else 0,
                persisted_error_code,
                persisted_error_message,
                _now_iso(),
                attempt_id,
            ),
        )
        conn.commit()
    if not violation:
        _RECORDED_MATCHES.add(match_key)


def validate_proxy_model(
    attempt_id: str,
    provider: str,
    observed: str | None,
) -> ModelDecision:
    """Validate and persist one outbound LLM request.

    A bound runtime with an existing attempt is enforced fail-closed.  A missing
    runtime is only accepted for low-level direct unit tests; production proxy
    routes always run under ``runtime_state``.
    """
    db_path = _runtime_db_path()
    if db_path is None:
        return ModelDecision(True, None, observed, enforced=False)

    try:
        found, raw_expected = _expected_model(db_path, attempt_id)
    except Exception as exc:
        logger.exception(
            "model integrity lookup failed attempt=%s", attempt_id
        )
        return ModelDecision(
            False,
            None,
            observed,
            error_code="model_integrity_unavailable",
            reason=f"model integrity lookup unavailable: {type(exc).__name__}",
            enforced=True,
        )
    if not found:
        return ModelDecision(
            False,
            None,
            observed,
            error_code="model_integrity_attempt_missing",
            reason="model integrity attempt record missing",
            enforced=True,
        )
    if raw_expected is None:
        # Legacy/default-model attempts have no explicit experiment contract.
        # Preserve compatibility, but do not claim they were verified.
        return ModelDecision(True, None, observed, enforced=False)

    expected = normalize_expected_model(raw_expected, provider)
    violation = not observed or observed != expected
    error_code = (
        "model_integrity_model_missing"
        if not observed
        else "model_integrity_model_mismatch"
        if violation
        else None
    )
    reason = (
        f"model integrity violation: expected={_short(expected)!r}, "
        f"observed={_short(observed)!r}"
        if violation
        else None
    )
    try:
        _record(
            db_path,
            attempt_id,
            observed=observed,
            violation=violation,
            error_code=error_code,
            error_message=reason,
        )
    except Exception as exc:
        logger.exception(
            "model integrity persistence failed attempt=%s", attempt_id
        )
        return ModelDecision(
            False,
            expected,
            observed,
            error_code="model_integrity_unavailable",
            reason=f"model integrity persistence unavailable: {type(exc).__name__}",
            enforced=True,
        )
    return ModelDecision(
        not violation,
        expected,
        observed,
        error_code=error_code,
        reason=reason,
        enforced=True,
    )


def observe_model(
    db_path: Path,
    attempt_id: str,
    observed: str,
    *,
    provider: str | None = None,
) -> ModelDecision:
    """Persist a trusted post-run model observation.

    This is used for Blade, whose inference happens inside the remote Blade
    service and therefore cannot pass through Octagon's pre-forward proxy.
    """
    found, raw_expected = _expected_model(Path(db_path), attempt_id)
    if not found or raw_expected is None:
        return ModelDecision(True, raw_expected, observed, enforced=False)
    expected = (
        normalize_expected_model(raw_expected, provider)
        if provider
        else raw_expected
    )
    violation = observed != expected
    error_code = "model_integrity_model_mismatch" if violation else None
    reason = (
        f"model integrity violation: expected={_short(expected)!r}, "
        f"observed={_short(observed)!r}"
        if violation
        else None
    )
    _record(
        Path(db_path),
        attempt_id,
        observed=observed,
        violation=violation,
        error_code=error_code,
        error_message=reason,
    )
    return ModelDecision(
        not violation,
        expected,
        observed,
        error_code=error_code,
        reason=reason,
        enforced=True,
    )


def audit_blade_event_models(
    db_path: Path,
    data_path: Path,
    attempt_id: str,
) -> list[ModelDecision]:
    """Verify models reported by Blade's trusted transport events.

    Blade calls cannot be blocked locally because they originate inside the
    remote service.  The session model remains the prevention boundary; this
    audit is the independent detection boundary that invalidates scoring when
    a root or fork response reports another model.
    """
    events_path = Path(data_path) / "attempts" / attempt_id / "events.jsonl"
    if not events_path.is_file():
        return []
    observed: list[str] = []
    try:
        with events_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(event, dict):
                    continue
                kind = event.get("kind")
                raw = event.get("raw")
                if not isinstance(raw, dict):
                    continue
                model: Any = None
                if kind == "turn:end":
                    model = raw.get("model")
                elif kind == "llm:response:done":
                    payload = raw.get("payload")
                    if isinstance(payload, dict):
                        model = payload.get("model")
                if (
                    isinstance(model, str)
                    and model.strip()
                    and model.strip() not in observed
                ):
                    observed.append(model.strip())
    except OSError:
        logger.exception("blade model event audit failed attempt=%s", attempt_id)
        return []
    return [
        observe_model(Path(db_path), attempt_id, model)
        for model in observed
    ]


def get_attempt_integrity(
    db_path: Path, attempt_id: str
) -> dict[str, Any] | None:
    """Read the persisted integrity result for runner/API consumers."""
    with _open_sync(db_path) as conn:
        row = conn.execute(
            "SELECT model,model_integrity_status,"
            "model_integrity_observed_json,model_integrity_violation_count,"
            "model_integrity_error_code,model_integrity_error_message,"
            "model_integrity_checked_at FROM attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()
    if row is None:
        return None
    try:
        observed = json.loads(row[2] or "[]")
        if not isinstance(observed, list):
            observed = []
    except (TypeError, ValueError):
        observed = []
    return {
        "expected_model": row[0],
        "status": row[1] or STATUS_NOT_OBSERVED,
        "observed_models": [x for x in observed if isinstance(x, str)],
        "violation_count": int(row[3] or 0),
        "error_code": row[4],
        "error_message": row[5],
        "checked_at": row[6],
        "valid": False if row[1] == STATUS_VIOLATED else (
            True if row[1] == STATUS_VERIFIED else None
        ),
    }
