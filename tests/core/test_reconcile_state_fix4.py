"""F15 round-5 regression: native re-read must persist the recovery obligation.

Codex round-4 finding (P1) against the round-4 state machine: a legacy/unknown-ack
row was correctly reported as an unverifiable gap, but calling the real native
re-read entry (``Outbox.replace_row_payload``) to replace it with a ``pending``
row silently erased the unresolved responsibility. ``unverifiable_rows`` only
looked at ``delivered`` rows and ``outstanding_replay_rows`` only at
``replay_epoch``, so the next default reconcile returned ``changed=false,
ok=true, replay.complete=true``, the real CLI exited 0, and the backend ``GET
source`` still returned 404. Re-reading is not durable redelivery.

The fixed contract exercised here:

* ``replace_row_payload`` sets the durable ``outbox.recovery_owed`` flag when the
  replaced row was a delivered row with an unverifiable legacy/unknown ack epoch
  (``ack_restore_epoch IS NULL`` or ``ack_epoch_unverified = 1``) or a row in the
  durable ``gap`` state. The flag survives pending/retry_wait/failed/gap and a
  process restart;
* every reconcile pass re-derives that obligation from the durable outbox and
  reports it explicitly (``recovery_owed_rows`` count/keys/by_state + a
  limitation) with ``ok=false``. ``--no-deliver`` reports it too and does not
  reset attempts/replay_epoch/state; it is never silently auto-replayed (legacy
  rows are not mass-replayed and ordinary pending rows are not gaps);
* the obligation is cleared only when a real delivery persists a durable receipt
  naming the *current* restore epoch. Then reconcile reports ``ok=true`` /
  ``replay.complete=true`` and the backend ``GET source`` returns 200; the real
  CLI exit code follows the honest report;
* a pre-F15 outbox gains the ``recovery_owed`` column idempotently; legacy rows
  stay unverified (not silently upgraded) until an explicit native re-read +
  current-epoch delivery.

Tests use the real KW store + Unix-socket service, real snapshot export/import,
the real durable outbox, real DeliveryClient and the real CLI; data is synthetic.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

pytest.importorskip("knowledge_weaver")

from dma.delivery import deliver_once  # noqa: E402
from dma.outbox import Outbox  # noqa: E402
from dma.reconcile import reconcile_after_restore  # noqa: E402

from knowledge_weaver.service import ServiceApp  # noqa: E402

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

_ROW_FIELDS = (
    "state",
    "attempts",
    "next_attempt_at",
    "replay_epoch",
    "recovery_owed",
    "ack_restore_epoch",
    "ack_epoch_unverified",
    "last_error_code",
)


def _cli(capsys, argv):
    from dma.cli import main as cli_main

    code = cli_main(argv)
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]
    return code, json.loads("\n".join(lines)) if lines else {}


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


def _force_unknown_ack_epoch(outbox: Outbox, event: dict) -> None:
    """Simulate a legacy outbox row: delivered, but its ack epoch is unknown."""
    with outbox.read() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE outbox SET ack_restore_epoch = NULL WHERE event_id = ?",
            (event["event_id"],),
        )
        conn.execute("COMMIT")


def _row_snapshot(outbox: Outbox, key: tuple[str, int, int]) -> dict:
    with outbox.read() as conn:
        row = conn.execute(
            "SELECT state, attempts, next_attempt_at, replay_epoch, recovery_owed, "
            "ack_restore_epoch, ack_epoch_unverified, last_error_code FROM outbox "
            "WHERE event_id = ? AND revision = ? AND part_index = ?",
            key,
        ).fetchone()
    assert row is not None, key
    return {field: row[field] for field in _ROW_FIELDS}


def _fetch_row(outbox: Outbox, key: tuple[str, int, int]) -> dict:
    return _row_snapshot(outbox, key)


# -- Codex round-4 original counterexample + mode variants --------------------


@pytest.mark.parametrize(
    "mode",
    ["default", "no-deliver", "cli"],
    ids=["default", "no-deliver", "cli"],
)
def test_F15_R5_native_reread_gap_stays_owed_until_current_epoch_receipt(
    tmp_path, capsys, mode
):
    """Re-read to pending must not turn the reported gap into ok=true / exit 0."""
    with _restored_after_other_client(tmp_path) as scenario:
        outbox = scenario["outbox"]
        app = scenario["app"]
        reader = scenario["reader"]
        lost = scenario["lost"][0]
        key = _key(lost)

        _force_unknown_ack_epoch(outbox, lost)

        # 1. The legacy gap is honestly reported before the re-read.
        before = reconcile_after_restore(outbox, _deliverer(app))
        assert before["ok"] is False, before
        assert before["changed"] is True, before
        assert before["unverifiable_epoch_rows"] == 1, before
        assert before["recovery_owed_rows"] == 0, before
        assert reader.get_source(key[0], key[1], key[2])[0] == 404

        # 2. Real native re-read entry: the old delivered row is replaced by a
        #    fresh pending row. The recovery obligation must be durable.
        assert outbox.replace_row_payload(key, [lost], state="pending") == 1
        reread = _fetch_row(outbox, key)
        assert reread["state"] == "pending", reread
        assert reread["recovery_owed"] == 1, reread
        assert reread["ack_restore_epoch"] is None, reread
        assert reread["replay_epoch"] is None, reread
        assert reread["attempts"] == 0, reread
        # It is a pending row rather than a delivered one, but it is no longer
        # silently absent from the unresolved accounting.
        assert outbox.unverifiable_rows() == []
        assert [row["event_id"] for row in outbox.recovery_owed_rows()] == [key[0]]

        # 3. Default reconcile / --no-deliver / real CLI must all stay honest;
        #    none of them may auto-replay the row or reset its delivery state.
        if mode == "cli":
            code, payload = _cli(capsys, _reconcile_cli_argv(outbox, app))
            assert code == 1, payload
            assert payload["ok"] is False, payload
            report = payload["reconcile"]
        else:
            report = reconcile_after_restore(
                outbox, _deliverer(app), deliver=(mode == "default")
            )
        assert report["changed"] is False, report
        assert report["ok"] is False, report
        assert report["recovery_owed_rows"] == 1, report
        assert report["recovery_owed_keys"] == [[key[0], key[1], key[2]]], report
        assert report["recovery_owed_by_state"] == {"pending": 1}, report
        assert report["unverifiable_epoch_rows"] == 0, report
        assert report["gaps"] == [], report
        assert report["requeued"] == 0, report
        assert report["replayed_rows"] == 0, report
        assert report["delivery"] is None, report  # no auto-replay of the owed row
        assert report["requeue_basis"] == "none", report
        assert report["replay"]["complete"] is True, report["replay"]
        assert any(
            "recovery_owed" in line.lower() for line in report["limitations"]
        ), report["limitations"]
        # No silent replay: the row is untouched pending and the backend still
        # has no such source (the honest 404 matches ok=false).
        after_row = _fetch_row(outbox, key)
        assert after_row["state"] == "pending", after_row
        assert after_row["attempts"] == 0, after_row
        assert after_row["replay_epoch"] is None, after_row
        assert after_row["recovery_owed"] == 1, after_row
        assert reader.get_source(key[0], key[1], key[2])[0] == 404
        # A second pass cannot age the obligation out.
        if mode == "cli":
            code2, payload2 = _cli(capsys, _reconcile_cli_argv(outbox, app))
            assert code2 == 1, payload2
            assert payload2["reconcile"]["recovery_owed_rows"] == 1, payload2
        else:
            again = reconcile_after_restore(
                outbox, _deliverer(app), deliver=(mode == "default")
            )
            assert again["ok"] is False, again
            assert again["recovery_owed_rows"] == 1, again
            assert again["replayed_rows"] == 0, again

        # 4. Only a real delivery persisting a current-epoch receipt resolves it.
        delivered = deliver_once(outbox, _deliverer(app))
        assert delivered["accepted"] == 1, delivered
        resolved_row = _fetch_row(outbox, key)
        assert resolved_row["state"] == "delivered", resolved_row
        assert resolved_row["recovery_owed"] == 0, resolved_row
        assert resolved_row["ack_restore_epoch"] is not None, resolved_row

        repaired = reconcile_after_restore(outbox, _deliverer(app))
        assert repaired["changed"] is False, repaired
        assert repaired["ok"] is True, repaired
        assert repaired["recovery_owed_rows"] == 0, repaired
        assert repaired["unverifiable_epoch_rows"] == 0, repaired
        assert repaired["replay"]["complete"] is True, repaired["replay"]
        status, source = reader.get_source(key[0], key[1], key[2])
        assert status == 200, {"status": status, "key": key}
        assert source["text"] == "恢复后丢失的数据0", source


# -- cross-process restart + --no-deliver does not reset the row --------------


def test_F15_R5_recovery_owed_survives_new_outbox_instance_and_no_deliver(tmp_path):
    with _restored_after_other_client(tmp_path) as scenario:
        outbox = scenario["outbox"]
        app = scenario["app"]
        lost = scenario["lost"][0]
        key = _key(lost)
        _force_unknown_ack_epoch(outbox, lost)
        assert reconcile_after_restore(outbox, _deliverer(app))["ok"] is False

        assert outbox.replace_row_payload(key, [lost], state="pending") == 1
        # Re-reading an already-obligated pending row keeps the obligation
        # (another product re-read must not discharge it by itself).
        assert outbox.replace_row_payload(key, [lost], state="pending") == 1
        first = reconcile_after_restore(outbox, _deliverer(app), deliver=False)
        assert first["ok"] is False and first["recovery_owed_rows"] == 1, first
        untouched = _row_snapshot(outbox, key)

        # A brand-new Outbox instance stands for a fresh process: the obligated
        # row is re-derived from the durable outbox, not from memory.
        reopened = Outbox(
            outbox.db_path, workspace_id=WORKSPACE, instance_id=INSTANCE_A
        )
        assert [row["event_id"] for row in reopened.recovery_owed_rows()] == [key[0]]
        assert reopened.states_for([key])[key] == "pending"

        second = reconcile_after_restore(reopened, _deliverer(app), deliver=False)
        assert second["ok"] is False, second
        assert second["recovery_owed_rows"] == 1, second
        assert second["replayed_rows"] == 0, second
        assert second["requeue_basis"] == "none", second
        # --no-deliver must not reset state/attempts/replay epoch.
        assert _row_snapshot(reopened, key) == untouched
        assert second["recovery_owed_by_state"] == {"pending": 1}, second

        # An explicit delivery from the reopened instance obtains the current
        # epoch receipt and only then a later reconcile may report ok=true.
        assert deliver_once(reopened, _deliverer(app))["accepted"] == 1
        assert reopened.recovery_owed_rows() == []
        resolved = reconcile_after_restore(reopened, _deliverer(app))
        assert resolved["ok"] is True, resolved
        assert resolved["recovery_owed_rows"] == 0, resolved
        assert resolved["replay"]["complete"] is True, resolved["replay"]
        assert scenario["reader"].get_source(key[0], key[1], key[2])[0] == 200


# -- ordinary pending rows are not recovery gaps ------------------------------


def test_F15_R5_ordinary_pending_rows_are_not_reported_as_recovery_gaps(tmp_path):
    live = _new_store(tmp_path / "kw-live" / "knowledge.db")
    app = ServiceApp(live, _socket("fix4-ordinary")).start()
    try:
        outbox = Outbox(
            tmp_path / "dma" / "outbox.db", workspace_id=WORKSPACE, instance_id=INSTANCE_A
        )
        # Established outbox identity: no restore epoch change in this test.
        health = _deliverer(app).health()
        outbox.set_remote_meta(
            backend_instance_id=health["backend_instance_id"],
            restore_epoch=health["restore_epoch"],
            watermark=health["watermark"],
            source="test",
        )
        events = _events("message:ordinary-pending", "普通待投递记忆")
        outbox.capture(events, stream=STREAM, position="1")
        key = _key(events[0])
        assert outbox.stats()["by_state"].get("pending") == 1

        report = reconcile_after_restore(outbox, _deliverer(app))
        assert report["changed"] is False, report
        assert report["ok"] is True, report
        assert report["recovery_owed_rows"] == 0, report
        assert report["unverifiable_epoch_rows"] == 0, report
        assert report["gaps"] == [], report
        assert report["replayed_rows"] == 0 and report["requeued"] == 0, report
        assert report["delivery"] is None, report
        # The reconciler does not consume or rewrite the ordinary delivery queue.
        assert outbox.recovery_owed_rows() == []
        assert _fetch_row(outbox, key)["state"] == "pending"
        assert _fetch_row(outbox, key)["recovery_owed"] == 0
        assert outbox.stats()["by_state"].get("pending") == 1
    finally:
        app.stop()


# -- un-reread legacy rows: reported, never mass-replayed ---------------------


def test_F15_R5_unreread_legacy_rows_stay_unreplayed_until_explicit_recovery(tmp_path):
    with _restored_after_other_client(
        tmp_path, extra_after_snapshot=2, other_client_writes=1
    ) as scenario:
        outbox = scenario["outbox"]
        app = scenario["app"]
        reader = scenario["reader"]
        lost_reread = scenario["lost"][0]
        lost_untouched = scenario["lost"][1]
        for event in (lost_reread, lost_untouched):
            _force_unknown_ack_epoch(outbox, event)

        first = reconcile_after_restore(outbox, _deliverer(app))
        assert first["ok"] is False, first
        assert first["unverifiable_epoch_rows"] == 2, first
        first_requeued = {tuple(key) for key in first["requeued_keys"]}
        assert _key(lost_reread) not in first_requeued
        assert _key(lost_untouched) not in first_requeued
        for event in (lost_reread, lost_untouched):
            key = _key(event)
            assert reader.get_source(key[0], key[1], key[2])[0] == 404

        # Repeated passes: still reported, still not replayed, backend still 404.
        second = reconcile_after_restore(outbox, _deliverer(app))
        assert second["changed"] is False, second
        assert second["ok"] is False, second
        assert second["unverifiable_epoch_rows"] == 2, second
        assert second["requeued"] == 0 and second["replayed_rows"] == 0, second
        for event in (lost_reread, lost_untouched):
            key = _key(event)
            assert outbox.states_for([key])[key] == "delivered"
            assert reader.get_source(key[0], key[1], key[2])[0] == 404

        # Only the explicitly re-read row becomes a recovery obligation.
        assert outbox.replace_row_payload(_key(lost_reread), [lost_reread], state="pending") == 1
        owed = reconcile_after_restore(outbox, _deliverer(app), deliver=False)
        assert owed["ok"] is False, owed
        assert owed["recovery_owed_rows"] == 1, owed
        assert owed["recovery_owed_keys"] == [list(_key(lost_reread))], owed
        assert owed["unverifiable_epoch_rows"] == 1, owed  # the un-reread row
        assert owed["replayed_rows"] == 0, owed
        key_untouched = _key(lost_untouched)
        assert reader.get_source(key_untouched[0], key_untouched[1], key_untouched[2])[0] == 404

        # The real delivery fixes only that row; the un-reread legacy row is
        # still disclosed and still absent from the backend (never mass-replayed).
        assert deliver_once(outbox, _deliverer(app))["accepted"] == 1
        key_reread = _key(lost_reread)
        assert reader.get_source(key_reread[0], key_reread[1], key_reread[2])[0] == 200
        assert reader.get_source(key_untouched[0], key_untouched[1], key_untouched[2])[0] == 404

        resolved = reconcile_after_restore(outbox, _deliverer(app))
        assert resolved["ok"] is False, resolved  # legacy row still unverifiable
        assert resolved["recovery_owed_rows"] == 0, resolved
        assert resolved["unverifiable_epoch_rows"] == 1, resolved
        assert resolved["requeued"] == 0 and resolved["replayed_rows"] == 0, resolved


# -- gap rows: re-read keeps the obligation, delivery discharges it -----------


def test_F15_R5_gap_row_reread_keeps_obligation_until_delivered(tmp_path):
    with _restored_after_other_client(tmp_path, other_client_writes=1) as scenario:
        outbox = scenario["outbox"]
        app = scenario["app"]
        reader = scenario["reader"]
        lost = scenario["lost"][0]
        key = _key(lost)

        # Durable gap state (the reconciler writes the same state when a payload
        # cannot be replayed). The gap is reported by the product reconcile.
        outbox.mark_gap(key, "body_unavailable_requires_native_reread")
        gap_report = reconcile_after_restore(outbox, _deliverer(app))
        assert gap_report["ok"] is False, gap_report
        assert [gap["event_id"] for gap in gap_report["gaps"]] == [key[0]], gap_report
        assert gap_report["recovery_owed_rows"] == 0, gap_report

        # Native re-read of the gap row: the replacement pending row must carry
        # the obligation instead of making the gap vanish.
        assert outbox.replace_row_payload(key, [lost], state="pending") == 1
        reread = _fetch_row(outbox, key)
        assert reread["state"] == "pending", reread
        assert reread["recovery_owed"] == 1, reread

        # If the native source disappears on a later re-read, the obligation is
        # kept as a durable gap instead of deleting the only record of it.
        assert outbox.replace_row_payload(key, [], state="pending") == 0
        empty_reread = _fetch_row(outbox, key)
        assert empty_reread["state"] == "gap", empty_reread
        assert empty_reread["recovery_owed"] == 1, empty_reread
        assert empty_reread["last_error_code"] == "native_reread_returned_no_event", empty_reread
        still_owed = reconcile_after_restore(outbox, _deliverer(app), deliver=False)
        assert still_owed["ok"] is False, still_owed
        assert still_owed["recovery_owed_rows"] == 1, still_owed
        assert [gap["event_id"] for gap in still_owed["gaps"]] == [key[0]], still_owed
        # Re-read again once the source is back: still pending + obligated.
        assert outbox.replace_row_payload(key, [lost], state="pending") == 1
        assert _fetch_row(outbox, key)["recovery_owed"] == 1

        owed = reconcile_after_restore(outbox, _deliverer(app), deliver=False)
        assert owed["ok"] is False, owed
        assert owed["gaps"] == [], owed
        assert owed["recovery_owed_rows"] == 1, owed
        assert owed["recovery_owed_by_state"] == {"pending": 1}, owed
        assert reader.get_source(key[0], key[1], key[2])[0] == 404

        assert deliver_once(outbox, _deliverer(app))["accepted"] == 1
        resolved = reconcile_after_restore(outbox, _deliverer(app))
        assert resolved["ok"] is True, resolved
        assert resolved["recovery_owed_rows"] == 0, resolved
        assert resolved["gaps"] == [], resolved
        status, source = reader.get_source(key[0], key[1], key[2])
        assert status == 200, status
        assert source["text"] == "恢复后丢失的数据0", source


# -- obligation survives retry_wait/failed; re-read+delivery resolves ---------


class _FlakyDeliveryClient:
    """DeliveryClient wrapper that fails one batch containing a given event."""

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


def test_F15_R5_recovery_owed_survives_retry_and_failed_states(tmp_path):
    with _restored_after_other_client(tmp_path) as scenario:
        outbox = scenario["outbox"]
        app = scenario["app"]
        lost = scenario["lost"][0]
        key = _key(lost)
        _force_unknown_ack_epoch(outbox, lost)
        assert reconcile_after_restore(outbox, _deliverer(app))["ok"] is False
        assert outbox.replace_row_payload(key, [lost], state="pending") == 1

        flaky = _FlakyDeliveryClient(
            _deliverer(app), fail_event_id=key[0], failures=1
        )
        failed_attempt = deliver_once(outbox, flaky, now=NOW)
        assert failed_attempt["retried"] == 1, failed_attempt
        retry_row = _fetch_row(outbox, key)
        assert retry_row["state"] == "retry_wait", retry_row
        assert retry_row["attempts"] == 1, retry_row
        assert retry_row["recovery_owed"] == 1, retry_row

        # --no-deliver reports the obligation without resetting the durable
        # retry state (attempts/next_attempt_at/state stay exactly as they are).
        retry_snapshot = _row_snapshot(outbox, key)
        no_deliver = reconcile_after_restore(outbox, _deliverer(app), deliver=False)
        assert no_deliver["ok"] is False, no_deliver
        assert no_deliver["recovery_owed_rows"] == 1, no_deliver
        assert no_deliver["recovery_owed_by_state"] == {"retry_wait": 1}, no_deliver
        assert _row_snapshot(outbox, key) == retry_snapshot

        stuck = reconcile_after_restore(outbox, _deliverer(app))
        assert stuck["ok"] is False, stuck
        assert stuck["recovery_owed_rows"] == 1, stuck
        assert stuck["recovery_owed_by_state"] == {"retry_wait": 1}, stuck
        assert stuck["recovery_owed_keys"] == [[key[0], key[1], key[2]]], stuck

        # A terminal error must not hide the obligation either.
        outbox.record_failure([key], "SCOPE_DENIED", retryable=False, now=NOW)
        failed_row = _fetch_row(outbox, key)
        assert failed_row["state"] == "failed", failed_row
        assert failed_row["recovery_owed"] == 1, failed_row
        failed_report = reconcile_after_restore(outbox, _deliverer(app))
        assert failed_report["ok"] is False, failed_report
        assert failed_report["recovery_owed_by_state"] == {"failed": 1}, failed_report

        # Another native re-read keeps the durable obligation and requeues it.
        assert outbox.replace_row_payload(key, [lost], state="pending") == 1
        reread_row = _fetch_row(outbox, key)
        assert reread_row["state"] == "pending", reread_row
        assert reread_row["recovery_owed"] == 1, reread_row
        assert reread_row["attempts"] == 0, reread_row

        # Backoff has elapsed for the earlier retry; the real delivery succeeds
        # and only then the current-epoch receipt clears the obligation.
        assert deliver_once(outbox, _deliverer(app), now=AFTER_BACKOFF)["accepted"] == 1
        resolved = reconcile_after_restore(outbox, _deliverer(app))
        assert resolved["ok"] is True, resolved
        assert resolved["recovery_owed_rows"] == 0, resolved
        assert scenario["reader"].get_source(key[0], key[1], key[2])[0] == 200


# -- idempotent legacy-outbox schema migration --------------------------------


def test_F15_R5_legacy_outbox_gains_recovery_owed_column_idempotently(tmp_path):
    """A pre-F15 outbox migrates in place; legacy rows are not auto-upgraded."""
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

    legacy_key = ("evt:" + "a" * 60, 1, 0)
    outbox = Outbox(db_path, workspace_id=WORKSPACE, instance_id=INSTANCE_A)
    with outbox.read() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(outbox)")}
        legacy = conn.execute(
            "SELECT recovery_owed, ack_restore_epoch FROM outbox WHERE event_id = ?",
            (legacy_key[0],),
        ).fetchone()
    assert "recovery_owed" in columns, columns
    # The legacy row is not silently upgraded: unknown ack epoch stays reported.
    assert legacy["recovery_owed"] == 0, dict(legacy)
    assert legacy["ack_restore_epoch"] is None, dict(legacy)
    assert [row["event_id"] for row in outbox.unverifiable_rows()] == [legacy_key[0]]
    assert outbox.recovery_owed_rows() == []

    # Re-opening is idempotent and keeps the disclosure.
    reopened = Outbox(db_path, workspace_id=WORKSPACE, instance_id=INSTANCE_A)
    assert reopened.stats()["total"] == 1
    assert [row["event_id"] for row in reopened.unverifiable_rows()] == [legacy_key[0]]
    assert reopened.recovery_owed_rows() == []

    # A native re-read of that migrated legacy row creates the durable
    # obligation instead of erasing the unknown-epoch responsibility.
    replacement = _events("message:legacy-reread", "旧 outbox 行重读后的记忆")
    assert reopened.replace_row_payload(legacy_key, replacement, state="pending") == 1
    new_key = _key(replacement[0])
    migrated = _fetch_row(reopened, new_key)
    assert migrated["state"] == "pending", migrated
    assert migrated["recovery_owed"] == 1, migrated
    assert [row["event_id"] for row in reopened.recovery_owed_rows()] == [new_key[0]]
