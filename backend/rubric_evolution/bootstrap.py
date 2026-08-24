"""Automatic bootstrap of v1 Rubrics from environment-owned Judge contracts."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from backend.db import _open_sync
from backend.experiments.hashing import canonical_hash

from .adapters import normalize_existing_rubric
from .store import register_active_rubric_sync

logger = logging.getLogger(__name__)

_CONVENTIONAL_PATHS = (
    "private/product_judge_rubric.json",
    "private/ui_judge_rubric.json",
    "private/official_rubric.json",
    "private/authoring_rubric.json",
    "task_judge_profile.json",
    "inputs/rubric.json",
)


@dataclass(frozen=True)
class BootstrapResult:
    env_name: str
    status: str
    version: str | None = None
    source_path: str | None = None
    detail: str | None = None


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _candidate_paths(env: Any) -> Iterable[Path]:
    root = Path(env.env_dir).resolve()
    seen: set[Path] = set()

    # The scorer module is the strongest authority: these are the files actually
    # passed to the environment Judge rather than files merely present on disk.
    scorer = getattr(env, "scorer_module", None)
    if scorer is not None:
        for name, value in sorted(vars(scorer).items()):
            if "RUBRIC" not in name.upper() or not isinstance(value, (str, Path)):
                continue
            path = Path(value)
            if not path.is_absolute():
                path = root / path
            path = path.resolve()
            if path.suffix.lower() == ".json" and _inside(path, root) and path not in seen:
                seen.add(path)
                yield path

    # Task context often declares the official hidden Rubric explicitly.
    for task in getattr(env, "tasks", []) or []:
        raw = getattr(task, "raw", {}) or {}
        context = raw.get("context") if isinstance(raw, dict) else None
        if not isinstance(context, dict):
            context = getattr(task, "context", {}) or {}
        for key, value in sorted(context.items()):
            if "rubric" not in str(key).lower() or not isinstance(value, str):
                continue
            path = (root / value).resolve()
            if path.suffix.lower() == ".json" and _inside(path, root) and path not in seen:
                seen.add(path)
                yield path

    for relative in _CONVENTIONAL_PATHS:
        path = (root / relative).resolve()
        if path not in seen:
            seen.add(path)
            yield path


def discover_environment_rubric(env: Any) -> tuple[Path, dict[str, Any], Any] | None:
    """Return the first executable Rubric that is owned by the environment."""
    errors: list[str] = []
    for path in _candidate_paths(env):
        if not path.is_file():
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                raise ValueError("root must be a JSON object")
            # Validation here prevents a judge prompt or unrelated JSON file from
            # silently becoming an Active Rubric.
            canonical = normalize_existing_rubric(
                env_name=str(env.name), version="discovery", document=document
            )
            return path, document, canonical
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            errors.append(f"{path.name}: {exc}")
    # Environments with deterministic scorers commonly declare the effective
    # Judge dimensions directly in meta.yaml. The evaluator freezes the same
    # dimensions into every evaluation manifest, so this is an existing Judge
    # contract rather than a newly invented Rubric.
    meta = getattr(env, "meta", {}) or {}
    dimensions = meta.get("dimensions") if isinstance(meta, dict) else None
    if isinstance(dimensions, list) and dimensions:
        items = []
        for item in dimensions:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            weight = float(item.get("weight", 0) or 0)
            if not name or weight <= 0:
                continue
            items.append({
                "id": name,
                "weight": weight,
                "criterion": str(item.get("description") or name),
            })
        if items:
            document = {
                "schema_version": (
                    "octagon-env-judge-dimensions."
                    + str(meta.get("schema_version") or "v1")
                ),
                "source_contract": "env.meta.dimensions",
                "pass_threshold": meta.get("pass_threshold"),
                "items": items,
            }
            canonical = normalize_existing_rubric(
                env_name=str(env.name), version="discovery", document=document
            )
            return Path(env.env_dir).resolve() / "meta.yaml", document, canonical
    if errors:
        logger.warning("no usable rubric discovered for env=%s: %s", env.name, errors)
    return None


def _executor_kind(path: Path, document: dict[str, Any]) -> str:
    role = str(document.get("judge_role") or "").lower()
    if "deterministic" in role:
        return "deterministic"
    if document.get("source_contract") == "env.meta.dimensions":
        return "hybrid"
    name = path.name.lower()
    if path.parent.name == "private" or any(
        token in name for token in ("official", "product_judge", "ui_judge", "authoring")
    ):
        return "llm_as_judge"
    return "hybrid"


def _version(document: dict[str, Any], canonical: Any) -> str:
    declared = str(document.get("schema_version") or "environment-rubric-v1")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", declared).strip("-")[:80]
    digest = canonical_hash(canonical.model_dump(mode="json", by_alias=True)).split(":")[-1]
    return f"bootstrap:{safe or 'environment-rubric-v1'}:{digest[:12]}"


def bootstrap_environment_rubrics_sync(
    *, db_path: Path, envs: dict[str, Any], actor: str = "system:environment-rubric-discovery"
) -> list[BootstrapResult]:
    """Register environment/Judge Rubrics only where no Active Version exists.

    This is extraction of the existing evaluation contract, not model-generated
    evolution and not a replacement for human-gated publication of later versions.
    """
    with _open_sync(db_path) as conn:
        active = {
            str(row[0]) for row in conn.execute(
                "SELECT ar.env_name FROM active_rubrics ar "
                "JOIN rubric_versions rv ON rv.id=ar.rubric_id "
                "WHERE rv.source_batch_id IS NULL AND rv.status='published'"
            )
        }
    results: list[BootstrapResult] = []
    for env_name, env in sorted(envs.items()):
        if env_name in active:
            results.append(BootstrapResult(env_name=env_name, status="already_active"))
            continue
        discovered = discover_environment_rubric(env)
        if discovered is None:
            results.append(BootstrapResult(env_name=env_name, status="not_found"))
            continue
        path, document, canonical = discovered
        version = _version(document, canonical)
        canonical = canonical.model_copy(update={"proposed_version": version})
        executor_kind = _executor_kind(path, document)
        registered = {
            "schema_version": "octagon-registered-rubric-v1",
            "evolution_domain": "product",
            "executor_kind": executor_kind,
            "bootstrap_source": "environment_judge_contract",
            "source_path": str(path.relative_to(Path(env.env_dir).resolve())),
            "source_document": document,
            "canonical_rubric": canonical.model_dump(mode="json", by_alias=True),
        }
        try:
            register_active_rubric_sync(
                db_path=db_path,
                env_name=env_name,
                version=version,
                rubric=registered,
                actor=actor,
                executor_kind=executor_kind,
            )
        except ValueError as exc:
            results.append(BootstrapResult(
                env_name=env_name, status="failed", source_path=str(path), detail=str(exc)
            ))
            continue
        results.append(BootstrapResult(
            env_name=env_name,
            status="registered",
            version=version,
            source_path=str(path),
        ))
    return results
