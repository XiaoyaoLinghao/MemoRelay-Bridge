"""T09: read-only Hermes capture, logical sessions, cursor and persistence."""
from __future__ import annotations

import json

import pytest

from dma import contracts
from dma.adapters.hermes.reader import SourceReadError, checkpoint_before_compression
from dma.pipeline import capture

from .conftest import (
    INSTANCE_HERMES,
    WORKSPACE,
    hermes_adapter,
    hermes_reader,
    make_hermes_db,
)


def _default_db(tmp_path, clock, *, extra_messages=()):
    now = clock.now.timestamp()
    messages = [
        {"id": 1, "session_id": "session:child", "role": "user", "text": "松塔项目端口改为4200", "timestamp": now - 500},
        {"id": 2, "session_id": "session:child", "role": "assistant", "text": "已记录端口4200", "timestamp": now - 400},
        {"id": 3, "session_id": "session:child", "role": "tool", "text": "exit code 0: listener on 4200", "timestamp": now - 300},
        {"id": 4, "session_id": "session:child", "role": "system", "text": "system prompt", "timestamp": now - 250},
        {"id": 5, "session_id": "session:child", "role": "assistant", "text": "hidden scaffolding", "timestamp": now - 240, "display_kind": "hidden"},
        {"id": 6, "session_id": "session:child", "role": "assistant", "text": "compressed summary", "timestamp": now - 230, "summary": 1},
        {"id": 7, "session_id": "session:child", "role": "user", "text": "inactive scratch", "timestamp": now - 220, "active": 0},
        {"id": 8, "session_id": "session:child", "role": "user", "text": "compacted original user text", "timestamp": now - 210, "active": 0, "compacted": 1},
        *extra_messages,
    ]
    sessions = [("session:root", None, "compression"), ("session:child", "session:root", None)]
    return make_hermes_db(tmp_path / "state.db", sessions, messages)


def test_reads_allowed_rows_with_logical_session_and_aliases(tmp_path, clock):
    reader = hermes_reader(_default_db(tmp_path, clock), clock=clock)
    pairs, final = reader.read_since("hermes:main", None)
    records = [record for record, _position in pairs]
    assert final == "8"
    assert [record.native_event_id for record in records] == ["msg:1", "msg:2", "msg:3", "msg:8"]
    by_id = {record.native_event_id: record for record in records}
    assert by_id["msg:1"].role == "user" and by_id["msg:1"].origin_kind == "user_input"
    assert by_id["msg:2"].origin_kind == "assistant_reply"
    assert by_id["msg:3"].origin_kind == "tool_execution"
    assert by_id["msg:1"].session_id == "session:root"  # compression lineage joined
    assert "session:session:root" in by_id["msg:1"].aliases
    assert by_id["msg:8"].aliases  # compacted original keeps content alias
    assert by_id["msg:1"].locator.startswith("hermes://main/")


def test_second_read_is_incremental_and_settle_window_is_not_skipped(tmp_path, clock):
    db = _default_db(tmp_path, clock)
    reader = hermes_reader(db, settle_seconds=1000, clock=clock)
    pairs, final = reader.read_since("hermes:main", None)
    assert pairs == []  # everything is inside the settle window
    assert final == "0"
    clock.advance(2000)
    pairs, final = reader.read_since("hermes:main", final)
    records = [record for record, _position in pairs]
    assert len(records) == 4
    assert final == "8"
    # Append one newer message: only it is returned.
    import sqlite3

    connection = sqlite3.connect(db)
    connection.execute(
        "INSERT INTO messages (id, session_id, role, content, timestamp, active, compacted, "
        "_compressed_summary, display_kind) VALUES (9, 'session:child', 'user', ?, ?, 1, 0, 0, NULL)",
        (json.dumps("后续消息", ensure_ascii=False), clock.now.timestamp() - 2000),
    )
    connection.commit()
    connection.close()
    pairs, final = reader.read_since("hermes:main", final)
    assert [record.native_event_id for record, _position in pairs] == ["msg:9"]
    assert final == "9"


def test_unknown_schema_is_refused(tmp_path):
    import sqlite3

    path = tmp_path / "bad.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT)")
    connection.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()
    reader = hermes_reader(path)
    with pytest.raises(SourceReadError):
        reader.read_since("hermes:main", None)


def test_adapter_produces_redacted_contract_events(tmp_path, clock, redactor, outbox):
    now = clock.now.timestamp()
    db = make_hermes_db(
        tmp_path / "state.db",
        [("session:root", None, None)],
        [
            {"id": 1, "text": "数据库密码: CANARY-PW-7f3a，端口改为4200", "timestamp": now - 100},
            {"id": 2, "role": "tool", "text": "curl exit 0; token sk-CANARY-API-91b2c3d4e5f6g7h8", "timestamp": now - 90},
        ],
    )
    reader = hermes_reader(db, clock=clock)
    adapter = hermes_adapter(redactor)
    result = capture(reader, outbox, stream="hermes:main", convert=adapter.to_events)
    assert result["inserted"] == 2
    raw = outbox.db_path.read_bytes()
    assert b"CANARY-PW-7f3a" not in raw
    assert b"CANARY-API-91b2c3d4e5f6g7h8" not in raw
    with outbox.read() as conn:
        payloads = [
            json.loads(row["payload_json"])
            for row in conn.execute("SELECT payload_json FROM outbox ORDER BY event_id").fetchall()
        ]
    from dma.validation import validate_event

    for event in payloads:
        assert validate_event(event) == []
        assert event["source"]["host"] == "hermes"
        assert event["source"]["instance_id"] == INSTANCE_HERMES
        assert event["workspace_id"] == WORKSPACE
        assert event["redaction"]["status"] == "redacted"
    assert any(event["source"]["role"] == "tool" for event in payloads)


def test_checkpoint_before_compression_persists_durable_rows(tmp_path, clock, redactor, outbox):
    db = _default_db(tmp_path, clock)
    reader = hermes_reader(db, clock=clock)
    adapter = hermes_adapter(redactor)
    result = checkpoint_before_compression(reader, adapter, outbox)
    assert result["inserted"] == 4
    assert outbox.cursor("hermes:main") == "8"
    # A repeated pre-compression checkpoint is idempotent and never duplicates rows.
    again = checkpoint_before_compression(reader, adapter, outbox)
    assert again["inserted"] == 0
    assert outbox.stats()["total"] == 4


def test_withheld_private_key_never_enters_outbox(tmp_path, clock, redactor, outbox):
    now = clock.now.timestamp()
    key = "-----BEGIN PRIVATE KEY-----\nCANARY-PRIVATE-KEY\n-----END PRIVATE KEY-----"
    db = make_hermes_db(
        tmp_path / "state.db",
        [("session:root", None, None)],
        [{"id": 1, "text": key, "timestamp": now - 100}],
    )
    reader = hermes_reader(db, clock=clock)
    adapter = hermes_adapter(redactor)
    capture(reader, outbox, stream="hermes:main", convert=adapter.to_events)
    assert b"CANARY-PRIVATE-KEY" not in outbox.db_path.read_bytes()
    with outbox.read() as conn:
        payload = json.loads(conn.execute("SELECT payload_json FROM outbox").fetchone()["payload_json"])
    assert payload["redaction"]["status"] == "withheld"
    assert payload["body"]["text"] == ""
