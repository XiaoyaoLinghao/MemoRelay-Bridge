"""F15 round-3 regression: restore reconciliation must replay by ack epoch.

Codex round-2 finding: after KW is restored from a snapshot, *another* client can
write first and advance the restored backend watermark. When DMA had no pending
restore marker it used that (already advanced) global watermark as the snapshot
baseline, so old-epoch acknowledgements below it were never replayed and
``reconcile_after_restore`` still reported ``ok=true``.

The fixed contract: on an observed restore-epoch change every delivered row whose
stored ack epoch differs from the current backend epoch is replayed; KW dedupes
rows the snapshot already contains. Rows whose ack epoch is unknown (legacy
outbox) are not mass-replayed but are reported via
``unverifiable_epoch_rows``/``limitations``. All tests use the real KW store +
Unix-socket service, real snapshot export/import, the real durable outbox and
real DeliveryClient; data is synthetic only.
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

pytest.importorskip("knowledge_weaver")

from dma.delivery import DeliveryClient, deliver_once  # noqa: E402
from dma.event import build_message_events  # noqa: E402
from dma.ids import EMPTY_BODY_SHA256  # noqa: E402
from dma.outbox import Outbox  # noqa: E402
from dma.reconcile import reconcile_after_restore  # noqa: E402
from dma.redaction import Redactor  # noqa: E402
from dma.cli import main as cli_main  # noqa: E402

from knowledge_weaver.portability import export_snapshot, import_snapshot  # noqa: E402
from knowledge_weaver.service import ServiceApp  # noqa: E402
from knowledge_weaver.service.client import ServiceClient  # noqa: E402
from knowledge_weaver.store import Store, register_client  # noqa: E402

WORKSPACE = "owner:demo"
INSTANCE_A = "instance:hermes-fix2"
INSTANCE_B = "instance:other-client"
CLIENT_A = "client:a"
CLIENT_B = "client:b"
STREAM = "hermes:main"
EVENT_TIME = {"value": "2026-10-30T01:00:00Z", "precision": "instant", "timezone": "UTC"}


def _source(native_id: str, instance: str) -> dict:
    return {
        "instance_id": instance,
        "host": "hermes",
        "actor_id": "jun-test",
        "session_id": "session:fix2",
        "native_event_id": native_id,
        "native_schema": "hermes-snapshot-20261030",
        "role": "user",
        "origin_kind": "user_input",
        "locator": f"hermes://session:fix2/{native_id}",
    }


def _events(native_id: str, text: str, *, instance: str = INSTANCE_A) -> list[dict]:
    return build_message_events(
        redactor=Redactor(),
        workspace_id=WORKSPACE,
        source=_source(native_id, instance),
        event_time=EVENT_TIME,
        text=text,
        content_revision="native:1",
        project_ids=("project:songta",),
    )


def _new_store(path: Path) -> Store:
    store = Store(path)
    register_client(
        store,
        client_id=CLIENT_A,
        workspace_id=WORKSPACE,
        host="hermes",
        instance_id=INSTANCE_A,
        actor_ids=["*"],
        project_ids=["project:songta"],
    )
    register_client(
        store,
        client_id=CLIENT_B,
        workspace_id=WORKSPACE,
        host="hermes",
        instance_id=INSTANCE_B,
        actor_ids=["*"],
        project_ids=["project:songta"],
    )
    return store


def _socket(name: str) -> Path:
    # Unix socket paths have a ~104 byte limit; pytest's basetemp can be longer.
    directory = os.environ.get("DMA_FIX2_SOCKET_DIR", "/tmp")
    return Path(directory) / f"dma-fix2-{os.getpid()}-{time.time_ns() % 10**9}-{name}.sock"


def _deliverer(app: ServiceApp) -> DeliveryClient:
    return DeliveryClient(app.socket_path, client_id=CLIENT_A, timeout=3.0)


def _post_other_client(app: ServiceApp, count: int) -> dict:
    client = ServiceClient(app.socket_path, client_id=CLIENT_B)
    last: dict = {}
    for index in range(count):
        events = _events(
            f"message:other:{index}",
            f"其他客户端新记忆{index}",
            instance=INSTANCE_B,
        )
        status, payload = client.post_batch(
            {
                "schema_version": "1.0",
                "batch_id": f"batch:client-b:{index}",
                "events": events,
            }
        )
        assert status == 200, payload
        last = payload
    return last


def _key(event: dict) -> tuple[str, int, int]:
    return (event["event_id"], int(event["event_revision"]), int(event["body"]["part_index"]))


@contextlib.contextmanager
def _restored_after_other_client(
    tmp_path: Path, *, extra_after_snapshot: int = 1, other_client_writes: int = 3
):
    """Live backend -> snapshot -> deliveries after snapshot -> restore -> B writes.

    Yields the durable outbox, the restored store/app, the snapshot-only events
    and the events acknowledged in the lost (pre-restore) timeline.
    """
    live = _new_store(tmp_path / "kw-live" / "knowledge.db")
    outbox = Outbox(
        tmp_path / "dma" / "outbox.db", workspace_id=WORKSPACE, instance_id=INSTANCE_A
    )
    app1 = ServiceApp(live, _socket("live")).start()
    try:
        first = _events("message:first", "松塔项目端口为4100")
        outbox.capture(first, stream=STREAM, position="1")
        assert deliver_once(outbox, _deliverer(app1))["accepted"] == 1
        snapshot = tmp_path / "snapshot"
        export_snapshot(live, snapshot)
        lost: list[dict] = []
        for index in range(extra_after_snapshot):
            events = _events(f"message:lost:{index}", f"恢复后丢失的数据{index}")
            lost.extend(events)
            outbox.capture(events, stream=STREAM, position=str(2 + index))
            assert deliver_once(outbox, _deliverer(app1))["accepted"] == 1
    finally:
        app1.stop()

    imported = import_snapshot(snapshot, tmp_path / "kw-restored")
    restored = Store(imported["target_db"])
    assert restored.watermark() == imported["watermark"]
    app2 = ServiceApp(restored, _socket("restored")).start()
    try:
        last = _post_other_client(app2, other_client_writes)
        assert last["watermark"] > imported["watermark"]
        yield {
            "outbox": outbox,
            "restored": restored,
            "app": app2,
            "imported": imported,
            "first": first,
            "lost": lost,
            "snapshot": snapshot,
            "reader": ServiceClient(app2.socket_path, client_id=CLIENT_A),
        }
    finally:
        app2.stop()


def test_f15_other_client_advances_watermark_before_reconcile(tmp_path):
    """Codex counterexample: another client writes first; A's lost ack must come back."""
    with _restored_after_other_client(tmp_path, other_client_writes=3) as scenario:
        report = reconcile_after_restore(scenario["outbox"], _deliverer(scenario["app"]))
        lost = scenario["lost"][0]
        first = scenario["first"][0]
        # The core Codex expectation: the lost source is back in the restored backend.
        status, source = scenario["reader"].get_source(lost["event_id"], 1, 0)
        assert status == 200, {"status": status, "report": report}
        assert source["text"] == "恢复后丢失的数据0"
        assert scenario["reader"].get_source(first["event_id"], 1, 0)[0] == 200
        assert report["ok"] is True, report
        assert report["changed"] is True
        assert report["changed_source"] == "epoch_compare"
        assert report["requeue_basis"] == "ack_epoch_mismatch"
        # Every old-epoch ack is replayed (2 rows), the backend answers duplicate
        # for the row the snapshot already had and accepted for the lost one.
        assert report["replayed_rows"] == 2, report
        assert report["unverifiable_epoch_rows"] == 0
        assert report["gaps"] == []
        assert report["requeued"] == report["delivery"]["accepted"] == 1
        assert report["duplicate_rows"] == report["delivery"]["duplicate"] == 1
        assert {tuple(key) for key in report["requeued_keys"]} == {
            _key(first),
            _key(lost),
        }


def test_f15_replay_is_idempotent_on_second_reconcile(tmp_path):
    with _restored_after_other_client(tmp_path, other_client_writes=3) as scenario:
        outbox = scenario["outbox"]
        client = _deliverer(scenario["app"])
        first_pass = reconcile_after_restore(outbox, client)
        assert first_pass["ok"] is True
        assert first_pass["replayed_rows"] == 2
        with scenario["restored"].read() as conn:
            before = conn.execute("SELECT COUNT(*) AS c FROM event_parts").fetchone()["c"]
        second_pass = reconcile_after_restore(outbox, client)
        assert second_pass["changed"] is False
        assert second_pass["requeued"] == 0
        assert second_pass["replayed_rows"] == 0
        assert second_pass["duplicate_rows"] == 0
        assert second_pass["delivery"] is None
        assert second_pass["requeued_keys"] == []
        with scenario["restored"].read() as conn:
            after = conn.execute("SELECT COUNT(*) AS c FROM event_parts").fetchone()["c"]
        assert after == before
        assert outbox.stats()["by_state"].get("delivered") == 2


def test_epoch_unchanged_does_not_replay(tmp_path):
    live = _new_store(tmp_path / "kw-live" / "knowledge.db")
    app = ServiceApp(live, _socket("live")).start()
    try:
        outbox = Outbox(
            tmp_path / "dma" / "outbox.db", workspace_id=WORKSPACE, instance_id=INSTANCE_A
        )
        events = _events("message:first", "松塔项目端口为4100")
        outbox.capture(events, stream=STREAM, position="1")
        assert deliver_once(outbox, _deliverer(app))["accepted"] == 1
        before = live.watermark()
        report = reconcile_after_restore(outbox, _deliverer(app))
        assert report["ok"] is True
        assert report["changed"] is False
        assert report["changed_source"] == "none"
        assert report["requeued"] == 0
        assert report["replayed_rows"] == 0
        assert report["duplicate_rows"] == 0
        assert report["delivery"] is None
        assert report["gaps"] == []
        assert live.watermark() == before
        assert outbox.stats()["by_state"].get("delivered") == 1
    finally:
        app.stop()


def test_pending_marker_path_still_replays_lost_acks(tmp_path):
    """The round-2 pending-marker path must not regress (real delivery before reconcile)."""
    with _restored_after_other_client(tmp_path, other_client_writes=1) as scenario:
        outbox = scenario["outbox"]
        app = scenario["app"]
        # A normal delivery observes the new epoch before the reconciler runs.
        fresh = _events("message:after-restore", "恢复后由A投递的新记忆")
        outbox.capture(fresh, stream=STREAM, position="9")
        assert deliver_once(outbox, _deliverer(app))["accepted"] == 1
        pending = outbox.pending_restore_change()
        assert pending is not None
        report = reconcile_after_restore(outbox, _deliverer(app))
        assert report["ok"] is True, report
        assert report["changed"] is True
        assert report["changed_source"] == "pending_restore_marker"
        assert report["requeue_basis"] == "ack_epoch_mismatch"
        replayed = {tuple(key) for key in report["requeued_keys"]}
        lost_key = _key(scenario["lost"][0])
        assert lost_key in replayed
        # The event delivered under the *current* epoch must not be replayed.
        assert _key(fresh[0]) not in replayed
        assert report["replayed_rows"] == 2
        assert report["requeued"] == report["delivery"]["accepted"] == 1
        assert report["duplicate_rows"] == report["delivery"]["duplicate"] == 1
        assert report["pending_restore_change_after"] is None
        assert scenario["reader"].get_source(lost_key[0], 1, 0)[0] == 200
        again = reconcile_after_restore(outbox, _deliverer(app))
        assert again["changed"] is False
        assert again["requeued"] == 0


def test_unreusable_payload_is_still_reported_as_gap(tmp_path):
    with _restored_after_other_client(tmp_path, other_client_writes=2) as scenario:
        outbox = scenario["outbox"]
        first = scenario["first"][0]
        # Simulate a purged body: minimal envelope without readable redacted text.
        minimal = {
            "schema_version": "1.0",
            "event_id": first["event_id"],
            "event_revision": 1,
            "workspace_id": first["workspace_id"],
            "operation": "create",
            "source": first["source"],
            "event_time": first["event_time"],
            "body": {
                "text": "",
                "sha256": EMPTY_BODY_SHA256,
                "part_index": 0,
                "part_count": 1,
                "content_revision": "native:purged",
                "format": "text/plain",
            },
            "redaction": {
                "policy_version": "redact:1",
                "status": "passed",
                "replacements": 0,
                "credential_refs": [],
            },
            "project_ids": first["project_ids"],
            "parent_revision": None,
            "derived_from": [],
        }
        with outbox.read() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE outbox SET payload_json = ? WHERE event_id = ?",
                (json.dumps(minimal, ensure_ascii=False), first["event_id"]),
            )
            conn.execute("COMMIT")
        report = reconcile_after_restore(outbox, _deliverer(scenario["app"]))
        assert report["changed"] is True
        assert report["replayed_rows"] == 1  # only the reusable lost row
        assert [gap["event_id"] for gap in report["gaps"]] == [first["event_id"]]
        assert "native" in report["gaps"][0]["reason"]
        assert report["requeued"] == report["delivery"]["accepted"] == 1
        assert report["ok"] is False  # an unrecoverable gap is not "ok"
        lost_key = _key(scenario["lost"][0])
        assert scenario["reader"].get_source(lost_key[0], 1, 0)[0] == 200


def test_unknown_ack_epoch_rows_are_reported_not_replayed(tmp_path):
    """Legacy outbox rows (NULL ack epoch) are disclosed, never silently dropped."""
    with _restored_after_other_client(tmp_path, other_client_writes=2) as scenario:
        outbox = scenario["outbox"]
        first = scenario["first"][0]
        # Simulate an outbox written before ack-epoch tracking existed.
        with outbox.read() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE outbox SET ack_restore_epoch = NULL WHERE event_id = ?",
                (first["event_id"],),
            )
            conn.execute("COMMIT")
        report = reconcile_after_restore(outbox, _deliverer(scenario["app"]))
        assert report["ok"] is False  # uncertainty is reported, not hidden
        assert report["changed"] is True
        assert report["requeue_basis"] == "ack_epoch_mismatch"
        assert report["unverifiable_epoch_rows"] == 1
        assert report["replayed_rows"] == 1  # the known old-epoch row
        assert _key(first) not in {tuple(key) for key in report["requeued_keys"]}
        assert any("epoch" in line.lower() and "re-read" in line for line in report["limitations"])
        assert outbox.states_for([_key(first)])[_key(first)] == "delivered"
        assert outbox.states_for([_key(scenario["lost"][0])])[_key(scenario["lost"][0])] == "delivered"


def test_no_pre_restore_baseline_never_mass_replays(tmp_path):
    """First contact without a recorded pre-restore epoch must not resend everything."""
    live = _new_store(tmp_path / "kw-live" / "knowledge.db")
    app1 = ServiceApp(live, _socket("live")).start()
    outbox = Outbox(
        tmp_path / "dma" / "outbox.db", workspace_id=WORKSPACE, instance_id=INSTANCE_A
    )
    try:
        events = [
            _events("message:first", "松塔项目端口为4100")[0],
            _events("message:second", "松塔项目端口改为4200")[0],
        ]
        for index, event in enumerate(events):
            outbox.capture([event], stream=STREAM, position=str(index + 1))
            assert deliver_once(outbox, _deliverer(app1))["accepted"] == 1
        snapshot = tmp_path / "snapshot"
        export_snapshot(live, snapshot)
    finally:
        app1.stop()
    # A fresh/lost metadata row: no pre-restore epoch baseline is available.
    with outbox.read() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM remote_meta")
        conn.execute("COMMIT")
    assert outbox.remote_meta()["restore_epoch"] is None

    imported = import_snapshot(snapshot, tmp_path / "kw-restored")
    restored = Store(imported["target_db"])
    app2 = ServiceApp(restored, _socket("restored")).start()
    try:
        _post_other_client(app2, 2)
        before = restored.watermark()
        report = reconcile_after_restore(outbox, _deliverer(app2))
        assert report["changed"] is True
        assert report["requeue_basis"] == "no_trusted_baseline"
        assert report["replayed_rows"] == 0
        assert report["requeued"] == 0
        assert report["delivery"] is None
        assert report["unverifiable_epoch_rows"] == 2
        assert report["ok"] is False
        assert any("baseline" in line.lower() or "re-read" in line for line in report["limitations"])
        # Nothing was mass-replayed and the outbox rows stay delivered locally.
        assert restored.watermark() == before
        assert outbox.stats()["by_state"].get("delivered") == 2
        # The observed identity is adopted so a second pass is stable.
        again = reconcile_after_restore(outbox, _deliverer(app2))
        assert again["changed"] is False
        assert again["requeued"] == 0
    finally:
        app2.stop()


def test_legacy_outbox_schema_migrates_ack_epoch_column(tmp_path):
    """Opening a pre-ack-epoch outbox adds the column idempotently; old rows are NULL."""
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
        restore_change_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'restore_changes'"
        ).fetchone()
    assert {"ack_watermark", "ack_restore_epoch"} <= columns
    assert restore_change_table is not None
    legacy = outbox.delivered_rows_unknown_epoch()
    assert [row["event_id"] for row in legacy] == ["evt:" + "a" * 60]
    assert legacy[0]["ack_restore_epoch"] is None
    # Re-opening is idempotent (no duplicate-column / duplicate-index error).
    reopened = Outbox(db_path, workspace_id=WORKSPACE, instance_id=INSTANCE_A)
    assert reopened.stats()["total"] == 1


# -- F15 follow-up: the reconcile CLI envelope must not hard-code ok=true ------


def _cli(capsys, argv):
    code = cli_main(argv)
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]
    return code, json.loads("\n".join(lines)) if lines else {}


def _synthetic_reconcile_report(**overrides):
    report = {
        "ok": True,
        "changed": True,
        "changed_source": "epoch_compare",
        "baseline_restore_epoch": "epoch:old",
        "previous": {
            "backend_instance_id": "kw:old",
            "restore_epoch": "epoch:old",
            "watermark": 4,
        },
        "current": {
            "backend_instance_id": "kw:new",
            "restore_epoch": "epoch:new",
            "watermark": 8,
        },
        "requeue_basis": "ack_epoch_mismatch",
        "requeued": 1,
        "replayed_rows": 2,
        "duplicate_rows": 1,
        "unverifiable_epoch_rows": 0,
        "requeued_keys": [],
        "gaps": [],
        "delivery": {"accepted": 1, "duplicate": 1},
        "limitations": [],
    }
    report.update(overrides)
    return report


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "ok": False,
            "requeued": 0,
            "replayed_rows": 1,
            "duplicate_rows": 0,
            "delivery": None,
            "gaps": [
                {
                    "event_id": "evt:" + "a" * 60,
                    "event_revision": 1,
                    "part_index": 0,
                    "reason": "body_unavailable_requires_native_reread",
                    "note": "source must be re-read by the adapter",
                }
            ],
        },
        {
            "ok": False,
            "unverifiable_epoch_rows": 2,
            "replayed_rows": 0,
            "requeued": 0,
            "duplicate_rows": 0,
            "delivery": None,
        },
    ],
    ids=["unrecoverable-gap", "unverifiable-legacy-epochs"],
)
def test_cli_reconcile_envelope_fails_when_report_not_ok(
    tmp_path, capsys, monkeypatch, overrides
):
    """F15 honesty: a not-ok reconcile report must surface as ok=false + exit != 0."""
    import dma.reconcile as reconcile_module

    report = _synthetic_reconcile_report(**overrides)
    monkeypatch.setattr(
        reconcile_module, "reconcile_after_restore", lambda *args, **kwargs: report
    )
    code, payload = _cli(
        capsys,
        [
            "reconcile",
            "--outbox",
            str(tmp_path / "dma" / "outbox.db"),
            "--socket",
            str(tmp_path / "kw.sock"),
            "--client",
            "client:a",
        ],
    )
    assert code != 0, payload
    assert payload["ok"] is False
    # The inner report is emitted unchanged for the operator/log consumer.
    assert payload["reconcile"] == report
    assert "stats" in payload


def test_cli_reconcile_envelope_ok_when_report_ok(tmp_path, capsys, monkeypatch):
    import dma.reconcile as reconcile_module

    report = _synthetic_reconcile_report()
    monkeypatch.setattr(
        reconcile_module, "reconcile_after_restore", lambda *args, **kwargs: report
    )
    code, payload = _cli(
        capsys,
        [
            "reconcile",
            "--outbox",
            str(tmp_path / "dma" / "outbox.db"),
            "--socket",
            str(tmp_path / "kw.sock"),
            "--client",
            "client:a",
        ],
    )
    assert code == 0, payload
    assert payload["ok"] is True
    assert payload["reconcile"] == report
    assert payload["reconcile"]["gaps"] == []
    assert payload["reconcile"]["unverifiable_epoch_rows"] == 0
    assert "stats" in payload
