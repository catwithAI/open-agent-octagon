"""Fail-fast YAML Profile catalog loader."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from backend.experiments.hashing import canonical_hash

from .models import ResearchProfile


@dataclass(frozen=True)
class CatalogProfile:
    profile: ResearchProfile
    content_hash: str
    source_ref: str

    def projection(self, *, detail: bool = False) -> dict[str, Any]:
        base = {
            "id": self.profile.id,
            "version": self.profile.version,
            "label": self.profile.label,
            "description": self.profile.description,
            "applies_to": self.profile.applies_to.model_dump(mode="json"),
            "limits": self.profile.limits.model_dump(mode="json"),
            "content_hash": self.content_hash,
            "source_ref": self.source_ref,
        }
        if detail:
            base["profile"] = self.profile.model_dump(mode="json", by_alias=True)
        return base


@dataclass(frozen=True)
class ProfileCatalog:
    items: tuple[CatalogProfile, ...]

    def list(self) -> list[dict[str, Any]]:
        return [item.projection() for item in self.items]

    def get(self, profile_id: str, version: str | None = None) -> CatalogProfile | None:
        matches = [
            item
            for item in self.items
            if item.profile.id == profile_id
            and (version is None or item.profile.version == version)
        ]
        if not matches:
            return None
        return max(
            matches,
            key=lambda item: tuple(int(part) for part in item.profile.version.split(".")),
        )


def load_profiles(root: Path) -> ProfileCatalog:
    root = Path(root)
    if not root.is_dir():
        raise ValueError(f"profiles directory not found: {root}")
    items: list[CatalogProfile] = []
    seen: set[tuple[str, str]] = set()
    for path in sorted((*root.glob("*.yaml"), *root.glob("*.yml"))):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ValueError(f"invalid profile YAML: {path.name}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"profile must be a mapping: {path.name}")
        profile = ResearchProfile.model_validate(raw)
        key = (profile.id, profile.version)
        if key in seen:
            raise ValueError(f"duplicate profile: {profile.id}@{profile.version}")
        seen.add(key)
        items.append(
            CatalogProfile(
                profile=profile,
                content_hash=canonical_hash(profile),
                source_ref=path.name,
            )
        )
    if not items:
        raise ValueError(f"no profiles found: {root}")
    return ProfileCatalog(tuple(items))
