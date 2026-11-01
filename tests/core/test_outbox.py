"""T03: durable outbox, cursor atomicity, retry state machine, batching."""
from __future__ import annotations

import pytest

from dma.errors import InvalidEventError
from dma.outbox import Outbox

from .conftest import build_events, make_source


def _events(count=2):
    return [
        event
        for index in range(count)
        for event in build_events(native_id=f"message:{index}", text=f"第{index}条工作记录")
    ]


def test_capture_stores_events_and_cursor_atomically(outbox):
    events = _events()
    result = outbox.capture(events, stream="hermes:main", position="42")
    assert result["inserted"] == len(events)
    reopened = Outbox(outbox.db_path, workspace_id="owner:demo", instance_id="instance:hermes-test")
    assert reopened.cursor("hermes:main") == "42"
    assert reopened.stats()["by_state"]["pending"] == len(events)
    payloads = [row["payload_json"] for row in reopened.pending_rows()]
    assert all('"event_id"' in payload for payload in payloads)


def test_capture_replay_is_idempotent(outbox):
    events = _events(1)
    first = outbox.capture(events, stream="hermes:main", position="1")
    second = outbox.capture(events, stream="hermes:main", position="1")
    assert first["inserted"] == len(events)
    assert second["inserted"] == 0
    assert second["duplicates"] == len(events)
    assert outbox.cursor("hermes:main") == "1"
    assert outbox.stats()["total"] == len(events)


def test_invalid_event_never_advances_cursor(outbox):
    bad = build_events()[0]
    bad["body"]["sha256"] = "0" * 64
    with pytest.raises(InvalidEventError):
        outbox.capture([bad], stream="hermes:main", position="9")
    assert outbox.cursor("hermes:main") is None
    assert outbox.stats()["total"] == 0


def test_crash_between_insert_and_commit_leaves_neither(outbox):
    class Crash(Exception):
        pass

    def crash():
        raise Crash("simulated crash before commit")

    with pytest.raises(Crash):
        outbox.capture(
            _events(1), stream="hermes:main", position="7", after_insert_hook=crash
        )
    assert outbox.cursor("hermes:main") is None
    assert outbox.stats()["total"] == 0


def test_build_batches_limits(outbox):
    events = [event for index in range(6) for event in build_events(native_id=f"message:{index}")]
    outbox.capture(events, stream="hermes:main", position="6")
    batches = outbox.build_batches(outbox.pending_rows(), max_events=2)
    assert len(batches) == len(events) // 2
    seen = []
    for batch, keys in batches:
        assert batch["schema_version"] == "1.0"
        assert batch["batch_id"].startswith("batch:")
        seen.extend(keys)
    assert len(seen) == len(set(seen)) == len(events)


def test_retry_backoff_and_terminal_failure(outbox, clock):
    events = _events(1)
    outbox.capture(events, stream="hermes:main", position="1")
    keys = [(row["event_id"], row["revision"], row["part_index"]) for row in outbox.pending_rows()]
    outbox.mark_delivering(keys)
    first = outbox.record_failure(keys, "STORE_UNAVAILABLE", retryable=True)
    assert first == {"retry_wait": 1, "failed": 0}
    # Next attempt is 10 s later; not yet eligible.
    assert outbox.pending_rows(now="2026-10-30T01:00:00Z") == []
    clock.advance(10)
    assert len(outbox.pending_rows()) == 1
    outbox.record_failure(keys, "STORE_UNAVAILABLE", retryable=True)
    clock.advance(60)
    outbox.record_failure(keys, "STORE_UNAVAILABLE", retryable=True)
    stats = outbox.stats()["by_state"]
    assert stats.get("failed") == 1
    assert outbox.pending_rows() == []


def test_permanent_error_is_terminal_immediately(outbox):
    events = _events(1)
    outbox.capture(events, stream="hermes:main", position="1")
    keys = [(row["event_id"], row["revision"], row["part_index"]) for row in outbox.pending_rows()]
    outbox.record_failure(keys, "SCOPE_DENIED", retryable=False)
    assert outbox.stats()["by_state"].get("failed") == 1


def test_apply_receipt_states(outbox):
    events = _events(2)
    outbox.capture(events, stream="hermes:main", position="1")
    rows = outbox.pending_rows()
    accepted = {
        "event_id": rows[0]["event_id"],
        "event_revision": rows[0]["revision"],
        "part_index": rows[0]["part_index"],
        "disposition": "accepted",
        "durable_remote": True,
        "processing": "pending",
        "searchability": "none",
        "error_code": None,
        "receipt_id": "rcpt:accepted",
    }
    conflict = {
        "event_id": rows[1]["event_id"],
        "event_revision": rows[1]["revision"],
        "part_index": rows[1]["part_index"],
        "disposition": "rejected",
        "durable_remote": False,
        "processing": "not_applicable",
        "searchability": "none",
        "error_code": "EVENT_CONFLICT",
        "receipt_id": "rcpt:conflict",
    }
    assert outbox.apply_receipt(accepted)["state"] == "delivered"
    assert outbox.apply_receipt(conflict)["state"] == "failed"
    stats = outbox.stats()["by_state"]
    assert stats["delivered"] == 1 and stats["failed"] == 1
    assert outbox.remote_meta()["backend_instance_id"] is None
    outbox.set_remote_meta(backend_instance_id="kw:stub", restore_epoch="epoch:1", watermark=3)
    assert outbox.remote_meta()["watermark"] == 3


def test_payload_is_stored_after_redaction(outbox, redactor):
    events = build_events(text="数据库密码: CANARY-PW-7f3a", redactor=redactor)
    outbox.capture(events, stream="hermes:main", position="1")
    raw = outbox.db_path.read_bytes()
    assert b"CANARY-PW-7f3a" not in raw
    with outbox.read() as conn:
        payload = conn.execute("SELECT payload_json FROM outbox").fetchone()["payload_json"]
    assert "CANARY-PW-7f3a" not in payload
    assert "[REDACTED:PASSWORD_ASSIGNMENT]" in payload
