"""Runner——把 create_attempt → adapter.run → evaluator → 终态串起来。

接口:

- `create_attempt(task_id, agent_name)` → `(AttemptModel, cleartext_env_token)`
  - 默认从 runtime_state 拿 db_path / data_path
  - run_id 默认与 attempt_id 一一对应,一个 run 一个 attempt(后续多对照
    组时再让外部传 run_id)
- `run_attempt(adapter, scorer)` → `RunAttemptResult`
  - adapter 无参 `.run()`,返回 AdapterResult-like(至少有 attempt_id / status)
  - 根据 adapter 返回的 status + scorer 输出决定 terminal status
  - terminal status 写回 DB,events_count / last_event_at / score_total 一并落

terminal 决策表:

| adapter status          | scorer 行为                  | final attempt status |
|--------------------------|------------------------------|----------------------|
| completed                | 跑成功,score >= threshold   | completed            |
| completed                | 跑成功,score < threshold    | gave_up              |
| completed                | 抛异常                       | scoring_failed       |
| timeout / chat_failed     | 跑成功,score >= threshold   | completed            |
| timeout / chat_failed     | 跑成功,score < threshold    | gave_up              |
| timeout / chat_failed     | 抛异常                       | scoring_failed       |
| auth_failed / blade_service_unavailable / session_create_failed
                           | 不跑                         | 原样保留             |

timeout/chat_failed 仍会有完整的 workspace 产物(轮次超时/流式断流发生在
"模型已经在动手做"之后,不是没启动)——评分与 completed 走同一条路径,不再
因收尾信号缺失就白白丢弃已产出的可评结果。原始失败原因保留在
external_refs.original_status(不会因评出分而丢失,前端可展示);
leader/robustness 只认 status in (completed, gave_up),所以最终 status
必须落在这两者之一分数才能真正参与排名/统计,不能一边评分一边保留
timeout/chat_failed 原值——那样会陷入"入库了但没人看得到"的半吊子状态。
真正没有产物的基础设施类失败(auth_failed 等)行为不变。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from . import runtime_state
from .cost.credential import judge_credentials_active
from .cost.estimate import apply_cost_columns
from .db import (
    _init_db_sync,
    _now_iso,
    _open_sync,
    generate_env_token,
    hash_env_token,
)
from .evaluator import (
    evaluate,
    write_security_summary_sync,
)
from .models import AttemptModel, TERMINAL_ATTEMPT_STATUSES

logger = logging.getLogger(__name__)

# ``None`` is a real execution value meaning "no deadline".  Attempt creation
# also needs an omitted state for legacy/direct callers that want to inherit the
# source task timeout, so it cannot use None as the default sentinel.
_INPUT_TIMEOUT_UNSET = object()


def _resolve_input_timeout_seconds(
    task_timeout_seconds: int | None,
    input_timeout_seconds: int | None | object = _INPUT_TIMEOUT_UNSET,
) -> int | None:
    if input_timeout_seconds is _INPUT_TIMEOUT_UNSET:
        return task_timeout_seconds
    return input_timeout_seconds  # type: ignore[return-value]


# ---------- create_attempt -----------------------------------------------


async def create_attempt(
    task_id: str,
    agent_name: str = "blade-agent",
    *,
    run_id: str | None = None,
    compare_mode: str = "multi-agent",
    model: str | None = None,
    input_prompt: str | None = None,
    input_context: dict[str, Any] | None = None,
    input_constraints: dict[str, Any] | None = None,
    input_timeout_seconds: int | None | object = _INPUT_TIMEOUT_UNSET,
    variant_id: str | None = None,
    external_refs: dict[str, Any] | None = None,
) -> tuple[AttemptModel, str]:
    """生成 attempt + env token。返回 (model, 明文 token)。

    明文 token 只出现在返回值中。**调用方拿到后必须立刻写到 attempt.json,
    然后丢弃,不要回传给任何持久化通道。** Pydantic AttemptModel 没有 token
    明文字段,因此 `repr(attempt)` 不会泄漏。
    """
    state = runtime_state.get()
    return await asyncio.to_thread(
        _create_attempt_sync,
        state.db_path,
        state.data_path,
        task_id=task_id,
        agent_name=agent_name,
        run_id=run_id,
        compare_mode=compare_mode,
        model_name=model,
        input_prompt=input_prompt,
        input_context=input_context,
        input_constraints=input_constraints,
        input_timeout_seconds=input_timeout_seconds,
        variant_id=variant_id,
        external_refs=external_refs,
    )


def _create_attempt_sync(
    db_path: Path,
    data_path: Path,
    *,
    task_id: str,
    agent_name: str,
    run_id: str | None,
    compare_mode: str = "multi-agent",
    model_name: str | None = None,
    input_prompt: str | None = None,
    input_context: dict[str, Any] | None = None,
    input_constraints: dict[str, Any] | None = None,
    input_timeout_seconds: int | None | object = _INPUT_TIMEOUT_UNSET,
    variant_id: str | None = None,
    external_refs: dict[str, Any] | None = None,
) -> tuple[AttemptModel, str]:
    _init_db_sync(db_path)
    attempt_id = f"att_{uuid.uuid4().hex[:12]}"
    run_id = run_id or f"run_{attempt_id[4:]}"
    env_session_id = f"env_{attempt_id[4:]}"
    token = generate_env_token()
    token_hash = hash_env_token(token)
    now = _now_iso()
    with _open_sync(db_path) as conn:
        # Source task remains relational lineage; execution input is frozen below.
        row = conn.execute(
            "SELECT env_name, prompt, context_json, constraints_json, timeout_seconds "
            "FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"task not found: {task_id}")
        env_name = row[0]

    prompt = row[1] if input_prompt is None else input_prompt
    context = (
        json.loads(row[2] or "{}") if input_context is None else input_context
    )
    constraints = (
        json.loads(row[3] or "{}")
        if input_constraints is None
        else input_constraints
    )
    timeout_seconds = _resolve_input_timeout_seconds(row[4], input_timeout_seconds)
    from .input_snapshots import insert_snapshot, prepare_snapshot

    snapshot = prepare_snapshot(
        data_path=data_path,
        attempt_id=attempt_id,
        prompt=prompt,
        context=context,
        constraints=constraints,
        timeout_seconds=timeout_seconds,
        variant_id=variant_id,
    )
    refs_json = json.dumps(external_refs or {}, ensure_ascii=False, sort_keys=True)

    with _open_sync(db_path) as conn:

        # 自动建 run 行(一对一);若已有同 id 直接复用
        existing_run = conn.execute(
            "SELECT id,rubric_version FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        active_rubric = conn.execute(
            "SELECT rubric_version FROM active_rubrics WHERE env_name=?", (env_name,)
        ).fetchone()
        active_rubric_version = str(active_rubric[0]) if active_rubric else None
        if existing_run is None:
            frozen_rubric_version = active_rubric_version
            conn.execute(
                "INSERT INTO runs(id, task_id, env_name, status, compare_mode, model,"
                " rubric_version, created_at)"
                " VALUES(?, ?, ?, 'queued', ?, ?, ?, ?)",
                (
                    run_id, task_id, env_name, compare_mode, model_name,
                    frozen_rubric_version, now,
                ),
            )
        else:
            frozen_rubric_version = existing_run[1]
            if frozen_rubric_version is None and active_rubric_version is not None:
                frozen_rubric_version = active_rubric_version
                conn.execute(
                    "UPDATE runs SET rubric_version=? WHERE id=? AND rubric_version IS NULL",
                    (frozen_rubric_version, run_id),
                )

        conn.execute(
            "INSERT INTO attempts("
            " id, run_id, task_id, env_name, agent_name, model, rubric_version, status,"
            " env_session_id, env_token_hash, external_refs_json,"
            " event_count, created_at"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, 0, ?)",
            (
                attempt_id,
                run_id,
                task_id,
                env_name,
                agent_name,
                model_name,
                frozen_rubric_version,
                env_session_id,
                token_hash,
                refs_json,
                now,
            ),
        )
        # Same transaction as Attempt: no executable experiment attempt can
        # become visible without its immutable input row.
        insert_snapshot(conn, snapshot)
        conn.commit()

    model = AttemptModel(
        id=attempt_id,
        run_id=run_id,
        task_id=task_id,
        env_name=env_name,
        agent_name=agent_name,
        model=model_name,
        status="queued",
        env_session_id=env_session_id,
        env_token_hash=token_hash,
        external_refs=external_refs or {},
        event_count=0,
        created_at=now,
    )
    return model, token


# ---------- run_attempt --------------------------------------------------


@dataclass
class RunAttemptResult:
    attempt_id: str
    status: str
    score_total: int | None
    pass_threshold: int
    error_code: str | None = None
    error_message: str | None = None


class _AdapterLike(Protocol):
    async def run(self) -> Any: ...


# timeout/chat_failed 发生在 adapter 已经跑起来之后（轮次超时、流式收尾信号
# 丢失），workspace 里通常已有完整产物——仍走评分路径，不当作 no-score 终态。
_ADAPTER_SCORABLE_DESPITE_FAILURE = {"timeout", "chat_failed"}

_ADAPTER_TERMINAL_NO_SCORE = TERMINAL_ATTEMPT_STATUSES - {
    "completed",
    "gave_up",
} - _ADAPTER_SCORABLE_DESPITE_FAILURE


async def run_attempt(
    *,
    adapter: _AdapterLike,
    scorer: Callable[..., list[dict[str, Any]]],
    observer: Any | None = None,
    defer_scoring: bool = False,
    scoring_capacity: int = 2,
) -> RunAttemptResult:
    """单 attempt 的全流程,异常都被包成 terminal status,绝不向上抛。

    observer：wire capture 的 phase 推进与 finalize 钩子。
    不传时用 NullAttemptObserver,行为与 wire 层不存在时完全一致。
    source start/ready 已由 dispatch 的 prepare() 完成,这里不再 attempt_start。
    整个决策流程包在外层 try/finally 里,所有 early return 都会经过
    attempt_end()（fail-open,其异常绝不影响 attempt 终态）。
    """
    from .wire.lifecycle import NullAttemptObserver

    observer = observer or NullAttemptObserver()
    try:
        return await _run_attempt_inner(
            adapter=adapter,
            scorer=scorer,
            observer=observer,
            defer_scoring=defer_scoring,
            scoring_capacity=scoring_capacity,
        )
    finally:
        try:
            await observer.attempt_end()
        except Exception:
            logger.exception("wire observer.attempt_end fail-open")


async def _run_attempt_inner(
    *,
    adapter: _AdapterLike,
    scorer: Callable[..., list[dict[str, Any]]],
    observer: Any,
    defer_scoring: bool = False,
    scoring_capacity: int = 2,
) -> RunAttemptResult:
    state = runtime_state.get()

    try:
        async with observer.phase("agent_run"):
            adapter_result = await adapter.run()
    except Exception as exc:
        logger.exception("adapter.run() 崩溃")
        return _finalize_no_score(
            db_path=state.db_path,
            attempt_id=getattr(adapter, "attempt_id", "<unknown>"),
            status="blade_service_unavailable",
            error_code="adapter_crashed",
            error_message=str(exc),
            pass_threshold=60,
        )
    try:
        await observer.agent_result(adapter_result)
    except Exception:
        logger.exception("wire observer.agent_result fail-open")

    attempt_id = adapter_result.attempt_id
    adapter_status = adapter_result.status

    def _adapter_stats() -> dict[str, Any]:
        return dict(
            external_refs=getattr(adapter_result, "external_refs", {}),
            event_count=getattr(adapter_result, "events_count", 0),
            last_event_at=getattr(adapter_result, "last_event_at", None),
            thinking_count=getattr(adapter_result, "thinking_count", 0),
            tool_call_count=getattr(adapter_result, "tool_call_count", 0),
            token_usage=getattr(adapter_result, "token_usage", {}),
            cost_estimate=getattr(adapter_result, "cost_estimate", None),
            duration_ms=getattr(adapter_result, "duration_ms", 0),
            transport_status=getattr(adapter_result, "transport_status", "unknown"),
        )

    # 拿 attempt 行 → env / task
    attempt_row, task_row = _fetch_attempt_and_task_sync(state.db_path, attempt_id)
    if attempt_row is None or task_row is None:
        return RunAttemptResult(
            attempt_id=attempt_id,
            status="blade_service_unavailable",
            score_total=0,
            pass_threshold=60,
            error_code="attempt_or_task_missing",
            error_message=f"attempt_id={attempt_id} 在 DB 中找不到对应 task/run",
        )
    env_name = attempt_row["env_name"]
    env = state.envs.get(env_name)
    if env is None:
        if adapter_status == "completed":
            return _finalize_no_score(
                db_path=state.db_path,
                attempt_id=attempt_id,
                status="scoring_failed",
                error_code="env_not_loaded",
                error_message=f"env not loaded: {env_name}",
                pass_threshold=60,
                **_adapter_stats(),
            )
        return _finalize_no_score(
            db_path=state.db_path,
            attempt_id=attempt_id,
            status=adapter_status,
            error_code=adapter_result.error_code,
            error_message=adapter_result.error_message,
            pass_threshold=60,
            **_adapter_stats(),
        )

    pass_threshold = int(getattr(env, "meta", {}).get("pass_threshold", 60))
    if str(adapter_result.error_code or "").startswith("iteration_") and not bool(
        getattr(adapter_result, "external_refs", {}).get(
            "iteration_finalized_from_last_successful"
        )
    ):
        return _finalize_no_score(
            db_path=state.db_path,
            attempt_id=attempt_id,
            status=adapter_status,
            error_code=adapter_result.error_code,
            error_message=adapter_result.error_message,
            pass_threshold=pass_threshold,
            **_adapter_stats(),
        )
    if attempt_row.get("agent_name") == "blade-agent":
        # Blade inference originates inside the remote service, outside the
        # local pre-forward proxy.  Verify every root/fork response model from
        # its transport events before allowing the result into scoring.
        from .model_integrity import audit_blade_event_models

        audit_blade_event_models(
            state.db_path,
            state.data_path,
            attempt_id,
        )
    # The proxy records a mismatch before rejecting the upstream request.
    # Even if a CLI swallows that HTTP 409 and exits "successfully", the attempt
    # must never enter scoring/ranking: its comparison contract was violated.
    from .model_integrity import get_attempt_integrity

    integrity = get_attempt_integrity(state.db_path, attempt_id)
    if integrity and integrity["status"] == "violated":
        return _finalize_no_score(
            db_path=state.db_path,
            attempt_id=attempt_id,
            status="model_integrity_failed",
            error_code=integrity["error_code"] or "model_integrity_violation",
            error_message=integrity["error_message"]
            or "outbound model did not match attempt model",
            pass_threshold=pass_threshold,
            **_adapter_stats(),
        )
    # timeout/chat_failed 仍尝试评分；resolve_input/scorer 若因此真的拿不到
    # 可评材料，回退到这个原始状态而不是 input_snapshot_missing/
    # scoring_failed——那两个状态意味着"评分设施出问题"，会掩盖"agent 本来
    # 就因为超时/断流没完成"这个更准确的事实。
    scorable_despite_failure = adapter_status in _ADAPTER_SCORABLE_DESPITE_FAILURE
    # 执行场合快照先落盘，再分终态：cli_error 等无评分终态也要留下
    # 沙盒镜像 / 容器 id / agent 版本，否则失败的 attempt 看不出跑在哪里。
    if adapter_result is not None:
        _write_security_meta_file(
            state.data_path, attempt_id,
            dict(getattr(adapter_result, "security_meta", {}) or {}),
        )
    no_score_fallback_status = adapter_status if scorable_despite_failure else None

    if adapter_status in _ADAPTER_TERMINAL_NO_SCORE:
        return _finalize_no_score(
            db_path=state.db_path,
            attempt_id=attempt_id,
            status=adapter_status,
            error_code=adapter_result.error_code,
            error_message=adapter_result.error_message,
            pass_threshold=pass_threshold,
            **_adapter_stats(),
        )

    # adapter completed（或 timeout/chat_failed 但仍评分）：scorer 消费与
    # adapter 相同的冻结输入。
    from .input_snapshots import InputSnapshotError, resolve_attempt_input

    try:
        frozen_input = resolve_attempt_input(
            data_path=state.data_path,
            db_path=state.db_path,
            attempt_id=attempt_id,
        )
    except InputSnapshotError as exc:
        return _finalize_no_score(
            db_path=state.db_path,
            attempt_id=attempt_id,
            status=no_score_fallback_status or "input_snapshot_missing",
            error_code=(
                adapter_result.error_code if scorable_despite_failure
                else "input_snapshot_missing"
            ),
            error_message=(
                adapter_result.error_message if scorable_despite_failure else str(exc)
            ),
            pass_threshold=pass_threshold,
            **_adapter_stats(),
        )
    task_dict = _row_to_task_dict(task_row, frozen_input=frozen_input)
    security_meta = dict(getattr(adapter_result, "security_meta", {}) or {})
    if defer_scoring:
        from .scoring_queue import enqueue_scoring_job

        enqueue_scoring_job(
            attempt_id=attempt_id,
            adapter_status=adapter_status,
            adapter_error_code=getattr(adapter_result, "error_code", None),
            adapter_error_message=getattr(adapter_result, "error_message", None),
            stats=_adapter_stats(),
            security_meta=security_meta,
            capacity=scoring_capacity,
        )
        return RunAttemptResult(
            attempt_id=attempt_id,
            status="scoring",
            score_total=None,
            pass_threshold=pass_threshold,
        )
    try:
        # judge 凭据：scorer 在同进程 to_thread 里 import judge_local，
        # 注入通道就是进程环境变量。with 块退出即恢复——绝不把 run key 永久留在
        # 进程环境里，否则下一个 run 的 judge 会用上一个 run 的 key。
        _judge_run_id = _run_id_of_attempt(state.db_path, attempt_id)
        with judge_credentials_active(_judge_run_id) as _used_run_key:
            if not _used_run_key:
                # judge 走了既有配置 → scoring 段费用不在 run key 上，
                # 结算时必须降级，不得标 final。
                from .cost.audit import note_scoring_credential_fallback

                note_scoring_credential_fallback(_judge_run_id)
            async with observer.phase("verification"):
                outcome = await asyncio.to_thread(
                    evaluate,
                    attempt_id=attempt_id,
                    task=task_dict,
                    env=env,
                    data_path=state.data_path,
                    scorer=scorer,
                    security_meta=security_meta,
                )
    except Exception as exc:
        logger.exception("scorer 异常 attempt=%s", attempt_id)
        from .evaluator import ScorerUnavailableError

        if isinstance(exc, ScorerUnavailableError):
            refs = _adapter_stats()
            if scorable_despite_failure:
                refs["external_refs"] = {
                    **refs.get("external_refs", {}),
                    "original_status": adapter_status,
                    "original_error_code": adapter_result.error_code,
                    "original_error_message": adapter_result.error_message,
                }
            return _finalize_no_score(
                db_path=state.db_path,
                attempt_id=attempt_id,
                status="scoring_failed",
                error_code="scorer_unavailable",
                error_message=str(exc),
                pass_threshold=pass_threshold,
                **refs,
            )
        if scorable_despite_failure:
            # 评分器炸了不代表"评分设施坏了"——更可能是 timeout/chat_failed
            # 遗留的产物本就不完整。保留原始失败原因，不伪装成 scoring_failed。
            return _finalize_no_score(
                db_path=state.db_path,
                attempt_id=attempt_id,
                status=adapter_status,
                error_code=adapter_result.error_code,
                error_message=adapter_result.error_message,
                pass_threshold=pass_threshold,
                **_adapter_stats(),
            )
        return _finalize_no_score(
            db_path=state.db_path,
            attempt_id=attempt_id,
            status="scoring_failed",
            error_code="scorer_exception",
            error_message=str(exc),
            pass_threshold=pass_threshold,
            **_adapter_stats(),
        )

    final_status = "completed" if outcome.passed else "gave_up"
    # 安全轴：执行场合快照 + 事件汇总，与 score_total 并列写入（不合并）。
    await asyncio.to_thread(
        _write_security_columns_sync,
        state.db_path,
        attempt_id,
        security_meta,
        outcome.security,
    )
    from .experiments.scoring import commit_scoring_result

    stats = _adapter_stats()
    if scorable_despite_failure:
        # 最终 status 落回 completed/gave_up（leader/robustness 只认这两个
        # 值），但"这次其实是 timeout/chat_failed"不能凭空消失——记进
        # external_refs 供前端/排查使用，原始 error_code/error_message 一并带上。
        stats["external_refs"] = {
            **stats.get("external_refs", {}),
            "original_status": adapter_status,
            "original_error_code": adapter_result.error_code,
            "original_error_message": adapter_result.error_message,
        }
    await asyncio.to_thread(
        commit_scoring_result,
        db_path=state.db_path,
        data_path=state.data_path,
        attempt_id=attempt_id,
        scores=outcome.scores,
        manifest=outcome.evaluation_manifest,
        status=final_status,
        score_total=outcome.score_total,
        ended_at=_now_iso(),
        **stats,
    )
    # Derived Judge evidence is valid only after the official score transaction
    # commits. Collection remains fail-open and can be reconciled independently.
    try:
        from .rubric_evolution.collector import record_evaluation_outcome_sync

        await asyncio.to_thread(
            record_evaluation_outcome_sync,
            db_path=state.db_path,
            attempt_id=attempt_id,
            scores=outcome.scores,
            evaluation_manifest=outcome.evaluation_manifest,
            metadata={"source": "runner"},
        )
    except Exception:
        logger.exception(
            "rubric judge record collection failed after score commit attempt=%s",
            attempt_id,
        )

    try:
        from .experiments.leader import consume_score_outbox

        await asyncio.to_thread(consume_score_outbox, state.db_path)
    except Exception:
        # Score/outbox are already committed. A dropped notification is safe:
        # startup or the next scoring transition consumes the durable cursor.
        logger.exception("leader outbox notification failed attempt=%s", attempt_id)

    return RunAttemptResult(
        attempt_id=attempt_id,
        status=final_status,
        score_total=outcome.score_total,
        pass_threshold=outcome.pass_threshold,
    )


# ---------- DB helpers ---------------------------------------------------


SECURITY_META_FILENAME = "security_meta.json"


def _write_security_meta_file(data_path: Path, attempt_id: str, security_meta: dict) -> None:
    """adapter 的 security_meta 原样落到 attempt 目录；失败只记日志。"""
    try:
        target = Path(data_path) / "attempts" / attempt_id / SECURITY_META_FILENAME
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(security_meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        logger.warning("无法写 %s attempt=%s", SECURITY_META_FILENAME, attempt_id, exc_info=True)


def _write_security_columns_sync(
    db_path: Path,
    attempt_id: str,
    security_meta: dict,
    security_summary: dict | None,
) -> None:
    """写执行场合快照列（② locus/permission_mode/workspace_root）+ 安全汇总列。"""
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE attempts SET execution_locus=?, permission_mode=?, "
            "workspace_root=? WHERE id=?",
            (
                security_meta.get("execution_locus"),
                security_meta.get("permission_mode"),
                security_meta.get("workspace_root"),
                attempt_id,
            ),
        )
        conn.commit()
    write_security_summary_sync(db_path, attempt_id, security_summary)


def _fetch_attempt_and_task_sync(
    db_path: Path, attempt_id: str
) -> tuple[dict | None, dict | None]:
    with _open_sync(db_path) as conn:
        conn.row_factory = sqlite3.Row
        att = conn.execute(
            "SELECT * FROM attempts WHERE id=?", (attempt_id,)
        ).fetchone()
        if att is None:
            return None, None
        task = conn.execute(
            "SELECT * FROM tasks WHERE id=?", (att["task_id"],)
        ).fetchone()
        return dict(att), (dict(task) if task else None)


def _row_to_task_dict(row: dict, *, frozen_input: Any | None = None) -> dict[str, Any]:
    task = {
        "id": row["id"],
        "env_name": row["env_name"],
        "prompt": (
            frozen_input.prompt if frozen_input is not None else row["prompt"]
        ),
        "context": (
            frozen_input.context
            if frozen_input is not None
            else json.loads(row.get("context_json") or "{}")
        ),
        "constraints": (
            frozen_input.constraints
            if frozen_input is not None
            else json.loads(row.get("constraints_json") or "{}")
        ),
        "timeout_seconds": (
            frozen_input.timeout_seconds
            if frozen_input is not None
            else row.get("timeout_seconds", 600)
        ),
        "source": row.get("source", "file"),
    }
    if frozen_input is not None:
        task["input_provenance"] = frozen_input.input_provenance
        task["input_content_hash"] = frozen_input.content_hash
        task["variant_id"] = frozen_input.variant_id
    return task


def _run_id_of_attempt(db_path: Path, attempt_id: str) -> str | None:
    """attempt → run_id，供 judge 凭据注入查 run 专属 key。查不到返回 None。"""
    try:
        with _open_sync(db_path) as conn:
            row = conn.execute(
                "SELECT run_id FROM attempts WHERE id=?", (attempt_id,)
            ).fetchone()
        return row[0] if row else None
    except Exception:  # pragma: no cover - 成本核算不得拖垮评分
        return None


def _finalize_no_score(
    *,
    db_path: Path,
    attempt_id: str,
    status: str,
    error_code: str | None,
    error_message: str | None,
    pass_threshold: int,
    external_refs: dict[str, Any] | None = None,
    event_count: int = 0,
    last_event_at: str | None = None,
    thinking_count: int = 0,
    tool_call_count: int = 0,
    token_usage: dict[str, int] | None = None,
    cost_estimate: float | None = None,
    duration_ms: int = 0,
    transport_status: str = "unknown",
) -> RunAttemptResult:
    ended = _now_iso()
    infrastructure_statuses = {
        "session_create_failed",
        "session_socket_overflow",
        "server_unreachable",
        "provider_quota_exhausted",
        "blade_service_unavailable",
        "capture_infrastructure_failed",
        "model_integrity_failed",
        "sandbox_unavailable",
        # 磁盘写满是评测机的资源问题，不是 agent 做得差——混进 agent 失败
        # 会直接污染横评矩阵。
        "disk_exhausted",
    }
    if status == "cancelled":
        failure_kind = "cancelled"
    elif status == "scoring_failed":
        failure_kind = "scoring"
    elif status in infrastructure_statuses or str(error_code or "").startswith(
        "iteration_"
    ):
        failure_kind = "infrastructure"
    elif status == "input_snapshot_missing":
        failure_kind = "input"
    else:
        failure_kind = "agent"
    retryable = error_code != "agent_timeout" and status in {
        "session_create_failed",
        "server_unreachable",
        "provider_quota_exhausted",
        "blade_service_unavailable",
        "timeout",
    }
    with _open_sync(db_path) as conn:
        current = conn.execute(
            "SELECT external_refs_json FROM attempts WHERE id=?", (attempt_id,)
        ).fetchone()
        try:
            stored_refs = json.loads(current[0] or "{}") if current else {}
        except json.JSONDecodeError:
            stored_refs = {}
        merged_refs = {**stored_refs, **(external_refs or {})}
        execution_status = (
            "cancelled"
            if status == "cancelled"
            else
            "timeout"
            if status == "timeout"
            else "completed"
            if status == "scoring_failed"
            else "failed"
        )
        scoring_status = (
            "cancelled"
            if status == "cancelled"
            else "failed"
            if status == "scoring_failed"
            else "skipped"
        )
        conn.execute(
            "UPDATE attempts SET status=?, score_total=NULL, error_code=?, error_message=?,"
            " failure_kind=?, retryable=?,"
            " external_refs_json=?, event_count=?, last_event_at=?,"
            " thinking_count=?, tool_call_count=?, token_usage_json=?,"
            " cost_estimate=?, duration_ms=?, transport_status=?, ended_at=?,"
            " execution_status=?,execution_ended_at=?,execution_error_code=?,"
            " execution_error_message=?,scoring_status=?,scoring_ended_at=?,"
            " scoring_error_code=?,scoring_error_message=? WHERE id=?",
            (
                status,
                error_code,
                error_message,
                failure_kind,
                int(retryable),
                json.dumps(merged_refs, ensure_ascii=False),
                event_count,
                last_event_at,
                thinking_count,
                tool_call_count,
                json.dumps(token_usage or {}, ensure_ascii=False),
                cost_estimate,
                duration_ms,
                transport_status,
                ended,
                execution_status,
                ended,
                error_code if execution_status != "completed" else None,
                error_message if execution_status != "completed" else None,
                scoring_status,
                ended,
                error_code if scoring_status == "failed" else None,
                error_message if scoring_status == "failed" else None,
                attempt_id,
            ),
        )
        # 成本估算：这些 attempt 没走评分，但同样已经花了钱。
        # 与 commit_scoring_result 共用同一 helper，口径一致；与上面的终态
        # UPDATE 同事务，不产生"有终态没成本"的中间态。
        apply_cost_columns(conn, attempt_id, token_usage=token_usage)
        conn.commit()
    # Deterministic automation is a post-terminal derived projection.  It may
    # fail independently, but must never change the authoritative attempt fact.
    try:
        from .automation.diagnostics import diagnose_and_persist

        diagnose_and_persist(
            db_path=db_path, data_path=db_path.parent, attempt_id=attempt_id
        )
    except Exception:
        logger.exception("automatic diagnosis failed attempt=%s", attempt_id)
    return RunAttemptResult(
        attempt_id=attempt_id,
        status=status,
        score_total=0,
        pass_threshold=pass_threshold,
        error_code=error_code,
        error_message=error_message,
    )


def _finalize_with_score_sync(
    db_path: Path,
    *,
    attempt_id: str,
    status: str,
    score_total: int,
    external_refs: dict[str, Any],
    event_count: int,
    last_event_at: str | None,
    thinking_count: int = 0,
    tool_call_count: int = 0,
    token_usage: dict[str, int] | None = None,
    cost_estimate: float | None = None,
    duration_ms: int = 0,
    transport_status: str = "unknown",
    ended_at: str,
) -> None:
    with _open_sync(db_path) as conn:
        conn.execute(
            "UPDATE attempts SET status=?, score_total=?, external_refs_json=?,"
            " event_count=?, last_event_at=?, thinking_count=?, tool_call_count=?,"
            " token_usage_json=?, cost_estimate=?, duration_ms=?, transport_status=?, ended_at=?"
            " WHERE id=?",
            (
                status,
                score_total,
                json.dumps(external_refs, ensure_ascii=False),
                event_count,
                last_event_at,
                thinking_count,
                tool_call_count,
                json.dumps(token_usage or {}, ensure_ascii=False),
                cost_estimate,
                duration_ms,
                transport_status,
                ended_at,
                attempt_id,
            ),
        )
        # 成本估算：与 _finalize_no_score 同理，共用 helper 保证口径一致。
        apply_cost_columns(conn, attempt_id, token_usage=token_usage)
        conn.commit()
