"""Provider-independent Insight generation, validation and version persistence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from backend.db import _now_iso

from .prompts import PROMPT_VERSION, render_prompt
from .providers import InsightProvider, InsightProviderError, ProviderResult
from .repository import InsightRepository
from .validation import SECTION_NAMES, InsightReport, validate_report


GENERATOR_VERSION = "octagon-insight-generator-v1"


@dataclass(frozen=True)
class GenerationOutcome:
    status: str
    report: dict[str, Any] | None
    generator: dict[str, Any]
    error_code: str | None = None
    error_message: str | None = None
    report_id: str | None = None
    version: int | None = None


def _parse_json(text: str) -> dict[str, Any]:
    value = text.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            value = "\n".join(lines[1:-1])
            if value.lstrip().startswith("json\n"):
                value = value.lstrip()[5:]
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("insight output must be a JSON object")
    return parsed


def _renest_sections(raw: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """确定性形状修复：模型常把五个 section 平铺在顶层而不是包进 ``sections``。

    只做键的重组、不改任何语句内容，并记录 validation_event 供审计；
    已有 ``sections`` 或没有任何 section 键时原样返回。
    """
    section_keys = set(SECTION_NAMES)
    if "sections" in raw or not (section_keys & raw.keys()):
        return raw, []
    candidate = {key: value for key, value in raw.items() if key not in section_keys}
    candidate["sections"] = {key: raw[key] for key in SECTION_NAMES if key in raw}
    return candidate, [{
        "event": "sections_renested",
        "reason": "model flattened section keys at the top level",
    }]


def _coerce_protocol_patches(raw: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """确定性修复：模型常把 builder_draft.protocol_patch 写成 JSON 字符串。

    只在字符串能严格解析为 JSON object 时替换为解析结果；自然语言字符串保持
    原样交给 schema 校验失败（不猜测语义）。"""
    events: list[dict[str, Any]] = []
    sections = raw.get("sections")
    probes = sections.get("suggested_probes") if isinstance(sections, dict) else None
    for probe in probes if isinstance(probes, list) else []:
        draft = probe.get("builder_draft") if isinstance(probe, dict) else None
        patch = draft.get("protocol_patch") if isinstance(draft, dict) else None
        if not isinstance(patch, str):
            continue
        try:
            parsed = json.loads(patch)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            draft["protocol_patch"] = parsed
            events.append({
                "statement_id": probe.get("id"),
                "event": "protocol_patch_parsed_from_string",
            })
    return raw, events


def _sanitized_error_detail(exc: Exception) -> str:
    """结构化错误摘要。绝不包含模型原文（redaction fail-closed）：

    - ValidationError 只取字段路径 + 错误类型（不取 input/msg 里的值）；
    - JSONDecodeError 只取行列号；
    - 其余 ValueError 是我们自己的校验消息，本身不含 payload。
    """
    if isinstance(exc, ValidationError):
        parts = [
            f"{'.'.join(str(part) for part in error['loc'])}: {error['type']}"
            for error in exc.errors()[:10]
        ]
        suffix = "" if exc.error_count() <= 10 else f" (+{exc.error_count() - 10} more)"
        return "; ".join(parts) + suffix
    if isinstance(exc, json.JSONDecodeError):
        return f"invalid JSON at line {exc.lineno} column {exc.colno}"
    return str(exc)[:500]


def _metadata(
    result: ProviderResult | None = None,
    provider: InsightProvider | None = None,
) -> dict[str, Any]:
    return {
        "generator_version": GENERATOR_VERSION,
        "prompt_version": PROMPT_VERSION,
        "provider": (
            result.provider
            if result
            else getattr(provider, "provider_name", provider.__class__.__name__ if provider else None)
        ),
        "model": result.model if result else getattr(provider, "model", None),
        "input_tokens": result.input_tokens if result else None,
        "output_tokens": result.output_tokens if result else None,
        "cost": result.cost if result else None,
        "completed_at": _now_iso(),
    }


async def generate_report(
    bundle: dict[str, Any], provider: InsightProvider
) -> GenerationOutcome:
    result: ProviderResult | None = None
    try:
        result = await provider.generate(render_prompt(bundle))
        raw = _parse_json(result.text)
        raw, shape_events = _renest_sections(raw)
        raw, patch_events = _coerce_protocol_patches(raw)
        repair_events = [*shape_events, *patch_events]
        if repair_events:
            raw["validation_events"] = [
                *(raw.get("validation_events") or []), *repair_events,
            ]
        report: InsightReport = validate_report(raw, bundle)
    except InsightProviderError as exc:
        return GenerationOutcome(
            status="failed",
            report=None,
            generator=_metadata(result, provider),
            error_code="provider_failed",
            error_message=str(exc)[:2000],
        )
    except (json.JSONDecodeError, ValueError, ValidationError) as exc:
        return GenerationOutcome(
            status="failed",
            report=None,
            generator=_metadata(result, provider),
            error_code="model_output_invalid",
            error_message=(
                "model output did not satisfy the Insight JSON schema "
                f"({exc.__class__.__name__}): {_sanitized_error_detail(exc)}"
            )[:2000],
        )
    return GenerationOutcome(
        status="ready",
        report=report.model_dump(mode="json"),
        generator=_metadata(result, provider),
    )


async def generate_and_store(
    *,
    experiment_id: str,
    bundle: dict[str, Any],
    provider: InsightProvider,
    repository: InsightRepository,
) -> GenerationOutcome:
    outcome = await generate_report(bundle, provider)
    version = repository.next_version(experiment_id)
    stored_report = outcome.report or {
        "schema_version": "octagon-insight-failure-v1",
        "error_code": outcome.error_code,
        "error_message": outcome.error_message,
    }
    report_id = repository.add(
        experiment_id=experiment_id,
        version=version,
        bundle_hash=str(bundle["bundle_hash"]),
        generator=outcome.generator,
        report=stored_report,
        status=outcome.status,
        schema_version="octagon-insight-v1",
        producer_version=GENERATOR_VERSION,
        input_refs={"bundle_hash": bundle["bundle_hash"]},
    )
    return GenerationOutcome(
        status=outcome.status,
        report=outcome.report,
        generator=outcome.generator,
        error_code=outcome.error_code,
        error_message=outcome.error_message,
        report_id=report_id,
        version=version,
    )
