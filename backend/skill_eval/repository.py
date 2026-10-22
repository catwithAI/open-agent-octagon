"""Short-lived SQLite transactions; no remote IO while holding write locks."""
from __future__ import annotations

import sqlite3
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .schema import migrate


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
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA journal_mode=WAL')
        try:
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
