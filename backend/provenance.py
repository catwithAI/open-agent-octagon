"""attempt_provenance —— 数据治理版本锚(从新 run 开始记录)。

每个新 attempt 一条 provenance 行,记录 4 个版本锚:

- **env 锚**:env 目录内容 hash(meta+core+scorer+tasks+private,判分契约)
- **task 锚**:canonical task JSON 的 sha256
- **agent 锚**:agent 名 + host CLI 版本 + 规范化模型 id
- **judge 锚**:judge 实际用的模型 + judge prompt 版本 + rubric hash/version

外加既有锚引用:`input_snapshot_ref`(attempt_input_snapshots.content_hash,
实际喂给 agent 的冻结输入)与 `manifest_ref`(scores.evaluation_manifest_ref,
评分契约)。

写入分两阶段,均幂等、缺锚留 NULL 不报错:

1. **attempt 创建时**(runner._create_attempt_sync):env/task/agent/model/input 锚,
   `provenance_complete=0`。
2. **评分完成时**(scoring_queue):judge 锚 + manifest_ref + agent_cli_version
   (从 attempts.external_refs_json 回读)+ rubric 锚,置 `provenance_complete=1`。

旧 attempts 没有 provenance 行 = 治理前数据,读时兼容。查询侧要进统计时
显式过滤 `provenance_complete=1`(或按需注明"未冻结版本锚")。

已知局限:5 个 env import 主仓库 `backend.*`,env_dir_hash 不覆盖主仓库变化;
judge 锚只能从 scoring-work 里现存的 judge_result.json 回读,被清理的记 NULL。
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any

from .db import _now_iso, _open_sync
from .experiments.hashing import canonical_hash, hash_bytes

_logger = logging.getLogger(__name__)

#: provenance 行的 schema 版本(表结构演进时递增,数据迁移由读写端各自兼容)。
PROVENANCE_SCHEMA_VERSION = "octagon-attempt-provenance-v1"

#: env 目录内容 hash 的 schema 版本(纳入 hash,避免文件集规则变化导致歧义)。
_ENV_DIR_HASH_VERSION = "octagon-env-dir-v1"


# ---------- env / task 内容锚 ------------------------------------------------


def _gather_env_contract(env_dir: Path) -> list[Path]:
    """收集 env 目录里「参与判分」的文件。

    判分契约 = meta(scorer 读 dims/weights/pass_threshold)+ core + scorer
    + judge 脚本 + schema + 任务定义(tasks/) + 评分依据(private/)+ 工具面
    (mcp_server / blade_skill)。**排除** inputs/ materials/(体积大、agent 可见,
    实际输入已由输入快照冻结)与 README/provenance/build_tasks 等说明性文件。
    """
    files: list[Path] = []
    for name in (
        "meta.yaml",
        "core.py",
        "scorer.py",
        "judge_local.py",
        "schema.sql",
        "mcp_server.py",
    ):
        p = env_dir / name
        if p.is_file():
            files.append(p)
    for sub in ("tasks", "private", "blade_skill"):
        d = env_dir / sub
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*")):
            if not p.is_file() or p.suffix == ".pyc" or "__pycache__" in p.parts:
                continue
            if sub == "tasks" and p.suffix != ".json":
                continue
            files.append(p)
    return files


def hash_env_dir(env_dir: Path | str) -> str | None:
    """env 目录判分契约的内容 hash(同目录两次一致,改任一判分文件即变)。

    返回 `sha256:<hex>`;目录不存在或无判分文件时返回 None(不报错)。
    """
    env_dir = Path(env_dir)
    if not env_dir.is_dir():
        return None
    manifest: dict[str, str] = {}
    for p in _gather_env_contract(env_dir):
        try:
            manifest[p.relative_to(env_dir).as_posix()] = hash_bytes(
                p.read_bytes()
            ).removeprefix("sha256:")
        except OSError:
            continue
    if not manifest:
        return None
    return canonical_hash({"schema_version": _ENV_DIR_HASH_VERSION, "files": manifest})


def hash_task(task: dict[str, Any]) -> str:
    """canonical task JSON 的 sha256。task 为 {id, env_name, prompt, context,
    constraints, timeout_seconds}。只 hash 内容相关字段,不含 created_at 等
    元数据,避免同名任务重导入导致 hash 漂移。"""
    return canonical_hash(
        {
            "id": task.get("id"),
            "env_name": task.get("env_name"),
            "prompt": task.get("prompt"),
            "context": task.get("context"),
            "constraints": task.get("constraints"),
            "timeout_seconds": task.get("timeout_seconds"),
        }
    )


# ---------- judge 锚(scoring-work 回读)--------------------------------------


def read_judge_anchors(scoring_data_path: Path | str, job_id: str) -> dict[str, str | None]:
    """从评分工作区回读 LLM judge 的 model / prompt_version。

    judge 产物写在各 env 的评分期副本 `scoring-work/<job_id>/.../judge_result.json`。
    一个 job 可能跑多个 judge(如 product/process),取并集,`|` 连接,去重排序。
    找不到任何 judge_result.json 时返回 {judge_model: None, judge_prompt_version: None}。
    """
    job_dir = Path(scoring_data_path) / "scoring-work" / job_id
    models: set[str] = set()
    prompts: set[str] = set()
    if job_dir.is_dir():
        for p in job_dir.rglob("judge_result.json"):
            try:
                data = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            if data.get("model"):
                models.add(str(data["model"]))
            if data.get("prompt_version"):
                prompts.add(str(data["prompt_version"]))
    return {
        "judge_model": "|".join(sorted(models)) or None,
        "judge_prompt_version": "|".join(sorted(prompts)) or None,
    }


# ---------- 写入 ------------------------------------------------------------


#: 允许写入的列白名单(锚字段)。unknown key 直接忽略,防止拼 SQL 注入。
_ALLOWED_COLUMNS = frozenset(
    {
        "env_dir_hash",
        "task_id",
        "task_content_hash",
        "agent_name",
        "agent_cli_version",
        "model_canonical",
        "judge_model",
        "judge_prompt_version",
        "rubric_hash",
        "rubric_version",
        "input_snapshot_ref",
        "manifest_ref",
        "provenance_complete",
    }
)


def write_attempt_provenance(
    db_path: Path | str, *, attempt_id: str, **anchors: Any
) -> None:
    """INSERT ... ON CONFLICT DO UPDATE,幂等。

    只写入非 None 的锚字段(None 直接跳过,避免评分阶段的空值覆盖创建阶段的
    已有值);`provenance_complete=0` 是合法写入(显式 int)。缺锚不报错。
    """
    values = {k: v for k, v in anchors.items() if k in _ALLOWED_COLUMNS and v is not None}
    if not values and "provenance_complete" not in anchors:
        return
    if "provenance_complete" in anchors and anchors["provenance_complete"] is not None:
        values["provenance_complete"] = int(anchors["provenance_complete"])
    now = _now_iso()
    columns = sorted(values)
    with _open_sync(db_path) as conn:
        existing = conn.execute(
            "SELECT created_at FROM attempt_provenance WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
        created_at = existing[0] if existing else now
        col_sql = ", ".join(columns)
        placeholders = ", ".join("?" for _ in columns)
        updates = ", ".join(f"{c}=excluded.{c}" for c in columns)
        conn.execute(
            f"INSERT INTO attempt_provenance("
            f" attempt_id, schema_version, created_at, updated_at, {col_sql}"
            f") VALUES(?, ?, ?, ?, {placeholders})"
            f" ON CONFLICT(attempt_id) DO UPDATE SET"
            f" {updates}, updated_at=excluded.updated_at",
            (attempt_id, PROVENANCE_SCHEMA_VERSION, created_at, now, *[values[c] for c in columns]),
        )
        conn.commit()


def record_attempt_creation(
    db_path: Path | str,
    *,
    attempt_id: str,
    env_name: str,
    task: dict[str, Any],
    agent_name: str,
    model: str | None,
    input_snapshot_ref: str | None,
    env_dir: Path | str | None = None,
    providers: dict[str, Any] | None = None,
) -> None:
    """attempt 创建阶段写锚(env/task/agent/model/input)。

    env_dir 未显式传入时尝试从 runtime_state 解析(backend 进程内已加载的 env)。
    model 规范化是纯增量:只写 model_canonical,不改 runs/attempts 原串。
    """
    anchors: dict[str, Any] = {
        "task_id": task.get("id"),
        "task_content_hash": hash_task(task),
        "agent_name": agent_name,
        "input_snapshot_ref": input_snapshot_ref,
    }
    if model:
        anchors["model_canonical"] = canonical_model(model, providers or _providers_from_runtime())
    if env_dir is None:
        env_dir = _env_dir_from_runtime(env_name)
    if env_dir is not None:
        anchors["env_dir_hash"] = hash_env_dir(env_dir)
    write_attempt_provenance(db_path, attempt_id=attempt_id, **anchors)


def finalize_attempt_provenance(
    db_path: Path | str,
    *,
    attempt_id: str,
    data_path: Path | str | None = None,
    job_id: str | None = None,
    manifest_ref: str | None = None,
) -> None:
    """评分完成阶段补全 judge 锚 + manifest_ref + agent_cli_version,置 complete。

    只更新这次调用带上的字段;没有 judge_result.json 时 judge 锚留 NULL,
    不覆盖已有值。agent_cli_version 从 attempts.external_refs_json 回读
    (adapter 在 external_refs 里记 `cli_version`,随最终态一起落库)。
    """
    anchors: dict[str, Any] = {}
    if data_path is not None and job_id is not None:
        anchors.update(read_judge_anchors(data_path, job_id))
    with _open_sync(db_path) as conn:
        row = conn.execute(
            "SELECT external_refs_json, rubric_version FROM attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()
        if not manifest_ref:
            ref_row = conn.execute(
                "SELECT evaluation_manifest_ref FROM scores WHERE attempt_id=? "
                "AND evaluation_manifest_ref IS NOT NULL LIMIT 1",
                (attempt_id,),
            ).fetchone()
            if ref_row is not None:
                manifest_ref = ref_row[0]
    if manifest_ref:
        anchors["manifest_ref"] = manifest_ref
    if row is not None:
        try:
            refs = json.loads(row[0] or "{}")
        except json.JSONDecodeError:
            refs = {}
        if refs.get("cli_version"):
            anchors["agent_cli_version"] = str(refs["cli_version"])
        if row[1] and not anchors.get("rubric_version"):
            anchors["rubric_version"] = str(row[1])
            anchors["rubric_hash"] = _rubric_hash_for_version(db_path, str(row[1]))
    anchors["provenance_complete"] = 1
    write_attempt_provenance(db_path, attempt_id=attempt_id, **anchors)
    # append-only judge 运行历史：provenance(1:1) 之外再留一条完整执行记录
    # （judge 锚 + 总分 + 各维度快照）。best-effort——judge 历史写失败不影响
    # 已提交的 provenance 与分数。
    try:
        record_judge_run(db_path, attempt_id=attempt_id, data_path=data_path, job_id=job_id)
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "attempt_judge_runs 写入失败(不影响评分) attempt=%s: %s", attempt_id, exc,
        )


def record_judge_run(
    db_path: Path | str,
    *,
    attempt_id: str,
    data_path: Path | str | None = None,
    job_id: str | None = None,
) -> str:
    """append 一行 judge 运行历史，revision 自动递增（1,2,3…）。

    从 DB 读 score_total / 各维度分快照 / manifest_ref / rubric，从 scoring-work
    读 judge 模型与 prompt 版本。重复调用会 append 下一 revision，不覆盖历史
    （UNIQUE(attempt_id, score_revision) 兜底）。返回新行 id `jgr_<hex>`。
    """
    judge_anchors: dict[str, str | None] = {}
    if data_path is not None and job_id is not None:
        judge_anchors = read_judge_anchors(data_path, job_id)
    run_id = f"jgr_{uuid.uuid4().hex[:12]}"
    now = _now_iso()
    with _open_sync(db_path) as conn:
        attempt = conn.execute(
            "SELECT score_total, rubric_version FROM attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()
        score_total = int(attempt[0]) if attempt and attempt[0] is not None else 0
        rubric_version = attempt[1] if attempt else None
        manifest_ref_row = conn.execute(
            "SELECT evaluation_manifest_ref FROM scores WHERE attempt_id=? "
            "AND evaluation_manifest_ref IS NOT NULL LIMIT 1",
            (attempt_id,),
        ).fetchone()
        manifest_ref = manifest_ref_row[0] if manifest_ref_row else None
        dims = conn.execute(
            "SELECT dimension, value, detail FROM scores WHERE attempt_id=? ORDER BY dimension",
            (attempt_id,),
        ).fetchall()
        rubric_hash: str | None = None
        if rubric_version:
            rh = conn.execute(
                "SELECT rubric_hash FROM rubric_versions WHERE version=? LIMIT 1",
                (rubric_version,),
            ).fetchone()
            rubric_hash = rh[0] if rh else None
        # judge_runs 的 revision 对齐 outbox 最新 revision：每次评分 commit 先写
        # outbox、judge_runs 紧跟，两者一一对应（正常路径 MAX(outbox)==
        # MAX(judge_runs)）。同步评分路径（defer_scoring=False）的 attempt 有
        # outbox rev1 却无 judge_runs 行：若只用 MAX(attempt_judge_runs)+1 会写
        # rev1，与 outbox rev2 错位，历史 revision 全线偏移（审查 #4）。
        # max(judge_runs+1, outbox)：无 outbox 行时（standalone/旧数据）仍递增。
        judge_rev = conn.execute(
            "SELECT COALESCE(MAX(score_revision),0) FROM attempt_judge_runs "
            "WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()[0]
        outbox_rev = conn.execute(
            "SELECT COALESCE(MAX(score_revision),0) FROM score_transition_outbox "
            "WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()[0]
        next_rev = max(int(judge_rev) + 1, int(outbox_rev))
        dimensions = [
            {"dimension": d[0], "value": d[1], "detail": d[2]} for d in dims
        ]
        conn.execute(
            "INSERT INTO attempt_judge_runs("
            " id, attempt_id, score_revision, score_total, status, judge_model,"
            " judge_prompt_version, rubric_hash, rubric_version, manifest_ref,"
            " scoring_job_id, dimensions_json, created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                attempt_id,
                next_rev,
                score_total,
                "completed",
                judge_anchors.get("judge_model"),
                judge_anchors.get("judge_prompt_version"),
                rubric_hash,
                rubric_version,
                manifest_ref,
                job_id,
                json.dumps(dimensions, ensure_ascii=False),
                now,
            ),
        )
        conn.commit()
    return run_id


def _rubric_hash_for_version(db_path: Path | str, version: str) -> str | None:
    try:
        with _open_sync(db_path) as conn:
            row = conn.execute(
                "SELECT rubric_hash FROM rubric_versions WHERE version=? LIMIT 1",
                (version,),
            ).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def _env_dir_from_runtime(env_name: str) -> Path | None:
    try:
        from .runtime_state import get as _get_runtime

        env = _get_runtime().envs.get(env_name)
        return env.env_dir if env is not None else None
    except Exception:
        return None


def _providers_from_runtime() -> dict[str, Any] | None:
    """从 runtime_state.settings 拿配置过的 provider 前缀(供 canonical_model 剥前缀)。

    拿不到时返回 None,canonical_model 退化为只剥传输别名(upstream/ 等)。
    """
    try:
        from .runtime_state import get as _get_runtime

        settings = _get_runtime().settings
        if settings is None:
            return None
        providers = getattr(settings, "model_providers", None)
        if providers is None and hasattr(settings, "model_providers"):
            providers = settings.model_providers
        return providers if isinstance(providers, dict) else None
    except Exception:
        return None


# 延迟 import:model_providers 有 pydantic 依赖,只在确实需要时加载。
def canonical_model(raw: str | None, providers: dict[str, Any] | None = None) -> str | None:
    from .model_providers import canonical_model as _canonical

    return _canonical(raw, providers)
