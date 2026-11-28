"""Modular helpers extracted from ``backend.adapters.blade_service``.

All public and internal symbols remain re-exported from
``backend.adapters.blade_service`` for 100% backward compatibility.
"""

from __future__ import annotations

from .artifacts import (
    _ARTIFACT_MAX_DEPTH,
    _ARTIFACT_MAX_FILES,
    _ARTIFACT_SKIP_DIRS,
    _EDIT_PATH_PATTERNS,
    _EDIT_TOOL_MARKERS,
    _REMOTE_PATH_PREFIXES,
    _agent_edited_paths,
    _candidate_remote_paths,
    _digest_bytes,
    _digest_of,
    _fail_incomplete_iterative_artifact_sync,
    _iterative_artifact_sync_error,
    artifact_recovery_failed,
    download_priority_paths,
    recover_workspace_artifacts,
    sync_blade_skills,
)
from .extraction import (
    _BLADE_SKILL_RUN_RE,
    _PROJECT_TITLE_MAX,
    _ROOT_LOOP,
    _RUNNING_BACKGROUND_TASK_STATUSES,
    _RUNNING_SESSION_STATUSES,
    _TERMINAL_SESSION_STATUSES,
    _TURN_TERMINAL_STATUSES,
    _append_jsonl,
    _append_unique_trace_rows,
    _chat_intent,
    _classify_outcome,
    _event_loop_name,
    _extract_trace_from_history,
    _is_active_chat_error,
    _is_not_found_error,
    _looks_like_error,
    _now_iso,
    _outcome_error_code,
    _parse_blade_skill_run,
    _persist_cleanup_pending_checkpoint,
    _project_prompt_title,
    _projection_identity,
    _render_prompt,
    _render_turn_prompt,
    _session_create_error_detail,
    _trace_row_identity,
    _ts_diff_ms,
    _turn_is_terminal,
    _write_json,
    _write_progress,
)
from .interactions import (
    _MAX_UNEXPECTED_AUTO_ANSWERS,
    UNEXPECTED_INTERACTION_AUTO_ANSWER,
    _interaction_question_summary,
    build_askuser_answer,
    build_generic_askuser_answer,
    match_interaction_turn,
)

__all__ = [
    "UNEXPECTED_INTERACTION_AUTO_ANSWER",
    "_MAX_UNEXPECTED_AUTO_ANSWERS",
    "artifact_recovery_failed",
    "build_askuser_answer",
    "build_generic_askuser_answer",
    "download_priority_paths",
    "match_interaction_turn",
    "recover_workspace_artifacts",
    "sync_blade_skills",
]
