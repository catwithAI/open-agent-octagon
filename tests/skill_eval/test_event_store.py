import concurrent.futures
from backend.skill_eval.capture import Capture
from backend.skill_eval.events import EventStore
from backend.skill_eval.repository import Repository
from .test_schema_runs import seed


def test_dedup_is_source_and_stream_scoped_not_content(db_path):
    repo=Repository(db_path);repo.initialize();seed(repo)
    def fail_notify(_):raise RuntimeError('notification unavailable')
    store=EventStore(repo,Capture(),notify=fail_notify)
    first=store.append('a','model',{'content':'same'},stream_id='g1',source_sequence=1)
    replay=store.append('a','model',{'content':'same'},stream_id='g1',source_sequence=1)
    assert first==replay
    store.append('a','model',{'content':'same'},stream_id='g2',source_sequence=1)
    store.append('a','model',{'content':'same'})
    assert [e['seq'] for e in store.read('a')['items']]==[1,2,3]
    assert store.read('a',limit=1)['has_more'] is True
    assert store.get('b',first['event_id']) is None


def test_concurrent_append_uses_committed_gapless_cursor(db_path):
    repo=Repository(db_path);repo.initialize();seed(repo)
    store=EventStore(repo,Capture())
    def append(i):return store.append('a','model',{'content':str(i)})['seq']
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(append,range(10)))==list(range(1,11))
    assert store.read('a',after=8)['next_cursor']==10
