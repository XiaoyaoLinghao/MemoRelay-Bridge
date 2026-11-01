"""Native compression copies retain source identity; ordinary repeated turns do not collapse."""
import json
import sqlite3

from dma.pipeline import capture
from .conftest import make_hermes_db, hermes_reader, hermes_adapter


def test_rotation_retained_tail_and_concurrent_append_are_idempotent(tmp_path, clock, redactor, outbox):
    now = clock.now.timestamp() - 500
    db = make_hermes_db(tmp_path / "state.db", [("root", None, "compression"), ("child", "root", None), ("tip", "child", None)], [
        {"id": 1, "session_id": "root", "text": "端口4411", "timestamp": now},
        {"id": 2, "session_id": "root", "text": "收到", "timestamp": now + 1},
        {"id": 3, "session_id": "root", "text": "并发追加待办", "timestamp": now + 2},
    ])
    reader = hermes_reader(db, clock=clock)
    adapter = hermes_adapter(redactor)
    assert capture(reader, outbox, stream="hermes:main", convert=adapter.to_events)["inserted"] == 3
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO messages SELECT 4, 'child', role, '摘要', timestamp + 20, 1, 0, 1, NULL FROM messages WHERE id=1")
        conn.execute("INSERT INTO messages SELECT 5, 'child', role, content, timestamp, 1, 0, 0, NULL FROM messages WHERE id=2")
        conn.execute("INSERT INTO messages SELECT 6, 'child', role, content, timestamp, 1, 0, 0, NULL FROM messages WHERE id=3")
        conn.execute("INSERT INTO messages SELECT 7, 'child', role, content, timestamp + 30, 1, 0, 0, NULL FROM messages WHERE id=2")
        conn.execute("UPDATE sessions SET end_reason='compression' WHERE id='child'")
        conn.execute("INSERT INTO messages SELECT 8, 'tip', role, '下一次摘要', timestamp + 40, 1, 0, 1, NULL FROM messages WHERE id=1")
        conn.execute("INSERT INTO messages SELECT 9, 'tip', role, content, timestamp, 1, 0, 0, NULL FROM messages WHERE id=7")
    assert capture(reader, outbox, stream="hermes:main", convert=adapter.to_events)["inserted"] == 1
    assert outbox.stats()["total"] == 4
    assert outbox.cursor("hermes:main") == "9"
    records, _ = reader.read_since("hermes:main", "3")
    assert [x.native_event_id for x, _ in records] == ["msg:2", "msg:3", "msg:7"]
    assert records[0][0].locator == "hermes://main/root/messages/2"
    assert "session:root" not in records[0][0].aliases
    assert records[0][0].event_time == reader.read_since("hermes:main", None)[0][1][0].event_time


def test_in_place_compaction_uses_archived_original_tail(tmp_path, clock, redactor, outbox):
    now = clock.now.timestamp() - 500
    db = make_hermes_db(tmp_path / "state.db", [("session:root", None, None)], [
        {"id": 1, "text": "同一句", "timestamp": now},
        {"id": 2, "text": "同一句", "timestamp": now},
    ])
    reader = hermes_reader(db, clock=clock);adapter = hermes_adapter(redactor)
    assert capture(reader, outbox, stream="hermes:main", convert=adapter.to_events)["inserted"] == 2
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE messages SET active=0, compacted=0")
        conn.execute("INSERT INTO messages SELECT 3, session_id, role, '摘要', timestamp + 5, 1, 0, 1, NULL FROM messages WHERE id=1")
        conn.execute("INSERT INTO messages SELECT id+3, session_id, role, content, timestamp, 1, 0, 0, NULL FROM messages WHERE id IN (1,2)")
        conn.execute("INSERT INTO messages SELECT 6, session_id, role, content, timestamp+20, 1, 0, 0, NULL FROM messages WHERE id=1")
    assert capture(reader, outbox, stream="hermes:main", convert=adapter.to_events)["inserted"] == 1
    records, _ = reader.read_since("hermes:main", "2")
    assert [x.native_event_id for x, _ in records] == ["msg:1", "msg:2", "msg:6"]
    assert outbox.stats()["total"] == 3


def test_explicit_native_fork_and_other_sessions_stay_distinct(tmp_path, clock):
    now = clock.now.timestamp()-500
    db = make_hermes_db(tmp_path / "state.db", [("root", None, "compression"), ("branch", "root", None), ("other", None, None)], [
        {"id": 1, "session_id": "root", "text": "同一句", "timestamp": now},
        {"id": 2, "session_id": "branch", "text": "同一句", "timestamp": now},
        {"id": 3, "session_id": "other", "text": "同一句", "timestamp": now},
    ])
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE sessions ADD COLUMN model_config TEXT")
        conn.execute("UPDATE sessions SET model_config=? WHERE id='branch'", (json.dumps({"_branched_from":"root"}),))
    records, _ = hermes_reader(db, clock=clock).read_since("hermes:main", None)
    assert [(v.native_event_id,v.session_id) for v,_ in records] == [("msg:1","root"),("msg:2","branch"),("msg:3","other")]
