"""Octagon 主 DB(aiosqlite + sqlite3 双模式)。

接口分两类:

- `open_db(data_path)` —— async,FastAPI lifespan 用。返回长期连接。
- `init_db(db_path)` / `list_tables(db_path)` / `insert_attempt(db_path, ...)` ——
  async helper,接受**单个 sqlite 文件路径**,内部短连接。Runner / Evaluator /
  测试用这条链路。

设计动机:lifespan 拿一个长连接做 read-mostly 查询,但按
attempt_id 频繁 insert/update 的路径,**不在 lifespan 连接上序列化所有写**——
short-lived connection + WAL 更简单且没瓶颈。

事件文件:`blade_events_json` 不进 DB,落到
`<data_path>/attempts/{attempt_id}/blade_events.jsonl`。DB 里 attempts 表只存
`event_count` / `last_event_at` 两个概览字段。`get_attempt_event_summary` 通过
`append_blade_event` 期间记录的 data_path 反查文件,M1 单进程内安全。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

# ---------- schema --------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    env_name TEXT NOT NULL,
    prompt TEXT NOT NULL,
    context_json TEXT NOT NULL DEFAULT '{}',
    constraints_json TEXT NOT NULL DEFAULT '{}',
    timeout_seconds INTEGER NOT NULL DEFAULT 600,
    source TEXT NOT NULL DEFAULT 'file',  -- file | adhoc
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    env_name TEXT NOT NULL,
    status TEXT NOT NULL,
    compare_mode TEXT NOT NULL DEFAULT 'multi-agent',
    model TEXT,
    execution TEXT,  -- serial | parallel；NULL=历史数据（按当前默认 parallel 恢复）
    rubric_version TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT
);

CREATE TABLE IF NOT EXISTS attempts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    task_id TEXT NOT NULL REFERENCES tasks(id),
    env_name TEXT NOT NULL,
    agent_name TEXT NOT NULL DEFAULT 'blade-agent',
    model TEXT,
    rubric_version TEXT,
    status TEXT NOT NULL,
    transport_status TEXT NOT NULL DEFAULT 'unknown',
    env_session_id TEXT NOT NULL,
    env_token_hash TEXT NOT NULL,
    external_refs_json TEXT NOT NULL DEFAULT '{}',
    event_count INTEGER NOT NULL DEFAULT 0,
    last_event_at TEXT,
    heartbeat_at TEXT,
    heartbeat_seq INTEGER NOT NULL DEFAULT 0,
    current_stage TEXT,
    progress_message TEXT,
    thinking_count INTEGER NOT NULL DEFAULT 0,
    tool_call_count INTEGER NOT NULL DEFAULT 0,
    token_usage_json TEXT NOT NULL DEFAULT '{}',
    cost_estimate REAL,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    score_total INTEGER,
    error_code TEXT,
    error_message TEXT,
    failure_kind TEXT,
    retryable INTEGER,
    started_at TEXT,
    ended_at TEXT,
    created_at TEXT NOT NULL,
    execution_locus TEXT,
    permission_mode TEXT,
    workspace_root TEXT,
    security_event_count INTEGER NOT NULL DEFAULT 0,
    security_max_severity TEXT,
    security_hitl_json TEXT NOT NULL DEFAULT '{}',
    security_reaction TEXT,
    security_coverage_json TEXT NOT NULL DEFAULT '{}',
    execution_status TEXT NOT NULL DEFAULT 'queued',
    execution_started_at TEXT,
    execution_ended_at TEXT,
    execution_deadline_at TEXT,
    execution_agent_deadline_at TEXT,
    execution_evaluator_started_at TEXT,
    execution_error_code TEXT,
    execution_error_message TEXT,
    scoring_status TEXT NOT NULL DEFAULT 'not_ready',
    scoring_queued_at TEXT,
    scoring_started_at TEXT,
    scoring_ended_at TEXT,
    scoring_deadline_at TEXT,
    scoring_error_code TEXT,
    scoring_error_message TEXT,
    model_integrity_status TEXT NOT NULL DEFAULT 'not_observed',
    model_integrity_observed_json TEXT NOT NULL DEFAULT '[]',
    model_integrity_violation_count INTEGER NOT NULL DEFAULT 0,
    model_integrity_error_code TEXT,
    model_integrity_error_message TEXT,
    model_integrity_checked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_attempts_run_id ON attempts(run_id);
CREATE INDEX IF NOT EXISTS idx_attempts_status ON attempts(status);
-- idx_attempts_heartbeat_at is created by _migrate_attempt_observability after
-- legacy attempts tables have received the heartbeat_at column.

-- 统一 attempt 事件信封（S1）。原始 adapter 日志仍保留在文件中；本表只保存
-- 实时投影和控制面事件，供 SSE、heartbeat、恢复器和后续分析器消费。
CREATE TABLE IF NOT EXISTS attempt_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    occurred_at TEXT NOT NULL,
    stage TEXT NOT NULL,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info',
    producer TEXT NOT NULL,
    operation_id TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    evidence_refs_json TEXT NOT NULL DEFAULT '[]',
    UNIQUE(attempt_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_attempt_events_attempt_seq
    ON attempt_events(attempt_id, sequence);
CREATE INDEX IF NOT EXISTS idx_attempt_events_run_seq
    ON attempt_events(run_id, sequence);

CREATE TABLE IF NOT EXISTS scoring_jobs (
    id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    scorer_version TEXT NOT NULL,
    scorer_config_json TEXT NOT NULL DEFAULT '{}',
    input_hash TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    deadline_at TEXT,
    lease_owner TEXT,
    lease_expires_at TEXT,
    heartbeat_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    cancel_requested_at TEXT,
    error_code TEXT,
    error_message TEXT,
    UNIQUE(attempt_id, scorer_version)
);
CREATE INDEX IF NOT EXISTS idx_scoring_jobs_status_created
    ON scoring_jobs(status, created_at);

-- 平台运行事件：后端每次启动/退出都留下可查询的事故记录。
-- 周末事故的日志只能证明 Uvicorn 走了优雅关闭，无法回答是谁发的信号、
-- 退出码是多少、进程活了多久、supervisor 有没有重启——归因证据必须持久化，
-- 而不是只存在于 stdout 日志里。
-- 刻意不记录任何凭据或环境变量原文：本表可经 API 读取。
CREATE TABLE IF NOT EXISTS platform_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,        -- startup | shutdown | crash
    occurred_at TEXT NOT NULL,
    pid INTEGER,
    boot_id TEXT,                    -- 同一进程的启动/退出配对
    signal_name TEXT,                -- SIGTERM / SIGINT / SIGHUP ...
    exit_code INTEGER,
    graceful INTEGER,                -- 1=优雅关闭 0=异常
    uptime_seconds REAL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_platform_events_time
    ON platform_events(occurred_at DESC);

CREATE TABLE IF NOT EXISTS scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id TEXT NOT NULL REFERENCES attempts(id),
    dimension TEXT NOT NULL,
    value INTEGER NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    scored_at TEXT,
    evaluation_manifest_ref TEXT
);

CREATE INDEX IF NOT EXISTS idx_scores_attempt_id ON scores(attempt_id);

-- Rubric Evolution uses append-only versioned score revisions. The legacy
-- scores/attempts.score_total projection remains the official score for old API
-- consumers; replay and shadow results never overwrite it.
CREATE TABLE IF NOT EXISTS rubric_versions (
    id TEXT PRIMARY KEY,
    env_name TEXT,
    version TEXT NOT NULL,
    parent_version TEXT NOT NULL,
    scope TEXT NOT NULL,
    status TEXT NOT NULL,
    rubric_json TEXT NOT NULL,
    rubric_hash TEXT NOT NULL,
    source_batch_id TEXT,
    created_at TEXT NOT NULL,
    published_at TEXT,
    published_by TEXT,
    evolution_domain TEXT NOT NULL DEFAULT 'product',
    executor_kind TEXT NOT NULL DEFAULT 'hybrid',
    UNIQUE(env_name, version)
);
CREATE INDEX IF NOT EXISTS idx_rubric_versions_env_status
    ON rubric_versions(env_name, status, created_at);

CREATE TABLE IF NOT EXISTS active_rubrics (
    env_name TEXT PRIMARY KEY,
    rubric_version TEXT NOT NULL,
    rubric_id TEXT NOT NULL REFERENCES rubric_versions(id),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rubric_score_revisions (
    id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES attempts(id),
    rubric_version TEXT NOT NULL,
    score_kind TEXT NOT NULL,
    score_with_unknown REAL NOT NULL,
    normalized_score_without_unknown REAL,
    unknown_count INTEGER NOT NULL DEFAULT 0,
    unknown_weight REAL NOT NULL DEFAULT 0,
    checks_json TEXT NOT NULL,
    evaluation_manifest_ref TEXT,
    source_batch_id TEXT,
    operation_id TEXT UNIQUE,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rubric_score_revisions_attempt
    ON rubric_score_revisions(attempt_id, rubric_version, score_kind, created_at);

CREATE TABLE IF NOT EXISTS rubric_judge_records (
    id TEXT PRIMARY KEY,
    env_name TEXT NOT NULL,
    rubric_version TEXT NOT NULL,
    run_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL REFERENCES attempts(id),
    judge_execution_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    valid_execution INTEGER NOT NULL,
    checks_json TEXT NOT NULL DEFAULT '[]',
    execution_error TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    operation_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_rubric_judge_records_scope
    ON rubric_judge_records(env_name, rubric_version, valid_execution, created_at);
-- The operation index is created by _migrate_rubric_evolution after legacy
-- rubric_judge_records tables receive the operation_id column.

CREATE TABLE IF NOT EXISTS rubric_evolution_batch_members (
    record_id TEXT NOT NULL REFERENCES rubric_judge_records(id),
    batch_id TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    role TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(record_id, batch_id, role)
);
CREATE INDEX IF NOT EXISTS idx_rubric_batch_members_scope
    ON rubric_evolution_batch_members(scope_key, role, created_at);

-- Process and association evolution have contracts and records that are
-- deliberately separate from official Product score records.
CREATE TABLE IF NOT EXISTS evolution_contract_versions (
    id TEXT PRIMARY KEY,
    evolution_domain TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    version TEXT NOT NULL,
    parent_version TEXT NOT NULL,
    status TEXT NOT NULL,
    contract_json TEXT NOT NULL,
    contract_hash TEXT NOT NULL,
    source_batch_id TEXT,
    created_at TEXT NOT NULL,
    published_at TEXT,
    published_by TEXT,
    UNIQUE(evolution_domain, scope_key, version)
);
CREATE INDEX IF NOT EXISTS idx_evolution_contract_versions_scope
    ON evolution_contract_versions(evolution_domain, scope_key, status, created_at);

CREATE TABLE IF NOT EXISTS evolution_active_contracts (
    evolution_domain TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    version TEXT NOT NULL,
    contract_id TEXT NOT NULL REFERENCES evolution_contract_versions(id),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(evolution_domain, scope_key)
);

CREATE TABLE IF NOT EXISTS process_judge_records (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    attempt_id TEXT NOT NULL REFERENCES attempts(id),
    env_name TEXT NOT NULL,
    process_rubric_version TEXT NOT NULL,
    analysis_hash TEXT NOT NULL,
    valid_execution INTEGER NOT NULL,
    claims_json TEXT NOT NULL DEFAULT '[]',
    accepted_claim_ids_json TEXT NOT NULL DEFAULT '[]',
    rejected_claims_json TEXT NOT NULL DEFAULT '[]',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(attempt_id, analysis_hash)
);
CREATE INDEX IF NOT EXISTS idx_process_judge_records_scope
    ON process_judge_records(env_name, process_rubric_version, valid_execution, created_at);

CREATE TABLE IF NOT EXISTS process_evolution_batch_members (
    record_id TEXT NOT NULL REFERENCES process_judge_records(id),
    batch_id TEXT NOT NULL,
    role TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(record_id, batch_id, role)
);
CREATE INDEX IF NOT EXISTS idx_process_batch_members_role
    ON process_evolution_batch_members(role, created_at);

CREATE TABLE IF NOT EXISTS association_cases (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    attempt_id TEXT NOT NULL REFERENCES attempts(id),
    env_name TEXT NOT NULL,
    product_rubric_version TEXT,
    process_rubric_version TEXT NOT NULL,
    product_outcome_json TEXT NOT NULL,
    process_findings_json TEXT NOT NULL,
    controls_json TEXT NOT NULL,
    case_hash TEXT NOT NULL UNIQUE,
    valid_evidence INTEGER NOT NULL DEFAULT 1,
    invalid_reason TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_association_cases_scope
    ON association_cases(env_name, created_at);

CREATE TABLE IF NOT EXISTS association_evolution_batch_members (
    case_id TEXT NOT NULL REFERENCES association_cases(id),
    batch_id TEXT NOT NULL,
    role TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(case_id, batch_id, role)
);
CREATE INDEX IF NOT EXISTS idx_association_batch_members_role
    ON association_evolution_batch_members(role, created_at);

CREATE TABLE IF NOT EXISTS association_hypotheses (
    id TEXT PRIMARY KEY,
    version TEXT NOT NULL UNIQUE,
    parent_version TEXT NOT NULL,
    status TEXT NOT NULL,
    hypothesis_json TEXT NOT NULL,
    hypothesis_hash TEXT NOT NULL,
    source_batch_id TEXT,
    created_at TEXT NOT NULL,
    published_at TEXT,
    published_by TEXT
);

CREATE TABLE IF NOT EXISTS experiments (
    id TEXT PRIMARY KEY,
    parent_experiment_id TEXT REFERENCES experiments(id),
    title TEXT NOT NULL,
    env_name TEXT NOT NULL,
    source_task_id TEXT REFERENCES tasks(id),
    question TEXT NOT NULL,
    protocol_json TEXT NOT NULL,
    protocol_hash TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_experiments_env_task
    ON experiments(env_name, source_task_id);

CREATE TABLE IF NOT EXISTS task_variants (
    id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    source_task_id TEXT REFERENCES tasks(id),
    kind TEXT NOT NULL,
    mutator_id TEXT NOT NULL,
    mutator_version TEXT NOT NULL,
    seed INTEGER NOT NULL,
    params_json TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    prompt_ref TEXT,
    context_delta_json TEXT NOT NULL DEFAULT '{}',
    summary_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    error_code TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(experiment_id, mutator_id, mutator_version, seed, params_json)
);
CREATE INDEX IF NOT EXISTS idx_task_variants_experiment
    ON task_variants(experiment_id);
CREATE INDEX IF NOT EXISTS idx_task_variants_content_hash
    ON task_variants(content_hash);

CREATE TABLE IF NOT EXISTS run_groups (
    id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    strategy TEXT NOT NULL,
    status TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    plan_hash TEXT NOT NULL,
    stop_policy_json TEXT NOT NULL,
    total_cells INTEGER NOT NULL,
    completed_cells INTEGER NOT NULL DEFAULT 0,
    partial_cells INTEGER NOT NULL DEFAULT 0,
    failed_cells INTEGER NOT NULL DEFAULT 0,
    cancelled_cells INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    error_code TEXT,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_run_groups_experiment
    ON run_groups(experiment_id, created_at);
CREATE INDEX IF NOT EXISTS idx_run_groups_status ON run_groups(status);

CREATE TABLE IF NOT EXISTS run_group_cells (
    id TEXT PRIMARY KEY,
    run_group_id TEXT NOT NULL REFERENCES run_groups(id) ON DELETE CASCADE,
    variant_id TEXT NOT NULL REFERENCES task_variants(id),
    repeat_index INTEGER NOT NULL,
    run_id TEXT REFERENCES runs(id),
    status TEXT NOT NULL,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(run_group_id, variant_id, repeat_index)
);
CREATE INDEX IF NOT EXISTS idx_run_group_cells_group
    ON run_group_cells(run_group_id, status);
CREATE INDEX IF NOT EXISTS idx_run_group_cells_run_id
    ON run_group_cells(run_id);
CREATE INDEX IF NOT EXISTS idx_run_group_cells_variant
    ON run_group_cells(variant_id);

CREATE TABLE IF NOT EXISTS run_group_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_group_id TEXT NOT NULL REFERENCES run_groups(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(run_group_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_run_group_events_cursor
    ON run_group_events(run_group_id, sequence);

CREATE TABLE IF NOT EXISTS leader_events (
    id TEXT PRIMARY KEY,
    run_group_id TEXT NOT NULL REFERENCES run_groups(id) ON DELETE CASCADE,
    scope_key TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    metric TEXT NOT NULL,
    previous_attempt_id TEXT REFERENCES attempts(id),
    current_attempt_id TEXT NOT NULL REFERENCES attempts(id),
    previous_value REAL,
    current_value REAL NOT NULL,
    delta REAL,
    reason TEXT NOT NULL,
    provisional INTEGER NOT NULL,
    source_score_fingerprint TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    producer_version TEXT NOT NULL,
    input_refs_json TEXT NOT NULL DEFAULT '{}',
    source_outbox_id TEXT UNIQUE REFERENCES score_transition_outbox(id),
    created_at TEXT NOT NULL,
    UNIQUE(run_group_id, scope_key, sequence)
);
CREATE INDEX IF NOT EXISTS idx_leader_events_group_scope
    ON leader_events(run_group_id, scope_key, sequence);

CREATE TABLE IF NOT EXISTS robustness_snapshots (
    id TEXT PRIMARY KEY,
    run_group_id TEXT NOT NULL REFERENCES run_groups(id) ON DELETE CASCADE,
    algorithm_version TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    data_json TEXT NOT NULL,
    status TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    producer_version TEXT NOT NULL,
    input_refs_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_robustness_snapshots_group
    ON robustness_snapshots(run_group_id, created_at);

CREATE TABLE IF NOT EXISTS insight_reports (
    id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    bundle_hash TEXT NOT NULL,
    generator_json TEXT NOT NULL,
    report_json TEXT NOT NULL,
    status TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    producer_version TEXT NOT NULL,
    input_refs_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(experiment_id, version)
);

CREATE TABLE IF NOT EXISTS research_feedback (
    id TEXT PRIMARY KEY,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    scope_json TEXT NOT NULL DEFAULT '{}',
    signal TEXT NOT NULL,
    reason TEXT,
    rationale TEXT,
    actor TEXT NOT NULL,
    supersedes TEXT REFERENCES research_feedback(id),
    schema_version TEXT NOT NULL,
    producer_version TEXT NOT NULL,
    input_refs_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_research_feedback_target
    ON research_feedback(target_type, target_id, created_at);

CREATE TABLE IF NOT EXISTS research_audit_log (
    id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    request_id TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    schema_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_research_audit_target
    ON research_audit_log(target_type, target_id, created_at);

CREATE TABLE IF NOT EXISTS recommendation_states (
    id TEXT PRIMARY KEY,
    scope_key TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    state_json TEXT NOT NULL,
    feedback_watermark TEXT,
    schema_version TEXT NOT NULL,
    producer_version TEXT NOT NULL,
    input_refs_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(scope_key, algorithm_version)
);

CREATE TABLE IF NOT EXISTS profile_recommendations (
    id TEXT PRIMARY KEY,
    scope_key TEXT NOT NULL,
    feature_hash TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    recommendation_json TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_profile_recommendations_scope
    ON profile_recommendations(scope_key, created_at);

CREATE TABLE IF NOT EXISTS normalized_outputs (
    id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
    source_hash TEXT NOT NULL,
    pipeline_hash TEXT NOT NULL,
    output_ref TEXT,
    status TEXT NOT NULL,
    warnings_json TEXT NOT NULL DEFAULT '[]',
    schema_version TEXT NOT NULL,
    producer_version TEXT NOT NULL,
    input_refs_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(attempt_id, pipeline_hash)
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    operation TEXT NOT NULL,
    key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    result_id TEXT,
    status TEXT NOT NULL,
    response_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(operation, key)
);
CREATE INDEX IF NOT EXISTS idx_idempotency_status
    ON idempotency_keys(status, updated_at);

CREATE TABLE IF NOT EXISTS score_transition_outbox (
    id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
    score_revision INTEGER NOT NULL,
    scope_key TEXT,
    score REAL NOT NULL,
    scorer_fingerprint TEXT NOT NULL,
    manifest_ref TEXT,
    seq INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    consumed_at TEXT,
    scope_terminal INTEGER,
    UNIQUE(attempt_id, score_revision)
);
CREATE INDEX IF NOT EXISTS idx_score_outbox_sequence
    ON score_transition_outbox(seq);

CREATE TABLE IF NOT EXISTS attempt_input_snapshots (
    id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
    variant_id TEXT REFERENCES task_variants(id),
    prompt_ref TEXT NOT NULL,
    context_ref TEXT NOT NULL,
    constraints_ref TEXT NOT NULL,
    context_meta_json TEXT NOT NULL DEFAULT '{}',
    material_refs_json TEXT NOT NULL DEFAULT '{}',
    content_hash TEXT NOT NULL,
    payload_state TEXT NOT NULL DEFAULT 'present',
    purged_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(attempt_id)
);
CREATE INDEX IF NOT EXISTS idx_input_snapshots_variant
    ON attempt_input_snapshots(variant_id);
CREATE INDEX IF NOT EXISTS idx_input_snapshots_content_hash
    ON attempt_input_snapshots(content_hash);
"""


# ---------- 同步路径(短连接 + asyncio.to_thread) -------------------------


def _open_sync(db_path: Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _migrate_runs_table(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
    if "compare_mode" not in cols:
        conn.execute("ALTER TABLE runs ADD COLUMN compare_mode TEXT NOT NULL DEFAULT 'multi-agent'")
    if "model" not in cols:
        conn.execute("ALTER TABLE runs ADD COLUMN model TEXT")
    if "execution" not in cols:
        conn.execute("ALTER TABLE runs ADD COLUMN execution TEXT")


def _migrate_attempts_model(conn: sqlite3.Connection) -> None:
    """attempts.model：该 attempt 请求使用的模型（multi-model / per-agent 场景
    同一 run 下各不相同）。区别于 runs.model（run 级单值）与
    external_refs.model_used（adapter 实际探测到的模型）。幂等。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(attempts)").fetchall()}
    if "model" not in cols:
        conn.execute("ALTER TABLE attempts ADD COLUMN model TEXT")
    if "transport_status" not in cols:
        conn.execute(
            "ALTER TABLE attempts ADD COLUMN transport_status TEXT NOT NULL DEFAULT 'unknown'"
        )


def _migrate_attempts_security(conn: sqlite3.Connection) -> None:
    """安全维度列（execution_locus 等）。老库平滑升级：逐列检查后 ALTER。

    分两组：② 执行场合快照 + ①③ 安全事件汇总。均与 score_total 并列，不合并。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(attempts)").fetchall()}
    additions = [
        ("execution_locus", "TEXT"),
        ("permission_mode", "TEXT"),
        ("workspace_root", "TEXT"),
        ("security_event_count", "INTEGER NOT NULL DEFAULT 0"),
        ("security_max_severity", "TEXT"),
        ("security_hitl_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("security_reaction", "TEXT"),
        ("security_coverage_json", "TEXT NOT NULL DEFAULT '{}'"),
    ]
    for name, decl in additions:
        if name not in cols:
            conn.execute(f"ALTER TABLE attempts ADD COLUMN {name} {decl}")


def _migrate_attempts_wire(conn: sqlite3.Connection) -> None:
    """wire 观测摘要列。只存摘要/索引，call/hop/payload 不进主 DB。
    逐列幂等。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(attempts)").fetchall()}
    additions = [
        ("wire_status", "TEXT NOT NULL DEFAULT 'not_available'"),
        ("wire_record_count", "INTEGER NOT NULL DEFAULT 0"),
        ("wire_call_count", "INTEGER NOT NULL DEFAULT 0"),
        ("wire_error_count", "INTEGER NOT NULL DEFAULT 0"),
        ("wire_manifest_version", "TEXT"),
    ]
    for name, decl in additions:
        if name not in cols:
            conn.execute(f"ALTER TABLE attempts ADD COLUMN {name} {decl}")


def _migrate_attempts_cost(conn: sqlite3.Connection) -> None:
    """成本核算列（token_cost_accounting）。逐列幂等。

    `cost_usd` 与 `cost_breakdown_json` 分开存：前者供排序/聚合，后者保留
    四档明细（哪些 token 花在缓存复读上）。`cost_priced` 为 0 时表示有 token
    因缺定价未计入——此时 `cost_usd` 只是**下界**，读取方必须一并检查，
    不能只读总额（静默低估比缺值更危险）。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(attempts)").fetchall()}
    additions = [
        ("cost_usd", "REAL"),
        ("cost_breakdown_json", "TEXT"),
        # 1=全部 token 都有定价；0=部分缺定价，cost_usd 是下界；NULL=未计算
        ("cost_priced", "INTEGER"),
        ("cost_model", "TEXT"),
    ]
    for name, decl in additions:
        if name not in cols:
            conn.execute(f"ALTER TABLE attempts ADD COLUMN {name} {decl}")


def _migrate_attempts_model_integrity(conn: sqlite3.Connection) -> None:
    """Add the fail-closed outbound-model audit fields to existing databases."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(attempts)").fetchall()}
    additions = [
        (
            "model_integrity_status",
            "TEXT NOT NULL DEFAULT 'not_observed'",
        ),
        (
            "model_integrity_observed_json",
            "TEXT NOT NULL DEFAULT '[]'",
        ),
        (
            "model_integrity_violation_count",
            "INTEGER NOT NULL DEFAULT 0",
        ),
        ("model_integrity_error_code", "TEXT"),
        ("model_integrity_error_message", "TEXT"),
        ("model_integrity_checked_at", "TEXT"),
    ]
    for name, decl in additions:
        if name not in cols:
            conn.execute(f"ALTER TABLE attempts ADD COLUMN {name} {decl}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_attempts_model_integrity_status "
        "ON attempts(model_integrity_status)"
    )


def _migrate_run_cost_audits(conn: sqlite3.Connection) -> None:
    """run 级成本审计快照（run_cost_audit）。每个 run 一行，不是逐请求账本。

    **为什么资金口径必须独立于 attempts.cost_\\***：后者是 token × 本地价格表的
    估算，只能回答"按我们的定价表算应该多少钱"；这张表存的是上游对一把
    run 专属 key 的累计实扣差值，回答"实际扣了多少钱"。两者定位不同，
    差异率本身就是要观测的指标，不能合并成一列。

    `usage_start` 必须在任何 agent 进程启动前落库——它是进程重启后能重新
    结算的唯一锚点（内存里的 key 明文会丢，但读 usage 只需 Management Key）。

    表中**不存任何 key 明文**：`api_key_hash` 是上游返回的 hash，
    `api_key_name` 是 run ID 的非敏感缩写。
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS run_cost_audits (
            run_id                TEXT PRIMARY KEY REFERENCES runs(id),
            provider              TEXT NOT NULL DEFAULT 'openrouter',
            api_key_hash          TEXT,
            api_key_name          TEXT,
            attribution_mode      TEXT NOT NULL,
            status                TEXT NOT NULL,
            usage_start           REAL,
            usage_after_execution REAL,
            usage_final           REAL,
            execution_cost_usd    REAL,
            scoring_cost_usd      REAL,
            total_cost_usd        REAL,
            key_limit_usd         REAL,
            activity_json         TEXT,
            key_disabled          INTEGER,
            settle_attempts       INTEGER NOT NULL DEFAULT 0,
            last_polled_at        TEXT,
            started_at            TEXT NOT NULL,
            finalized_at          TEXT,
            error_code            TEXT,
            error_message         TEXT
        )
        """
    )
    # settler 按 status 扫描 pending/settling 做重启恢复，必须走索引。
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_run_cost_audits_status "
        "ON run_cost_audits(status)"
    )
    # 逐列幂等补齐：老库若已建过早期版本的表，这里补新增列。
    cols = {r[1] for r in conn.execute("PRAGMA table_info(run_cost_audits)").fetchall()}
    additions = [
        ("api_key_name", "TEXT"),
        ("key_limit_usd", "REAL"),
        ("activity_json", "TEXT"),
        ("key_disabled", "INTEGER"),
        ("settle_attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("last_polled_at", "TEXT"),
        ("error_message", "TEXT"),
    ]
    for name, decl in additions:
        if name not in cols:
            conn.execute(f"ALTER TABLE run_cost_audits ADD COLUMN {name} {decl}")


def _migrate_cost_key_ledgers(conn: sqlite3.Connection) -> None:
    """逐 key 成本账（run_cost_audit revision-per-attempt-key，2026-07-29）。

    **为什么不用 token 估算**：实测同一个 attempt 三种口径差 8 倍
    （上游真值 $0.0575 / 五维重算 $0.1139 / 库存截断 $0.4740），
    全实验高估 26×，且偏差方向不一致（高估 3.5~4× 与低估 5~10× 并存）。
    根因之一是结构性的——两种 provider 口径靠 `cache_read > input` 判别，
    在 claude-code 真实数据上失效。只要自己算 token，就要维护一张跟着
    各家语义漂移的口径表；而上游本来就知道答案。

    **一把 key 一行**。`scope` 区分两类：

    - `attempt`：`scope_id` 是 attempt_id，该 agent 执行期间的独占 key；
    - `judge`：`scope_id` 是 run_id，整个 run 的评分共用一把。

    run 级总成本 = 该 run 下全部 attempt 行 + 一行 judge 行，聚合得到，
    **不再另建 run 级 key**（会重复计数）。

    表中不存任何 key 明文，只存上游返回的 hash。
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cost_key_ledgers (
            id               TEXT PRIMARY KEY,
            run_id           TEXT NOT NULL,
            scope            TEXT NOT NULL,
            scope_id         TEXT NOT NULL,
            agent_name       TEXT,
            provider         TEXT NOT NULL DEFAULT 'openrouter',
            api_key_hash     TEXT,
            api_key_name     TEXT,
            attribution_mode TEXT NOT NULL,
            status           TEXT NOT NULL,
            usage_start      REAL,
            usage_final      REAL,
            cost_usd         REAL,
            key_limit_usd    REAL,
            key_disabled     INTEGER,
            settle_attempts  INTEGER NOT NULL DEFAULT 0,
            last_polled_at   TEXT,
            activity_json    TEXT,
            started_at       TEXT NOT NULL,
            finalized_at     TEXT,
            error_code       TEXT,
            error_message    TEXT
        )
        """
    )
    # 一个 scope 只能有一把 key——防并发/重启造出第二把导致重复计数。
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_cost_key_scope "
        "ON cost_key_ledgers(scope, scope_id)"
    )
    # settler 扫描未结算的账；run 详情按 run_id 聚合。
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cost_key_status ON cost_key_ledgers(status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cost_key_run ON cost_key_ledgers(run_id)"
    )
    # 逐列幂等补齐（老库）
    cols = {r[1] for r in conn.execute("PRAGMA table_info(cost_key_ledgers)").fetchall()}
    if "activity_json" not in cols:
        conn.execute("ALTER TABLE cost_key_ledgers ADD COLUMN activity_json TEXT")


def _migrate_attempt_observability(conn: sqlite3.Connection) -> None:
    """S1 additive heartbeat/progress columns and attempt event indexes."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(attempts)").fetchall()}
    for name, decl in (
        ("heartbeat_at", "TEXT"),
        ("heartbeat_seq", "INTEGER NOT NULL DEFAULT 0"),
        ("current_stage", "TEXT"),
        ("progress_message", "TEXT"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE attempts ADD COLUMN {name} {decl}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_attempts_heartbeat_at ON attempts(heartbeat_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_attempt_events_attempt_seq "
        "ON attempt_events(attempt_id, sequence)"
    )


def _migrate_execution_scoring(conn: sqlite3.Connection) -> None:
    """Add the independent execution/scoring state axes to existing databases."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(attempts)").fetchall()}
    additions = [
        ("execution_status", "TEXT NOT NULL DEFAULT 'queued'"),
        ("execution_started_at", "TEXT"),
        ("execution_ended_at", "TEXT"),
        ("execution_deadline_at", "TEXT"),
        ("execution_agent_deadline_at", "TEXT"),
        ("execution_evaluator_started_at", "TEXT"),
        ("execution_error_code", "TEXT"),
        ("execution_error_message", "TEXT"),
        ("scoring_status", "TEXT NOT NULL DEFAULT 'not_ready'"),
        ("scoring_queued_at", "TEXT"),
        ("scoring_started_at", "TEXT"),
        ("scoring_ended_at", "TEXT"),
        ("scoring_deadline_at", "TEXT"),
        ("scoring_error_code", "TEXT"),
        ("scoring_error_message", "TEXT"),
    ]
    for name, decl in additions:
        if name not in cols:
            conn.execute(f"ALTER TABLE attempts ADD COLUMN {name} {decl}")
    # 老库只有一个 execution deadline，且不存在 evaluator active marker；它可安全
    # 作为首次迁移时的 canonical Agent deadline。
    conn.execute(
        "UPDATE attempts SET execution_agent_deadline_at=execution_deadline_at "
        "WHERE execution_agent_deadline_at IS NULL "
        "AND execution_evaluator_started_at IS NULL"
    )
    # Backfill only the two status axes. Historical timestamps remain unknown.
    conn.execute(
        "UPDATE attempts SET execution_status=CASE "
        "WHEN status='queued' THEN 'queued' "
        "WHEN status IN ('running','starting_blade_session') THEN 'running' "
        "WHEN status='timeout' THEN 'timeout' "
        "WHEN status IN ('completed','gave_up','scoring_failed') THEN 'completed' "
        "ELSE 'failed' END "
        "WHERE execution_status IS NULL OR execution_status='queued' AND status<>'queued'"
    )
    score_completed = (
        "score_total IS NOT NULL" if "score_total" in cols else "0"
    )
    conn.execute(
        "UPDATE attempts SET scoring_status=CASE "
        f"WHEN {score_completed} THEN 'completed' "
        "WHEN status='scoring_failed' THEN 'failed' "
        "WHEN status='scoring' THEN 'queued' "
        "WHEN status IN ('completed','gave_up') THEN 'completed' "
        "WHEN status IN ('queued','running','starting_blade_session') THEN 'not_ready' "
        "ELSE 'skipped' END "
        "WHERE scoring_status IS NULL OR scoring_status='not_ready'"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_attempts_execution_status "
        "ON attempts(execution_status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_attempts_scoring_status "
        "ON attempts(scoring_status)"
    )


def _migrate_rubric_evolution(conn: sqlite3.Connection) -> None:
    """Additive Rubric version freeze and append-only score revision storage."""
    run_cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
    if "rubric_version" not in run_cols:
        conn.execute("ALTER TABLE runs ADD COLUMN rubric_version TEXT")
    attempt_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(attempts)").fetchall()
    }
    if "rubric_version" not in attempt_cols:
        conn.execute("ALTER TABLE attempts ADD COLUMN rubric_version TEXT")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS rubric_versions (
            id TEXT PRIMARY KEY,
            env_name TEXT,
            version TEXT NOT NULL,
            parent_version TEXT NOT NULL,
            scope TEXT NOT NULL,
            status TEXT NOT NULL,
            rubric_json TEXT NOT NULL,
            rubric_hash TEXT NOT NULL,
            source_batch_id TEXT,
            created_at TEXT NOT NULL,
            published_at TEXT,
            published_by TEXT,
            UNIQUE(env_name, version)
        );
        CREATE INDEX IF NOT EXISTS idx_rubric_versions_env_status
            ON rubric_versions(env_name, status, created_at);
        CREATE TABLE IF NOT EXISTS active_rubrics (
            env_name TEXT PRIMARY KEY,
            rubric_version TEXT NOT NULL,
            rubric_id TEXT NOT NULL REFERENCES rubric_versions(id),
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS rubric_score_revisions (
            id TEXT PRIMARY KEY,
            attempt_id TEXT NOT NULL REFERENCES attempts(id),
            rubric_version TEXT NOT NULL,
            score_kind TEXT NOT NULL,
            score_with_unknown REAL NOT NULL,
            normalized_score_without_unknown REAL,
            unknown_count INTEGER NOT NULL DEFAULT 0,
            unknown_weight REAL NOT NULL DEFAULT 0,
            checks_json TEXT NOT NULL,
            evaluation_manifest_ref TEXT,
            source_batch_id TEXT,
            operation_id TEXT UNIQUE,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_rubric_score_revisions_attempt
            ON rubric_score_revisions(attempt_id, rubric_version, score_kind, created_at);
        CREATE TABLE IF NOT EXISTS rubric_judge_records (
            id TEXT PRIMARY KEY,
            env_name TEXT NOT NULL,
            rubric_version TEXT NOT NULL,
            run_id TEXT NOT NULL,
            attempt_id TEXT NOT NULL REFERENCES attempts(id),
            judge_execution_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            valid_execution INTEGER NOT NULL,
            checks_json TEXT NOT NULL DEFAULT '[]',
            execution_error TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_rubric_judge_records_scope
            ON rubric_judge_records(env_name, rubric_version, valid_execution, created_at);
        CREATE TABLE IF NOT EXISTS rubric_evolution_batch_members (
            record_id TEXT NOT NULL REFERENCES rubric_judge_records(id),
            batch_id TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            role TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(record_id, batch_id,role)
        );
        CREATE INDEX IF NOT EXISTS idx_rubric_batch_members_scope
            ON rubric_evolution_batch_members(scope_key, role, created_at);
        """
    )
    rubric_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(rubric_versions)").fetchall()
    }
    if "evolution_domain" not in rubric_cols:
        conn.execute(
            "ALTER TABLE rubric_versions ADD COLUMN evolution_domain TEXT "
            "NOT NULL DEFAULT 'product'"
        )
    if "executor_kind" not in rubric_cols:
        conn.execute(
            "ALTER TABLE rubric_versions ADD COLUMN executor_kind TEXT "
            "NOT NULL DEFAULT 'hybrid'"
        )

    revision_cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(rubric_score_revisions)").fetchall()
    }
    if "operation_id" not in revision_cols:
        conn.execute("ALTER TABLE rubric_score_revisions ADD COLUMN operation_id TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_rubric_score_revisions_operation "
        "ON rubric_score_revisions(operation_id) WHERE operation_id IS NOT NULL"
    )
    judge_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(rubric_judge_records)").fetchall()
    }
    if "operation_id" not in judge_cols:
        conn.execute("ALTER TABLE rubric_judge_records ADD COLUMN operation_id TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_rubric_judge_records_operation "
        "ON rubric_judge_records(operation_id) WHERE operation_id IS NOT NULL"
    )
    association_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(association_cases)").fetchall()
    }
    if "valid_evidence" not in association_cols:
        conn.execute(
            "ALTER TABLE association_cases ADD COLUMN valid_evidence INTEGER "
            "NOT NULL DEFAULT 1"
        )
    if "invalid_reason" not in association_cols:
        conn.execute("ALTER TABLE association_cases ADD COLUMN invalid_reason TEXT")


def _migrate_research_columns(conn: sqlite3.Connection) -> None:
    """RC-F-03 additive columns on legacy authority tables.

    Failure classification remains subordinate to ``attempts.status``: these
    columns make robustness filtering structured without inventing a second
    failure-fact table.
    """
    attempt_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(attempts)").fetchall()
    }
    for name, decl in (
        ("failure_kind", "TEXT"),
        ("retryable", "INTEGER"),
    ):
        if name not in attempt_cols:
            conn.execute(f"ALTER TABLE attempts ADD COLUMN {name} {decl}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_attempts_failure_kind "
        "ON attempts(failure_kind, retryable)"
    )

    score_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(scores)").fetchall()
    }
    for name, decl in (
        ("scored_at", "TEXT"),
        ("evaluation_manifest_ref", "TEXT"),
    ):
        if name not in score_cols:
            conn.execute(f"ALTER TABLE scores ADD COLUMN {name} {decl}")

    experiment_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(experiments)").fetchall()
    }
    if "parent_experiment_id" not in experiment_cols:
        conn.execute(
            "ALTER TABLE experiments ADD COLUMN parent_experiment_id TEXT "
            "REFERENCES experiments(id)"
        )

    outbox_cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(score_transition_outbox)").fetchall()
    }
    if "consumed_at" not in outbox_cols:
        conn.execute("ALTER TABLE score_transition_outbox ADD COLUMN consumed_at TEXT")
    if "scope_terminal" not in outbox_cols:
        conn.execute(
            "ALTER TABLE score_transition_outbox ADD COLUMN scope_terminal INTEGER"
        )
    leader_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(leader_events)").fetchall()
    }
    if "source_outbox_id" not in leader_cols:
        conn.execute(
            "ALTER TABLE leader_events ADD COLUMN source_outbox_id TEXT "
            "REFERENCES score_transition_outbox(id)"
        )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_leader_events_source_outbox "
        "ON leader_events(source_outbox_id)"
    )
    feedback_cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(research_feedback)").fetchall()
    }
    if "scope_json" not in feedback_cols:
        conn.execute(
            "ALTER TABLE research_feedback ADD COLUMN scope_json TEXT NOT NULL DEFAULT '{}'"
        )


def _init_db_sync(db_path: Path) -> None:
    with _open_sync(db_path) as conn:
        conn.executescript(_SCHEMA)
        _migrate_runs_table(conn)
        _migrate_attempts_model(conn)
        _migrate_attempts_security(conn)
        _migrate_attempts_wire(conn)
        _migrate_attempts_cost(conn)
        _migrate_attempts_model_integrity(conn)
        _migrate_run_cost_audits(conn)
        _migrate_cost_key_ledgers(conn)
        _migrate_execution_scoring(conn)
        _migrate_attempt_observability(conn)
        _migrate_rubric_evolution(conn)
        _migrate_research_columns(conn)
        conn.commit()


def _list_tables_sync(db_path: Path) -> list[str]:
    if not Path(db_path).exists():
        return []
    with _open_sync(db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    return sorted(r[0] for r in rows)


# ---------- async wrappers ------------------------------------------------


async def init_db(db_path: Path | str) -> None:
    """async init,创建 4 张表 + 索引。幂等。"""
    await asyncio.to_thread(_init_db_sync, Path(db_path))


async def list_tables(db_path: Path | str) -> list[str]:
    return await asyncio.to_thread(_list_tables_sync, Path(db_path))


# ---------- 长连接(lifespan 用) -----------------------------------------


def resolve_db_path(data_path: Path | str) -> Path:
    return Path(data_path) / "octagon.db"


async def open_db(data_path: Path | str) -> aiosqlite.Connection:
    """供 FastAPI lifespan 用的长连接。lifespan 关闭时 close。"""
    Path(data_path).mkdir(parents=True, exist_ok=True)
    db_path = resolve_db_path(data_path)
    await init_db(db_path)
    conn = await aiosqlite.connect(db_path)
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.commit()
    return conn


# ---------- env_token 生成与校验 ------------------------------------------


def hash_env_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_env_token() -> str:
    """生成 attempt 独立 env token(URL-safe)。"""
    return secrets.token_urlsafe(32)


# ---------- attempts insert/update ---------------------------------------


_FORBIDDEN_EXTERNAL_REFS_KEYS = {"env_token", "env_token_hash"}


def _validate_external_refs(external_refs_json: str | None) -> None:
    if not external_refs_json:
        return
    try:
        data = json.loads(external_refs_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"external_refs_json 不是合法 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("external_refs_json 顶层必须是 object")
    leaks = _FORBIDDEN_EXTERNAL_REFS_KEYS & set(data)
    if leaks:
        raise ValueError(
            f"external_refs_json 禁止包含敏感字段 env_token: {sorted(leaks)}"
        )


def _insert_attempt_sync(db_path: Path, row: dict[str, Any]) -> None:
    _validate_external_refs(row.get("external_refs_json"))
    _init_db_sync(db_path)  # 幂等保证 schema 存在
    now = _now_iso()
    columns = {
        "id": row["id"],
        "run_id": row.get("run_id", ""),
        "task_id": row.get("task_id", ""),
        "env_name": row.get("env_name", ""),
        "agent_name": row.get("agent_name", "blade-agent"),
        "model": row.get("model"),
        "status": row.get("status", "queued"),
        "transport_status": row.get("transport_status", "unknown"),
        "env_session_id": row.get("env_session_id", ""),
        "env_token_hash": row.get("env_token_hash", ""),
        "external_refs_json": row.get("external_refs_json") or "{}",
        "event_count": row.get("event_count", 0),
        "last_event_at": row.get("last_event_at"),
        "score_total": row.get("score_total"),
        "error_code": row.get("error_code"),
        "error_message": row.get("error_message"),
        "failure_kind": row.get("failure_kind"),
        "retryable": row.get("retryable"),
        "started_at": row.get("started_at"),
        "ended_at": row.get("ended_at"),
        "created_at": row.get("created_at", now),
    }
    placeholders = ", ".join("?" for _ in columns)
    cols_str = ", ".join(columns.keys())
    with _open_sync(db_path) as conn:
        conn.execute(
            f"INSERT INTO attempts ({cols_str}) VALUES ({placeholders})",
            tuple(columns.values()),
        )
        conn.commit()


async def insert_attempt(db_path: Path | str, row: dict[str, Any]) -> None:
    """插入 attempt 行。如果 row 里 external_refs_json 含 env_token,直接 raise ValueError。"""
    await asyncio.to_thread(_insert_attempt_sync, Path(db_path), dict(row))


@dataclass
class CreatedAttempt:
    """create_attempt 返回值。

    `env_token` 是**明文**,只在创建瞬间从这个返回值取一次,写到 blade workspace
    的 `.octagon/attempt.json`,**不**得回写 DB / 不得记日志。DB 里只存 hash
    (`attempts.env_token_hash`)。
    """

    attempt_id: str
    env_token: str  # 明文
    env_token_hash: str
    env_session_id: str


class IdempotencyConflict(RuntimeError):
    pass


class IdempotencyInProgress(RuntimeError):
    pass


@dataclass(frozen=True)
class IdempotencyClaim:
    operation: str
    key: str
    request_hash: str
    replayed: bool
    result_id: str | None = None
    response: dict[str, Any] | None = None


def claim_idempotency(
    conn: sqlite3.Connection,
    *,
    operation: str,
    key: str,
    request_hash: str,
) -> IdempotencyClaim:
    """Claim a key inside the caller's transaction.

    Experiment repositories call this on the same connection/transaction that
    creates their result row, making key claim and result creation atomic.
    """
    row = conn.execute(
        "SELECT request_hash, result_id, status, response_json "
        "FROM idempotency_keys WHERE operation=? AND key=?",
        (operation, key),
    ).fetchone()
    now = _now_iso()
    if row is None:
        conn.execute(
            "INSERT INTO idempotency_keys(operation, key, request_hash, status, "
            "created_at, updated_at) VALUES(?, ?, ?, 'in_progress', ?, ?)",
            (operation, key, request_hash, now, now),
        )
        return IdempotencyClaim(operation, key, request_hash, replayed=False)
    stored_hash, result_id, status, response_json = row
    if stored_hash != request_hash:
        raise IdempotencyConflict(
            f"idempotency_conflict: operation={operation!r} key={key!r}"
        )
    if status == "in_progress":
        raise IdempotencyInProgress(
            f"idempotency_in_progress: operation={operation!r} key={key!r}"
        )
    if status == "completed":
        response = json.loads(response_json) if response_json else None
        return IdempotencyClaim(
            operation,
            key,
            request_hash,
            replayed=True,
            result_id=result_id,
            response=response,
        )
    # A reconciled/explicit failure is retryable only for the same request.
    conn.execute(
        "UPDATE idempotency_keys SET status='in_progress', result_id=NULL, "
        "response_json=NULL, updated_at=? WHERE operation=? AND key=?",
        (now, operation, key),
    )
    return IdempotencyClaim(operation, key, request_hash, replayed=False)


def complete_idempotency(
    conn: sqlite3.Connection,
    *,
    operation: str,
    key: str,
    result_id: str,
    response: dict[str, Any],
) -> None:
    cursor = conn.execute(
        "UPDATE idempotency_keys SET result_id=?, status='completed', "
        "response_json=?, updated_at=? "
        "WHERE operation=? AND key=? AND status='in_progress'",
        (
            result_id,
            json.dumps(response, ensure_ascii=False, sort_keys=True),
            _now_iso(),
            operation,
            key,
        ),
    )
    if cursor.rowcount != 1:
        raise IdempotencyConflict(
            f"idempotency claim is not in progress: {operation!r}/{key!r}"
        )


def fail_idempotency(
    conn: sqlite3.Connection, *, operation: str, key: str
) -> None:
    conn.execute(
        "UPDATE idempotency_keys SET status='failed', updated_at=? "
        "WHERE operation=? AND key=? AND status='in_progress'",
        (_now_iso(), operation, key),
    )


def reconcile_idempotency(conn: sqlite3.Connection) -> int:
    """Converge crash-left in-progress rows without inventing results."""
    table_by_operation = {
        "experiment:create": "experiments",
        "run_group:create": "run_groups",
        "legacy_run:create": "runs",
    }
    rows = conn.execute(
        "SELECT operation, key, result_id, response_json FROM idempotency_keys "
        "WHERE status='in_progress'"
    ).fetchall()
    changed = 0
    for operation, key, result_id, response_json in rows:
        table = table_by_operation.get(operation)
        exists = False
        if table and result_id:
            exists = (
                conn.execute(
                    f"SELECT 1 FROM {table} WHERE id=?", (result_id,)
                ).fetchone()
                is not None
            )
        status = "completed" if exists else "failed"
        response = response_json
        if exists and not response:
            response = json.dumps({"id": result_id}, sort_keys=True)
        conn.execute(
            "UPDATE idempotency_keys SET status=?, response_json=?, updated_at=? "
            "WHERE operation=? AND key=? AND status='in_progress'",
            (status, response, _now_iso(), operation, key),
        )
        changed += 1
    return changed


def reconcile_idempotency_sync(db_path: Path) -> int:
    _init_db_sync(db_path)
    with _open_sync(db_path) as conn:
        changed = reconcile_idempotency(conn)
        conn.commit()
    return changed


async def create_attempt(
    db_path: Path | str,
    *,
    attempt_id: str,
    run_id: str,
    task_id: str,
    env_name: str,
    env_session_id: str,
    external_refs: dict[str, Any] | None = None,
    model: str | None = None,
) -> CreatedAttempt:
    """生成 env_token + 写 attempts 行,返回明文 token。

    简版:runner 会扩展 status 流转、started_at 等。
    """
    token = generate_env_token()
    token_hash = hash_env_token(token)
    await asyncio.to_thread(
        _create_attempt_with_snapshot_sync,
        Path(db_path),
        attempt_id=attempt_id,
        run_id=run_id,
        task_id=task_id,
        env_name=env_name,
        env_session_id=env_session_id,
        token_hash=token_hash,
        external_refs=external_refs or {},
        model=model,
    )
    return CreatedAttempt(
        attempt_id=attempt_id,
        env_token=token,
        env_token_hash=token_hash,
        env_session_id=env_session_id,
    )


def _create_attempt_with_snapshot_sync(
    db_path: Path,
    *,
    attempt_id: str,
    run_id: str,
    task_id: str,
    env_name: str,
    env_session_id: str,
    token_hash: str,
    external_refs: dict[str, Any],
    model: str | None,
) -> None:
    """Low-level helper kept atomic for callers outside ``runner``."""
    _init_db_sync(db_path)
    refs_json = json.dumps(external_refs, ensure_ascii=False, sort_keys=True)
    _validate_external_refs(refs_json)
    with _open_sync(db_path) as conn:
        task = conn.execute(
            "SELECT prompt, context_json, constraints_json, timeout_seconds "
            "FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
    if task is None:
        raise ValueError(f"task not found: {task_id}")
    from .input_snapshots import insert_snapshot, prepare_snapshot

    snapshot = prepare_snapshot(
        data_path=db_path.parent,
        attempt_id=attempt_id,
        prompt=task[0],
        context=json.loads(task[1] or "{}"),
        constraints=json.loads(task[2] or "{}"),
        timeout_seconds=task[3],
        variant_id=(
            str(external_refs["variant_id"])
            if external_refs.get("variant_id")
            else None
        ),
    )
    with _open_sync(db_path) as conn:
        conn.execute(
            "INSERT INTO attempts(id,run_id,task_id,env_name,agent_name,model,status,"
            "env_session_id,env_token_hash,external_refs_json,event_count,created_at) "
            "VALUES(?,?,?,?,?,?, 'queued', ?,?,?,0,?)",
            (
                attempt_id,
                run_id,
                task_id,
                env_name,
                "blade-agent",
                model,
                env_session_id,
                token_hash,
                refs_json,
                _now_iso(),
            ),
        )
        insert_snapshot(conn, snapshot)
        conn.commit()


# ---------- blade_events.jsonl + 概览 -------------------------------------

_event_data_paths: dict[str, Path] = {}
_event_lock = threading.Lock()


def _attempt_dir(data_path: Path, attempt_id: str) -> Path:
    return data_path / "attempts" / attempt_id


def _events_path(data_path: Path, attempt_id: str) -> Path:
    return _attempt_dir(data_path, attempt_id) / "blade_events.jsonl"


def _append_event_sync(data_path: Path, attempt_id: str, event: dict[str, Any]) -> None:
    p = _events_path(data_path, attempt_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False, default=str) + "\n"
    with _event_lock:
        with p.open("a", encoding="utf-8") as fp:
            fp.write(line)
        _event_data_paths[attempt_id] = data_path


async def append_blade_event(
    data_path: Path | str, attempt_id: str, event: dict[str, Any]
) -> None:
    """追加 blade Socket.IO 事件到 attempt 专属 jsonl。同时把 data_path 注册到
    内存 map,供 `get_attempt_event_summary` 反查。"""
    await asyncio.to_thread(_append_event_sync, Path(data_path), attempt_id, dict(event))


@dataclass
class AttemptEventSummary:
    attempt_id: str
    count: int
    last_event_at: str | None  # ISO8601;来自 event 内 timestamp 字段(若有),否则 None


def _summary_sync(attempt_id: str) -> AttemptEventSummary:
    with _event_lock:
        data_path = _event_data_paths.get(attempt_id)
    if data_path is None:
        return AttemptEventSummary(attempt_id=attempt_id, count=0, last_event_at=None)
    p = _events_path(data_path, attempt_id)
    if not p.exists():
        return AttemptEventSummary(attempt_id=attempt_id, count=0, last_event_at=None)
    count = 0
    last_ts: str | None = None
    with p.open("r", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            count += 1
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = ev.get("timestamp")
            if isinstance(ts, str):
                last_ts = ts
    return AttemptEventSummary(attempt_id=attempt_id, count=count, last_event_at=last_ts)


async def get_attempt_event_summary(attempt_id: str) -> AttemptEventSummary:
    """读 jsonl 算条数。M1 单进程内可信;若 octagon 跨进程部署需要改 API 显式传 data_path。"""
    return await asyncio.to_thread(_summary_sync, attempt_id)


# ---------- 实时 attempt 观测 ---------------------------------------------


def _append_attempt_observation_sync(
    db_path: Path,
    attempt_id: str,
    *,
    stage: str,
    event_type: str,
    severity: str = "info",
    producer: str = "octagon-control-plane",
    operation_id: str | None = None,
    payload: dict[str, Any] | None = None,
    evidence_refs: list[str] | None = None,
    heartbeat: bool = False,
) -> dict[str, Any] | None:
    """写入统一 attempt 事件，并可原子更新实时进度投影。

    这是 S1 的控制面旁路：不替换原始 trace/events，只为 SSE、恢复器和后续
    分析器提供稳定的事件信封。重复 operation_id 由调用方保证幂等；事件自身
    通过 event_id + (attempt_id, sequence) 防止重复写入。
    """
    with _open_sync(db_path) as conn:
        conn.row_factory = sqlite3.Row
        attempt = conn.execute(
            "SELECT run_id,status FROM attempts WHERE id=?", (attempt_id,)
        ).fetchone()
        if attempt is None:
            return None
        now = _now_iso()
        sequence = int(conn.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM attempt_events WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()[0])
        event_id = f"evt_{uuid.uuid4().hex}"
        conn.execute(
            "INSERT INTO attempt_events(event_id,attempt_id,run_id,sequence,occurred_at,"
            "stage,event_type,severity,producer,operation_id,payload_json,evidence_refs_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id, attempt_id, attempt["run_id"], sequence, now, stage,
                event_type, severity, producer, operation_id,
                json.dumps(payload or {}, ensure_ascii=False, default=str),
                json.dumps(evidence_refs or [], ensure_ascii=False),
            ),
        )
        if heartbeat:
            conn.execute(
                "UPDATE attempts SET heartbeat_at=?,heartbeat_seq=heartbeat_seq+1,"
                "current_stage=?,progress_message=? WHERE id=? AND execution_status "
                "IN ('queued','running')",
                (now, stage, (payload or {}).get("message"), attempt_id),
            )
        conn.commit()
        return {
            "event_id": event_id,
            "attempt_id": attempt_id,
            "run_id": attempt["run_id"],
            "sequence": sequence,
            "occurred_at": now,
            "stage": stage,
            "event_type": event_type,
            "severity": severity,
            "producer": producer,
            "operation_id": operation_id,
            "data": payload or {},
            "evidence_refs": evidence_refs or [],
        }


async def append_attempt_observation(
    db_path: Path | str,
    attempt_id: str,
    *,
    stage: str,
    event_type: str,
    severity: str = "info",
    producer: str = "octagon-control-plane",
    operation_id: str | None = None,
    payload: dict[str, Any] | None = None,
    evidence_refs: list[str] | None = None,
    heartbeat: bool = False,
) -> dict[str, Any] | None:
    return await asyncio.to_thread(
        _append_attempt_observation_sync,
        Path(db_path), attempt_id,
        stage=stage, event_type=event_type, severity=severity,
        producer=producer, operation_id=operation_id, payload=payload,
        evidence_refs=evidence_refs, heartbeat=heartbeat,
    )


def list_attempt_observations(
    db_path: Path | str, attempt_id: str, *, after: int = 0, limit: int = 200
) -> list[dict[str, Any]]:
    with _open_sync(Path(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT event_id,attempt_id,run_id,sequence,occurred_at,stage,event_type,"
            "severity,producer,operation_id,payload_json,evidence_refs_json "
            "FROM attempt_events WHERE attempt_id=? AND sequence>? "
            "ORDER BY sequence LIMIT ?",
            (attempt_id, max(0, after), max(1, min(limit, 1000))),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["data"] = json.loads(item.pop("payload_json") or "{}")
        item["evidence_refs"] = json.loads(item.pop("evidence_refs_json") or "[]")
        result.append(item)
    return result


# ---------- 时间 ----------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(value: str | None) -> datetime | None:
    """解析本模块写出的 Z 结尾 ISO 时间戳；无法解析时返回 None 而不是抛。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _iso_after(start_iso: str, seconds: float) -> str:
    """``start_iso`` 之后 ``seconds`` 秒的绝对时间戳。

    deadline 一律用绝对时钟持久化：monotonic 计时器重启即失忆，常驻 sweeper
    和启动恢复都无法据此判断一个 attempt 是否早该收敛。
    """
    base = _parse_iso(start_iso) or datetime.now(timezone.utc)
    return (
        (base + timedelta(seconds=seconds))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
