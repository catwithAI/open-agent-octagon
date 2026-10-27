"""Additive, transactional SQLite migrations for the Skill Eval domain."""
from __future__ import annotations

import sqlite3

ASSET_SCHEMA = """
CREATE TABLE IF NOT EXISTS skill_eval_skills (
 id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, name TEXT NOT NULL, name_key TEXT NOT NULL,
 source_kind TEXT NOT NULL DEFAULT 'upload', source_ref TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(scope_id,name_key)
);
CREATE TABLE IF NOT EXISTS skill_eval_versions (
 id TEXT PRIMARY KEY, skill_id TEXT NOT NULL REFERENCES skill_eval_skills(id),
 content_hash TEXT NOT NULL, manifest_ref TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(skill_id,content_hash), UNIQUE(id,skill_id)
);
CREATE TABLE IF NOT EXISTS skill_eval_releases (
 id TEXT PRIMARY KEY, skill_id TEXT NOT NULL REFERENCES skill_eval_skills(id),
 version_label TEXT NOT NULL, version_key TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('unpublished','published')),
 current_version_id TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0),
 source_release_id TEXT REFERENCES skill_eval_releases(id), published_at TEXT, source_ref TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(skill_id,version_key), UNIQUE(id,skill_id),
 FOREIGN KEY(current_version_id,skill_id) REFERENCES skill_eval_versions(id,skill_id)
);
CREATE TABLE IF NOT EXISTS skill_eval_experiments (
 id TEXT PRIMARY KEY, skill_id TEXT NOT NULL REFERENCES skill_eval_skills(id),
 name TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(id,skill_id)
);
CREATE TABLE IF NOT EXISTS skill_eval_imports (
 id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, staging_ref TEXT NOT NULL,
 detected_name TEXT, detected_version TEXT, metadata_json TEXT NOT NULL DEFAULT '{}',
 state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','committed','expired')),
 expires_at TEXT NOT NULL, created_at TEXT NOT NULL,
 committed_release_id TEXT REFERENCES skill_eval_releases(id), committed_result_json TEXT
);
CREATE TABLE IF NOT EXISTS skill_eval_drafts (
 id TEXT PRIMARY KEY, skill_id TEXT NOT NULL REFERENCES skill_eval_skills(id),
 source_run_id TEXT REFERENCES skill_eval_runs(id), base_version_id TEXT,
 source_release_id TEXT, target_release_id TEXT,
 manifest_hash TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 FOREIGN KEY(base_version_id,skill_id) REFERENCES skill_eval_versions(id,skill_id),
 FOREIGN KEY(source_release_id,skill_id) REFERENCES skill_eval_releases(id,skill_id),
 FOREIGN KEY(target_release_id,skill_id) REFERENCES skill_eval_releases(id,skill_id)
);
CREATE TABLE IF NOT EXISTS skill_eval_blobs (
 hash TEXT PRIMARY KEY, size INTEGER NOT NULL CHECK(size>=0), storage_ref TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS skill_eval_inputs (
 id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, hash TEXT NOT NULL REFERENCES skill_eval_blobs(hash),
 original_name TEXT NOT NULL, media_type TEXT NOT NULL, size INTEGER NOT NULL CHECK(size>=0),
 state TEXT NOT NULL DEFAULT 'complete' CHECK(state IN ('uploading','complete','failed')),
 created_at TEXT NOT NULL
);
"""

RUN_SCHEMA = """
CREATE TABLE IF NOT EXISTS skill_eval_runs (
 id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL, skill_id TEXT NOT NULL,
 draft_id TEXT REFERENCES skill_eval_drafts(id), skill_version_id TEXT NOT NULL,
 source_release_id TEXT REFERENCES skill_eval_releases(id), source_release_label TEXT,
 source_release_revision INTEGER, source_run_id TEXT REFERENCES skill_eval_runs(id),
 input_snapshot_json TEXT NOT NULL DEFAULT '{}', input_hash TEXT NOT NULL DEFAULT '',
 agent TEXT NOT NULL DEFAULT 'blade-agent' CHECK(agent='blade-agent'),
 requested_provider TEXT NOT NULL DEFAULT '', requested_model TEXT NOT NULL DEFAULT '',
 resolved_provider TEXT, resolved_model TEXT, model_resolution_source TEXT,
 group_key TEXT NOT NULL DEFAULT 'requested/unverified',
 run_status TEXT NOT NULL DEFAULT 'queued' CHECK(run_status IN
 ('queued','preparing','active','waiting_input','waiting_continue','finishing','completed','failed','cancelled')),
 generation_status TEXT NOT NULL DEFAULT 'idle' CHECK(generation_status IN
 ('idle','queued','running','paused','stopping','stopped','completed','failed')),
 capture_status TEXT NOT NULL DEFAULT 'pending', artifact_status TEXT NOT NULL DEFAULT 'pending',
 revision INTEGER NOT NULL DEFAULT 1, finish_requested_at TEXT, finalized_result_ref TEXT,
 heartbeat_at TEXT, created_at TEXT NOT NULL, started_at TEXT, ended_at TEXT,
 error_code TEXT, error_message TEXT, external_refs_json TEXT NOT NULL DEFAULT '{}',
 FOREIGN KEY(experiment_id,skill_id) REFERENCES skill_eval_experiments(id,skill_id),
 FOREIGN KEY(skill_version_id,skill_id) REFERENCES skill_eval_versions(id,skill_id)
);
CREATE TABLE IF NOT EXISTS skill_eval_messages (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES skill_eval_runs(id),
 seq INTEGER NOT NULL CHECK(seq>0), kind TEXT NOT NULL CHECK(kind IN ('initial','followup','answer','context_reuse')),
 text TEXT NOT NULL DEFAULT '', attachment_refs_json TEXT NOT NULL DEFAULT '[]',
 interaction_id TEXT REFERENCES skill_eval_interactions(id), source_message_refs_json TEXT NOT NULL DEFAULT '[]',
 delivery_state TEXT NOT NULL DEFAULT 'pending', remote_receipt_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL, UNIQUE(run_id,seq)
);
CREATE TABLE IF NOT EXISTS skill_eval_interactions (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES skill_eval_runs(id),
 remote_tool_call_id TEXT, question_json TEXT NOT NULL DEFAULT '{}',
 state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','answering','answered','closed')),
 revision INTEGER NOT NULL DEFAULT 1, answer_message_id TEXT REFERENCES skill_eval_messages(id),
 created_at TEXT NOT NULL, UNIQUE(run_id,remote_tool_call_id)
);
CREATE TABLE IF NOT EXISTS skill_eval_generations (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES skill_eval_runs(id),
 generation_index INTEGER NOT NULL, triggering_message_id TEXT REFERENCES skill_eval_messages(id),
 chat_run_id TEXT, status TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT, error TEXT,
 UNIQUE(run_id,generation_index)
);
CREATE TABLE IF NOT EXISTS skill_eval_rerun_plans (
 id TEXT PRIMARY KEY, source_run_id TEXT NOT NULL REFERENCES skill_eval_runs(id),
 through_user_seq INTEGER NOT NULL, mode TEXT NOT NULL CHECK(mode IN ('reuse_all','replace')),
 state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','confirmed')),
 confirmed_at TEXT, confirmed_message_refs_json TEXT NOT NULL DEFAULT '[]',
 confirmed_input_refs_json TEXT NOT NULL DEFAULT '[]', hash TEXT NOT NULL,
 expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS skill_eval_notes (
 run_id TEXT PRIMARY KEY REFERENCES skill_eval_runs(id), text TEXT NOT NULL DEFAULT '',
 revision INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS skill_eval_jobs (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES skill_eval_runs(id),
 operation_id TEXT NOT NULL UNIQUE, phase TEXT NOT NULL, state TEXT NOT NULL,
 owner TEXT, lease_until TEXT, retry_count INTEGER NOT NULL DEFAULT 0,
 next_retry_at TEXT, last_error TEXT
);
CREATE TABLE IF NOT EXISTS skill_eval_requests (
 scope TEXT NOT NULL, operation TEXT NOT NULL, idempotency_key TEXT NOT NULL,
 request_hash TEXT NOT NULL, resource_id TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(scope,operation,idempotency_key)
);
CREATE TABLE IF NOT EXISTS skill_eval_events (
 run_id TEXT NOT NULL REFERENCES skill_eval_runs(id), seq INTEGER NOT NULL CHECK(seq>0),
 event_id TEXT NOT NULL, source TEXT NOT NULL, source_key TEXT,
 type TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}', raw_ref TEXT,
 occurred_at TEXT, observed_at TEXT NOT NULL,
 PRIMARY KEY(run_id,seq), UNIQUE(run_id,event_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS skill_eval_event_source
 ON skill_eval_events(run_id,source,source_key) WHERE source_key IS NOT NULL;
CREATE TABLE IF NOT EXISTS skill_eval_artifacts (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES skill_eval_runs(id),
 relative_path TEXT NOT NULL, hash TEXT REFERENCES skill_eval_blobs(hash), size INTEGER,
 media_type TEXT NOT NULL DEFAULT 'application/octet-stream', state TEXT NOT NULL,
 error TEXT, provenance_json TEXT NOT NULL DEFAULT '{}', UNIQUE(run_id,relative_path)
);
CREATE TABLE IF NOT EXISTS skill_eval_result_collections (
 run_id TEXT NOT NULL REFERENCES skill_eval_runs(id), generation INTEGER NOT NULL,
 result_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(run_id,generation)
);
CREATE TABLE IF NOT EXISTS skill_eval_comparisons (
 id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, baseline_run_id TEXT NOT NULL,
 manifest_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS skill_eval_runs_skill ON skill_eval_runs(skill_id,created_at,id);
CREATE INDEX IF NOT EXISTS skill_eval_runs_experiment ON skill_eval_runs(experiment_id,created_at,id);
CREATE INDEX IF NOT EXISTS skill_eval_runs_group ON skill_eval_runs(group_key,created_at,id);
CREATE INDEX IF NOT EXISTS skill_eval_runs_status ON skill_eval_runs(run_status,created_at,id);
CREATE INDEX IF NOT EXISTS skill_eval_jobs_claim ON skill_eval_jobs(state,next_retry_at,lease_until);
CREATE UNIQUE INDEX IF NOT EXISTS skill_eval_one_remote_writer
 ON skill_eval_jobs(run_id) WHERE state='running';
CREATE UNIQUE INDEX IF NOT EXISTS skill_eval_one_remote_or_recovery_writer
 ON skill_eval_jobs(run_id) WHERE state IN ('running','reconciling');
"""

# Each trigger is one SQL statement. Do not split its internal semicolons.
TRIGGERS = (
    """CREATE TRIGGER IF NOT EXISTS skill_eval_version_immutable
    BEFORE UPDATE ON skill_eval_versions BEGIN SELECT RAISE(ABORT,'immutable snapshot'); END""",
    """CREATE TRIGGER IF NOT EXISTS skill_eval_published_immutable
    BEFORE UPDATE ON skill_eval_releases WHEN OLD.status='published'
    BEGIN SELECT RAISE(ABORT,'immutable published release'); END""",
    """CREATE TRIGGER IF NOT EXISTS skill_eval_experiment_skill_immutable
    BEFORE UPDATE OF skill_id ON skill_eval_experiments WHEN NEW.skill_id!=OLD.skill_id
    BEGIN SELECT RAISE(ABORT,'experiment skill is fixed'); END""",
)


def migrate(conn: sqlite3.Connection) -> None:
    """Idempotent and atomic even when called inside an existing transaction."""
    conn.execute("SAVEPOINT skill_eval_migration")
    try:
        for statement in (ASSET_SCHEMA + RUN_SCHEMA).split(';'):
            if statement.strip():
                conn.execute(statement)
        columns = {row[1] for row in conn.execute('PRAGMA table_info(skill_eval_imports)')}
        if 'committed_result_json' not in columns:
            conn.execute('ALTER TABLE skill_eval_imports ADD COLUMN committed_result_json TEXT')
        additions = {
            'skill_eval_runs': [('stop_requested_at', 'TEXT')],
            'skill_eval_messages': [('answer_json', "TEXT NOT NULL DEFAULT '{}'")],
            'skill_eval_rerun_plans': [('consumed_run_id', 'TEXT REFERENCES skill_eval_runs(id)')],
            'skill_eval_jobs': [('payload_json', "TEXT NOT NULL DEFAULT '{}'"), ('checkpoint_json', "TEXT NOT NULL DEFAULT '{}'")],
        }
        for table, fields in additions.items():
            existing = {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}
            for name, definition in fields:
                if name not in existing:
                    conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
        for statement in TRIGGERS:
            conn.execute(statement)
    except Exception:
        conn.execute("ROLLBACK TO skill_eval_migration")
        conn.execute("RELEASE skill_eval_migration")
        raise
    conn.execute("RELEASE skill_eval_migration")
