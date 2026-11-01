"""AC-51/SEC-01: REDACTION_REQUIRED -> purge outbox body -> reread native source -> retry."""
from __future__ import annotations

import hashlib
import json

from dma import contracts
from dma.event import build_part_event
from dma.ids import body_digest, canonical_event_id
from dma.outbox import Outbox
from dma.redaction_recovery import reread_after_redaction

from tests.adapters.conftest import hermes_adapter, hermes_reader, make_hermes_db
from tests.core.conftest import FakeKwServer, accept_all, build_events


def _withheld_redaction(batch):
    receipts = []
    for event in batch.get("events", []):
        body = event.get("body") or {}
        receipts.append(
            {
                "event_id": event["event_id"],
                "event_revision": event["event_revision"],
                "part_index": body.get("part_index", 0),
                "disposition": "withheld",
                "durable_remote": True,
                "processing": "not_applicable",
                "searchability": "none",
                "error_code": "REDACTION_REQUIRED",
                "receipt_id": "rcpt:" + "w" * 32,
            }
        )
    return 200, {
        "schema_version": "1.0",
        "backend_instance_id": "kw:stub",
        "restore_epoch": "epoch:1",
        "watermark": 1,
        "receipts": receipts,
    }


def _raw_secret_event(*, native_id="msg:1", text="数据库密码: CANARY-PW-7f3a"):
    source = {
        "instance_id": "instance:hermes-test",
        "host": "hermes",
        "actor_id": "jun-test",
        "session_id": "session:test",
        "native_event_id": native_id,
        "native_schema": "hermes-snapshot-20261030",
        "role": "user",
        "origin_kind": "user_input",
        "locator": f"hermes://session:test/{native_id}",
    }
    event = build_part_event(
        workspace_id="owner:demo",
        source=source,
        event_time={"value": "2026-10-30T01:00:00Z", "precision": "instant", "timezone": "UTC"},
        text="placeholder",
        part_index=0,
        part_count=1,
        content_revision="native:1",
        redaction={"policy_version": "redact:1", "status": "passed", "replacements": 0, "credential_refs": []},
        project_ids=("project:songta",),
    )
    # Simulate an adapter bug: raw secret left in the queued payload.
    event["body"]["text"] = text
    event["body"]["sha256"] = body_digest(text)
    return event


def _insert_raw(outbox, event):
    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    with outbox.read() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO outbox (event_id, revision, part_index, payload_json, payload_sha256, "
            "state, attempts, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'pending', 0, "
            "'2026-10-30T00:00:00Z', '2026-10-30T00:00:00Z')",
            (
                event["event_id"],
                event["event_revision"],
                event["body"]["part_index"],
                payload,
                hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            ),
        )
        conn.execute("COMMIT")


def test_redaction_required_purges_outbox_then_reread_recovers(tmp_path, clock, redactor, outbox):
    db = make_hermes_db(
        tmp_path / "state.db",
        [("session:root", None, None)],
        [{"id": 1, "text": "数据库密码: CANARY-PW-7f3a，端口改为4200", "timestamp": clock.now.timestamp() - 100}],
    )
    reader = hermes_reader(db, clock=clock)
    adapter = hermes_adapter(redactor)
    _insert_raw(outbox, _raw_secret_event())
    from dma.delivery import DeliveryClient, deliver_once

    with FakeKwServer(_withheld_redaction) as kw:
        client = DeliveryClient(kw.socket_path, client_id="client:test", timeout=2.0)
        result = deliver_once(outbox, client)
        assert result["redaction_required"] == 1
    row = outbox.read()
    with outbox.read() as conn:
        stored = conn.execute("SELECT * FROM outbox").fetchone()
    assert stored["state"] == "redaction_required"
    assert stored["last_error_code"] == "REDACTION_REQUIRED"
    # Suspect body is purged from the outbox database.
    assert b"CANARY-PW-7f3a" not in outbox.db_path.read_bytes()
    assert json.loads(stored["payload_json"])["body"]["text"] == ""

    report = reread_after_redaction(outbox, [(reader, adapter)])
    # Re-read/requeue is not a recovery until KW durably accepts it; a bare
    # re-read stays visible as requeued (F03: no optimistic recovered counter).
    assert report["requeued"] == 1
    assert report["recovered"] == 0
    assert report["gaps"] == []
    with outbox.read() as conn:
        recovered = conn.execute("SELECT * FROM outbox").fetchone()
    assert recovered["state"] == "pending"
    payload = json.loads(recovered["payload_json"])
    assert "CANARY-PW-7f3a" not in payload["body"]["text"]
    assert "[REDACTED:PASSWORD_ASSIGNMENT]" in payload["body"]["text"]
    # Retry now reaches the normal accepted path.
    with FakeKwServer(accept_all) as kw:
        client = DeliveryClient(kw.socket_path, client_id="client:test", timeout=2.0)
        retried = deliver_once(outbox, client)
    assert retried["accepted"] == 1
    assert outbox.stats()["by_state"]["delivered"] == 1


def test_unredactable_private_key_becomes_receipt_only_withheld(tmp_path, clock, redactor, outbox):
    key = "-----BEGIN PRIVATE KEY-----\nCANARY-PRIVATE-KEY\n-----END PRIVATE KEY-----"
    db = make_hermes_db(
        tmp_path / "state.db",
        [("session:root", None, None)],
        [{"id": 1, "text": key, "timestamp": clock.now.timestamp() - 100}],
    )
    reader = hermes_reader(db, clock=clock)
    adapter = hermes_adapter(redactor)
    _insert_raw(outbox, _raw_secret_event(text="密码: CANARY-OTHER-1"))
    from dma.delivery import DeliveryClient, deliver_once

    with FakeKwServer(_withheld_redaction) as kw:
        deliver_once(outbox, DeliveryClient(kw.socket_path, client_id="client:test", timeout=2.0))
    report = reread_after_redaction(outbox, [(reader, adapter)])
    assert report["requeued"] == 1 and report["withheld"] == 1
    assert report["recovered"] == 0  # not delivered yet
    with outbox.read() as conn:
        payload = json.loads(conn.execute("SELECT payload_json FROM outbox").fetchone()["payload_json"])
    assert payload["redaction"]["status"] == "withheld"
    assert payload["body"]["text"] == ""
    assert b"CANARY-PRIVATE-KEY" not in outbox.db_path.read_bytes()


def test_missing_native_source_is_reported_and_not_retried(tmp_path, clock, redactor, outbox):
    db = make_hermes_db(
        tmp_path / "state.db",
        [("session:root", None, None)],
        [{"id": 1, "text": "无关记录", "timestamp": clock.now.timestamp() - 100}],
    )
    reader = hermes_reader(db, clock=clock)
    adapter = hermes_adapter(redactor)
    event = _raw_secret_event(native_id="msg:404")
    _insert_raw(outbox, event)
    from dma.delivery import DeliveryClient, deliver_once

    with FakeKwServer(_withheld_redaction) as kw:
        deliver_once(outbox, DeliveryClient(kw.socket_path, client_id="client:test", timeout=2.0))
    report = reread_after_redaction(outbox, [(reader, adapter)])
    assert report["recovered"] == 0
    assert len(report["gaps"]) == 1
    assert report["gaps"][0]["native_event_id"] == "msg:404"
    with outbox.read() as conn:
        row = conn.execute("SELECT state, last_error_code FROM outbox").fetchone()
    assert row["state"] == "gap"
    assert "native_source_unavailable" in row["last_error_code"]
    assert outbox.pending_rows() == []
