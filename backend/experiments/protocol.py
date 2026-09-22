"""P0 protocol normalization and secret-free catalog snapshots."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import Any

from backend.config import Settings
from backend.profiles.loader import CatalogProfile
from backend.wire.policy import policy_rank

from .hashing import canonical_hash
from .models import AgentModel, ExperimentProtocol, ProtocolLimits

# agent → CLI 可执行文件名。blade-agent 不在此表：它的可用性判据是
# api_key（+ transport=cli 时的 blade 二进制），见下面 availability 的构造。
_CLI_EXECUTABLES = {
    "claude-code": "claude",
    "codex": "codex",
    "kimi-code": "kimi",
    "opencode": "opencode",
    "mimo-code": "mimo",
}

#: dsh 装的是 pip 包（deepseek-harness-sdk），没有 PATH 上的可执行文件，
#: 所以既不能塞进 _CLI_EXECUTABLES，也不能用 shutil.which 判可用性。
_IMPORT_PROBE_AGENTS = {"dsh": "deepseek_harness"}

KNOWN_AGENTS = frozenset({"blade-agent", *_CLI_EXECUTABLES, *_IMPORT_PROBE_AGENTS})


def _blade_availability(settings: Settings) -> str:
    """blade-agent 可用性：api_key 必需；transport=cli 时还要 blade 二进制在位。

    只看 api_key 会让实验建得出来、跑起来才 cli_not_found——横评一整批
    attempt 全废，还会被误读成 agent 能力问题。
    """
    if not settings.blade.api_key:
        return "not_configured"
    if settings.blade.transport == "cli" and not (
        settings.blade.cli_path or shutil.which("blade")
    ):
        return "not_found"
    return "configured"


def _module_installed(module: str) -> bool:
    """SDK 类 agent 的可用性判据。共享实现见 `backend.adapters.dsh_events`。"""
    from backend.adapters.dsh_events import module_available

    return module_available(module)


@dataclass(frozen=True)
class NormalizedProtocol:
    protocol: ExperimentProtocol
    advisory_warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class CatalogSnapshotBuilder:
    settings: Settings

    def build(self, candidates: tuple[AgentModel, ...]) -> dict[str, Any]:
        unknown = sorted({item.agent for item in candidates} - KNOWN_AGENTS)
        if unknown:
            raise ValueError(f"unknown agents: {unknown}")

        # CLI agent 的可用性判据统一是"可执行文件在 PATH 上"。这里必须覆盖
        # KNOWN_AGENTS 的**每一个**成员：下面 selections 用
        # `availability[item.agent]` 直接索引，漏一个就是建实验时 KeyError
        # （新接 agent 时踩过）。
        availability = {"blade-agent": _blade_availability(self.settings)}
        for agent_name, executable in _CLI_EXECUTABLES.items():
            availability[agent_name] = (
                "available" if shutil.which(executable) else "not_found"
            )
        for agent_name, module in _IMPORT_PROBE_AGENTS.items():
            availability[agent_name] = (
                "available" if _module_installed(module) else "not_found"
            )
        providers = {
            name: {"kind": provider.kind, "agent": provider.agent}
            for name, provider in sorted(self.settings.model_providers.items())
        }
        selections: list[dict[str, Any]] = []
        for item in candidates:
            provider_name = None
            if item.model and "/" in item.model:
                prefix = item.model.split("/", 1)[0]
                provider = self.settings.model_providers.get(prefix)
                if provider is not None:
                    if provider.agent not in (None, item.agent):
                        raise ValueError(
                            f"provider {prefix!r} is not configured for {item.agent!r}"
                        )
                    provider_name = prefix
            selections.append(
                {
                    "agent": item.agent,
                    "model": item.model,
                    "provider": provider_name,
                    "availability": availability[item.agent],
                }
            )
        snapshot: dict[str, Any] = {
            "schema_version": "octagon-catalog-snapshot-v1",
            "selections": selections,
            "providers": providers,
        }
        snapshot["hash"] = canonical_hash(snapshot)
        return snapshot


@dataclass(frozen=True)
class ProtocolNormalizer:
    settings: Settings

    def normalize(self, requested: ExperimentProtocol) -> NormalizedProtocol:
        candidates = requested.agents
        if requested.compare_mode == "same-model":
            explicit = tuple(item for item in candidates if item.model)
            explicit_models = {item.model for item in explicit}
            if not explicit_models:
                raise ValueError("same-model requires exactly one shared explicit model")
            if len(explicit_models) == 1:
                # Backward-compatible shorthand: one bare model may be supplied
                # once and is expanded to candidates that omitted it.
                shared_model = next(iter(explicit_models))
                candidates = tuple(
                    AgentModel(agent=item.agent, model=item.model or shared_model)
                    for item in candidates
                )
            else:
                if len(explicit) != len(candidates):
                    raise ValueError(
                        "same-model provider mappings must cover every candidate"
                    )
                non_blade_identities = {
                    self._provider_model_identity(item.agent, item.model or "")
                    for item in candidates
                    if item.agent != "blade-agent"
                }
                if len(non_blade_identities) != 1:
                    raise ValueError(
                        "same-model requires deployable mappings of one shared model"
                    )
                shared_identity = next(iter(non_blade_identities))
                if any(
                    item.agent == "blade-agent"
                    and item.model != shared_identity
                    and not (item.model or "").endswith(f"/{shared_identity}")
                    for item in candidates
                ):
                    raise ValueError(
                        "same-model requires deployable mappings of one shared model"
                    )

        server = self.settings.octagon
        effective_limits = ProtocolLimits(
            max_cells=min(requested.limits.max_cells, server.research_max_cells),
            max_attempts=min(
                requested.limits.max_attempts or server.research_max_attempts,
                server.research_max_attempts,
            ),
        )
        warnings: list[str] = []
        effective_capture = requested.capture_policy
        if policy_rank(server.research_capture_max_policy) < policy_rank(
            effective_capture
        ):
            effective_capture = server.research_capture_max_policy
            warnings.append(
                "capture policy downgraded by server maximum: "
                f"{requested.capture_policy} -> {effective_capture}"
            )
        normalized = requested.model_copy(
            update={
                "agents": candidates,
                "capture_policy": effective_capture,
                "limits": effective_limits,
                "catalog_snapshot": CatalogSnapshotBuilder(self.settings).build(
                    candidates
                ),
            }
        )
        # model_copy does not rerun validators; fail closed after all server
        # policy merges so cardinality is checked against effective limits.
        normalized = ExperimentProtocol.model_validate(
            normalized.model_dump(mode="json", by_alias=True)
        )
        return NormalizedProtocol(
            protocol=normalized,
            advisory_warnings=tuple(warnings),
        )

    def _provider_model_identity(self, agent: str, model: str) -> str:
        """Remove only a configured provider prefix, preserving the bare model ID."""
        prefix, separator, identity = model.partition("/")
        provider = self.settings.model_providers.get(prefix)
        if (
            separator
            and provider is not None
            and provider.agent in (None, agent)
        ):
            return identity
        return model


@dataclass(frozen=True)
class ExpandedProfile:
    protocol: ExperimentProtocol
    advisory_warnings: tuple[str, ...]
    blocking_warnings: tuple[str, ...]
    estimate: dict[str, Any]


@dataclass(frozen=True)
class ProfileExpander:
    """P1 Profile adapter that delegates final policy to ProtocolNormalizer."""

    settings: Settings

    def expand(
        self,
        selected: CatalogProfile,
        *,
        compare_mode: str,
        agents: tuple[AgentModel, ...],
        overrides: dict[str, Any] | None = None,
        env_meta: dict[str, Any] | None = None,
        prerequisite_warnings: tuple[str, ...] = (),
    ) -> ExpandedProfile:
        overrides = dict(overrides or {})
        allowed = {
            "repeats",
            "variant_specs",
            "timeout_seconds",
            "notify_model_of_timeout",
            "execution",
            "max_concurrency",
            "capture_policy",
            "leader",
            "limits",
        }
        unknown = sorted(set(overrides) - allowed)
        if unknown:
            raise ValueError(f"unknown profile override fields: {unknown}")
        profile = selected.profile
        base: dict[str, Any] = {
            "schema_version": "octagon-experiment-protocol-v1",
            "compare_mode": compare_mode,
            "profile": {
                "id": profile.id,
                "version": profile.version,
                "hash": selected.content_hash,
            },
            "agents": agents,
            "variant_specs": profile.protocol.variants,
            "repeats": profile.protocol.repeats,
            "execution": profile.protocol.execution,
            "max_concurrency": profile.protocol.max_concurrency,
            "timeout_seconds": profile.protocol.timeout_seconds,
            "notify_model_of_timeout": False,
            "capture_policy": profile.protocol.capture_policy,
            "leader": profile.protocol.leader,
            "catalog_snapshot": {},
            "limits": profile.limits,
        }
        base.update(overrides)
        raw_limits = base["limits"]
        if hasattr(raw_limits, "model_dump"):
            raw_limits = raw_limits.model_dump(mode="python")
        requested_limits = ProtocolLimits.model_validate(raw_limits)
        base["limits"] = ProtocolLimits(
            max_cells=min(requested_limits.max_cells, profile.limits.max_cells),
            max_attempts=min(
                requested_limits.max_attempts or profile.limits.max_attempts or 32000,
                profile.limits.max_attempts or 32000,
            ),
        )
        requested = ExperimentProtocol.model_validate(base)
        normalized = ProtocolNormalizer(self.settings).normalize(requested)
        advisory = list(normalized.advisory_warnings)
        blocking = list(prerequisite_warnings)
        metadata = env_meta or {}
        if not metadata.get("dimensions"):
            advisory.append("environment does not declare scorer dimensions")
        required_modalities = set(
            ((metadata.get("prerequisites") or {}).get("agent_modalities") or [])
        )
        profile_modalities = set(profile.applies_to.modalities)
        unsupported_modalities = sorted(required_modalities - profile_modalities)
        if unsupported_modalities:
            advisory.append(
                "profile applicability does not declare required modalities: "
                + ", ".join(unsupported_modalities)
            )
        missing_refs = sorted(
            set(profile.provider_refs) - set(self.settings.model_providers)
        )
        if missing_refs:
            advisory.append("profile provider refs unavailable: " + ", ".join(missing_refs))
        protocol = normalized.protocol
        return ExpandedProfile(
            protocol=protocol,
            advisory_warnings=tuple(advisory),
            blocking_warnings=tuple(blocking),
            estimate={
                "cells": protocol.cell_count,
                "attempts": protocol.attempt_count,
                "timeout_upper_bound_seconds": protocol.attempt_count
                * (protocol.timeout_seconds or 0),
                "pricing": "not_estimated",
            },
        )
