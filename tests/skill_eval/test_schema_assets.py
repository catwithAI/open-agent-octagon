import sqlite3

import pytest

from backend import db
from backend.skill_eval.repository import Repository, key
from backend.skill_eval.schema import migrate


async def test_old_database_and_repeated_migration_preserve_values(tmp_path):
    path = tmp_path / "db.sqlite"
    await db.init_db(path)
    repo = Repository(path)
    with repo.transaction() as conn:
        conn.execute("INSERT INTO tasks(id,env_name,prompt,created_at) VALUES('old','env','original','now')")
        conn.execute("INSERT INTO skill_eval_skills(id,scope_id,name,name_key,created_at,updated_at) VALUES('a','one','é',?,'now','now')", (key('e\u0301'),))
    await db.init_db(path)
    with repo.transaction() as conn:
        assert conn.execute("SELECT prompt FROM tasks WHERE id='old'").fetchone()[0] == "original"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO skill_eval_skills(id,scope_id,name,name_key,created_at,updated_at) VALUES('b','one','é',?,'now','now')", (key('é'),))
        conn.execute("INSERT INTO skill_eval_skills(id,scope_id,name,name_key,created_at,updated_at) VALUES('b','two','é',?,'now','now')", (key('é'),))


def test_migration_rolls_back_partial_schema(tmp_path):
    path = tmp_path / "bad.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE skill_eval_runs (id TEXT)")
        with pytest.raises(sqlite3.OperationalError):
            migrate(conn)
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='skill_eval_skills'").fetchone() is None
