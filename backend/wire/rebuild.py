"""离线重建入口。

    python -m backend.wire.rebuild <attempt_id> [--data-path ...]

从原始 events（events.jsonl 等）重跑 native normalizer → 重写 native-event
spool → finalize，产出调用级 canonical `llm_call`。重复执行幂等（evidence ID
由 raw ref 派生），且每次成功 finalize 使 manifest generation 递增。

不触碰原始 events；先写 `.rebuild` 校验、通过后由 finalize 的原子写替换。
需要知道 attempt 的 agent 与 policy——从 DB attempts 行读取（agent_name），
policy 从既有 manifest 复用或回退默认 metadata。
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from pathlib import Path

from backend.wire import finalize, paths
from backend.wire.normalizers.runner import normalizer_for, run_native_normalizer
from backend.wire.policy import EffectivePolicy, resolve_effective_policy

logger = logging.getLogger(__name__)


def _agent_of(db_path: Path, attempt_id: str) -> str | None:
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT agent_name FROM attempts WHERE id=?", (attempt_id,)
            ).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def _adapter_usage_of(db_path: Path, attempt_id: str) -> dict | None:
    """历史 attempt 的 adapter token_usage_json（对账用）。"""
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT token_usage_json FROM attempts WHERE id=?", (attempt_id,)
            ).fetchone()
        if row and row[0]:
            data = json.loads(row[0])
            return data if isinstance(data, dict) and data else None
    except (sqlite3.Error, json.JSONDecodeError):
        return None
    return None


def _policy_of(data_path: Path, attempt_id: str) -> EffectivePolicy:
    try:
        manifest = json.loads(
            paths.manifest_file(data_path, attempt_id).read_text(encoding="utf-8")
        )
        p = manifest.get("policy") or {}
        return EffectivePolicy(
            requested=p.get("requested", "metadata"),
            effective=p.get("effective", "metadata"),
            downgrade_reason=p.get("downgrade_reason"),
        )
    except (OSError, json.JSONDecodeError, KeyError):
        return resolve_effective_policy(task_requested="metadata")


def rebuild_attempt(
    *,
    data_path: Path,
    attempt_id: str,
    agent_name: str | None = None,
    db_path: Path | None = None,
) -> dict:
    """重建单 attempt 的 canonical wire。返回新 manifest。

    agent_name 未传时从 DB 推断；无对应 native normalizer 时抛错（不静默）。
    """
    data_path = Path(data_path)
    if agent_name is None and db_path is not None:
        agent_name = _agent_of(db_path, attempt_id)
    if agent_name is None or normalizer_for(agent_name) is None:
        raise ValueError(
            f"attempt {attempt_id} 无可用 native normalizer（agent={agent_name}）"
        )
    # 历史 attempt 的 adapter 累计 usage 带入对账：在线 finalize 与
    # offline rebuild 必须得到相同的 reconciliation conflict。
    adapter_usage = _adapter_usage_of(db_path, attempt_id) if db_path else None
    produced = run_native_normalizer(
        agent_name=agent_name, attempt_id=attempt_id, data_path=data_path,
        adapter_usage=adapter_usage,
    )
    if not produced:
        raise ValueError(f"native normalize 未产出 source: {attempt_id}")
    policy = _policy_of(data_path, attempt_id)
    manifest = finalize.finalize_attempt(
        data_path=data_path,
        attempt_id=attempt_id,
        policy=policy,
        declared_sources=[{"kind": "native-event", "instance": "native-event"}],
    )
    if db_path is not None:
        finalize.update_db_summary(db_path, attempt_id, manifest)
        try:
            from backend.wire.aggregate import backfill_token_usage

            backfill_token_usage(db_path, data_path, attempt_id)
        except Exception:
            logger.exception("token 聚合回填失败 attempt=%s", attempt_id)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.wire.rebuild")
    parser.add_argument("attempt_id")
    parser.add_argument("--data-path", default="./data")
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--agent", default=None)
    args = parser.parse_args(argv)

    data_path = Path(args.data_path)
    db_path = Path(args.db_path) if args.db_path else data_path / "octagon.db"
    manifest = rebuild_attempt(
        data_path=data_path,
        attempt_id=args.attempt_id,
        agent_name=args.agent,
        db_path=db_path if db_path.exists() else None,
    )
    print(
        json.dumps(
            {
                "attempt_id": args.attempt_id,
                "status": manifest["status"],
                "generation": manifest["generation"],
                "logical_calls": manifest["totals"]["logical_calls"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
