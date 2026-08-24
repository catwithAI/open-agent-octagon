"""本地 Agent 进程的跨重启身份持久化与存活判定。"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class BootMatch(StrEnum):
    """落盘 boot id 与当前机器启动实例的比较结果。"""

    SAME = "same"
    DIFFERENT = "different"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProcessIdentity:
    """一个 Agent 进程的跨重启身份。"""

    pid: int
    pgid: int | None = None
    boot_id: str | None = None
    recorded_at: str | None = None
    boot_id_corrupt: bool = False

    @classmethod
    def from_payload(cls, data: object) -> ProcessIdentity | None:
        """从落盘 JSON 构造；无法使用的记录返回 ``None``。"""
        if not isinstance(data, dict):
            return None
        pid = data.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            return None

        pgid_value = data.get("pgid")
        pgid = (
            pgid_value
            if isinstance(pgid_value, int) and pgid_value > 0
            else None
        )

        boot_value = data.get("boot_id")
        boot_id_corrupt = bool(boot_value) and not isinstance(boot_value, str)
        boot_id = boot_value if isinstance(boot_value, str) and boot_value else None

        recorded_value = data.get("recorded_at")
        recorded_at = recorded_value if isinstance(recorded_value, str) else None
        return cls(
            pid=pid,
            pgid=pgid,
            boot_id=boot_id,
            recorded_at=recorded_at,
            boot_id_corrupt=boot_id_corrupt,
        )

    def to_payload(self) -> dict[str, Any]:
        """返回与既有 ``agent_process.json`` 完全相同的磁盘结构。"""
        return {
            "pid": self.pid,
            "pgid": self.pgid,
            "boot_id": self.boot_id,
            "recorded_at": self.recorded_at,
        }

    def boot_match(self, current: str | None) -> BootMatch:
        if self.boot_id_corrupt:
            return BootMatch.DIFFERENT
        if self.boot_id is None:
            return BootMatch.UNKNOWN
        if current is None or self.boot_id != current:
            return BootMatch.DIFFERENT
        return BootMatch.SAME


def record_agent_process(
    data_path: Path | None, attempt_id: str, proc: object
) -> None:
    """把本地 CLI 的 pid / pgid 落盘；失败不得阻断 Agent 执行。"""
    if data_path is None:
        return
    pid = getattr(proc, "pid", None)
    if not isinstance(pid, int) or pid <= 0:
        return
    try:
        pgid = os.getpgid(pid)
    except OSError:
        pgid = None
    identity = ProcessIdentity(
        pid=pid,
        pgid=pgid,
        boot_id=_machine_boot_id(),
        recorded_at=_utc_now_iso(),
    )
    try:
        target = Path(data_path) / "attempts" / attempt_id / "agent_process.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(identity.to_payload(), ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        logger.warning("无法记录 agent 进程信息 attempt=%s", attempt_id, exc_info=True)


def _machine_boot_id() -> str | None:
    """本次机器启动的标识；取不到返回 ``None``。"""
    for path in ("/proc/sys/kernel/random/boot_id",):
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            continue
    try:
        import subprocess

        out = subprocess.run(
            ["sysctl", "-n", "kern.boottime"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return out.stdout.strip() or None
    except Exception:
        return None


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_agent_process(
    data_path: Path | None, attempt_id: str
) -> ProcessIdentity | None:
    """读回落盘身份；无记录、损坏或不可用时返回 ``None``。"""
    if data_path is None:
        return None
    try:
        raw = (
            Path(data_path) / "attempts" / attempt_id / "agent_process.json"
        ).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None
    return ProcessIdentity.from_payload(data)


def agent_process_is_alive(data_path: Path | None, attempt_id: str) -> bool:
    """记录中的本地 Agent 进程当前是否仍存活。"""
    identity = read_agent_process(data_path, attempt_id)
    if identity is None:
        return False
    match identity.boot_match(_machine_boot_id()):
        case BootMatch.DIFFERENT:
            return False
        case BootMatch.SAME | BootMatch.UNKNOWN:
            pass
    try:
        os.kill(identity.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
