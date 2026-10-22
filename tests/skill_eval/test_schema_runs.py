import concurrent.futures
import sqlite3

import pytest

from backend.skill_eval.repository import Repository


def seed(repo):
    with repo.transaction() as c:
        for i in ('a', 'b'):
            c.execute("INSERT INTO skill_eval_skills(id,scope_id,name,name_key,created_at,updated_at) VALUES(?, 'scope', ?, ?, 'now','now')", (i,i,i))
            c.execute("INSERT INTO skill_eval_versions(id,skill_id,content_hash,manifest_ref,created_at) VALUES(?,?,?,?,'now')", (i,i,i,i))
            c.execute("INSERT INTO skill_eval_experiments(id,skill_id,name,created_at,updated_at) VALUES(?,?,?,'now','now')", (i,i,i))
            c.execute("INSERT INTO skill_eval_runs(id,experiment_id,skill_id,skill_version_id,created_at) VALUES(?,?,?,?,'now')", (i,i,i,i))


def test_constraints_and_transactional_sequence(db_path):
    repo = Repository(db_path)
    repo.initialize()
    seed(repo)
    with repo.transaction() as c:
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO skill_eval_runs(id,experiment_id,skill_id,skill_version_id,created_at) VALUES('bad','a','b','b','now')")
    def append(_):
        with repo.transaction() as c:
            seq = repo.next_sequence(c, "messages", "a")
            c.execute("INSERT INTO skill_eval_messages(id,run_id,seq,kind,created_at) VALUES(?, 'a', ?, 'followup','now')", (f'm{seq}', seq))
            return seq
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(append, range(6))) == list(range(1,7))
    with repo.transaction() as c:
        c.execute("INSERT INTO skill_eval_events(run_id,seq,event_id,source,type,observed_at) VALUES('a',1,'evt','socket','call','now')")
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO skill_eval_events(run_id,seq,event_id,source,type,observed_at) VALUES('a',2,'evt','socket','call','now')")
        c.execute("INSERT INTO skill_eval_events(run_id,seq,event_id,source,type,observed_at) VALUES('b',1,'evt','socket','call','now')")
    repo.initialize()
    with repo.transaction() as c:
        assert c.execute("SELECT count(*) FROM skill_eval_messages").fetchone()[0] == 6


def test_operation_and_final_path_uniqueness_and_single_writer(db_path):
    repo=Repository(db_path);repo.initialize();seed(repo)
    with repo.transaction() as c:
        c.execute("INSERT INTO skill_eval_jobs(id,run_id,operation_id,phase,state) VALUES('j1','a','op1','prepare','running')")
        for id_,run_id,operation,state in [('j2','b','op1','pending'),('j3','a','op2','running')]:
            with pytest.raises(sqlite3.IntegrityError):
                c.execute("INSERT INTO skill_eval_jobs(id,run_id,operation_id,phase,state) VALUES(?,?,?,'message',?)",(id_,run_id,operation,state))
        c.execute("INSERT INTO skill_eval_artifacts(id,run_id,relative_path,state) VALUES('f1','a','out.txt','missing')")
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO skill_eval_artifacts(id,run_id,relative_path,state) VALUES('f2','a','out.txt','missing')")
