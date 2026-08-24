"""Automatic Process and Association evolution batch cycles."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from backend.db import _now_iso, _open_sync
from backend.experiments.hashing import canonical_hash
from backend.insights.jsonutil import parse_json_object
from backend.insights.providers import InsightProvider, InsightProviderError, invoke_generate
from backend.insights.workspace import EvidenceWorkspace

from .cross_layer import PROCESS_SCOPE_KEY
from .loop_models import AssociationEvolutionResult, ProcessEvolutionResult


@dataclass(frozen=True)
class DirectionalCycle:
    process_batches: int = 0
    association_batches: int = 0
    process_result: str | None = None
    association_result: str | None = None


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, default=str)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _parse_json(text: str) -> dict[str, Any]:
    return parse_json_object(text)


def _select_process_records(db_path: Path, threshold: int, overlap_ratio: float):
    with _open_sync(db_path) as conn:
        conn.row_factory = sqlite3.Row
        trigger = conn.execute(
            "SELECT * FROM process_judge_records r WHERE valid_execution=1 "
            "AND NOT EXISTS (SELECT 1 FROM process_evolution_batch_members m "
            "WHERE m.record_id=r.id AND m.role='trigger') "
            "ORDER BY created_at,id LIMIT ?",
            (threshold,),
        ).fetchall()
        if len(trigger) < threshold:
            return [], []
        overlap_count = round(threshold * overlap_ratio)
        latest = conn.execute(
            "SELECT batch_id FROM process_evolution_batch_members "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        overlap = []
        if latest is not None and overlap_count > 0:
            overlap = conn.execute(
                "SELECT r.* FROM process_evolution_batch_members m "
                "JOIN process_judge_records r ON r.id=m.record_id "
                "WHERE m.batch_id=? ORDER BY m.created_at DESC,r.id LIMIT ?",
                (latest[0], overlap_count),
            ).fetchall()
    return [dict(row) for row in trigger], [dict(row) for row in overlap]


def _select_association_cases(db_path: Path, threshold: int, overlap_ratio: float):
    with _open_sync(db_path) as conn:
        conn.row_factory = sqlite3.Row
        trigger = conn.execute(
            "SELECT * FROM association_cases c WHERE c.valid_evidence=1 AND NOT EXISTS ("
            "SELECT 1 FROM association_evolution_batch_members m "
            "WHERE m.case_id=c.id AND m.role='trigger') "
            "ORDER BY created_at,id LIMIT ?",
            (threshold,),
        ).fetchall()
        if len(trigger) < threshold:
            return [], []
        overlap_count = round(threshold * overlap_ratio)
        latest = conn.execute(
            "SELECT batch_id FROM association_evolution_batch_members "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        overlap = []
        if latest is not None and overlap_count > 0:
            overlap = conn.execute(
                "SELECT c.* FROM association_evolution_batch_members m "
                "JOIN association_cases c ON c.id=m.case_id "
                "WHERE m.batch_id=? ORDER BY m.created_at DESC,c.id LIMIT ?",
                (latest[0], overlap_count),
            ).fetchall()
    return [dict(row) for row in trigger], [dict(row) for row in overlap]


def _process_prompt(
    contract: dict[str, Any],
    records: list[dict[str, Any]],
    catalog: dict[str, Any] | None = None,
) -> str:
    record_ids = [row["id"] for row in records]
    example = {
        "schema_version": "octagon-process-evolution-result-v1",
        "result": "no_change",
        "summary": "当前诊断契约已覆盖这批 Process Record，无需改尺子。",
        "evidence_record_ids": record_ids,
        "limitations": ["样本只有一个环境"],
        "candidate": None,
    }
    return (
        "你是 Process Rubric Evolution 分析器，不是 Product Judge，也不是 Process Judge。"
        "你评价的是 Runtrace 诊断契约够不够，不是给候选打分。不得修改 Product 分数或 Reward。\n"
        "先用 list_evidence_files / read_evidence_file 读工作区，不要在最终 JSON 里写 thinking 或 tool。"
        "最终根对象必须恰好是 schema_version,result,summary,evidence_record_ids,limitations,candidate。"
        "no_change 和 insufficient_evidence 都是正常下班。只有稳定缺口才 candidate_generated。\n"
        f"形状参考：{json.dumps(example, ensure_ascii=False)}\n"
        f"Current contract version/purpose: {json.dumps({k: contract.get(k) for k in ('schema_version','purpose','scope_key')}, ensure_ascii=False)}\n"
        f"Record ids: {json.dumps(record_ids, ensure_ascii=False)}\n"
        f"Evidence catalog: {json.dumps(catalog or {}, ensure_ascii=False)}"
    )


def _association_prompt(
    cases: list[dict[str, Any]],
    catalog: dict[str, Any] | None = None,
) -> str:
    case_ids = [row["id"] for row in cases]
    example = {
        "schema_version": "octagon-association-evolution-result-v1",
        "result": "insufficient_evidence",
        "summary": "两例都是满分且任务过短，无法建立可证伪关联。",
        "hypotheses": [],
        "limitations": ["n=2", "同一任务"],
    }
    return (
        "你是 Association Evolution 分析器。寻找过程发现与产品结果之间可证伪的相关，"
        "不得写成因果，不得提出 Reward，不得改官方分。\n"
        "先读工作区文件，不要在最终 JSON 里写 thinking 或 tool。"
        "最终根对象必须恰好是 schema_version,result,summary,hypotheses,limitations。"
        "no_change 和 insufficient_evidence 都是正常下班。causal_status 若出现必须是 correlational。\n"
        f"形状参考：{json.dumps(example, ensure_ascii=False)}\n"
        f"Case ids: {json.dumps(case_ids, ensure_ascii=False)}\n"
        f"Evidence catalog: {json.dumps(catalog or {}, ensure_ascii=False)}"
    )


def _active_process_contract(db_path: Path) -> tuple[str, dict[str, Any]]:
    with _open_sync(db_path) as conn:
        row = conn.execute(
            "SELECT ac.version,cv.contract_json FROM evolution_active_contracts ac "
            "JOIN evolution_contract_versions cv ON cv.id=ac.contract_id "
            "WHERE ac.evolution_domain='process' AND ac.scope_key=?",
            (PROCESS_SCOPE_KEY,),
        ).fetchone()
    if row is None:
        raise ValueError("active Process Rubric is not registered")
    return str(row[0]), json.loads(row[1])


def _persist_members(db_path: Path, table: str, key: str, batch_id: str,
                     trigger: list[dict[str, Any]], overlap: list[dict[str, Any]]) -> None:
    now = _now_iso()
    with _open_sync(db_path) as conn:
        for role, rows in (("trigger", trigger), ("overlap", overlap)):
            for row in rows:
                conn.execute(
                    f"INSERT OR IGNORE INTO {table}({key},batch_id,role,created_at) "
                    "VALUES(?,?,?,?)",
                    (row["id"], batch_id, role, now),
                )
        conn.commit()


def publish_process_candidate_sync(
    *, db_path: Path, artifact_dir: Path, actor: str
) -> str:
    """Human-gated publication of one Process Rubric candidate."""
    if not actor.strip():
        raise ValueError("publication actor is required")
    validation = json.loads(
        (artifact_dir / "validation.json").read_text(encoding="utf-8")
    )
    if validation.get("decision") != "eligible_for_human":
        raise ValueError("Process candidate did not pass automatic validation")
    result = ProcessEvolutionResult.model_validate_json(
        (artifact_dir / "evolution-result.json").read_text(encoding="utf-8")
    )
    if result.candidate is None:
        raise ValueError("Process evolution result has no candidate")
    candidate = result.candidate
    payload = candidate.model_dump(mode="json")
    contract_hash = canonical_hash(payload)
    contract_id = "contract_" + contract_hash.split(":")[-1][:24]
    now = _now_iso()
    with _open_sync(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        active = conn.execute(
            "SELECT version FROM evolution_active_contracts "
            "WHERE evolution_domain='process' AND scope_key=?",
            (candidate.scope_key,),
        ).fetchone()
        if active is None or str(active[0]) != candidate.parent_version:
            raise ValueError("Process candidate parent is stale")
        conn.execute(
            "INSERT OR IGNORE INTO evolution_contract_versions("
            "id,evolution_domain,scope_key,version,parent_version,status,contract_json,"
            "contract_hash,source_batch_id,created_at,published_at,published_by) "
            "VALUES(?,'process',?,?,?,'published',?,?,?,?,?,?)",
            (
                contract_id, candidate.scope_key, candidate.proposed_version,
                candidate.parent_version,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                contract_hash, artifact_dir.name, now, now, actor.strip(),
            ),
        )
        conn.execute(
            "UPDATE evolution_active_contracts SET version=?,contract_id=?,updated_at=? "
            "WHERE evolution_domain='process' AND scope_key=?",
            (candidate.proposed_version, contract_id, now, candidate.scope_key),
        )
        conn.commit()
    return candidate.proposed_version


def accept_association_hypotheses_sync(
    *, db_path: Path, artifact_dir: Path, actor: str
) -> str:
    """Human acceptance records a research hypothesis; it never creates Reward."""
    if not actor.strip():
        raise ValueError("acceptance actor is required")
    validation = json.loads(
        (artifact_dir / "validation.json").read_text(encoding="utf-8")
    )
    if validation.get("decision") != "eligible_for_human":
        raise ValueError("Association hypotheses did not pass automatic validation")
    result = AssociationEvolutionResult.model_validate_json(
        (artifact_dir / "evolution-result.json").read_text(encoding="utf-8")
    )
    if not result.hypotheses:
        raise ValueError("Association evolution result has no hypotheses")
    payload = result.model_dump(mode="json")
    hypothesis_hash = canonical_hash(payload)
    version = "association:" + hypothesis_hash.split(":")[-1][:16]
    with _open_sync(db_path) as conn:
        cursor = conn.execute(
            "UPDATE association_hypotheses SET status='accepted',published_at=?,"
            "published_by=? WHERE version=? AND status='awaiting_human'",
            (_now_iso(), actor.strip(), version),
        )
        if cursor.rowcount != 1:
            raise ValueError("Association hypothesis is absent or already decided")
        conn.commit()
    return version


async def run_directional_cycle(
    *, db_path: Path, data_path: Path, provider: InsightProvider,
    process_threshold: int = 10, association_threshold: int = 100,
    overlap_ratio: float = 0.25,
) -> DirectionalCycle:
    process_batches = association_batches = 0
    process_result = association_result = None

    process_trigger, process_overlap = _select_process_records(
        db_path, process_threshold, overlap_ratio
    )
    if process_trigger:
        version, contract = _active_process_contract(db_path)
        batch_payload = {
            "domain": "process", "scope_key": PROCESS_SCOPE_KEY,
            "parent_version": version,
            "trigger_ids": [row["id"] for row in process_trigger],
            "overlap_ids": [row["id"] for row in process_overlap],
        }
        batch_id = "peb_" + canonical_hash(batch_payload).split(":")[-1][:24]
        root = data_path / "process-evolution" / "batches" / batch_id
        _atomic_json(root / "batch.json", batch_payload)
        compact_records = [{
            "record_id": row["id"],
            "env_name": row["env_name"],
            "claims": json.loads(row["claims_json"]),
            "accepted_claim_ids": json.loads(row["accepted_claim_ids_json"]),
            "rejected_claims": json.loads(row["rejected_claims_json"]),
            "metadata": json.loads(row["metadata_json"]),
        } for row in process_trigger + process_overlap]
        _atomic_json(root / "evidence" / "process-records.json", compact_records)
        _atomic_json(root / "evidence" / "current-contract.json", contract)
        workspace = EvidenceWorkspace(
            {
                "batch.json": root / "batch.json",
                "evidence/current-contract.json": root / "evidence" / "current-contract.json",
                "evidence/process-records.json": root / "evidence" / "process-records.json",
            }
        )
        try:
            generated = await invoke_generate(
                provider,
                _process_prompt(
                    contract,
                    process_trigger + process_overlap,
                    catalog={"files": workspace.catalog()},
                ),
                workspace=workspace,
            )
            _atomic_json(root / "raw-provider-output.json", {
                "provider": generated.provider, "model": generated.model,
                "text": generated.text, "received_at": _now_iso(),
            })
            result = ProcessEvolutionResult.model_validate(_parse_json(generated.text))
            allowed = set(batch_payload["trigger_ids"] + batch_payload["overlap_ids"])
            if not set(result.evidence_record_ids).issubset(allowed):
                raise ValueError("Process result cited records outside the frozen batch")
            if result.candidate is not None:
                if result.candidate.parent_version != version:
                    raise ValueError("Process candidate parent_version is stale")
                if result.candidate.scope_key != PROCESS_SCOPE_KEY:
                    raise ValueError("Process candidate scope_key is invalid")
            _atomic_json(root / "evolution-result.json", result.model_dump(mode="json"))
            _atomic_json(root / "validation.json", {
                "schema_version": "octagon-process-contract-replay-v1",
                "domain": "process",
                "batch_id": batch_id,
                "frozen_record_ids": sorted(allowed),
                "record_count": len(process_trigger + process_overlap),
                "trigger_threshold": process_threshold,
                "mandatory_invariants_validated": True,
                "decision": "eligible_for_human",
                "note": (
                    "Pydantic already rejected invalid ProcessEvolutionResult combinations. "
                    "This file records structural eligibility only; semantic frozen-Trace "
                    "replay remains required before production rollout."
                ),
                "validated_at": _now_iso(),
            })
            _persist_members(
                db_path, "process_evolution_batch_members", "record_id", batch_id,
                process_trigger, process_overlap,
            )
            process_result = result.result
        except (InsightProviderError, json.JSONDecodeError, ValidationError, ValueError) as exc:
            _atomic_json(root / "evolution-failure.json", {
                "domain": "process", "batch_id": batch_id,
                "error": str(exc), "failed_at": _now_iso(),
            })
            process_result = "failed"
        process_batches = 1 if process_result != "failed" else 0

    association_trigger, association_overlap = _select_association_cases(
        db_path, association_threshold, overlap_ratio
    )
    if association_trigger:
        batch_payload = {
            "domain": "association",
            "trigger_ids": [row["id"] for row in association_trigger],
            "overlap_ids": [row["id"] for row in association_overlap],
        }
        batch_id = "aeb_" + canonical_hash(batch_payload).split(":")[-1][:24]
        root = data_path / "association-evolution" / "batches" / batch_id
        _atomic_json(root / "batch.json", batch_payload)
        compact_cases = [{
            "case_id": row["id"],
            "env_name": row["env_name"],
            "product_outcome": json.loads(row["product_outcome_json"]),
            "process_findings": json.loads(row["process_findings_json"]),
            "controls": json.loads(row["controls_json"]),
        } for row in association_trigger + association_overlap]
        _atomic_json(root / "evidence" / "association-cases.json", compact_cases)
        workspace = EvidenceWorkspace(
            {
                "batch.json": root / "batch.json",
                "evidence/association-cases.json": root / "evidence" / "association-cases.json",
            }
        )
        try:
            generated = await invoke_generate(
                provider,
                _association_prompt(
                    association_trigger + association_overlap,
                    catalog={"files": workspace.catalog()},
                ),
                workspace=workspace,
            )
            _atomic_json(root / "raw-provider-output.json", {
                "provider": generated.provider, "model": generated.model,
                "text": generated.text, "received_at": _now_iso(),
            })
            result = AssociationEvolutionResult.model_validate(_parse_json(generated.text))
            allowed = set(batch_payload["trigger_ids"] + batch_payload["overlap_ids"])
            for hypothesis in result.hypotheses:
                refs = set(hypothesis.supporting_case_ids + hypothesis.contradicting_case_ids)
                if not refs.issubset(allowed):
                    raise ValueError("Association hypothesis cited Cases outside the batch")
            payload = result.model_dump(mode="json")
            _atomic_json(root / "evolution-result.json", payload)
            _atomic_json(root / "validation.json", {
                "schema_version": "octagon-association-batch-validation-v1",
                "domain": "association",
                "batch_id": batch_id,
                "frozen_case_ids": sorted(allowed),
                "case_count": len(association_trigger + association_overlap),
                "trigger_threshold": association_threshold,
                "all_hypotheses_correlational": True,
                "all_hypotheses_falsifiable": True,
                "decision": "eligible_for_human",
                "note": (
                    "causal_status and falsification_test are enforced by AssociationHypothesisSpec. "
                    "Acceptance records a research hypothesis only; it never publishes Reward."
                ),
                "validated_at": _now_iso(),
            })
            if result.hypotheses:
                hypothesis_hash = canonical_hash(payload)
                version = "association:" + hypothesis_hash.split(":")[-1][:16]
                with _open_sync(db_path) as conn:
                    conn.execute(
                        "INSERT OR IGNORE INTO association_hypotheses("
                        "id,version,parent_version,status,hypothesis_json,hypothesis_hash,"
                        "source_batch_id,created_at) VALUES(?,?,?,'awaiting_human',?,?,?,?)",
                        (
                            "ahyp_" + hypothesis_hash.split(":")[-1][:24], version,
                            "bootstrap", json.dumps(payload, ensure_ascii=False, sort_keys=True),
                            hypothesis_hash, batch_id, _now_iso(),
                        ),
                    )
                    conn.commit()
            _persist_members(
                db_path, "association_evolution_batch_members", "case_id", batch_id,
                association_trigger, association_overlap,
            )
            association_result = result.result
        except (InsightProviderError, json.JSONDecodeError, ValidationError, ValueError) as exc:
            _atomic_json(root / "evolution-failure.json", {
                "domain": "association", "batch_id": batch_id,
                "error": str(exc), "failed_at": _now_iso(),
            })
            association_result = "failed"
        association_batches = 1 if association_result != "failed" else 0

    return DirectionalCycle(
        process_batches=process_batches,
        association_batches=association_batches,
        process_result=process_result,
        association_result=association_result,
    )
