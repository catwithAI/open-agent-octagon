from backend.api import CreateRunRequest
from backend.runner import _resolve_input_timeout_seconds
from backend.run_service import NormalizedRunRequest, _resolve_timeout_seconds


def _request(
    timeout_seconds: int | None,
    *,
    explicit: bool,
) -> NormalizedRunRequest:
    return NormalizedRunRequest(
        env_name="env",
        task_id="task",
        timeout_seconds=timeout_seconds,
        timeout_seconds_explicit=explicit,
    )


def test_omitted_timeout_follows_selected_task() -> None:
    request = _request(1000, explicit=False)

    assert _resolve_timeout_seconds(1200, request) == 1200


def test_explicit_timeout_overrides_selected_task() -> None:
    request = _request(1800, explicit=True)

    assert _resolve_timeout_seconds(1200, request) == 1800


def test_explicit_null_disables_selected_task_timeout() -> None:
    request = _request(None, explicit=True)

    assert _resolve_timeout_seconds(1200, request) is None


def test_api_distinguishes_omitted_timeout_from_explicit_null() -> None:
    omitted = CreateRunRequest(env_name="env", task_id="task")
    unlimited = CreateRunRequest(
        env_name="env", task_id="task", timeout_seconds=None
    )

    assert "timeout_seconds" not in omitted.model_fields_set
    assert "timeout_seconds" in unlimited.model_fields_set


def test_attempt_snapshot_preserves_explicit_unlimited_timeout() -> None:
    assert _resolve_input_timeout_seconds(1200, None) is None


def test_attempt_snapshot_omission_still_inherits_task_timeout() -> None:
    assert _resolve_input_timeout_seconds(1200) == 1200
