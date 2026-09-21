"""磁盘护栏：写满之前就停下来，而不是写满之后才发现。

2026-09-18 的 7-agent 横评在第 336 个 attempt 处把 444G 的评测机写到 0 字节，
被迫中止。更糟的是那时连 stop 都失效——停止流程本身要落盘写状态，没有空间
就返回 500，必须先手动腾地方才能停。

所以护栏分两层：
1. **调度闸**：可用空间低于阈值就不再启动新 attempt（`ensure_free_disk`）。
2. **停止路径留余量**：阈值远大于 0（默认 15G），保证触发时仍有足够空间把
   状态写完、把 run 收敛掉。
"""

from __future__ import annotations

import errno
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# 磁盘不足导致的失败统一用这个 error_code，别塌缩进 scoring 阶段的通用异常
# ——排查时要一眼看出是资源问题而非 agent 问题（需求 7.3）。
DISK_EXHAUSTED_CODE = "disk_exhausted"

GIB = 1024**3


@dataclass(frozen=True)
class DiskStatus:
    free_bytes: int
    total_bytes: int
    threshold_bytes: int

    @property
    def ok(self) -> bool:
        return self.threshold_bytes <= 0 or self.free_bytes >= self.threshold_bytes

    @property
    def free_gb(self) -> float:
        return self.free_bytes / GIB

    @property
    def threshold_gb(self) -> float:
        return self.threshold_bytes / GIB

    def message(self) -> str:
        return (
            f"可用磁盘 {self.free_gb:.1f}G 低于阈值 {self.threshold_gb:.1f}G，"
            "暂停调度新 attempt"
        )


def check_free_disk(path: Path | str, *, min_free_gb: float) -> DiskStatus:
    """查 `path` 所在文件系统的可用空间。

    统计的是**可用**（available）而非 free：root 预留块普通用户拿不到，
    按 free 判断会在还差几 G 时误以为安全。
    """
    target = Path(path)
    # data 目录可能还没建出来；往上找到第一个存在的祖先，同一文件系统。
    while not target.exists() and target != target.parent:
        target = target.parent
    try:
        usage = shutil.disk_usage(target)
    except OSError as exc:
        # 查不到就不要误判为「磁盘满」而卡住整条流水线：记日志、放行。
        logger.warning("无法读取 %s 的磁盘用量：%s", target, exc)
        return DiskStatus(free_bytes=1 << 62, total_bytes=1 << 62, threshold_bytes=0)
    return DiskStatus(
        free_bytes=usage.free,
        total_bytes=usage.total,
        threshold_bytes=int(max(min_free_gb, 0) * GIB),
    )


class LowDiskSpace(RuntimeError):
    """可用磁盘低于阈值，不应再启动新 attempt。"""

    def __init__(self, status: DiskStatus) -> None:
        super().__init__(status.message())
        self.status = status


def ensure_free_disk(path: Path | str, *, min_free_gb: float) -> DiskStatus:
    """空间不足时抛 `LowDiskSpace`，供调度侧早退。"""
    status = check_free_disk(path, min_free_gb=min_free_gb)
    if not status.ok:
        raise LowDiskSpace(status)
    return status


def is_disk_full_error(exc: BaseException) -> bool:
    """判断一个异常是不是「盘满」。

    停止路径与落盘失败的兜底都要认出它——磁盘满表现为 OSError(ENOSPC)，
    被包在各种 IO 异常里；按 errno 判，不按字符串。
    """
    # EDQUOT 没有跨平台的 errno 常量（Linux 122 / macOS 69），用 getattr 取。
    full_errnos = {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, OSError) and current.errno in full_errnos:
            return True
        current = current.__cause__ or current.__context__
    return False
