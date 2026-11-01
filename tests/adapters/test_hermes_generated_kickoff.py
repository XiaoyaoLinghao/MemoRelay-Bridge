import sqlite3
import pytest
from .conftest import make_hermes_db,hermes_reader,hermes_adapter

@pytest.mark.parametrize("source",["cron","kanban","subagent","tool","feishu","weixin","oneshot",None])
def test_only_explicit_scheduled_delegated_kickoffs_are_not_human(tmp_path,clock,redactor,source):
 now=clock.now.timestamp()-100
 db=make_hermes_db(tmp_path/"state.db",[("session:root",None,None)],[
 {"id":1,"role":"user","text":"review work kickoff","timestamp":now},
 {"id":2,"role":"assistant","text":"reported result remains a report","timestamp":now+1},
 {"id":3,"role":"tool","text":"execution evidence","timestamp":now+2}])
 with sqlite3.connect(db) as c:
  c.execute("ALTER TABLE sessions ADD COLUMN source TEXT")
  c.execute("UPDATE sessions SET source=?",(source,))
 records,_=hermes_reader(db,clock=clock).read_since("hermes:main",None)
 generated=source in {"cron","kanban","subagent","tool"}
 assert records[0][0].origin_kind==("memory_injection" if generated else "user_input")
 assert records[1][0].origin_kind=="assistant_reply" and records[2][0].origin_kind=="tool_execution"
 adapter=hermes_adapter(redactor)
 for record,_ in records:assert adapter.to_events(record)
 assert records[0][0].native_event_id=="msg:1"
