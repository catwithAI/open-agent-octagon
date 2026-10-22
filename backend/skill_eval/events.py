"""Committed event cursor is the source of truth; notifications are advisory."""
from __future__ import annotations

import json
import uuid
from datetime import datetime,timezone
from typing import Callable

from .capture import Capture
from .repository import Repository


class EventStore:
    def __init__(self, repository:Repository,capture:Capture,notify:Callable|None=None):
        self.repo,self.capture,self.notify=repository,capture,notify

    def append(self,run_id,kind,raw,*,source='socket',source_id=None,stream_id=None,source_sequence=None):
        # Scope transport IDs to a generation/stream. The same text is never a
        # deduplication key, nor is a timestamp or a tool name.
        source_key=None
        if source_id is not None:
            source_key=json.dumps([stream_id,'id',str(source_id)],separators=(',',':'))
        elif source_sequence is not None:
            if not stream_id:raise ValueError('source sequence requires stream identity')
            source_key=json.dumps([stream_id,'sequence',source_sequence],separators=(',',':'))
        encoded=self.capture.encode(kind,raw)
        with self.repo.transaction() as c:
            if source_key is not None:
                old=c.execute('SELECT * FROM skill_eval_events WHERE run_id=? AND source=? AND source_key=?',(run_id,source,source_key)).fetchone()
                if old:return self._decode(old)
            seq=self.repo.next_sequence(c,'events',run_id)
            id_=uuid.uuid4().hex
            observed=datetime.now(timezone.utc).isoformat()
            c.execute('INSERT INTO skill_eval_events(run_id,seq,event_id,source,source_key,type,payload_json,observed_at) VALUES(?,?,?,?,?,?,?,?)',
                (run_id,seq,id_,source,source_key,kind,json.dumps(encoded,ensure_ascii=False),observed))
            result=self._decode(c.execute('SELECT * FROM skill_eval_events WHERE run_id=? AND seq=?',(run_id,seq)).fetchone())
        if self.notify:
            try:self.notify({'run_id':run_id,'seq':seq,'event_id':id_})
            except Exception:pass  # Readers recover committed facts with their DB cursor.
        return result

    @staticmethod
    def _decode(row):
        value=dict(row);value['payload']=json.loads(value.pop('payload_json'));return value

    def read(self,run_id,*,after=0,limit=100):
        if after<0 or limit<1 or limit>500:raise ValueError('invalid event cursor/limit')
        with self.repo.connection() as c:
            rows=c.execute('SELECT * FROM skill_eval_events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?', (run_id,after,limit+1)).fetchall()
        page=rows[:limit]
        return {'items':[self._decode(row) for row in page],'next_cursor':page[-1]['seq'] if page else after,'has_more':len(rows)>limit}

    def head(self,run_id):
        with self.repo.connection() as c:
            return c.execute('SELECT coalesce(max(seq),0) FROM skill_eval_events WHERE run_id=?',(run_id,)).fetchone()[0]

    def get(self,run_id,event_id):
        with self.repo.connection() as c:
            row=c.execute('SELECT * FROM skill_eval_events WHERE run_id=? AND event_id=?',(run_id,event_id)).fetchone()
        return self._decode(row) if row else None
