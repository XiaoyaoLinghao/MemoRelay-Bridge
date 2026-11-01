"""F15 round-6 regression: only a durable receipt bound to the current recovery
target epoch may discharge a native-reread recovery obligation.

Codex round-5 finding (P1, AC-46) against the round-5 state machine:
``Outbox.apply_receipt`` cleared ``outbox.recovery_owed`` as soon as the delivery
payload named *any* non-NULL ``restore_epoch``.  It did not require the receipt to
be durable (``durable_remote``) and did not bind it to the current recovery
target epoch, so two real ``deliver_once`` paths reported lost data as recovered
while the real backend ``GET source`` still returned 404:

1. a delivery boundary that returns HTTP 200 with the current epoch and a
   synthetic ``durable_remote=false`` receipt (the lost event is never written to
   KW);
2. a cached *pre-restore* (old epoch) durable receipt replayed after the real
   ``apply_receipt`` but before ``set_remote_meta`` commits, i.e. the process
   crashes in that window; a fresh Outbox instance then reconciles against the
   real current KW.

The fixed contract exercised here:

* the recovery target epoch is durable metadata established only by an
  authoritative reconcile ``/v1/health`` observation
  (``meta.recovery_target_epoch``); a delivery response cannot move it;
* ``apply_receipt`` reads that target inside the same outbox transaction that
  records the ack, and clears ``recovery_owed`` only for a ``durable_remote``
  receipt naming exactly that epoch.  HTTP 200, a non-NULL epoch, a non-durable
  ack and a cached older-epoch receipt are all insufficient; a non-durable
  "delivered" outcome is persisted as a bounded retry instead of a completed
  delivery;
* after a process restart a new Outbox instance + real reconcile keeps the
  obligation (``recovery_owed_rows``/``ok=false``/CLI exit 1) while the backend
  ``GET source`` is still 404; the real current-epoch durable delivery still
  clears it (source 200, ``ok=true``);
* each reconcile pass atomically binds the freshly observed epoch as the target
  and discharges rows whose durable ack already names it (a delivery that ran
  before that pass adopted the post-restore epoch still resolves afterwards);
* ordinary pending rows and un-reread legacy delivered rows are never
  mass-replayed, and a pre-F15 outbox still migrates idempotently without a false
  "recovered" state.

Tests use the real KW store + Unix-socket service, real snapshot export/import,
the real durable outbox, real DeliveryClient, real ``deliver_once`` and the real
CLI; data is synthetic.
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

_ROW_FIELDS = (
    "state",
    "attempts",
    "next_attempt_at",
    "replay_epoch",
    "recovery_owed",
    "ack_restore_epoch",
    "remote_durable",
    "ack_epoch_unverified",
    "last_error_code",
)


def _row_snapshot(outbox: Outbox, key: tuple[str, int, int]) -> dict:
    with outbox.read() as conn:
        row = conn.execute(
            "SELECT state, attempts, next_attempt_at, replay_epoch, recovery_owed, "
            "ack_restore_epoch, remote_durable, ack_epoch_unverified, last_error_code "
            "FROM outbox WHERE event_id = ? AND revision = ? AND part_index = ?",
            key,
        ).fetchone()
    assert row is not None, key
    return {field: row[field] for field in _ROW_FIELDS}


def _force_unknown_ack_epoch(outbox: Outbox, event: dict) -> None:
    """Simulate a legacy outbox row: delivered, but its ack epoch is unknown."""
    with outbox.read() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE outbox SET ack_restore_epoch = NULL WHERE event_id = ?",
            (event["event_id"],),
        )
        conn.execute("COMMIT")


def _cached_ack(outbox: Outbox, key: tuple[str, int, int]) -> tuple[str | None, dict]:
    """Return the recorded ack epoch and the durable receipt stored by a real delivery."""
    with outbox.read() as conn:
        row = conn.execute(
            "SELECT ack_restore_epoch, receipt_json FROM outbox "
            "WHERE event_id = ? AND revision = ? AND part_index = ?",
            key,
        ).fetchone()
    assert row is not None and row["receipt_json"] is not None, key
    return row["ack_restore_epoch"], json.loads(row["receipt_json"])


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
        "--no-deliver",
    ]


class _ReceiptClient:
    """Delivery client that returns one synthetic receipt for the real health data."""

    def __init__(self, app: ServiceApp, *, receipt: dict, epoch: str | None, health: dict) -> None:
        self._app = app
        self._receipt = dict(receipt)
        self._epoch = epoch
        self._health = dict(health)

    def health(self) -> dict:
        return _deliverer(self._app).health()

    def post_batch(self, batch):
        return 200, {
            "schema_version": "1.0",
            "receipts": [dict(self._receipt)],
            "restore_epoch": self._epoch,
            "backend_instance_id": self._health.get("backend_instance_id"),
            "watermark": self._health.get("watermark"),
        }


# -- Codex round-5 real deliver_once path 1: HTTP 200 + durable_remote=false ----


def test_F15_R6_non_durable_http200_does_not_discharge_recovery(tmp_path):
    """A 200 without a durable ack must not clear owed; restart reconcile stays 404."""
    with _restored_after_other_client(tmp_path) as scenario:
        outbox = scenario["outbox"]
        app = scenario["app"]
        reader = scenario["reader"]
        lost = scenario["lost"][0]
        key = _key(lost)

        _old_epoch, cached_receipt = _cached_ack(outbox, key)
        _force_unknown_ack_epoch(outbox, lost)
        before = reconcile_after_restore(outbox, _deliverer(app))
        assert before["ok"] is False, before
        assert before["unverifiable_epoch_rows"] == 1, before
        current_epoch = before["current"]["restore_epoch"]
        assert current_epoch is not None, before
        assert outbox.recovery_target_epoch() == current_epoch

        # Real native re-read -> durable recovery obligation.
        assert outbox.replace_row_payload(key, [lost], state="pending") == 1
        assert [row["event_id"] for row in outbox.recovery_owed_rows()] == [key[0]]

        # Delivery boundary returns HTTP 200, the *current* epoch, but the
        # receipt is not durable (the lost event is never written to KW).
        receipt = dict(cached_receipt)
        receipt["durable_remote"] = False
        client = _ReceiptClient(
            app, receipt=receipt, epoch=current_epoch, health=before["current"]
        )
        delivered = deliver_once(outbox, client)
        # A non-durable "delivered" ack is not a completed delivery.
        assert delivered["retried"] == 1, delivered
        attempted = _row_snapshot(outbox, key)
        assert attempted["state"] == "retry_wait", attempted
        assert attempted["remote_durable"] == 0, attempted
        assert attempted["recovery_owed"] == 1, attempted
        assert attempted["last_error_code"] == "REMOTE_ACK_NOT_DURABLE", attempted

        # Fresh process: the obligation is re-derived from the durable outbox and
        # the real current KW still has no such source.
        reopened = Outbox(
            outbox.db_path, workspace_id=WORKSPACE, instance_id=INSTANCE_A
        )
        report = reconcile_after_restore(reopened, _deliverer(app), deliver=False)
        assert report["changed"] is False, report
        assert report["ok"] is False, report
        assert report["recovery_owed_rows"] == 1, report
        assert report["recovery_owed_keys"] == [[key[0], key[1], key[2]]], report
        assert report["recovery_owed_by_state"] == {"retry_wait": 1}, report
        assert report["recovery_target_epoch"] == current_epoch, report
        assert report["resolved_recovery_keys"] == [], report
        assert reader.get_source(key[0], key[1], key[2])[0] == 404
        again = reconcile_after_restore(reopened, _deliverer(app), deliver=False)
        assert again["ok"] is False and again["recovery_owed_rows"] == 1, again

        # The documented recovery path (native re-read + real delivery to the
        # current backend) still resolves it.
        assert reopened.replace_row_payload(key, [lost], state="pending") == 1
        reread = _row_snapshot(reopened, key)
        assert reread["state"] == "pending" and reread["recovery_owed"] == 1, reread
        assert deliver_once(reopened, _deliverer(app))["accepted"] == 1
        resolved = _row_snapshot(reopened, key)
        assert resolved["state"] == "delivered", resolved
        assert resolved["recovery_owed"] == 0, resolved
        assert resolved["remote_durable"] == 1, resolved
        final = reconcile_after_restore(reopened, _deliverer(app))
        assert final["ok"] is True, final
        assert final["recovery_owed_rows"] == 0, final
        status, source = reader.get_source(key[0], key[1], key[2])
        assert status == 200, {"status": status, "key": key}
        assert source["text"] == "恢复后丢失的数据0", source


# -- Codex round-5 real deliver_once path 2: old-epoch cached receipt + crash --


def test_F15_R6_old_epoch_cached_receipt_crash_window_keeps_owed(tmp_path, monkeypatch, capsys):
    """A pre-restore durable receipt applied inside the commit window proves nothing."""
    with _restored_after_other_client(tmp_path) as scenario:
        outbox = scenario["outbox"]
        app = scenario["app"]
        reader = scenario["reader"]
        lost = scenario["lost"][0]
        key = _key(lost)

        old_epoch, cached_receipt = _cached_ack(outbox, key)
        assert old_epoch is not None, old_epoch
        _force_unknown_ack_epoch(outbox, lost)
        before = reconcile_after_restore(outbox, _deliverer(app))
        assert before["ok"] is False, before
        current_epoch = before["current"]["restore_epoch"]
        assert current_epoch is not None and current_epoch != old_epoch, before
        assert outbox.recovery_target_epoch() == current_epoch

        assert outbox.replace_row_payload(key, [lost], state="pending") == 1
        assert [row["event_id"] for row in outbox.recovery_owed_rows()] == [key[0]]

        # Replay the cached *pre-restore* durable receipt; the process is
        # interrupted after the real apply_receipt commit and before
        # set_remote_meta, exactly like a crash in that window.
        client = _ReceiptClient(
            app, receipt=cached_receipt, epoch=old_epoch, health=before["current"]
        )

        def crash(**kwargs):
            raise RuntimeError("C5 interruption after receipt before remote-meta commit")

        monkeypatch.setattr(outbox, "set_remote_meta", crash)
        with pytest.raises(RuntimeError):
            deliver_once(outbox, client)

        committed = _row_snapshot(outbox, key)
        assert committed["state"] == "delivered", committed
        assert committed["ack_restore_epoch"] == old_epoch, committed
        assert committed["remote_durable"] == 1, committed
        # The old-epoch durable receipt must NOT have discharged the obligation.
        assert committed["recovery_owed"] == 1, committed

        # Fresh process observes the real current KW: the cached old-epoch ack is
        # not proof, the backend source is still 404, and the report stays honest.
        reopened = Outbox(
            outbox.db_path, workspace_id=WORKSPACE, instance_id=INSTANCE_A
        )
        report = reconcile_after_restore(reopened, _deliverer(app), deliver=False)
        assert report["changed"] is False, report
        assert report["ok"] is False, report
        assert report["recovery_owed_rows"] == 1, report
        assert report["recovery_owed_keys"] == [[key[0], key[1], key[2]]], report
        assert report["recovery_owed_by_state"] == {"delivered": 1}, report
        assert report["unverifiable_epoch_rows"] == 0, report
        assert report["resolved_recovery_keys"] == [], report
        assert reader.get_source(key[0], key[1], key[2])[0] == 404
        second = reconcile_after_restore(reopened, _deliverer(app), deliver=False)
        assert second["ok"] is False and second["recovery_owed_rows"] == 1, second
        # The real CLI follows the honest report even with delivery disabled.
        code, payload = _cli(capsys, _reconcile_cli_argv(reopened, app))
        assert code == 1, payload
        assert payload["ok"] is False, payload
        assert payload["reconcile"]["recovery_owed_rows"] == 1, payload
        assert payload["reconcile"]["recovery_owed_by_state"] == {"delivered": 1}, payload

        # Explicit native re-read + real current-epoch delivery resolves it.
        assert reopened.replace_row_payload(key, [lost], state="pending") == 1
        assert deliver_once(reopened, _deliverer(app))["accepted"] == 1
        resolved = _row_snapshot(reopened, key)
        assert resolved["state"] == "delivered", resolved
        assert resolved["recovery_owed"] == 0, resolved
        assert resolved["ack_restore_epoch"] == current_epoch, resolved
        final = reconcile_after_restore(reopened, _deliverer(app))
        assert final["ok"] is True, final
        assert final["recovery_owed_rows"] == 0, final
        status, source = reader.get_source(key[0], key[1], key[2])
        assert status == 200, {"status": status, "key": key}
        assert source["text"] == "恢复后丢失的数据0", source


# -- control: real current-target-epoch durable delivery discharges the duty ---


def test_F15_R6_current_target_epoch_durable_delivery_discharges_owed(tmp_path):
    with _restored_after_other_client(tmp_path) as scenario:
        outbox = scenario["outbox"]
        app = scenario["app"]
        reader = scenario["reader"]
        lost = scenario["lost"][0]
        key = _key(lost)

        _force_unknown_ack_epoch(outbox, lost)
        before = reconcile_after_restore(outbox, _deliverer(app))
        assert before["ok"] is False and before["unverifiable_epoch_rows"] == 1, before
        current_epoch = before["current"]["restore_epoch"]
        assert outbox.recovery_target_epoch() == current_epoch

        assert outbox.replace_row_payload(key, [lost], state="pending") == 1
        assert [_row_snapshot(outbox, key)["recovery_owed"]] == [1]
        delivered = deliver_once(outbox, _deliverer(app))
        assert delivered["accepted"] == 1, delivered

        resolved = _row_snapshot(outbox, key)
        assert resolved["state"] == "delivered", resolved
        assert resolved["recovery_owed"] == 0, resolved
        assert resolved["remote_durable"] == 1, resolved
        assert resolved["ack_restore_epoch"] == current_epoch, resolved

        report = reconcile_after_restore(outbox, _deliverer(app))
        assert report["changed"] is False, report
        assert report["ok"] is True, report
        assert report["recovery_owed_rows"] == 0, report
        assert report["unverifiable_epoch_rows"] == 0, report
        assert report["resolved_recovery_keys"] == [], report
        status, source = reader.get_source(key[0], key[1], key[2])
        assert status == 200, {"status": status, "key": key}
        assert source["text"] == "恢复后丢失的数据0", source


# -- control: durable receipt without an epoch keeps the responsibility --------


def test_F15_R6_no_epoch_durable_receipt_keeps_recovery_owed(tmp_path):
    with _restored_after_other_client(tmp_path) as scenario:
        outbox = scenario["outbox"]
        app = scenario["app"]
        reader = scenario["reader"]
        lost = scenario["lost"][0]
        key = _key(lost)

        _old_epoch, cached_receipt = _cached_ack(outbox, key)
        _force_unknown_ack_epoch(outbox, lost)
        before = reconcile_after_restore(outbox, _deliverer(app))
        assert before["ok"] is False, before
        current_epoch = before["current"]["restore_epoch"]
        assert outbox.replace_row_payload(key, [lost], state="pending") == 1

        # Durable receipt, but the delivery payload names no restore epoch, so it
        # cannot be bound to the current recovery target.
        client = _ReceiptClient(
            app, receipt=cached_receipt, epoch=None, health=before["current"]
        )
        delivered = deliver_once(outbox, client)
        assert delivered["accepted"] == 1, delivered
        attempted = _row_snapshot(outbox, key)
        assert attempted["state"] == "delivered", attempted
        assert attempted["remote_durable"] == 1, attempted
        assert attempted["ack_restore_epoch"] is None, attempted
        assert attempted["recovery_owed"] == 1, attempted

        reopened = Outbox(
            outbox.db_path, workspace_id=WORKSPACE, instance_id=INSTANCE_A
        )
        report = reconcile_after_restore(reopened, _deliverer(app), deliver=False)
        assert report["changed"] is False, report
        assert report["ok"] is False, report
        assert report["recovery_owed_rows"] == 1, report
        assert report["recovery_owed_by_state"] == {"delivered": 1}, report
        assert report["unverifiable_epoch_rows"] == 1, report
        assert reader.get_source(key[0], key[1], key[2])[0] == 404

        # Real current-epoch delivery still resolves it.
        assert reopened.replace_row_payload(key, [lost], state="pending") == 1
        assert deliver_once(reopened, _deliverer(app))["accepted"] == 1
        resolved = _row_snapshot(reopened, key)
        assert resolved["recovery_owed"] == 0, resolved
        assert resolved["ack_restore_epoch"] == current_epoch, resolved
        final = reconcile_after_restore(reopened, _deliverer(app))
        assert final["ok"] is True, final
        assert reader.get_source(key[0], key[1], key[2])[0] == 200


# -- binding: a delivery that ran before the pass adopted the epoch resolves ---


def test_F15_R6_delivery_before_epoch_observation_resolves_on_reconcile(tmp_path):
    """The strict rule must not deadlock a genuinely recovered row."""
    with _restored_after_other_client(tmp_path) as scenario:
        outbox = scenario["outbox"]
        app = scenario["app"]
        reader = scenario["reader"]
        lost = scenario["lost"][0]
        key = _key(lost)

        # No reconcile has run yet in this outbox: the target epoch is unknown.
        assert outbox.recovery_target_epoch() is None
        _force_unknown_ack_epoch(outbox, lost)
        assert outbox.replace_row_payload(key, [lost], state="pending") == 1
        assert [_row_snapshot(outbox, key)["recovery_owed"]] == [1]

        # A normal delivery runs against the post-restore backend before the
        # reconciler adopts the new epoch. The durable ack is recorded but cannot
        # discharge the duty yet (the target is unknown / still the old epoch).
        health = _deliverer(app).health()
        delivered = deliver_once(outbox, _deliverer(app))
        assert delivered["accepted"] == 1, delivered
        pending_ack = _row_snapshot(outbox, key)
        assert pending_ack["state"] == "delivered", pending_ack
        assert pending_ack["remote_durable"] == 1, pending_ack
        assert pending_ack["ack_restore_epoch"] == health["restore_epoch"], pending_ack
        assert pending_ack["recovery_owed"] == 1, pending_ack
        assert outbox.pending_restore_change() is not None

        # This reconcile pass observes the current epoch authoritatively, persists
        # it as the recovery target and atomically discharges the proven row, so
        # the obligation does not stay stuck forever.
        report = reconcile_after_restore(outbox, _deliverer(app))
        assert report["changed"] is True, report
        assert report["current"]["restore_epoch"] == health["restore_epoch"], report
        assert report["recovery_target_epoch"] == health["restore_epoch"], report
        assert report["resolved_recovery_keys"] == [[key[0], key[1], key[2]]], report
        assert report["recovery_owed_rows"] == 0, report
        assert report["ok"] is True, report
        resolved = _row_snapshot(outbox, key)
        assert resolved["recovery_owed"] == 0, resolved
        status, source = reader.get_source(key[0], key[1], key[2])
        assert status == 200, {"status": status, "key": key}
        assert source["text"] == "恢复后丢失的数据0", source


# -- controls: ordinary pending rows / un-reread legacy rows are not replayed --


def test_F15_R6_ordinary_pending_rows_are_not_mass_replayed(tmp_path):
    live = _new_store(tmp_path / "kw-live" / "knowledge.db")
    app = ServiceApp(live, _socket("fix5-ordinary")).start()
    try:
        outbox = Outbox(
            tmp_path / "dma" / "outbox.db", workspace_id=WORKSPACE, instance_id=INSTANCE_A
        )
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

        report = reconcile_after_restore(outbox, _deliverer(app))
        assert report["changed"] is False, report
        assert report["ok"] is True, report
        assert report["recovery_owed_rows"] == 0, report
        assert report["resolved_recovery_keys"] == [], report
        assert report["recovery_target_epoch"] == health["restore_epoch"], report
        # The reconciler does not consume or rewrite the ordinary delivery queue.
        assert report["delivery"] is None, report
        assert report["replayed_rows"] == 0 and report["requeued"] == 0, report
        row = _row_snapshot(outbox, key)
        assert row["state"] == "pending", row
        assert row["attempts"] == 0 and row["recovery_owed"] == 0, row

        again = reconcile_after_restore(outbox, _deliverer(app))
        assert again["ok"] is True and again["delivery"] is None, again
        assert _row_snapshot(outbox, key)["state"] == "pending"
        assert outbox.stats()["by_state"].get("pending") == 1
    finally:
        app.stop()


def test_F15_R6_unreread_legacy_rows_stay_unreplayed_and_disclosed(tmp_path):
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
        requeued = {tuple(key) for key in first["requeued_keys"]}
        assert _key(lost_reread) not in requeued
        assert _key(lost_untouched) not in requeued
        assert outbox.recovery_owed_rows() == []
        for event in (lost_reread, lost_untouched):
            key = _key(event)
            assert outbox.states_for([key])[key] == "delivered"
            assert reader.get_source(key[0], key[1], key[2])[0] == 404

        # Only the explicitly re-read row becomes an obligation; the other legacy
        # row stays delivered and is never mass-replayed or silently reset.
        assert outbox.replace_row_payload(_key(lost_reread), [lost_reread], state="pending") == 1
        owed = reconcile_after_restore(outbox, _deliverer(app), deliver=False)
        assert owed["ok"] is False, owed
        assert owed["recovery_owed_rows"] == 1, owed
        assert owed["recovery_owed_keys"] == [list(_key(lost_reread))], owed
        assert owed["unverifiable_epoch_rows"] == 1, owed
        assert owed["replayed_rows"] == 0 and owed["requeued"] == 0, owed
        key_untouched = _key(lost_untouched)
        assert outbox.states_for([key_untouched])[key_untouched] == "delivered"
        assert reader.get_source(key_untouched[0], key_untouched[1], key_untouched[2])[0] == 404
        # The unchanged pass keeps disclosing both kinds of unresolved state.
        again = reconcile_after_restore(outbox, _deliverer(app), deliver=False)
        assert again["ok"] is False, again
        assert again["recovery_owed_rows"] == 1, again
        assert again["unverifiable_epoch_rows"] == 1, again


# -- idempotent legacy-outbox migration: no false recovery --------------------


def test_F15_R6_legacy_outbox_migration_stays_idempotent_without_false_recovery(tmp_path):
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
        connection.execute(
            "INSERT INTO outbox (event_id, revision, part_index, payload_json, payload_sha256, "
            "state, attempts, created_at, updated_at) VALUES (?, 1, 0, ?, ?, 'pending', 0, ?, ?)",
            ("evt:" + "b" * 60, "{}", "1" * 64, "2026-10-30T00:00:00Z", "2026-10-30T00:00:00Z"),
        )
        connection.commit()
    finally:
        connection.close()

    legacy_key = ("evt:" + "a" * 60, 1, 0)
    pending_key = ("evt:" + "b" * 60, 1, 0)
    outbox = Outbox(db_path, workspace_id=WORKSPACE, instance_id=INSTANCE_A)
    with outbox.read() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(outbox)")}
        legacy = conn.execute(
            "SELECT recovery_owed, ack_restore_epoch FROM outbox WHERE event_id = ?",
            (legacy_key[0],),
        ).fetchone()
    assert {"ack_restore_epoch", "replay_epoch", "ack_epoch_unverified", "recovery_owed"} <= columns
    assert legacy["recovery_owed"] == 0, dict(legacy)
    assert legacy["ack_restore_epoch"] is None, dict(legacy)
    assert outbox.recovery_target_epoch() is None
    assert [row["event_id"] for row in outbox.unverifiable_rows()] == [legacy_key[0]]
    assert outbox.recovery_owed_rows() == []
    assert _row_snapshot(outbox, pending_key)["state"] == "pending"
    assert _row_snapshot(outbox, pending_key)["recovery_owed"] == 0

    reopened = Outbox(db_path, workspace_id=WORKSPACE, instance_id=INSTANCE_A)
    assert reopened.stats()["total"] == 2
    assert [row["event_id"] for row in reopened.unverifiable_rows()] == [legacy_key[0]]
    assert reopened.recovery_owed_rows() == []
    assert _row_snapshot(reopened, pending_key)["state"] == "pending"


# -- minimal apply_receipt variants: only durable + current target epoch counts -


@pytest.mark.parametrize(
    "mode",
    ["non-durable", "stale-epoch", "no-epoch"],
    ids=["non-durable", "stale-epoch", "no-epoch"],
)
def test_F15_R6_minimal_apply_receipt_requires_durable_current_target_epoch(tmp_path, mode):
    with _restored_after_other_client(tmp_path) as scenario:
        outbox = scenario["outbox"]
        app = scenario["app"]
        reader = scenario["reader"]
        lost = scenario["lost"][0]
        key = _key(lost)

        old_epoch, cached_receipt = _cached_ack(outbox, key)
        _force_unknown_ack_epoch(outbox, lost)
        before = reconcile_after_restore(outbox, _deliverer(app))
        assert before["ok"] is False, before
        current_epoch = before["current"]["restore_epoch"]
        assert old_epoch is not None and old_epoch != current_epoch
        assert outbox.recovery_target_epoch() == current_epoch
        assert outbox.replace_row_payload(key, [lost], state="pending") == 1
        assert len(outbox.recovery_owed_rows()) == 1

        receipt = dict(cached_receipt)
        receipt["durable_remote"] = mode != "non-durable"
        epoch = {"non-durable": current_epoch, "stale-epoch": old_epoch}.get(mode)
        outbox.apply_receipt(receipt, restore_epoch=epoch)

        row = _row_snapshot(outbox, key)
        assert row["recovery_owed"] == 1, (mode, row)
        if mode == "non-durable":
            assert row["state"] == "retry_wait", (mode, row)
            assert row["remote_durable"] == 0, (mode, row)
        else:
            assert row["state"] == "delivered", (mode, row)
            assert row["remote_durable"] == 1, (mode, row)
        if mode == "stale-epoch":
            assert row["ack_restore_epoch"] == old_epoch, (mode, row)
        if mode == "no-epoch":
            assert row["ack_restore_epoch"] is None, (mode, row)

        after = reconcile_after_restore(outbox, _deliverer(app), deliver=False)
        assert after["ok"] is False, (mode, after)
        assert after["recovery_owed_rows"] == 1, (mode, after)
        assert after["recovery_owed_keys"] == [[key[0], key[1], key[2]]], (mode, after)
        assert reader.get_source(key[0], key[1], key[2])[0] == 404


# -- atomicity control: the current-epoch commit window is not punished --------


def test_F15_R6_current_epoch_durable_receipt_crash_window_still_recovers(tmp_path, monkeypatch):
    """Ack metadata and duty discharge commit atomically: a crash after the
    receipt but before ``set_remote_meta`` must not fabricate a gap."""
    with _restored_after_other_client(tmp_path) as scenario:
        outbox = scenario["outbox"]
        app = scenario["app"]
        reader = scenario["reader"]
        lost = scenario["lost"][0]
        key = _key(lost)

        _force_unknown_ack_epoch(outbox, lost)
        before = reconcile_after_restore(outbox, _deliverer(app))
        assert before["ok"] is False, before
        current_epoch = before["current"]["restore_epoch"]
        assert outbox.recovery_target_epoch() == current_epoch
        assert outbox.replace_row_payload(key, [lost], state="pending") == 1

        class Client:
            def health(self):
                return _deliverer(app).health()

            def post_batch(self, batch):
                return _deliverer(app).post_batch(batch)

        def crash(**kwargs):
            raise RuntimeError("C5 interruption after receipt before remote-meta commit")

        monkeypatch.setattr(outbox, "set_remote_meta", crash)
        with pytest.raises(RuntimeError):
            deliver_once(outbox, Client())

        committed = _row_snapshot(outbox, key)
        assert committed["state"] == "delivered", committed
        assert committed["remote_durable"] == 1, committed
        assert committed["ack_restore_epoch"] == current_epoch, committed
        # The durable current-epoch ack and the discharge were one transaction.
        assert committed["recovery_owed"] == 0, committed

        reopened = Outbox(
            outbox.db_path, workspace_id=WORKSPACE, instance_id=INSTANCE_A
        )
        report = reconcile_after_restore(reopened, _deliverer(app))
        assert report["changed"] is False, report
        assert report["ok"] is True, report
        assert report["recovery_owed_rows"] == 0, report
        assert report["resolved_recovery_keys"] == [], report
        status, source = reader.get_source(key[0], key[1], key[2])
        assert status == 200, {"status": status, "key": key}
        assert source["text"] == "恢复后丢失的数据0", source


# -- a non-durable ack cannot drain the restore replay either ------------------


def test_F15_R6_non_durable_ack_cannot_drain_restore_replay(tmp_path):
    with _restored_after_other_client(tmp_path) as scenario:
        outbox = scenario["outbox"]
        app = scenario["app"]
        reader = scenario["reader"]
        lost = scenario["lost"][0]
        first_event = scenario["first"][0]

        # Requeue the old-epoch rows without delivering them (operator used
        # --no-deliver / crash after the durable requeue).
        first = reconcile_after_restore(outbox, _deliverer(app), deliver=False)
        assert first["changed"] is True, first
        assert first["replay"]["complete"] is False, first["replay"]
        requiring = outbox.outstanding_replay_rows()
        assert len(requiring) == 2, requiring
        assert {row["state"] for row in requiring} == {"pending"}
        current_epoch = first["current"]["restore_epoch"]

        class Client:
            def health(self):
                return _deliverer(app).health()

            def post_batch(self, batch):
                receipts = []
                for event in batch["events"]:
                    body = event["body"]
                    receipts.append(
                        {
                            "event_id": event["event_id"],
                            "event_revision": event["event_revision"],
                            "part_index": body["part_index"],
                            "disposition": "accepted",
                            "durable_remote": False,
                            "processing": "pending",
                            "searchability": "none",
                            "error_code": None,
                            "receipt_id": "rcpt:" + "n" * 32,
                        }
                    )
                return 200, {
                    "schema_version": "1.0",
                    "receipts": receipts,
                    "restore_epoch": current_epoch,
                    "backend_instance_id": first["current"]["backend_instance_id"],
                    "watermark": first["current"]["watermark"],
                }

        delivered = deliver_once(outbox, Client())
        assert delivered["retried"] == 2, delivered
        # HTTP 200 + accepted-looking receipts without durable persistence must
        # not drain the replay or be reported as delivered.
        outstanding = outbox.outstanding_replay_rows()
        assert len(outstanding) == 2, outstanding
        assert {row["state"] for row in outstanding} == {"retry_wait"}
        assert {row["remote_durable"] for row in outstanding} == {0}
        assert {
            row["last_error_code"] for row in outstanding
        } == {"REMOTE_ACK_NOT_DURABLE"}
        assert outbox.stats()["by_state"].get("delivered", 0) == 0
        blocked = reconcile_after_restore(outbox, _deliverer(app), deliver=False)
        assert blocked["ok"] is False, blocked
        assert blocked["replay"]["complete"] is False, blocked["replay"]
        # The lost row is genuinely absent; the snapshot row is present but the
        # replay (ack-epoch proof) is still blocked from completing.
        assert reader.get_source(lost["event_id"], 1, 0)[0] == 404
        assert reader.get_source(first_event["event_id"], 1, 0)[0] == 200

        # Once the retry backoff has elapsed a real delivery drains the replay and
        # only then the pass reports completion.
        resumed = reconcile_after_restore(outbox, _deliverer(app), now=max(row["next_attempt_at"] for row in outstanding))
        assert resumed["ok"] is True, resumed
        assert resumed["replay"]["complete"] is True, resumed["replay"]
        assert resumed["replay"]["stopped_reason"] == "drained", resumed["replay"]
        assert outbox.stats()["by_state"].get("delivered", 0) == 2
        assert reader.get_source(lost["event_id"], 1, 0)[0] == 200
        assert reader.get_source(first_event["event_id"], 1, 0)[0] == 200
