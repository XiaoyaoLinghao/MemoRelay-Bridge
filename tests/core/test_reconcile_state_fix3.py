"""F15 round-4 regression: the restore state machine must stay honest.

Codex round-3 finding (P1) against the round-3 ack-epoch replay fix: the replay
itself was correct, but the *state machine* still claimed success while recovery
was incomplete.

1. ``test_F15_unverifiable_gap_stays_visible_on_second_reconcile``: one delivered
   legacy row had ``ack_restore_epoch = NULL``. The first reconcile reported
   ``ok=false`` and ``unverifiable_epoch_rows=1``; the next reconcile saw
   ``changed=false`` and returned ``ok=true`` although the same row was still
   unverifiable.
2. ``test_F15_large_replay_cannot_report_complete_with_pending_rows``: 2003
   old-epoch rows exceed the default ``pending_rows`` delivery window (2000).
   The reconciler replayed 2000, left 3 pending, still reported ``ok=true`` and
   cleared the restore marker.

The fixed contract exercised here:

* every pass (changed or not, delivering or not) re-derives the persistent
  unresolved state from the durable outbox: unverifiable legacy/unknown-epoch
  rows, unrecoverable gaps and an unfinished replay. ``changed=false`` can
  never turn the same unresolved gap into ``ok=true``;
* a replay is delivered in bounded rounds until it drains or stops making
  progress. Each round records its counters and the remaining queue; an
  unfinished replay keeps ``ok=false`` *and* the restore marker;
* a restore marker is cleared only when the replay it belongs to has actually
  drained; markers for other epochs stay pending;
* replay is idempotent per ack epoch: rows already acknowledged in the current
  epoch are never requeued again, failed rows keep their retry state and error,
  and unknown-epoch rows are still disclosed instead of mass-replayed;
* the CLI ``reconcile`` envelope/exit code follows the honest report (never a
  hard-coded success).

Tests use the real KW store + Unix-socket service, real snapshot export/import,
the real durable outbox and the real DeliveryClient; data is synthetic only.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

pytest.importorskip("knowledge_weaver")

from dma.delivery import DeliveryClient, deliver_once  # noqa: E402
from dma.outbox import Outbox  # noqa: E402
from dma.reconcile import reconcile_after_restore  # noqa: E402

from knowledge_weaver.portability import export_snapshot, import_snapshot  # noqa: E402
from knowledge_weaver.service import ServiceApp  # noqa: E402
from knowledge_weaver.service.client import ServiceClient  # noqa: E402
from knowledge_weaver.store import Store  # noqa: E402

from .test_reconcile_fix2 import (  # noqa: E402
    CLIENT_A,
    INSTANCE_A,
    STREAM,
    WORKSPACE,
    _deliverer,
    _events,
    _key,
    _new_store,
    _restored_after_other_client,
    _socket,
)

NOW = "2026-10-31T00:00:00Z"
AFTER_BACKOFF = "2026-10-31T00:10:00Z"


def _cli(capsys, argv):
    from dma.cli import main as cli_main

    code = cli_main(argv)
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]
    return code, json.loads("\n".join(lines)) if lines else {}


def _force_unknown_ack_epoch(outbox: Outbox, event: dict) -> None:
    """Simulate a legacy outbox row: delivered, but its ack epoch is unknown."""
    with outbox.read() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE outbox SET ack_restore_epoch = NULL WHERE event_id = ?",
            (event["event_id"],),
        )
        conn.execute("COMMIT")


def _patient_deliverer(app: ServiceApp) -> DeliveryClient:
    """DeliveryClient for the >2000-row counterexample; 100-event batches can be
    slow under a loaded machine, so allow more than the default 3s timeout."""
    return DeliveryClient(app.socket_path, client_id=CLIENT_A, timeout=15.0)


def _reconcile_cli_argv(outbox: Outbox, app: ServiceApp) -> list[str]:
    return [
        "reconcile",
        "--outbox",
        str(outbox.db_path),
        "--socket",
        str(app.socket_path),
        "--client",
        CLIENT_A,
        "--workspace",
        WORKSPACE,
        "--instance",
        INSTANCE_A,
    ]


# -- Codex round-3 counterexample 1: a persistent legacy gap cannot become ok --


def test_F15_unverifiable_legacy_row_stays_visible_across_reconciles(tmp_path):
    with _restored_after_other_client(tmp_path) as scenario:
        outbox = scenario["outbox"]
        lost = scenario["lost"][0]
        client = _deliverer(scenario["app"])
        _force_unknown_ack_epoch(outbox, lost)

        first = reconcile_after_restore(outbox, client)
        assert first["changed"] is True, first
        assert first["ok"] is False, first
        assert first["unverifiable_epoch_rows"] == 1, first

        # The next pass sees no epoch change, but the same unresolved gap is
        # still there: changed=false must not turn it into ok=true.
        second = reconcile_after_restore(outbox, client)
        assert second["changed"] is False, second
        assert second["ok"] is False, second
        assert second["unverifiable_epoch_rows"] == 1, second
        assert second["gaps"] == []
        assert second["replayed_rows"] == 0

        # A third pass stays honest as well; the gap does not "age out".
        third = reconcile_after_restore(outbox, client)
        assert third["changed"] is False, third
        assert third["ok"] is False, third
        assert third["unverifiable_epoch_rows"] == 1, third

        # The unknown-epoch row is disclosed, not silently replayed/dropped.
        assert outbox.states_for([_key(lost)])[_key(lost)] == "delivered"
        requeued = {tuple(key) for key in first["requeued_keys"]}
        assert _key(lost) not in requeued


# -- legacy outbox schema: durable replay state migrates in place ------------


def test_legacy_outbox_gains_round4_replay_state_columns_idempotently(tmp_path):
    """A pre-F15 outbox keeps working; replay/unverified state is added in place."""
    db_path = tmp_path / "dma" / "outbox.db"
    db_path.parent.mkdir(parents=True)
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(
            """
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE cursor (stream TEXT PRIMARY KEY, position TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE outbox (
                event_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                part_index INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT NULL,
                last_error_code TEXT NULL,
                receipt_json TEXT NULL,
                receipt_id TEXT NULL,
                remote_durable INTEGER NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (event_id, revision, part_index)
            );
            CREATE TABLE remote_meta (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                backend_instance_id TEXT NULL,
                restore_epoch TEXT NULL,
                watermark INTEGER NULL,
                updated_at TEXT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO outbox (event_id, revision, part_index, payload_json, payload_sha256, "
            "state, attempts, created_at, updated_at) VALUES (?, 1, 0, ?, ?, 'delivered', 1, ?, ?)",
            ("evt:" + "a" * 60, "{}", "0" * 64, "2026-10-30T00:00:00Z", "2026-10-30T00:00:00Z"),
        )
        connection.commit()
    finally:
        connection.close()

    outbox = Outbox(db_path, workspace_id=WORKSPACE, instance_id=INSTANCE_A)
    with outbox.read() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(outbox)")}
        row = conn.execute(
            "SELECT ack_restore_epoch, replay_epoch, ack_epoch_unverified FROM outbox "
            "WHERE event_id = ?",
            ("evt:" + "a" * 60,),
        ).fetchone()
    assert {
        "ack_watermark",
        "ack_restore_epoch",
        "replay_epoch",
        "ack_epoch_unverified",
    } <= columns
    # The legacy row stays honest: unknown epoch, not owed to any replay, not
    # silently upgraded to "verified".
    assert row["ack_restore_epoch"] is None
    assert row["replay_epoch"] is None
    assert row["ack_epoch_unverified"] == 0
    assert [r["event_id"] for r in outbox.delivered_rows_unknown_epoch()] == ["evt:" + "a" * 60]
    assert [r["event_id"] for r in outbox.unverifiable_rows()] == ["evt:" + "a" * 60]
    assert outbox.outstanding_replay_rows() == []
    # Re-opening is idempotent (no duplicate-column error, no data change).
    reopened = Outbox(db_path, workspace_id=WORKSPACE, instance_id=INSTANCE_A)
    assert reopened.stats()["total"] == 1
    assert [r["event_id"] for r in reopened.unverifiable_rows()] == ["evt:" + "a" * 60]


# -- Codex round-3 counterexample 2: 2003-row replay must not fake completion --


def test_F15_large_replay_across_pending_batches_completes_only_after_drain(tmp_path):
    live = _new_store(tmp_path / "kw-live" / "knowledge.db")
    outbox = Outbox(
        tmp_path / "dma" / "outbox.db", workspace_id=WORKSPACE, instance_id=INSTANCE_A
    )
    events: list[dict] = []
    app1 = ServiceApp(live, _socket("f15-large-live")).start()
    try:
        outbox.capture(_events("message:initial", "恢复前已投递的记忆"), stream=STREAM, position="1")
        assert deliver_once(outbox, _patient_deliverer(app1))["accepted"] == 1
        snapshot = tmp_path / "snapshot"
        export_snapshot(live, snapshot)
        # 2002 events > the default pending_rows delivery window (2000).
        events = [
            event
            for index in range(2002)
            for event in _events(f"message:large:{index}", f"恢复后丢失的数据{index}")
        ]
        for start in range(0, len(events), 100):
            outbox.capture(events[start : start + 100], stream=STREAM, position=str(start + 2))
        while outbox.stats()["by_state"].get("pending", 0):
            deliver_once(outbox, _patient_deliverer(app1))
    finally:
        app1.stop()
    assert len(events) == 2002
    assert outbox.stats()["by_state"].get("delivered", 0) == 2003

    imported = import_snapshot(snapshot, tmp_path / "kw-restored")
    restored = Store(imported["target_db"])
    app2 = ServiceApp(restored, _socket("f15-large-restored")).start()
    try:
        client = _patient_deliverer(app2)
        report = reconcile_after_restore(outbox, client)
        pending = outbox.stats()["by_state"].get("pending", 0)
        # Codex counterexample: claiming completion while rows are still pending
        # is exactly the lie this regression must prevent.
        assert pending == 0 or report["ok"] is False, {"pending": pending, "report": report}
        assert report["replayed_rows"] == 2003, report["replayed_rows"]
        assert report["ok"] is True, report
        assert report["unverifiable_epoch_rows"] == 0
        assert report["gaps"] == []
        replay = report["replay"]
        assert replay["complete"] is True, replay
        assert replay["stopped_reason"] == "drained", replay
        # More than one delivery round means the 2000-row window was continued.
        assert len(replay["rounds"]) >= 2, replay
        assert replay["rounds"][0]["remaining"].get("pending", 0) == 3, replay["rounds"][0]
        assert replay["outstanding_rows"] == 0, replay
        assert outbox.stats()["by_state"].get("delivered", 0) == 2003
        assert outbox.stats()["by_state"].get("pending", 0) == 0
        # Every row is acknowledged in the current restore epoch now.
        epoch = report["current"]["restore_epoch"]
        assert outbox.delivered_rows_epoch_mismatch(epoch) == []
        # The rows across the batch boundary are really back in the KW backend.
        reader = ServiceClient(app2.socket_path, client_id=CLIENT_A)
        for event in (events[0], events[-1]):
            status, _source = reader.get_source(event["event_id"], 1, 0)
            assert status == 200, {"status": status, "event_id": event["event_id"]}
        # Idempotent: the drained queue is not reset or replayed again.
        again = reconcile_after_restore(outbox, client)
        assert again["changed"] is False, again
        assert again["replayed_rows"] == 0, again
        assert again["requeued_keys"] == [], again
        assert again["replay"]["rounds"] == [], again
        assert again["ok"] is True, again
    finally:
        app2.stop()


# -- interruption / bounded rounds: the marker survives an unfinished replay --


def test_F15_interrupted_replay_keeps_marker_until_resumed_delivery_drains(tmp_path, capsys):
    with _restored_after_other_client(tmp_path, extra_after_snapshot=2, other_client_writes=1) as scenario:
        outbox = scenario["outbox"]
        # A normal delivery after the restore records the pending restore marker.
        fresh = _events("message:after-restore", "恢复后由本客户端投递的新记忆")
        outbox.capture(fresh, stream=STREAM, position="9")
        assert deliver_once(outbox, _deliverer(scenario["app"]))["accepted"] == 1
        assert outbox.pending_restore_change() is not None

        # First pass requeues but cannot deliver (operator used --no-deliver or a
        # crash happened after the durable requeue).
        first = reconcile_after_restore(outbox, _deliverer(scenario["app"]), deliver=False)
        assert first["changed"] is True, first
        assert first["replayed_rows"] == 3, first  # first + 2 lost rows
        assert first["ok"] is False, first
        assert first["replay"]["complete"] is False, first["replay"]
        assert first["replay"]["stopped_reason"] == "deliver_disabled", first["replay"]
        assert first["replay"]["outstanding_by_state"].get("pending", 0) == 3, first["replay"]
        assert first["remaining"].get("pending", 0) == 3, first["remaining"]
        # An unfinished replay must not clear the restore marker.
        assert first["pending_restore_change_after"] is not None, first
        assert outbox.pending_restore_change() is not None

        # The durable replay state lets the next pass resume and drain it.
        resumed = reconcile_after_restore(outbox, _deliverer(scenario["app"]))
        assert resumed["ok"] is True, resumed
        assert resumed["replay"]["complete"] is True, resumed["replay"]
        assert resumed["replay"]["stopped_reason"] == "drained", resumed["replay"]
        assert outbox.stats()["by_state"].get("pending", 0) == 0
        assert outbox.pending_restore_change() is None
        assert resumed["pending_restore_change_after"] is None
        with outbox.read() as conn:
            delivered = [
                dict(row)
                for row in conn.execute(
                    "SELECT ack_restore_epoch, replay_epoch FROM outbox WHERE state = 'delivered'"
                )
            ]
        assert delivered
        assert all(
            row["ack_restore_epoch"] == resumed["current"]["restore_epoch"]
            and row["replay_epoch"] is None
            for row in delivered
        ), delivered

        # The real CLI follows the honest report: a drained replay exits 0.
        code, payload = _cli(capsys, _reconcile_cli_argv(outbox, scenario["app"]))
        assert code == 0, payload
        assert payload["ok"] is True and payload["reconcile"]["ok"] is True
        assert payload["reconcile"]["replay"]["complete"] is True


# -- partial failures: durable retry state blocks completion until recovered --


class _FlakyDeliveryClient:
    """DeliveryClient wrapper that fails one batch of the first replay round."""

    def __init__(self, inner, *, fail_event_id: str, failures: int = 1) -> None:
        self.inner = inner
        self.fail_event_id = fail_event_id
        self.remaining_failures = failures

    def health(self) -> dict:
        return self.inner.health()

    def post_batch(self, batch: dict):
        if self.remaining_failures and any(
            event["event_id"] == self.fail_event_id for event in batch["events"]
        ):
            self.remaining_failures -= 1
            raise OSError("synthetic loopback delivery failure")
        return self.inner.post_batch(batch)


def test_F15_failed_replay_row_keeps_retry_state_and_blocks_completion(tmp_path):
    with _restored_after_other_client(tmp_path, extra_after_snapshot=2, other_client_writes=1) as scenario:
        outbox = scenario["outbox"]
        fresh = _events("message:after-restore", "恢复后由本客户端投递的新记忆")
        outbox.capture(fresh, stream=STREAM, position="9")
        assert deliver_once(outbox, _deliverer(scenario["app"]))["accepted"] == 1
        target = scenario["lost"][0]
        flaky = _FlakyDeliveryClient(
            _deliverer(scenario["app"]), fail_event_id=target["event_id"]
        )

        first = reconcile_after_restore(outbox, flaky, now=NOW)
        assert first["changed"] is True, first
        assert first["ok"] is False, first  # a failed replay is not success
        assert first["replay"]["complete"] is False, first["replay"]
        assert first["pending_restore_change_after"] is not None, first
        assert outbox.states_for([_key(target)])[_key(target)] == "retry_wait"
        with outbox.read() as conn:
            row = conn.execute(
                "SELECT attempts, last_error_code, replay_epoch FROM outbox "
                "WHERE event_id = ? AND revision = 1 AND part_index = 0",
                (target["event_id"],),
            ).fetchone()
        assert row["last_error_code"] == "STORE_UNAVAILABLE"
        assert row["attempts"] >= 1
        assert row["replay_epoch"] == first["current"]["restore_epoch"]

        # The bounded backoff has not elapsed: no progress is possible, so ok
        # stays false and the marker stays pending.
        stuck = reconcile_after_restore(outbox, _deliverer(scenario["app"]), now=NOW)
        assert stuck["ok"] is False, stuck
        assert outbox.states_for([_key(target)])[_key(target)] == "retry_wait"
        assert outbox.pending_restore_change() is not None

        # Once the backoff elapsed the durable retry state is retried to drain.
        recovered = reconcile_after_restore(
            outbox, _deliverer(scenario["app"]), now=AFTER_BACKOFF
        )
        assert recovered["ok"] is True, recovered
        assert recovered["replay"]["complete"] is True, recovered["replay"]
        assert outbox.states_for([_key(target)])[_key(target)] == "delivered"
        assert outbox.pending_restore_change() is None
        assert outbox.stats()["by_state"].get("retry_wait", 0) == 0


# -- only the drained epoch's marker is cleared --------------------------------


def test_F15_completion_clears_only_the_replayed_epoch_marker(tmp_path):
    with _restored_after_other_client(tmp_path, other_client_writes=1) as scenario:
        outbox = scenario["outbox"]
        fresh = _events("message:after-restore", "恢复后由本客户端投递的新记忆")
        outbox.capture(fresh, stream=STREAM, position="9")
        assert deliver_once(outbox, _deliverer(scenario["app"]))["accepted"] == 1
        with outbox.read() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO restore_changes (previous_backend_instance_id, "
                "previous_restore_epoch, previous_watermark, observed_backend_instance_id, "
                "observed_restore_epoch, detected_at, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "kw:ancient",
                    "epoch:ancient",
                    1,
                    "kw:other",
                    "epoch:other",
                    "2026-10-30T00:00:00Z",
                    "test",
                ),
            )
            conn.execute("COMMIT")

        report = reconcile_after_restore(outbox, _deliverer(scenario["app"]))
        assert report["ok"] is True, report
        assert report["replay"]["complete"] is True, report["replay"]
        pending = [change for change in outbox.restore_changes() if change["cleared_at"] is None]
        # The marker for the drained epoch is cleared; another epoch's stays.
        assert [change["observed_restore_epoch"] for change in pending] == ["epoch:other"], pending
        assert report["pending_restore_change_after"] is not None


# -- a legacy gap is resolved only by native re-read + re-delivery -------------


def test_F15_legacy_gap_is_resolved_only_by_native_reread_and_redelivery(tmp_path):
    with _restored_after_other_client(tmp_path, other_client_writes=1) as scenario:
        outbox = scenario["outbox"]
        lost = scenario["lost"][0]
        _force_unknown_ack_epoch(outbox, lost)

        first = reconcile_after_restore(outbox, _deliverer(scenario["app"]))
        assert first["ok"] is False and first["unverifiable_epoch_rows"] == 1, first
        assert _key(lost) not in {tuple(key) for key in first["requeued_keys"]}
        second = reconcile_after_restore(outbox, _deliverer(scenario["app"]))
        assert second["ok"] is False and second["unverifiable_epoch_rows"] == 1, second

        # Native re-read replaces the durable payload (same product path the
        # redaction recovery uses); the adapter then re-delivers it.
        outbox.replace_row_payload(_key(lost), [lost], state="pending")
        assert deliver_once(outbox, _deliverer(scenario["app"]))["accepted"] == 1
        repaired = reconcile_after_restore(outbox, _deliverer(scenario["app"]))
        assert repaired["ok"] is True, repaired
        assert repaired["unverifiable_epoch_rows"] == 0
        assert repaired["replay"]["complete"] is True


# -- real CLI: output and exit code mirror the persistent state ----------------

def test_reconcile_cli_reports_nonzero_while_legacy_gap_persists(tmp_path, capsys):
    with _restored_after_other_client(tmp_path, other_client_writes=1) as scenario:
        outbox = scenario["outbox"]
        lost = scenario["lost"][0]
        _force_unknown_ack_epoch(outbox, lost)
        assert reconcile_after_restore(outbox, _deliverer(scenario["app"]))["ok"] is False

        code, payload = _cli(capsys, _reconcile_cli_argv(outbox, scenario["app"]))
        assert code == 1, payload
        assert payload["ok"] is False, payload
        assert payload["reconcile"]["changed"] is False, payload["reconcile"]
        assert payload["reconcile"]["ok"] is False, payload["reconcile"]
        assert payload["reconcile"]["unverifiable_epoch_rows"] == 1, payload["reconcile"]
        assert payload["stats"]["by_state"].get("delivered", 0) == 2
