"""Thread-safe streaming primitives.

``backend.py`` launches one worker thread per parallel request; each worker
appends tokens into a :class:`StreamSlot`.  The frontend polls all slots on a
short timer and redraws the accumulated text, which produces the real-time,
token-by-token effect the UI is after.
"""

from __future__ import annotations

import threading
from typing import List, Optional


class StreamSlot:
    """Accumulates tokens for a single request, safely across threads."""

    def __init__(self, request_index: int) -> None:
        self.request_index = request_index
        self._parts: List[str] = []
        self._lock = threading.Lock()
        self._error: Optional[str] = None
        self._finished = threading.Event()

    # -- written by the backend worker thread -------------------------------
    def append(self, token: str) -> None:
        with self._lock:
            self._parts.append(token)

    def finish(self) -> None:
        self._finished.set()

    def fail(self, message: str) -> None:
        with self._lock:
            self._error = message
        self._finished.set()

    # -- read by the frontend render loop -----------------------------------
    @property
    def text(self) -> str:
        with self._lock:
            return "".join(self._parts)

    @property
    def error(self) -> Optional[str]:
        with self._lock:
            return self._error

    @property
    def is_finished(self) -> bool:
        return self._finished.is_set()

    @property
    def ok(self) -> bool:
        return self.is_finished and self._error is None


class StreamManager:
    """Tracks the N slots belonging to one SUBMIT action."""

    def __init__(self, total: int, started_slots: Optional[List[StreamSlot]] = None) -> None:
        self.total = total
        self.slots: List[StreamSlot] = started_slots or [StreamSlot(i) for i in range(total)]

    # -- helpers ------------------------------------------------------------
    @property
    def active_count(self) -> int:
        return sum(1 for s in self.slots if not s.is_finished)

    @property
    def all_finished(self) -> bool:
        return all(s.is_finished for s in self.slots)

    def finished_count(self) -> int:
        return sum(1 for s in self.slots if s.is_finished)

    def summary(self) -> str:
        n = self.finished_count()
        errors = sum(1 for s in self.slots if s.error)
        tail = f" ({errors} failed)" if errors else ""
        return f"{n}/{self.total} requests completed{tail}"