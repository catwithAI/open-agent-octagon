"""Short-lived SQLite transactions; no remote IO while holding write locks."""
from __future__ import annotations

import sqlite3
import threading
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .schema import migrate

_WAL_INITIALIZED_DBS: set[tuple[str, int, int]] = set()
_WAL_LOCK = threading.Lock()


def _db_file_key(db_path: Path) -> tuple[str, int, int] | None:
    try:
        resolved = db_path.resolve()
        st = resolved.stat()
        return (str(resolved), st.st_dev, st.st_ino)
    except OSError:
        return None


def key(value: str) -> str:
    if not isinstance(value,str):raise ValueError('name/version must be a string')
    normalized = unicodedata.normalize('NFC', value).strip()
    if not normalized or len(normalized) > 200 or any(ord(c) < 32 for c in normalized):
        raise ValueError('name/version must contain 1–200 printable characters')
    return normalized


class Repository:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA busy_timeout=30000')
            conn.execute('PRAGMA synchronous=NORMAL')
            conn.execute('PRAGMA foreign_keys=ON')
            file_key = _db_file_key(self.db_path)
            with _WAL_LOCK:
                needs_wal = file_key is None or file_key not in _WAL_INITIALIZED_DBS
            if needs_wal:
                conn.execute('PRAGMA journal_mode=WAL')
                final_key = _db_file_key(self.db_path)
                if final_key is not None:
                    with _WAL_LOCK:
                        _WAL_INITIALIZED_DBS.add(final_key)
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def initialize(self) -> None:
        with self.transaction() as conn:
            migrate(conn)

    @staticmethod
    def next_sequence(conn: sqlite3.Connection, table: str, run_id: str) -> int:
        if table not in {'events', 'messages'} or not conn.in_transaction:
            raise ValueError('sequence allocation requires an allowed table and transaction')
        row = conn.execute(
            f'SELECT coalesce(max(seq),0)+1 FROM skill_eval_{table} WHERE run_id=?', (run_id,),
        ).fetchone()
        return row[0]
