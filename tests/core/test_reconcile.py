"""AC-46: restore-epoch reconciliation and unrecoverable-gap reporting."""
from __future__ import annotations

import json

from dma.ids import EMPTY_BODY_SHA256
from dma.outbox import Outbox
from dma.reconcile import reconcile_after_restore

from .conftest import build_events


class FakeKwClient:
    def __init__(self, *, backend="kw:restored", epoch="epoch:new", watermark=3):
        self.health_payload = {
            "schema_version": "1.0",
            "status": "ready",
            "backend_instance_id": backend,
            "restore_epoch": epoch,
            "watermark": watermark,
            "supported_contracts": ["1.0"],
            "database_integrity": "ok",
        }
        self.posts = 0

    def health(self):
        return self.health_payload

    def post_batch(self, batch):
        self.posts += 1
        receipts = []
        for event in batch["events"]:
            body = event["body"]
            receipts.append(
                {
                    "event_id": event["event_id"],
                    "event_revision": event["event_revision"],
                    "part_index": body["part_index"],
                    "disposition": "accepted",
                    "durable_remote": True,
                    "processing": "pending",
                    "searchability": "none",
                    "error_code": None,
                    "receipt_id": "rcpt:" + "r" * 32,
                }
            )
        self.health_payload["watermark"] += len(receipts)
        return 200, {
            "schema_version": "1.0",
            "backend_instance_id": self.health_payload["backend_instance_id"],
            "restore_epoch": self.health_payload["restore_epoch"],
            "watermark": self.health_payload["watermark"],
            "receipts": receipts,
        }


def _delivered(outbox, events, *, watermark, epoch="epoch:old", backend="kw:old"):
    outbox.capture(events, stream="stream:1", position=str(watermark))
    for event in events:
        body = event["body"]
        outbox.apply_receipt(
            {
                "event_id": event["event_id"],
                "event_revision": event["event_revision"],
                "part_index": body["part_index"],
                "disposition": "accepted",
                "durable_remote": True,
                "processing": "pending",
                "searchability": "none",
                "error_code": None,
                "receipt_id": "rcpt:" + "a" * 32,
            },
            watermark=watermark,
            restore_epoch=epoch,
        )
    outbox.set_remote_meta(backend_instance_id=backend, restore_epoch=epoch, watermark=watermark)


def test_epoch_change_requeues_rows_above_restored_watermark(outbox):
    events = [event for index in range(2) for event in build_events(native_id=f"message:{index}")]
    _delivered(outbox, events, watermark=5)
    client = FakeKwClient(watermark=3)
    report = reconcile_after_restore(outbox, client)
    assert report["changed"] is True
    assert report["previous"]["restore_epoch"] == "epoch:old"
    assert report["current"]["restore_epoch"] == "epoch:new"
    assert report["requeued"] == 2
    assert report["gaps"] == []
    assert report["delivery"]["accepted"] == 2
    assert client.posts == 1
    stats = outbox.stats()
    assert stats["by_state"]["delivered"] == 2
    assert stats["remote"]["restore_epoch"] == "epoch:new"
    # A second pass sees no change and re-sends nothing.
    again = reconcile_after_restore(outbox, client)
    assert again["changed"] is False
    assert again["requeued"] == 0
    assert client.posts == 1


def test_rows_acked_in_an_old_epoch_are_replayed_even_below_restored_watermark(outbox):
    """F15 (round 3): the global watermark is not a trusted snapshot baseline.

    Another client can advance the restored backend watermark before the
    reconciler runs, so an old-epoch acknowledgement at or below the *observed*
    watermark may still have been lost by the restore. Uniform ack-epoch replay
    is the safe direction and KW dedupes rows the snapshot already has.

    This replaces the previous ``requeued == 0 / posts == 0`` assertion, which
    encoded the unsafe "skip everything at/below the watermark" behaviour Codex
    round 2 reported as F15. The replacement is strictly stronger: it requires
    the replay instead of forbidding it.
    """
    events = [event for index in range(2) for event in build_events(native_id=f"message:{index}")]
    _delivered(outbox, events, watermark=5)
    client = FakeKwClient(watermark=5)
    report = reconcile_after_restore(outbox, client)
    assert report["changed"] is True  # epoch still differs
    assert report["requeue_basis"] == "ack_epoch_mismatch"
    assert report["replayed_rows"] == 2
    assert report["requeued"] == 2
    assert client.posts == 1
    again = reconcile_after_restore(outbox, client)
    assert again["changed"] is False
    assert again["requeued"] == 0


def test_unavailable_body_is_reported_as_gap_not_silently_dropped(outbox):
    events = [event for index in range(2) for event in build_events(native_id=f"message:{index}")]
    _delivered(outbox, events, watermark=5)
    # Simulate a purged body: minimal envelope without readable redacted text.
    minimal = {
        "schema_version": "1.0",
        "event_id": events[0]["event_id"],
        "event_revision": 1,
        "workspace_id": events[0]["workspace_id"],
        "operation": "create",
        "source": events[0]["source"],
        "event_time": events[0]["event_time"],
        "body": {
            "text": "", "sha256": EMPTY_BODY_SHA256, "part_index": 0, "part_count": 1,
            "content_revision": "native:purged", "format": "text/plain",
        },
        "redaction": {"policy_version": "redact:1", "status": "passed", "replacements": 0, "credential_refs": []},
        "project_ids": events[0]["project_ids"],
        "parent_revision": None,
        "derived_from": [],
    }
    with outbox.read() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE outbox SET payload_json = ? WHERE event_id = ?",
            (json.dumps(minimal, ensure_ascii=False), events[0]["event_id"]),
        )
        conn.execute("COMMIT")
    client = FakeKwClient(watermark=3)
    report = reconcile_after_restore(outbox, client)
    assert len(report["gaps"]) == 1
    assert report["gaps"][0]["event_id"] == events[0]["event_id"]
    assert "native" in report["gaps"][0]["reason"] or report["gaps"][0]["reason"]
    assert report["requeued"] == 1  # the reusable row is still replayed
    assert report["delivery"]["accepted"] == 1
