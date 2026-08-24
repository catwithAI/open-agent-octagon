"""Shared fail-closed secret and audit policies for research metadata."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from backend.db import _now_iso, _open_sync


FORBIDDEN_KEYS = frozenset({
    "api_key", "apikey", "authorization", "password", "private_key",
    "secret", "secret_key", "token", "access_token", "refresh_token",
})
SECRET_VALUE = re.compile(
    r"(?:-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"\bsk-[A-Za-z0-9_-]{12,}|\bBearer\s+[A-Za-z0-9._~-]{12,})",
    re.IGNORECASE,
)


def assert_secret_free(value: Any, *, label: str) -> None:
    def inspect(item: Any, path: str) -> None:
        if isinstance(item, dict):
            for key, nested in item.items():
                normalized = str(key).lower().replace("-", "_")
                if normalized in FORBIDDEN_KEYS:
                    raise ValueError(f"{label} contains forbidden secret field at {path}.{key}")
                inspect(nested, f"{path}.{key}")
        elif isinstance(item, (list, tuple)):
            for index, nested in enumerate(item):
                inspect(nested, f"{path}[{index}]")
        elif isinstance(item, str) and SECRET_VALUE.search(item):
            raise ValueError(f"{label} contains secret-like material at {path}")

    inspect(value, "$")


def append_research_audit(
    db_path: Path,
    *,
    action: str,
    target_type: str,
    target_id: str,
    actor: str = "local:anonymous",
    request_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    safe_metadata = dict(metadata or {})
    assert_secret_free(safe_metadata, label="audit metadata")
    audit_id = f"aud_{uuid.uuid4().hex}"
    with _open_sync(Path(db_path)) as conn:
        conn.execute(
            "INSERT INTO research_audit_log(id,action,target_type,target_id,actor,"
            "request_id,metadata_json,schema_version,created_at) VALUES(?,?,?,?,?,?,?,"
            "'octagon-research-audit-v1',?)",
            (
                audit_id,
                action,
                target_type,
                target_id,
                actor,
                request_id,
                json.dumps(safe_metadata, ensure_ascii=False, sort_keys=True),
                _now_iso(),
            ),
        )
        conn.commit()
    return audit_id
