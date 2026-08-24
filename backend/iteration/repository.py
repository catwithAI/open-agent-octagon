"""Submission 顺序、幂等事件和恢复 checkpoint 的轻量 repository。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .models import IterativeReviewPolicy, SubmissionRecord, SubmissionSignal
from .summary import read_iteration_events, summarize_iteration
from .writer import (
    ITERATIONS_FILENAME,
    ITERATION_STATE_FILENAME,
    IterationEventWriter,
    atomic_write_json,
    now_iso,
    operation_id,
)


class IterationRepositoryError(RuntimeError):
    pass


def submission_id(attempt_id: str, round_index: int) -> str:
    digest = hashlib.sha256(
        f"{attempt_id}:{round_index}".encode("utf-8")
    ).hexdigest()[:12]
    return f"sub_{digest}"


class IterationRepository:
    def __init__(self, attempt_dir: Path, *, attempt_id: str) -> None:
        self.attempt_dir = Path(attempt_dir)
        self.attempt_id = attempt_id
        self.events_path = self.attempt_dir / ITERATIONS_FILENAME
        self.state_path = self.attempt_dir / ITERATION_STATE_FILENAME

    def _events(self) -> list[dict[str, Any]]:
        return read_iteration_events(self.events_path)[0]

    def _has_operation(self, operation: str) -> bool:
        expected = operation_id(self.attempt_id, operation)
        return any(event.get("operation_id") == expected for event in self._events())

    def start(
        self,
        policy: IterativeReviewPolicy,
        *,
        producer_session_id: str | None,
    ) -> None:
        if self._has_operation("iteration.start"):
            return
        with IterationEventWriter(
            self.events_path, attempt_id=self.attempt_id
        ) as writer:
            writer.emit(
                "iteration.started",
                operation="iteration.start",
                submission_count=policy.submission_count,
                max_iterations=policy.max_iterations,
                submission_boundary=policy.submission_boundary,
                stop_policy=policy.stop_policy,
                producer_session_id=producer_session_id,
                session_continuity=(
                    "continuous" if producer_session_id else "unknown"
                ),
            )
        self._checkpoint()

    def create_submission(
        self,
        signal: SubmissionSignal,
        *,
        round_index: int,
        producer_session_id: str | None,
        feedback_required: bool,
    ) -> SubmissionRecord:
        summary = summarize_iteration(self.attempt_dir)
        if summary is None:
            raise IterationRepositoryError("iteration 尚未开始")
        if isinstance(round_index, bool) or not isinstance(round_index, int):
            raise IterationRepositoryError("round_index 必须是整数")
        existing_submissions = summary["submissions"]
        if round_index < len(existing_submissions):
            existing_projection = existing_submissions[round_index]
            identifier = str(existing_projection["submission_id"])
            created_event = next(
                event
                for event in self._events()
                if event.get("event") == "submission.created"
                and event.get("submission_id") == identifier
            )
            return self._record_from_event(created_event)
        if round_index != len(existing_submissions):
            raise IterationRepositoryError(
                f"Submission round_index 断裂：期望 {len(existing_submissions)}，"
                f"实际为 {round_index}"
            )
        declared = summary.get("submission_count")
        if isinstance(declared, int) and round_index >= declared:
            raise IterationRepositoryError("Submission 数量已达到声明上限")

        identifier = submission_id(self.attempt_id, round_index)
        operation = f"submission.{round_index}.create"
        expected_operation = operation_id(
            self.attempt_id, operation, identifier
        )
        existing = next(
            (
                event
                for event in self._events()
                if event.get("operation_id") == expected_operation
            ),
            None,
        )
        if existing is None:
            with IterationEventWriter(
                self.events_path, attempt_id=self.attempt_id
            ) as writer:
                existing = writer.emit(
                    "submission.created",
                    operation=operation,
                    submission_id=identifier,
                    round_index=round_index,
                    timestamp=signal.emitted_at,
                    producer_session_id=producer_session_id,
                    summary=signal.summary,
                    verification=signal.verification,
                    signal_source=signal.source,
                    feedback_required=feedback_required,
                )
        self._checkpoint()
        return self._record_from_event(existing)

    def _record_from_event(self, event: dict[str, Any]) -> SubmissionRecord:
        feedback_required = bool(event.get("feedback_required", False))
        return SubmissionRecord(
            submission_id=str(event["submission_id"]),
            attempt_id=self.attempt_id,
            round_index=int(event["round_index"]),
            producer_session_id=event.get("producer_session_id"),
            summary=str(event.get("summary") or ""),
            verification=str(event.get("verification") or ""),
            signal_source=event.get("signal_source", "tool"),
            created_at=str(event.get("timestamp") or now_iso()),
            feedback_status="pending" if feedback_required else "not_required",
        )

    def record_submission_event(
        self,
        event: str,
        *,
        submission: SubmissionRecord,
        operation: str,
        **fields: Any,
    ) -> None:
        expected = operation_id(
            self.attempt_id, operation, submission.submission_id
        )
        if any(row.get("operation_id") == expected for row in self._events()):
            return
        with IterationEventWriter(
            self.events_path, attempt_id=self.attempt_id
        ) as writer:
            writer.emit(
                event,
                operation=operation,
                submission_id=submission.submission_id,
                round_index=submission.round_index,
                **fields,
            )
        self._checkpoint()

    def select_for_final_score(self, submission: SubmissionRecord) -> None:
        summary = summarize_iteration(self.attempt_dir)
        if summary and any(
            row["selected_for_final_score"]
            and row["submission_id"] != submission.submission_id
            for row in summary["submissions"]
        ):
            raise IterationRepositoryError("已存在另一个最终成绩 Submission")
        self.record_submission_event(
            "submission.selected_for_final_score",
            submission=submission,
            operation=f"submission.{submission.round_index}.select_final",
        )

    def complete(self) -> None:
        if self._has_operation("iteration.complete"):
            return
        with IterationEventWriter(
            self.events_path, attempt_id=self.attempt_id
        ) as writer:
            writer.emit("iteration.completed", operation="iteration.complete")
        self._checkpoint()

    def _checkpoint(self) -> None:
        summary = summarize_iteration(self.attempt_dir)
        if summary is not None:
            atomic_write_json(self.state_path, summary)
