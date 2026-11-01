"""T03: delivery to KW over UDS, retries, lost-response replay, terminal errors."""
from __future__ import annotations

import time

from dma.delivery import DeliveryClient, deliver_once

from .conftest import FakeKwServer, accept_all, build_events


def _capture(outbox, count=2):
    events = [event for index in range(count) for event in build_events(native_id=f"message:{index}", text=f"记录{index}")]
    outbox.capture(events, stream="hermes:main", position=str(count))
    return events


def test_delivery_success_and_idempotent_noop(outbox):
    events = _capture(outbox)
    with FakeKwServer(accept_all) as kw:
        client = DeliveryClient(kw.socket_path, client_id="client:test", timeout=2.0)
        result = deliver_once(outbox, client)
        assert result["batches"] == 1
        assert result["accepted"] == len(events)
        stats = outbox.stats()
        assert stats["by_state"]["delivered"] == len(events)
        assert stats["remote"]["backend_instance_id"] == "kw:stub"
        assert stats["remote"]["watermark"] == 1
        assert len(kw.requests) == 1
        # Nothing pending any more: no further request.
        again = deliver_once(outbox, client)
        assert again["batches"] == 0
        assert len(kw.requests) == 1


def test_transient_server_error_retries_then_succeeds(outbox, clock):
    _capture(outbox, 1)

    def fail_once(batch):
        return 503, {
            "error": {
                "code": "STORE_UNAVAILABLE",
                "message": "store unavailable",
                "retryable": True,
                "request_id": "req:1",
            }
        }

    with FakeKwServer(fail_once) as kw:
        client = DeliveryClient(kw.socket_path, client_id="client:test", timeout=2.0)
        result = deliver_once(outbox, client)
        assert result["retried"] == 1
        assert outbox.stats()["by_state"]["retry_wait"] == 1
        clock.advance(10)
        kw.server.handler = accept_all  # type: ignore[attr-defined]
        result = deliver_once(outbox, client)
        assert result["accepted"] == 1
        assert outbox.stats()["by_state"]["delivered"] == 1


def test_permanent_server_error_is_terminal(outbox):
    _capture(outbox, 1)

    def reject(batch):
        return 400, {
            "error": {
                "code": "UNSUPPORTED_SCHEMA",
                "message": "unsupported",
                "retryable": False,
                "request_id": "req:2",
            }
        }

    with FakeKwServer(reject) as kw:
        client = DeliveryClient(kw.socket_path, client_id="client:test", timeout=2.0)
        result = deliver_once(outbox, client)
        assert result["failed"] == 1
        assert outbox.stats()["by_state"]["failed"] == 1


def test_parent_pending_retries(outbox, clock):
    _capture(outbox, 1)

    def pending(batch):
        return 409, {
            "error": {
                "code": "PARENT_PENDING",
                "message": "parent pending",
                "retryable": True,
                "request_id": "req:3",
            }
        }

    with FakeKwServer(pending) as kw:
        client = DeliveryClient(kw.socket_path, client_id="client:test", timeout=2.0)
        deliver_once(outbox, client)
        assert outbox.stats()["by_state"]["retry_wait"] == 1
        clock.advance(10)
        kw.server.handler = accept_all  # type: ignore[attr-defined]
        deliver_once(outbox, client)
        assert outbox.stats()["by_state"]["delivered"] == 1


def test_missing_socket_keeps_payload_for_retry(outbox, tmp_path):
    _capture(outbox, 1)
    client = DeliveryClient(tmp_path / "missing" / "kw.sock", client_id="client:test", timeout=0.5)
    result = deliver_once(outbox, client)
    assert result["retried"] == 1
    assert outbox.stats()["by_state"]["retry_wait"] == 1


def test_lost_response_replay_returns_duplicate(outbox, clock):
    _capture(outbox, 1)

    def slow_then_duplicate(batch):
        if not getattr(slow_then_duplicate, "called", False):
            slow_then_duplicate.called = True  # type: ignore[attr-defined]
            time.sleep(0.6)  # outlive the client timeout: response is lost
            return accept_all(batch)
        receipts = []
        for event in batch["events"]:
            body = event["body"]
            receipts.append(
                {
                    "event_id": event["event_id"],
                    "event_revision": event["event_revision"],
                    "part_index": body["part_index"],
                    "disposition": "duplicate",
                    "durable_remote": True,
                    "processing": "pending",
                    "searchability": "none",
                    "error_code": None,
                    "receipt_id": "rcpt:" + "d" * 32,
                }
            )
        return 200, {
            "schema_version": "1.0",
            "backend_instance_id": "kw:stub",
            "restore_epoch": "epoch:1",
            "watermark": 2,
            "receipts": receipts,
        }

    with FakeKwServer(slow_then_duplicate) as kw:
        client = DeliveryClient(kw.socket_path, client_id="client:test", timeout=0.2)
        first = deliver_once(outbox, client)
        assert first["retried"] == 1
        clock.advance(10)
        second = deliver_once(outbox, client)
        assert second["duplicate"] == 1
        assert outbox.stats()["by_state"]["delivered"] == 1
