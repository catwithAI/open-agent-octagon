"""正常 Agent turn 结束后的 Submission、评测和返工控制核心。"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import importlib.util
import inspect
import json
import os
import threading
import uuid
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

from ..db import _open_sync
from .evaluator import evaluate_submission, load_submission_evaluation
from .models import IterativeReviewPolicy, SubmissionEvaluation, SubmissionSignal
from .repository import IterationRepository
from .snapshot import freeze_submission_snapshot
from .summary import summarize_iteration
from .writer import atomic_write_json, now_iso


@dataclass(frozen=True, slots=True)
class IterationTurnDecision:
    round_index: int
    submission_id: str
    evaluation: SubmissionEvaluation
    next_prompt: str | None
    completed: bool


class IterationControllerError(RuntimeError):
    def __init__(self, message: str, *, status: str, error_code: str) -> None:
        super().__init__(message)
        self.status = status
        self.error_code = error_code


def _deadline_after(seconds: float) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=seconds)
    ).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _shift_deadline(value: str, seconds: float) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (parsed + timedelta(seconds=seconds)).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


class _ExecutionDeadlineLease:
    """Evaluator 阶段的持久化 watchdog 租约。

    进入 evaluator 时临时把 execution deadline 切换为短租约并周期续租；退出时
    按 evaluator 实际耗时平移原 Agent deadline。进程崩溃后续租停止，sweeper
    仍能在租约到期后回收 attempt，不会永久占用并发槽。
    """

    def __init__(
        self,
        *,
        db_path: Path,
        attempt_id: str,
        lease_seconds: float = 60.0,
    ) -> None:
        self.db_path = Path(db_path)
        self.attempt_id = attempt_id
        self.lease_seconds = max(0.05, float(lease_seconds))
        self._original_deadline: str | None = None
        self._evaluator_started_at: str | None = None
        self._active = False
        self._renew_task: asyncio.Task[None] | None = None

    def _load_original_and_renew(self) -> None:
        with _open_sync(self.db_path) as conn:
            row = conn.execute(
                "SELECT execution_deadline_at,execution_agent_deadline_at,"
                "execution_evaluator_started_at FROM attempts WHERE id=? "
                "AND execution_status='running'",
                (self.attempt_id,),
            ).fetchone()
            if not row:
                return
            effective_deadline, agent_deadline, evaluator_started_at = row
            if evaluator_started_at:
                # 后端在 evaluator 中重启：canonical Agent deadline 与最初暂停
                # 时刻均已持久化，不能把旧 lease deadline 当成 Agent 预算。
                self._original_deadline = agent_deadline
                self._evaluator_started_at = evaluator_started_at
            else:
                self._original_deadline = (
                    agent_deadline
                    if agent_deadline is not None
                    else effective_deadline
                )
                self._evaluator_started_at = _deadline_after(0)
            conn.execute(
                "UPDATE attempts SET execution_deadline_at=?,"
                "execution_agent_deadline_at=?,execution_evaluator_started_at=? "
                "WHERE id=? AND execution_status='running'",
                (
                    _deadline_after(self.lease_seconds),
                    self._original_deadline,
                    self._evaluator_started_at,
                    self.attempt_id,
                ),
            )
            conn.commit()
            self._active = True

    def _renew(self) -> None:
        with _open_sync(self.db_path) as conn:
            conn.execute(
                "UPDATE attempts SET execution_deadline_at=? "
                "WHERE id=? AND execution_status='running'",
                (_deadline_after(self.lease_seconds), self.attempt_id),
            )
            conn.commit()

    async def _renew_loop(self) -> None:
        interval = max(0.02, self.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            self._renew()

    async def __aenter__(self) -> "_ExecutionDeadlineLease":
        self._load_original_and_renew()
        if self._active:
            self._renew_task = asyncio.create_task(self._renew_loop())
        return self

    async def __aexit__(self, _exc_type, _exc, _tb) -> None:
        if self._renew_task is not None:
            self._renew_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._renew_task
        if not self._active or self._evaluator_started_at is None:
            return
        started = datetime.fromisoformat(
            self._evaluator_started_at.replace("Z", "+00:00")
        )
        elapsed = max(0.0, (datetime.now(timezone.utc) - started).total_seconds())
        restored = (
            None
            if self._original_deadline is None
            else _shift_deadline(self._original_deadline, elapsed)
        )
        with _open_sync(self.db_path) as conn:
            conn.execute(
                "UPDATE attempts SET execution_deadline_at=?,"
                "execution_agent_deadline_at=?,execution_evaluator_started_at=NULL "
                "WHERE id=? AND execution_status='running'",
                (restored, restored, self.attempt_id),
            )
            conn.commit()


def _load_environment_module(
    *, env: Any, filename: str, suffix: str, required: tuple[str, ...]
) -> ModuleType:
    path = Path(env.env_dir) / filename
    spec = importlib.util.spec_from_file_location(
        f"_octagon_iteration_{suffix}_{env.name.replace('-', '_')}", path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载环境模块: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in required:
        if not callable(getattr(module, name, None)):
            raise RuntimeError(f"环境 {filename} 缺少 {name}()")
    return module


class IterativeAttemptController:
    def __init__(
        self,
        *,
        attempt_id: str,
        attempt_dir: Path,
        task: dict[str, Any],
        env: Any,
        policy: IterativeReviewPolicy,
        scorer: Callable[..., Any],
        env_db: Path | None = None,
        judge_deadline_seconds: float | None = None,
        execution_db_path: Path | None = None,
        evaluator_lease_seconds: float = 60.0,
    ) -> None:
        self.attempt_id = attempt_id
        self.attempt_dir = Path(attempt_dir)
        self.task = task
        self.env = env
        self.policy = policy
        self.scorer = scorer
        self.env_db = env_db
        self.execution_db_path = (
            None if execution_db_path is None else Path(execution_db_path)
        )
        self.evaluator_lease_seconds = evaluator_lease_seconds
        self.judge_deadline_seconds = (
            None
            if judge_deadline_seconds is None or judge_deadline_seconds <= 0
            else float(judge_deadline_seconds)
        )
        # Product Reviewer 与 Judge 都属于 evaluator。复用 scoring deadline，
        # 保证二者均有明确上限；0/None 表示 evaluator 不限时。
        self.reviewer_deadline_seconds = self.judge_deadline_seconds
        self.repository = IterationRepository(
            self.attempt_dir, attempt_id=self.attempt_id
        )
        self._feedback_module = _load_environment_module(
            env=env,
            filename="feedback.py",
            suffix="feedback",
            required=("build_feedback_facts",),
        )
        self._reviewer_module = _load_environment_module(
            env=env,
            filename="product_reviewer.py",
            suffix="reviewer",
            required=("review_product", "guard_public_feedback", "render_feedback"),
        )
        self._round_index = 0
        self._previous_review: dict[str, Any] | None = None
        self._previous_feedback: dict[str, Any] | None = None
        self._restore_memory()

    def _submission_dir(self, submission_id: str) -> Path:
        return self.attempt_dir / "private_eval" / "submissions" / submission_id

    @staticmethod
    def _load_json(path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _restore_memory(self) -> None:
        summary = summarize_iteration(self.attempt_dir)
        if summary is None:
            return
        submissions = list(summary.get("submissions") or [])
        self._round_index = len(submissions)
        for item in submissions:
            submission_dir = self._submission_dir(str(item["submission_id"]))
            evaluation = self._load_json(submission_dir / "submission-evaluation.json")
            if isinstance(evaluation, dict) and isinstance(
                evaluation.get("judge_review"), dict
            ):
                self._previous_review = evaluation["judge_review"]
            feedback = self._load_json(submission_dir / "product-review.json")
            if feedback is not None:
                self._previous_feedback = feedback

    def _review_before(self, round_index: int) -> dict[str, Any] | None:
        summary = summarize_iteration(self.attempt_dir)
        if summary is None:
            return None
        previous: dict[str, Any] | None = None
        for item in summary.get("submissions") or []:
            if int(item["round_index"]) >= round_index:
                break
            evaluation = self._load_json(
                self._submission_dir(str(item["submission_id"]))
                / "submission-evaluation.json"
            )
            if isinstance(evaluation, dict) and isinstance(
                evaluation.get("judge_review"), dict
            ):
                previous = evaluation["judge_review"]
        return previous

    async def resume_after_restart(
        self, *, producer_session_id: str | None
    ) -> IterationTurnDecision:
        summary = summarize_iteration(self.attempt_dir)
        if summary is None or not summary.get("submissions"):
            return await self.on_turn_completed(
                producer_session_id=producer_session_id
            )
        submissions = list(summary["submissions"])
        latest = submissions[-1]
        round_index = int(latest["round_index"])
        submission_id = str(latest["submission_id"])
        if summary.get("completed"):
            evaluation = self._load_completed_evaluation(latest)
            return IterationTurnDecision(
                round_index=round_index,
                submission_id=submission_id,
                evaluation=evaluation,
                next_prompt=None,
                completed=True,
            )
        if latest.get("feedback_status") == "ready":
            evaluation = self._load_completed_evaluation(latest)
            prompt = self._load_feedback_prompt(submission_id)
            return IterationTurnDecision(
                round_index=round_index,
                submission_id=submission_id,
                evaluation=evaluation,
                next_prompt=prompt,
                completed=False,
            )
        if latest.get("feedback_status") == "sending":
            raise IterationControllerError(
                "反馈发送结果不确定，拒绝在恢复后自动重发",
                status="chat_failed",
                error_code="iteration_feedback_delivery_uncertain",
            )
        if latest.get("feedback_status") == "delivered":
            return await self.on_turn_completed(
                producer_session_id=producer_session_id
            )
        return await self._process_round(
            round_index=round_index,
            producer_session_id=producer_session_id,
        )

    async def on_turn_completed(
        self, *, producer_session_id: str | None
    ) -> IterationTurnDecision:
        if self._round_index >= self.policy.max_iterations:
            raise IterationControllerError(
                "Agent turn 数量超过环境 max_iterations",
                status="chat_failed",
                error_code="iteration_round_limit_exceeded",
            )
        return await self._process_round(
            round_index=self._round_index,
            producer_session_id=producer_session_id,
        )

    def _record_feedback_transport_event(
        self, decision: IterationTurnDecision, *, event: str, operation: str
    ) -> None:
        if decision.next_prompt is None:
            return
        prompt_hash = "sha256:" + hashlib.sha256(
            decision.next_prompt.encode("utf-8")
        ).hexdigest()
        summary = summarize_iteration(self.attempt_dir)
        if summary is None:
            raise IterationControllerError(
                "反馈送达时 iteration 状态缺失",
                status="chat_failed",
                error_code="iteration_feedback_state_missing",
            )
        item = next(
            (
                row
                for row in summary["submissions"]
                if row["submission_id"] == decision.submission_id
            ),
            None,
        )
        if item is None:
            raise IterationControllerError(
                "反馈送达时 Submission 状态缺失",
                status="chat_failed",
                error_code="iteration_feedback_state_missing",
            )
        signal = SubmissionSignal(
            summary="恢复反馈送达状态",
            verification="反馈已交给同一 Agent session",
            source="implicit_turn_completion",
            emitted_at=now_iso(),
        )
        submission = self.repository.create_submission(
            signal,
            round_index=int(item["round_index"]),
            producer_session_id=item.get("producer_session_id"),
            feedback_required=True,
        )
        self.repository.record_submission_event(
            event,
            submission=submission,
            operation=f"submission.{submission.round_index}.feedback.{operation}",
            feedback_hash=prompt_hash,
        )

    def mark_feedback_sending(self, decision: IterationTurnDecision) -> None:
        self._record_feedback_transport_event(
            decision,
            event="submission.feedback_sending",
            operation="send",
        )

    def mark_feedback_delivered(self, decision: IterationTurnDecision) -> None:
        self._record_feedback_transport_event(
            decision,
            event="submission.feedback_delivered",
            operation="deliver",
        )

    def finalize_last_successful_submission(self) -> bool:
        summary = summarize_iteration(self.attempt_dir)
        if summary is None:
            return False
        successful = [
            item
            for item in summary.get("submissions") or []
            if item.get("evaluation_status") == "completed"
        ]
        if not successful:
            return False
        latest = successful[-1]
        self._load_completed_evaluation(latest)
        signal = SubmissionSignal(
            summary="Agent 后续轮次失败，使用最后一个成功评测版本",
            verification="平台已确认该 Submission 评测产物完整",
            source="implicit_turn_completion",
            emitted_at=now_iso(),
        )
        submission = self.repository.create_submission(
            signal,
            round_index=int(latest["round_index"]),
            producer_session_id=latest.get("producer_session_id"),
            feedback_required=latest.get("feedback_status") != "not_required",
        )
        self.repository.select_for_final_score(submission)
        self.repository.complete()
        return True

    def _load_completed_evaluation(
        self, projection: dict[str, Any]
    ) -> SubmissionEvaluation:
        submission_id = str(projection["submission_id"])
        manifest_hash = projection.get("snapshot_manifest_hash")
        if not isinstance(manifest_hash, str):
            raise IterationControllerError(
                "已完成 Submission 缺少 snapshot manifest hash",
                status="scoring_failed",
                error_code="iteration_evaluation_artifact_invalid",
            )
        try:
            return load_submission_evaluation(
                self._submission_dir(submission_id),
                attempt_id=self.attempt_id,
                submission_id=submission_id,
                round_index=int(projection["round_index"]),
                snapshot_manifest_hash=manifest_hash,
            )
        except Exception as exc:
            raise IterationControllerError(
                str(exc),
                status="scoring_failed",
                error_code="iteration_evaluation_artifact_invalid",
            ) from exc

    def _load_feedback_prompt(self, submission_id: str) -> str:
        payload = self._load_json(
            self._submission_dir(submission_id) / "public-feedback.json"
        )
        prompt = payload.get("prompt") if payload else None
        if not isinstance(prompt, str) or not prompt:
            raise IterationControllerError(
                "已就绪反馈缺少公开 prompt",
                status="chat_failed",
                error_code="iteration_feedback_artifact_invalid",
            )
        expected = "sha256:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        if payload.get("prompt_hash") != expected:
            raise IterationControllerError(
                "公开反馈 prompt hash 不一致",
                status="chat_failed",
                error_code="iteration_feedback_artifact_invalid",
            )
        return prompt

    @staticmethod
    async def _run_callable_in_daemon_thread(
        func: Callable[..., Any], *, thread_name: str, kwargs: dict[str, Any]
    ) -> Any:
        """运行同步 callable；timeout 后的迟到线程不会阻止事件循环退出。"""
        context = contextvars.copy_context()
        done = threading.Event()
        outcome: dict[str, Any] = {}

        def run() -> None:
            try:
                outcome["result"] = context.run(func, **kwargs)
            except BaseException as exc:
                outcome["error"] = exc
            finally:
                done.set()

        threading.Thread(target=run, name=thread_name, daemon=True).start()
        while not done.is_set():
            await asyncio.sleep(0.01)
        error = outcome.get("error")
        if error is not None:
            raise error
        return outcome["result"]

    @classmethod
    async def _run_judge_in_daemon_thread(cls, **kwargs: Any) -> SubmissionEvaluation:
        return await cls._run_callable_in_daemon_thread(
            evaluate_submission,
            thread_name=f"octagon-iteration-judge-{kwargs['submission_id']}",
            kwargs=kwargs,
        )

    async def _run_product_reviewer(self, **kwargs: Any) -> Any:
        feedback = await self._run_callable_in_daemon_thread(
            self._reviewer_module.review_product,
            thread_name=f"octagon-product-reviewer-{self.attempt_id}",
            kwargs=kwargs,
        )
        if inspect.isawaitable(feedback):
            return await feedback
        return feedback

    @staticmethod
    def _promote_evaluation_artifacts(work_dir: Path, submission_dir: Path) -> None:
        """把成功 Judge 的 staging 产物发布到 Submission 根目录。

        Judge 在线程中运行，取消 await 不能停止底层线程。先写 staging 可保证
        timeout 后迟到的 Judge 只能污染隔离目录，不能伪造正式 completed 产物。
        submission-evaluation.json 最后发布，作为整组产物可见的提交标志。
        """
        entries = sorted(
            work_dir.iterdir(),
            key=lambda path: (path.name == "submission-evaluation.json", path.name),
        )
        for source in entries:
            target = submission_dir / source.name
            if target.exists():
                raise IterationControllerError(
                    f"Submission Judge 产物冲突: {target}",
                    status="scoring_failed",
                    error_code="iteration_evaluation_artifact_conflict",
                )
            os.replace(source, target)
        try:
            work_dir.rmdir()
        except OSError:
            pass

    async def _process_round(
        self, *, round_index: int, producer_session_id: str | None
    ) -> IterationTurnDecision:
        if self.execution_db_path is None:
            return await self._process_round_inner(
                round_index=round_index,
                producer_session_id=producer_session_id,
            )
        async with _ExecutionDeadlineLease(
            db_path=self.execution_db_path,
            attempt_id=self.attempt_id,
            lease_seconds=self.evaluator_lease_seconds,
        ):
            return await self._process_round_inner(
                round_index=round_index,
                producer_session_id=producer_session_id,
            )

    async def _process_round_inner(
        self, *, round_index: int, producer_session_id: str | None
    ) -> IterationTurnDecision:
        self.repository.start(self.policy, producer_session_id=producer_session_id)
        signal = SubmissionSignal(
            summary=f"第 {round_index + 1} 次正常轮次结束",
            verification="平台已同步当前工作区，等待冻结快照评测",
            source="implicit_turn_completion",
            emitted_at=now_iso(),
        )
        submission = self.repository.create_submission(
            signal,
            round_index=round_index,
            producer_session_id=producer_session_id,
            feedback_required=round_index + 1 < self.policy.max_iterations,
        )
        submission_dir = self._submission_dir(submission.submission_id)
        try:
            snapshot = freeze_submission_snapshot(
                workspace=self.attempt_dir / "skill_workspace",
                submission_dir=submission_dir,
                attempt_id=self.attempt_id,
                submission_id=submission.submission_id,
                round_index=round_index,
            )
        except Exception as exc:
            self.repository.record_submission_event(
                "submission.snapshot_failed",
                submission=submission,
                operation=f"submission.{round_index}.snapshot.failed",
                error_code="iteration_snapshot_failed",
                error_message=str(exc),
            )
            raise IterationControllerError(
                str(exc),
                status="chat_failed",
                error_code="iteration_snapshot_failed",
            ) from exc
        self.repository.record_submission_event(
            "submission.snapshot_completed",
            submission=submission,
            operation=f"submission.{round_index}.snapshot",
            snapshot_manifest_hash=snapshot.manifest_hash,
        )

        evaluation_path = submission_dir / "submission-evaluation.json"
        if evaluation_path.is_file():
            try:
                evaluation = load_submission_evaluation(
                    submission_dir,
                    attempt_id=self.attempt_id,
                    submission_id=submission.submission_id,
                    round_index=round_index,
                    snapshot_manifest_hash=snapshot.manifest_hash,
                )
            except Exception as exc:
                raise IterationControllerError(
                    str(exc),
                    status="scoring_failed",
                    error_code="iteration_evaluation_artifact_invalid",
                ) from exc
        else:
            current = summarize_iteration(self.attempt_dir)
            projection = next(
                row
                for row in current["submissions"]
                if row["submission_id"] == submission.submission_id
            )
            if projection.get("evaluation_status") == "running":
                raise IterationControllerError(
                    "Judge 在进程退出时处于运行中，无法证明可安全重放",
                    status="scoring_failed",
                    error_code="iteration_evaluation_recovery_unsafe",
                )
            self.repository.record_submission_event(
                "submission.evaluation_started",
                submission=submission,
                operation=f"submission.{round_index}.evaluation.start",
            )
            work_dir = submission_dir / (
                f".evaluation-work-{uuid.uuid4().hex[:12]}"
            )
            work_dir.mkdir(parents=True, exist_ok=False)
            try:
                evaluation_call = self._run_judge_in_daemon_thread(
                    attempt_id=self.attempt_id,
                    submission_id=submission.submission_id,
                    round_index=round_index,
                    snapshot_workspace=snapshot.snapshot_path,
                    snapshot_manifest_hash=snapshot.manifest_hash,
                    task=self.task,
                    env=self.env,
                    env_db=self.env_db,
                    scorer=self.scorer,
                    artifact_dir=work_dir,
                )
                if self.judge_deadline_seconds is None:
                    evaluation = await evaluation_call
                else:
                    evaluation = await asyncio.wait_for(
                        evaluation_call, timeout=self.judge_deadline_seconds
                    )
                self._promote_evaluation_artifacts(work_dir, submission_dir)
                evaluation = replace(evaluation, artifact_dir=str(submission_dir))
            except asyncio.TimeoutError as exc:
                message = (
                    "Submission Judge 超过独立 deadline "
                    f"({self.judge_deadline_seconds:g}s)"
                )
                self.repository.record_submission_event(
                    "submission.evaluation_failed",
                    submission=submission,
                    operation=f"submission.{round_index}.evaluation.failed",
                    error_code="iteration_judge_deadline_exceeded",
                    error_message=message,
                )
                raise IterationControllerError(
                    message,
                    status="scoring_failed",
                    error_code="iteration_judge_deadline_exceeded",
                ) from exc
            except Exception as exc:
                self.repository.record_submission_event(
                    "submission.evaluation_failed",
                    submission=submission,
                    operation=f"submission.{round_index}.evaluation.failed",
                    error_code="iteration_judge_failed",
                    error_message=str(exc),
                )
                raise IterationControllerError(
                    str(exc),
                    status="scoring_failed",
                    error_code="iteration_judge_failed",
                ) from exc
        self.repository.record_submission_event(
            "submission.evaluation_completed",
            submission=submission,
            operation=f"submission.{round_index}.evaluation.complete",
            score_total=evaluation.score_total,
            feedback_required=round_index + 1 < self.policy.max_iterations,
        )

        facts = self._load_json(submission_dir / "feedback-facts.json")
        if facts is None:
            facts = self._feedback_module.build_feedback_facts(
                evaluation.judge_review or {},
                self._review_before(round_index),
                max_problems=self.policy.reviewer.max_required_changes,
            )
            atomic_write_json(submission_dir / "feedback-facts.json", facts)
        self._previous_review = evaluation.judge_review
        self._round_index = max(self._round_index, round_index + 1)

        problems = facts.get("observed_problems") or []
        reached_limit = self._round_index >= self.policy.max_iterations
        if reached_limit or not problems:
            self.repository.select_for_final_score(submission)
            self.repository.complete()
            return IterationTurnDecision(
                round_index=round_index,
                submission_id=submission.submission_id,
                evaluation=evaluation,
                next_prompt=None,
                completed=True,
            )

        safe_feedback = self._load_json(submission_dir / "product-review.json")
        if safe_feedback is None:
            try:
                reviewer_call = self._run_product_reviewer(
                    task_prompt=str(self.task.get("prompt") or ""),
                    facts=facts,
                    previous_feedback=self._previous_feedback,
                    max_required_changes=self.policy.reviewer.max_required_changes,
                    artifact_dir=submission_dir,
                )
                if self.reviewer_deadline_seconds is None:
                    feedback = await reviewer_call
                else:
                    feedback = await asyncio.wait_for(
                        reviewer_call, timeout=self.reviewer_deadline_seconds
                    )
            except asyncio.TimeoutError as exc:
                message = (
                    "Product Reviewer 超过独立 deadline "
                    f"({self.reviewer_deadline_seconds:g}s)"
                )
                raise IterationControllerError(
                    message,
                    status="scoring_failed",
                    error_code="iteration_reviewer_deadline_exceeded",
                ) from exc
            except Exception:
                feedback = None
            try:
                safe_feedback = self._reviewer_module.guard_public_feedback(
                    feedback,
                    facts=facts,
                    max_required_changes=self.policy.reviewer.max_required_changes,
                    max_feedback_chars=self.policy.reviewer.max_feedback_chars,
                )
            except Exception as exc:
                raise IterationControllerError(
                    str(exc),
                    status="chat_failed",
                    error_code="iteration_feedback_guard_failed",
                ) from exc
            atomic_write_json(submission_dir / "product-review.json", safe_feedback)
        self._previous_feedback = safe_feedback
        try:
            prompt = self._load_feedback_prompt(submission.submission_id)
        except IterationControllerError:
            try:
                prompt = self._reviewer_module.render_feedback(safe_feedback)
            except Exception as exc:
                raise IterationControllerError(
                    str(exc),
                    status="chat_failed",
                    error_code="iteration_feedback_guard_failed",
                ) from exc
            prompt_hash = "sha256:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            atomic_write_json(
                submission_dir / "public-feedback.json",
                {"prompt": prompt, "prompt_hash": prompt_hash},
            )
        prompt_hash = "sha256:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        self.repository.record_submission_event(
            "submission.feedback_ready",
            submission=submission,
            operation=f"submission.{round_index}.feedback.ready",
            feedback_hash=prompt_hash,
            requested_change_count=len(safe_feedback.get("requested_changes") or []),
            resolved_problem_count=len(facts.get("resolved_problems") or []),
            regression_count=len(facts.get("regressions") or []),
        )
        return IterationTurnDecision(
            round_index=round_index,
            submission_id=submission.submission_id,
            evaluation=evaluation,
            next_prompt=prompt,
            completed=False,
        )
