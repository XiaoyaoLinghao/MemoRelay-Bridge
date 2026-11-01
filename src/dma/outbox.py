"""Durable DMA outbox with source cursor in one SQLite transaction.

Durability rules (RAW-01 / AC-37):

* the native source cursor advances **only** in the same ``synchronous=FULL``
  transaction that stores the sanitized events; a crash can leave either both
  or neither, never "cursor advanced but outbox missing";
* an event is acknowledged to the user as "saved locally" only after commit;
* delivery retries are bounded (default 3 attempts, 10s/60s/300s backoff) and a
  permanent error stops automatic retries while keeping a queryable state;
* replay is idempotent: the same event part is never queued twice.

Standard library only.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import json
import os
import sqlite3
import threading
import uuid
from pathlib import Path

from .errors import InvalidEventError
from .ids import MAX_BATCH_BYTES, MAX_BATCH_EVENTS, compact_json, sha256_hex
from .validation import validate_batch

SCHEMA_VERSION = 1
DEFAULT_BACKOFF = (10, 60, 300)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cursor (
    stream TEXT PRIMARY KEY,
    position TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    event_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    part_index INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    delivery_lease_until TEXT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NULL,
    last_error_code TEXT NULL,
    receipt_json TEXT NULL,
    receipt_id TEXT NULL,
    remote_durable INTEGER NULL,
    ack_watermark INTEGER NULL,
    ack_restore_epoch TEXT NULL,
    replay_epoch TEXT NULL,
    ack_epoch_unverified INTEGER NOT NULL DEFAULT 0,
    recovery_owed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (event_id, revision, part_index)
);
CREATE INDEX IF NOT EXISTS idx_outbox_state ON outbox (state, next_attempt_at);
CREATE TABLE IF NOT EXISTS remote_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    backend_instance_id TEXT NULL,
    restore_epoch TEXT NULL,
    watermark INTEGER NULL,
    updated_at TEXT NULL
);
CREATE TABLE IF NOT EXISTS restore_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    previous_backend_instance_id TEXT NULL,
    previous_restore_epoch TEXT NULL,
    previous_watermark INTEGER NULL,
    observed_backend_instance_id TEXT NULL,
    observed_restore_epoch TEXT NULL,
    detected_at TEXT NOT NULL,
    source TEXT NOT NULL,
    cleared_at TEXT NULL
);
"""

_WRITE_LOCK = threading.RLock()


def utc_now(clock=None) -> str:
    moment = clock() if clock else _dt.datetime.now(_dt.timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_dt.timezone.utc)
    return moment.astimezone(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Outbox:
    def __init__(
        self,
        db_path: str | Path,
        *,
        workspace_id: str = "owner:demo",
        instance_id: str | None = None,
        clock=None,
        max_attempts: int = 3,
        backoff_delays=DEFAULT_BACKOFF,
    ) -> None:
        self.db_path = Path(db_path)
        self.workspace_id = workspace_id
        self.instance_id = instance_id or ("instance:" + uuid.uuid4().hex)
        self.clock = clock
        self.max_attempts = max_attempts
        self.backoff_delays = tuple(backoff_delays)
        self.initialize()

    # -- connection ---------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path), timeout=10.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @contextlib.contextmanager
    def read(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.db_path.parent, 0o700)
        except OSError:  # pragma: no cover - platform dependent
            pass
        with _WRITE_LOCK:
            connection = self._connect()
            try:
                connection.executescript(_SCHEMA)
                # Idempotent schema migration: outboxes created before ack-epoch
                # tracking gain the ack columns in place. Legacy rows keep
                # ``ack_restore_epoch = NULL`` (= unknown epoch) on purpose: the
                # reconciler reports them as unverifiable instead of guessing.
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(outbox)")
                }
                for name, ddl in (
                    ("delivery_lease_until", "TEXT NULL"),
                    ("ack_watermark", "INTEGER NULL"),
                    ("ack_restore_epoch", "TEXT NULL"),
                    # F15 round-4: durable restore-replay state. ``replay_epoch``
                    # marks a row that was requeued for the given restore epoch
                    # but is not delivered yet, so a later reconciler pass can
                    # resume an interrupted/limited replay instead of guessing
                    # from the state alone. ``ack_epoch_unverified`` marks a
                    # delivered row whose ack epoch cannot be proven (legacy
                    # outbox or no trusted pre-restore baseline); it stays
                    # visible on every pass until a native re-read fixes it.
                    # F15 round-5: ``recovery_owed`` is the durable recovery
                    # responsibility created when a native re-read replaces a
                    # legacy/unknown-epoch delivered row or a gap row. It
                    # survives pending/retry/failed/gap and process restarts
                    # and is only cleared by a *durable* receipt naming the
                    # current recovery target epoch (F15 round-6), so neither
                    # "re-read to pending" nor a plain HTTP 200 can be reported
                    # as recovered.
                    ("replay_epoch", "TEXT NULL"),
                    ("ack_epoch_unverified", "INTEGER NOT NULL DEFAULT 0"),
                    ("recovery_owed", "INTEGER NOT NULL DEFAULT 0"),
                ):
                    if name not in columns:
                        connection.execute(f"ALTER TABLE outbox ADD COLUMN {name} {ddl}")
                connection.execute(
                    "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO meta (key, value) VALUES ('instance_id', ?)",
                    (self.instance_id,),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO meta (key, value) VALUES ('workspace_id', ?)",
                    (self.workspace_id,),
                )
                row = connection.execute("SELECT value FROM meta WHERE key = 'instance_id'").fetchone()
                self.instance_id = row["value"]
                row = connection.execute("SELECT value FROM meta WHERE key = 'workspace_id'").fetchone()
                self.workspace_id = row["value"]
            finally:
                connection.close()
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:  # pragma: no cover
            pass

    # -- capture ------------------------------------------------------------
    def capture(
        self,
        events: list[dict],
        *,
        stream: str,
        position: str,
        after_insert_hook=None,
    ) -> dict:
        """Store events and advance the source cursor atomically.

        ``after_insert_hook`` is a test seam used to rehearse a crash between the
        inserts and the commit; production callers never pass it.
        """
        if not isinstance(stream, str) or not stream:
            raise ValueError("stream must be a non-empty string")
        if not isinstance(position, str) or not position:
            raise ValueError("position must be a non-empty string")
        batch = {
            "schema_version": "1.0",
            "batch_id": "batch:capture:" + hashlib.sha256(
                f"{stream}|{position}".encode("utf-8")
            ).hexdigest()[:24],
            "events": events,
        }
        # This is local durable capture, not a network request. Validate in
        # bounded contract windows, then keep all writes + cursor in ONE tx.
        # Delivery still enforces MAX_BATCH_EVENTS/MAX_BATCH_BYTES separately.
        if not isinstance(events, list) or not events:
            errors = validate_batch(batch)
        else:
            errors = []
            seen = set()
            for offset in range(0, len(events), MAX_BATCH_EVENTS):
                window = events[offset:offset + MAX_BATCH_EVENTS]
                errors = validate_batch({**batch, "events": window})
                if errors:
                    break
                for event in window:
                    key = (event["event_id"], event["event_revision"],
                           event["body"]["part_index"])
                    if key in seen:
                        errors.append("events: duplicate part key in capture")
                        break
                    seen.add(key)
                if errors:
                    break
        if errors:
            raise InvalidEventError("; ".join(errors))
        now = utc_now(self.clock)
        with _WRITE_LOCK:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    inserted = 0
                    for event in events:
                        body = event["body"]
                        payload = compact_json(event)
                        cursor = connection.execute(
                            "INSERT OR IGNORE INTO outbox (event_id, revision, part_index, "
                            "payload_json, payload_sha256, state, attempts, created_at, updated_at) "
                            "VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?)",
                            (
                                event["event_id"],
                                int(event["event_revision"]),
                                int(body["part_index"]),
                                payload,
                                hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                                now,
                                now,
                            ),
                        )
                        inserted += cursor.rowcount
                    connection.execute(
                        "INSERT INTO cursor (stream, position, updated_at) VALUES (?, ?, ?) "
                        "ON CONFLICT (stream) DO UPDATE SET position = excluded.position, "
                        "updated_at = excluded.updated_at",
                        (stream, position, now),
                    )
                    if after_insert_hook is not None:
                        after_insert_hook()
                    connection.execute("COMMIT")
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
            finally:
                connection.close()
        return {"inserted": inserted, "duplicates": len(events) - inserted, "stream": stream, "position": position}

    # -- querying -----------------------------------------------------------
    def cursor(self, stream: str) -> str | None:
        with self.read() as conn:
            row = conn.execute("SELECT position FROM cursor WHERE stream = ?", (stream,)).fetchone()
            return row["position"] if row is not None else None

    def pending_rows(self, *, now: str | None = None, limit: int = 2000) -> list[dict]:
        moment = now or utc_now(self.clock)
        with self.read() as conn:
            rows = conn.execute(
                "SELECT * FROM outbox WHERE "
                "(state IN ('pending', 'retry_wait') AND "
                "(next_attempt_at IS NULL OR next_attempt_at <= ?)) OR "
                "(state = 'delivering' AND (delivery_lease_until IS NULL OR delivery_lease_until <= ?)) "
                "ORDER BY created_at, event_id, revision, part_index LIMIT ?",
                (moment, moment, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def build_batches(self, rows: list[dict] | None = None, *, max_events: int = MAX_BATCH_EVENTS, max_bytes: int = MAX_BATCH_BYTES):
        """Group pending rows into request batches; returns [(batch, keys)]."""
        if rows is None:
            rows = self.pending_rows()
        batches: list[tuple[dict, list[tuple[str, int, int]]]] = []
        current_events: list[dict] = []
        current_keys: list[tuple[str, int, int]] = []
        current_bytes = 0
        for row in rows:
            event = json.loads(row["payload_json"])
            size = len(row["payload_json"].encode("utf-8"))
            if current_events and (len(current_events) >= max_events or current_bytes + size > max_bytes):
                batches.append(self._batch(current_events, current_keys))
                current_events, current_keys, current_bytes = [], [], 0
            current_events.append(event)
            current_keys.append((row["event_id"], row["revision"], row["part_index"]))
            current_bytes += size
        if current_events:
            batches.append(self._batch(current_events, current_keys))
        return batches

    @staticmethod
    def _batch(events: list[dict], keys: list[tuple[str, int, int]]):
        digest = hashlib.sha256(compact_json(sorted(keys)).encode("utf-8")).hexdigest()
        return (
            {
                "schema_version": "1.0",
                "batch_id": "batch:" + digest[:32],
                "events": events,
            },
            keys,
        )

    # -- delivery state -----------------------------------------------------
    def mark_delivering(self, keys: list[tuple[str, int, int]], *, lease_seconds: int = 30, now: str | None = None) -> None:
        """Bound the transient sending state so process death cannot strand rows.

        Expired leases (and legacy rows with no lease) become ready again. A
        live sender's lease is not reset on another process's startup. Replay
        after an uncertain remote commit uses the same batch/receipt identity.
        """
        now = now or utc_now(self.clock)
        lease_until = _shift(now, max(1, int(lease_seconds)))
        with _WRITE_LOCK, self.read() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for event_id, revision, part_index in keys:
                    conn.execute(
                        "UPDATE outbox SET state = 'delivering', delivery_lease_until = ?, updated_at = ? "
                        "WHERE event_id = ? AND revision = ? AND part_index = ? "
                        "AND (state IN ('pending', 'retry_wait') OR "
                        "(state = 'delivering' AND (delivery_lease_until IS NULL OR delivery_lease_until <= ?)))",
                        (lease_until, now, event_id, revision, part_index, now),
                    )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def apply_receipt(
        self,
        receipt: dict,
        *,
        now: str | None = None,
        watermark: int | None = None,
        restore_epoch: str | None = None,
    ) -> dict:
        moment = now or utc_now(self.clock)
        key = (receipt.get("event_id"), int(receipt.get("event_revision") or 0), int(receipt.get("part_index") or 0))
        disposition = receipt.get("disposition")
        durable = bool(receipt.get("durable_remote"))
        error_code = receipt.get("error_code")
        purge_payload = False
        if disposition == "withheld" and error_code == "REDACTION_REQUIRED":
            # SEC-01: KW refused the body. The adapter must purge the suspect
            # outbox content immediately and re-read the native source under the
            # updated policy before retrying (see redaction_recovery.py).
            new_state = "redaction_required"
            purge_payload = True
        elif disposition in ("accepted", "duplicate", "withheld", "excluded"):
            new_state = "delivered"
        elif disposition == "rejected":
            from . import contracts

            entry = contracts.error_entry(error_code) if error_code else None
            retryable = bool(entry and entry.get("retryable"))
            new_state = "retry_wait" if retryable else "failed"
        else:
            new_state = "failed"
        if new_state == "delivered" and not durable:
            # F15 round-6: an HTTP 200 whose receipt is not ``durable_remote``
            # proves nothing about remote persistence. Treat it as a bounded
            # retry instead of a completed delivery so a non-durable ack can
            # never drain replay/recovery accounting (the row keeps its
            # ``recovery_owed``/``replay_epoch`` state).
            new_state = "retry_wait"
            error_code = error_code or "REMOTE_ACK_NOT_DURABLE"
        with _WRITE_LOCK, self.read() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                current = conn.execute(
                    "SELECT * FROM outbox WHERE event_id = ? AND revision = ? AND part_index = ?",
                    key,
                ).fetchone()
                attempts = int(current["attempts"]) if current is not None else 0
                inferred_watermark = watermark
                if purge_payload and current is not None:
                    from .errors import InvalidEventError  # noqa: F401 - keep error surface
                    from .ids import EMPTY_BODY_SHA256
                    import json as _json

                    try:
                        payload = _json.loads(current["payload_json"])
                    except (TypeError, ValueError):
                        payload = {}
                    body = payload.get("body") or {}
                    minimal = {
                        "schema_version": payload.get("schema_version", "1.0"),
                        "event_id": payload.get("event_id", key[0]),
                        "event_revision": payload.get("event_revision", key[1]),
                        "workspace_id": payload.get("workspace_id", self.workspace_id),
                        "operation": payload.get("operation", "create"),
                        "source": payload.get("source", {}),
                        "event_time": payload.get("event_time", {"value": None, "precision": "unknown", "timezone": "UTC"}),
                        "body": {
                            "text": "",
                            "sha256": EMPTY_BODY_SHA256,
                            "part_index": body.get("part_index", key[2]),
                            "part_count": body.get("part_count", 1),
                            "content_revision": body.get("content_revision", "native:purged"),
                            "format": "text/plain",
                        },
                        "redaction": {
                            **(payload.get("redaction") or {}),
                            "status": "withheld",
                        },
                        "project_ids": payload.get("project_ids", []),
                        "parent_revision": payload.get("parent_revision"),
                        "derived_from": payload.get("derived_from", []),
                    }
                    import hashlib as _hashlib
                    import json as _json2

                    encoded = _json2.dumps(minimal, ensure_ascii=False, separators=(",", ":"))
                    conn.execute(
                        "UPDATE outbox SET payload_json = ?, payload_sha256 = ?, state = ?, "
                        "attempts = ?, next_attempt_at = NULL, last_error_code = ?, receipt_json = ?, "
                        "receipt_id = ?, remote_durable = 0, ack_watermark = ?, ack_restore_epoch = ?, "
                        "updated_at = ? WHERE event_id = ? AND revision = ? AND part_index = ?",
                        (
                            encoded,
                            _hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                            new_state,
                            attempts + 1,
                            error_code,
                            compact_json(receipt),
                            receipt.get("receipt_id"),
                            watermark,
                            restore_epoch,
                            moment,
                            *key,
                        ),
                    )
                    conn.execute("COMMIT")
                    return {"key": key, "state": new_state, "disposition": disposition}
                # F15 round-6: the recovery obligation may only be discharged by
                # a *durable* receipt naming the current recovery target epoch.
                # The target is durably established by an authoritative
                # reconcile health observation (see :meth:`observe_recovery_target`)
                # and is read inside this same write transaction, so the epoch
                # metadata and the discharge decision cannot drift apart. A
                # delivery response can never move the target by itself, and a
                # non-NULL, stale or non-durable receipt is not enough.
                target_epoch = self._recovery_target_epoch_tx(conn)
                durable_current_ack = bool(
                    durable
                    and restore_epoch is not None
                    and target_epoch is not None
                    and str(restore_epoch) == str(target_epoch)
                )
                next_attempt = None
                if new_state == "retry_wait" and attempts + 1 < self.max_attempts:
                    delay = self.backoff_delays[min(attempts, len(self.backoff_delays) - 1)]
                    next_attempt = _shift(moment, delay)
                elif new_state == "retry_wait":
                    new_state = "failed"
                conn.execute(
                    "UPDATE outbox SET state = ?, attempts = ?, next_attempt_at = ?, "
                    "last_error_code = ?, receipt_json = ?, receipt_id = ?, remote_durable = ?, "
                    "ack_watermark = ?, ack_restore_epoch = ?, "
                    "updated_at = ? WHERE event_id = ? AND revision = ? AND part_index = ?",
                    (
                        new_state,
                        attempts + 1,
                        next_attempt,
                        error_code,
                        compact_json(receipt),
                        receipt.get("receipt_id"),
                        1 if durable else 0,
                        inferred_watermark,
                        restore_epoch,
                        moment,
                        *key,
                    ),
                )
                if new_state == "delivered":
                    # F15 round-4: a delivered row is no longer part of an
                    # unfinished restore replay.
                    # F15 round-6: only a durable receipt that names the current
                    # recovery target epoch may discharge a native-reread
                    # recovery obligation. A receipt without an epoch, from an
                    # older epoch (e.g. a cached pre-restore receipt replayed
                    # after the restore) or without a durable remote ack keeps
                    # ``recovery_owed``/``ack_epoch_unverified`` so the gap stays
                    # reported. Failed/retry rows keep
                    # ``replay_epoch``/``recovery_owed`` as well so the
                    # reconciler cannot claim the replay completed while they
                    # are stuck.
                    if durable_current_ack:
                        conn.execute(
                            "UPDATE outbox SET replay_epoch = NULL, ack_epoch_unverified = 0, "
                            "recovery_owed = 0 WHERE event_id = ? AND revision = ? AND part_index = ?",
                            key,
                        )
                    else:
                        conn.execute(
                            "UPDATE outbox SET replay_epoch = NULL "
                            "WHERE event_id = ? AND revision = ? AND part_index = ?",
                            key,
                        )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return {"key": key, "state": new_state, "disposition": disposition}

    def record_failure(self, keys: list[tuple[str, int, int]], error_code: str, *, retryable: bool, now: str | None = None) -> dict:
        moment = now or utc_now(self.clock)
        counts = {"retry_wait": 0, "failed": 0}
        with _WRITE_LOCK, self.read() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for event_id, revision, part_index in keys:
                    row = conn.execute(
                        "SELECT attempts FROM outbox WHERE event_id = ? AND revision = ? AND part_index = ?",
                        (event_id, revision, part_index),
                    ).fetchone()
                    if row is None:
                        continue
                    attempts = int(row["attempts"]) + 1
                    if retryable and attempts < self.max_attempts:
                        delay = self.backoff_delays[min(attempts - 1, len(self.backoff_delays) - 1)]
                        next_attempt = _shift(moment, delay)
                        state = "retry_wait"
                    else:
                        next_attempt = None
                        state = "failed"
                    conn.execute(
                        "UPDATE outbox SET state = ?, attempts = ?, next_attempt_at = ?, "
                        "last_error_code = ?, updated_at = ? "
                        "WHERE event_id = ? AND revision = ? AND part_index = ?",
                        (state, attempts, next_attempt, error_code, moment, event_id, revision, part_index),
                    )
                    counts[state] += 1
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return counts

    # -- restore reconciliation / redaction recovery ------------------------
    def delivered_rows_above(
        self, watermark: int, *, restore_epoch: str | None = None
    ) -> list[dict]:
        """Delivered rows whose durable receipt is above a restored watermark.

        Kept for diagnostics only: after F15 the reconciler no longer treats a
        *global* backend watermark as a trusted snapshot baseline, because another
        client may already have advanced the restored backend. Replaying by the
        per-row ack epoch is the safe direction.
        """
        sql = (
            "SELECT * FROM outbox WHERE state = 'delivered' "
            "AND (ack_watermark IS NULL OR ack_watermark > ?) "
        )
        params: list = [int(watermark)]
        if restore_epoch is not None:
            sql += "AND (ack_restore_epoch IS NULL OR ack_restore_epoch != ?) "
            params.append(str(restore_epoch))
        sql += "ORDER BY event_id, revision, part_index"
        with self.read() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def delivered_rows_epoch_mismatch(self, restore_epoch: str) -> list[dict]:
        """Delivered rows whose recorded ack epoch differs from the backend epoch.

        F15: every row the adapter acknowledged under a *different* restore epoch
        is replayed after a restore; KW dedupes the rows the restored snapshot
        already contains. Rows with no recorded ack epoch (legacy outbox) are
        deliberately excluded here — they are surfaced through
        :meth:`delivered_rows_unknown_epoch` instead of being mass-replayed.
        """
        with self.read() as conn:
            rows = conn.execute(
                "SELECT * FROM outbox WHERE state = 'delivered' "
                "AND ack_restore_epoch IS NOT NULL AND ack_restore_epoch != ? "
                "ORDER BY event_id, revision, part_index",
                (str(restore_epoch),),
            ).fetchall()
        return [dict(row) for row in rows]

    def delivered_rows_unknown_epoch(self) -> list[dict]:
        """Delivered rows whose ack epoch is unknown (NULL = legacy/unknown)."""
        with self.read() as conn:
            rows = conn.execute(
                "SELECT * FROM outbox WHERE state = 'delivered' AND ack_restore_epoch IS NULL "
                "ORDER BY event_id, revision, part_index"
            ).fetchall()
        return [dict(row) for row in rows]

    def delivered_rows_unconfirmed(self, restore_epoch: str | None) -> list[dict]:
        """Delivered rows that cannot be proven to belong to ``restore_epoch``.

        Used when there is no trusted pre-restore epoch baseline: rows acked in a
        different (or unknown) epoch are reported, never blindly re-sent.
        """
        with self.read() as conn:
            if restore_epoch is None:
                rows = conn.execute(
                    "SELECT * FROM outbox WHERE state = 'delivered' "
                    "ORDER BY event_id, revision, part_index"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM outbox WHERE state = 'delivered' "
                    "AND (ack_restore_epoch IS NULL OR ack_restore_epoch != ?) "
                    "ORDER BY event_id, revision, part_index",
                    (str(restore_epoch),),
                ).fetchall()
        return [dict(row) for row in rows]

    def unverifiable_rows(self) -> list[dict]:
        """Delivered rows whose restore epoch cannot be proven (persistent gap).

        F15 round-4: this is re-derived from durable state on *every* reconcile
        pass, so a legacy/unknown ack epoch (``ack_restore_epoch IS NULL``) or a
        row that was left unverified because no trusted pre-restore baseline
        existed (``ack_epoch_unverified = 1``) stays visible instead of turning
        into ``ok=true`` once ``changed`` becomes false.
        """
        with self.read() as conn:
            rows = conn.execute(
                "SELECT * FROM outbox WHERE state = 'delivered' "
                "AND (ack_restore_epoch IS NULL OR ack_epoch_unverified = 1) "
                "ORDER BY event_id, revision, part_index"
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_epoch_unverified(self, keys: list[tuple[str, int, int]]) -> int:
        """Durably flag delivered rows whose epoch could not be verified.

        Used only in the ``no_trusted_baseline`` path: the rows are deliberately
        not mass-replayed, but the uncertainty must survive later passes and the
        process restart. A later verified delivery/native re-read clears the flag.
        """
        now = utc_now(self.clock)
        count = 0
        with _WRITE_LOCK, self.read() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for event_id, revision, part_index in keys:
                    cursor = conn.execute(
                        "UPDATE outbox SET ack_epoch_unverified = 1, updated_at = ? "
                        "WHERE event_id = ? AND revision = ? AND part_index = ? "
                        "AND state = 'delivered'",
                        (now, event_id, revision, part_index),
                    )
                    count += cursor.rowcount
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return count

    def recovery_owed_rows(self) -> list[dict]:
        """Rows carrying an unresolved native-reread recovery responsibility.

        F15 round-5: :meth:`replace_row_payload` sets ``recovery_owed = 1`` when
        the row it replaces was a delivered legacy/unknown-epoch row or a row in
        the durable ``gap`` state. The obligation is durable across
        pending/retry_wait/failed/gap and process restarts.

        F15 round-6: it is cleared only when a delivery persists a *durable*
        receipt (``remote_durable = 1``) that names the current recovery target
        epoch (``meta.recovery_target_epoch``, established by an authoritative
        reconcile health observation). A plain HTTP 200, a receipt without an
        epoch, a non-durable receipt or an older-epoch (e.g. cached pre-restore)
        receipt all keep the obligation. Merely re-reading the native source and
        getting a ``pending`` row is *not* recovery.
        """
        with self.read() as conn:
            rows = conn.execute(
                "SELECT * FROM outbox WHERE recovery_owed = 1 "
                "ORDER BY created_at, event_id, revision, part_index"
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _recovery_target_epoch_tx(conn) -> str | None:
        """Read the durable recovery target epoch inside an open transaction."""
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'recovery_target_epoch'"
        ).fetchone()
        return row["value"] if row is not None else None

    def recovery_target_epoch(self) -> str | None:
        """The durable current recovery target epoch (``None`` = not observed yet).

        F15 round-6: the target epoch is only ever written by
        :meth:`observe_recovery_target`, i.e. from an authoritative reconcile
        ``/v1/health`` observation. Delivery responses cannot move it, so an old
        epoch receipt replayed after a restore can never be mistaken for the
        current recovery target.
        """
        with self.read() as conn:
            return self._recovery_target_epoch_tx(conn)

    def observe_recovery_target(self, restore_epoch: str | None) -> list[tuple[str, int, int]]:
        """Durably bind the current recovery target epoch and discharge proven rows.

        F15 round-6: a reconcile pass is the authoritative epoch observation. In
        one transaction it (1) persists ``meta.recovery_target_epoch`` and
        (2) clears ``recovery_owed`` only for delivered rows whose recorded ack
        was durable (``remote_durable = 1``) and names exactly that epoch. Doing
        both in one transaction makes the binding version-constrained: a
        concurrent target-epoch change can never discharge rows against a stale
        observation, and an old/non-durable ack keeps its obligation.

        This also covers a normal delivery that ran *before* the reconcile pass
        adopted the post-restore epoch (the target was not known yet when the
        receipt was applied); the durable ack is then validated against the
        freshly observed target instead of never resolving.

        Returns the keys discharged by this call.
        """
        if restore_epoch is None:
            return []
        target = str(restore_epoch)
        cleared: list[tuple[str, int, int]] = []
        with _WRITE_LOCK, self.read() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                current = self._recovery_target_epoch_tx(conn)
                if current is None or str(current) != target:
                    conn.execute(
                        "INSERT INTO meta (key, value) VALUES ('recovery_target_epoch', ?) "
                        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                        (target,),
                    )
                rows = conn.execute(
                    "SELECT event_id, revision, part_index FROM outbox "
                    "WHERE recovery_owed = 1 AND state = 'delivered' "
                    "AND remote_durable = 1 AND ack_restore_epoch = ? "
                    "ORDER BY event_id, revision, part_index",
                    (target,),
                ).fetchall()
                for row in rows:
                    key = (row["event_id"], int(row["revision"]), int(row["part_index"]))
                    conn.execute(
                        "UPDATE outbox SET recovery_owed = 0, ack_epoch_unverified = 0, updated_at = ? "
                        "WHERE event_id = ? AND revision = ? AND part_index = ?",
                        (utc_now(self.clock), *key),
                    )
                    cleared.append(key)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return cleared

    def gap_rows(self) -> list[dict]:
        """Rows whose payload is unrecoverable; kept visible on every pass."""
        with self.read() as conn:
            rows = conn.execute(
                "SELECT * FROM outbox WHERE state = 'gap' "
                "ORDER BY event_id, revision, part_index"
            ).fetchall()
        return [dict(row) for row in rows]

    def outstanding_replay_rows(self) -> list[dict]:
        """Every row still owed to *some* restore-epoch replay (not drained yet)."""
        with self.read() as conn:
            rows = conn.execute(
                "SELECT * FROM outbox WHERE replay_epoch IS NOT NULL "
                "ORDER BY created_at, event_id, revision, part_index"
            ).fetchall()
        return [dict(row) for row in rows]

    def requeue(
        self,
        keys: list[tuple[str, int, int]],
        *,
        replay_epoch: str | None = None,
    ) -> int:
        """Re-send rows after a restore epoch change; KW dedupes duplicates.

        ``replay_epoch`` records durably which restore the requeue belongs to so
        an unfinished replay is visible to later passes (F15 round-4).
        """
        now = utc_now(self.clock)
        count = 0
        with _WRITE_LOCK, self.read() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for event_id, revision, part_index in keys:
                    cursor = conn.execute(
                        "UPDATE outbox SET state = 'pending', attempts = 0, next_attempt_at = NULL, "
                        "ack_watermark = NULL, ack_restore_epoch = NULL, replay_epoch = ?, "
                        "ack_epoch_unverified = 0, updated_at = ? "
                        "WHERE event_id = ? AND revision = ? AND part_index = ?",
                        (replay_epoch, now, event_id, revision, part_index),
                    )
                    count += cursor.rowcount
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return count

    def mark_gap(self, key: tuple[str, int, int], reason: str) -> None:
        """Stop retrying a row whose native source is gone; keep the gap visible."""
        with _WRITE_LOCK, self.read() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE outbox SET state = 'gap', last_error_code = ?, updated_at = ? "
                    "WHERE event_id = ? AND revision = ? AND part_index = ?",
                    (reason, utc_now(self.clock), *key),
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def redaction_required_rows(self) -> list[dict]:
        with self.read() as conn:
            rows = conn.execute(
                "SELECT * FROM outbox WHERE state = 'redaction_required' "
                "ORDER BY event_id, revision, part_index"
            ).fetchall()
        return [dict(row) for row in rows]

    def states_for(self, keys: list[tuple[str, int, int]]) -> dict[tuple[str, int, int], str]:
        """Current durable state per outbox key (missing rows report ``missing``)."""
        result: dict[tuple[str, int, int], str] = {}
        with self.read() as conn:
            for key in keys:
                row = conn.execute(
                    "SELECT state FROM outbox WHERE event_id = ? AND revision = ? AND part_index = ?",
                    key,
                ).fetchone()
                result[key] = row["state"] if row is not None else "missing"
        return result

    def replace_row_payload(self, key: tuple[str, int, int], events: list[dict], *, state: str = "pending") -> int:
        """Replace one outbox row with the re-read sanitized event part(s).

        F15 round-4: a row that was part of an unfinished restore replay keeps its
        ``replay_epoch`` across a native re-read, so the reconciler still treats it
        as owed until it is durably delivered again (the previous payload/purge may
        have been the reason the replay could not finish).

        F15 round-5: a native re-read must not erase the recovery responsibility
        of the row it replaces. When the previous row was ``delivered`` with an
        unverifiable legacy/unknown ack epoch (``ack_restore_epoch IS NULL`` or
        ``ack_epoch_unverified = 1``) or was sitting in the durable ``gap``
        state, the replacement row(s) durably carry ``recovery_owed = 1``. That
        flag survives pending/retry_wait/failed/gap and a process restart and is
        cleared only by a receipt naming the current restore epoch, so turning
        the row back into ``pending`` is never mistaken for recovered data. A
        row already carrying the obligation keeps it across another re-read.
        """
        now = utc_now(self.clock)
        with _WRITE_LOCK, self.read() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                previous = conn.execute(
                    "SELECT state, replay_epoch, ack_restore_epoch, ack_epoch_unverified, "
                    "recovery_owed FROM outbox WHERE event_id = ? AND revision = ? "
                    "AND part_index = ?",
                    key,
                ).fetchone()
                replay_epoch = previous["replay_epoch"] if previous is not None else None
                recovery_owed = 0
                if previous is not None:
                    unknown_ack = (
                        previous["ack_restore_epoch"] is None
                        or bool(previous["ack_epoch_unverified"])
                    )
                    if (
                        bool(previous["recovery_owed"])
                        or previous["state"] == "gap"
                        or (previous["state"] == "delivered" and unknown_ack)
                    ):
                        recovery_owed = 1
                if not events and recovery_owed:
                    # The native source no longer provides the event part. Keep
                    # the responsibility visible as a gap instead of deleting the
                    # only durable record of it.
                    conn.execute(
                        "UPDATE outbox SET state = 'gap', last_error_code = ?, "
                        "recovery_owed = 1, updated_at = ? "
                        "WHERE event_id = ? AND revision = ? AND part_index = ?",
                        ("native_reread_returned_no_event", now, *key),
                    )
                    conn.execute("COMMIT")
                    return 0
                conn.execute(
                    "DELETE FROM outbox WHERE event_id = ? AND revision = ? AND part_index = ?",
                    key,
                )
                for event in events:
                    body = event["body"]
                    payload = compact_json(event)
                    conn.execute(
                        "INSERT OR REPLACE INTO outbox (event_id, revision, part_index, payload_json, "
                        "payload_sha256, state, attempts, replay_epoch, recovery_owed, "
                        "created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
                        (
                            event["event_id"],
                            int(event["event_revision"]),
                            int(body["part_index"]),
                            payload,
                            sha256_hex(payload),
                            state,
                            replay_epoch,
                            recovery_owed,
                            now,
                            now,
                        ),
                    )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return len(events)

    # -- remote meta / stats ------------------------------------------------
    def set_remote_meta(
        self, *, backend_instance_id=None, restore_epoch=None, watermark=None, source="delivery"
    ) -> None:
        """Update the observed KW identity; durably note a restore-epoch change.

        F15: a normal delivery can observe the post-restore epoch *before*
        ``reconcile_after_restore`` runs. The old epoch/watermark must be written
        to a pending-restore marker before the meta row is overwritten, otherwise
        the change is lost forever and the old acks are never replayed. Ordinary
        writes never clear the marker; only a successful reconciliation does.
        """
        moment = utc_now(self.clock)
        with _WRITE_LOCK, self.read() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                previous = conn.execute("SELECT * FROM remote_meta WHERE id = 1").fetchone()
                if previous is not None and (
                    (restore_epoch is not None and previous["restore_epoch"] != restore_epoch)
                    or (
                        backend_instance_id is not None
                        and previous["backend_instance_id"] != backend_instance_id
                    )
                ):
                    pending = conn.execute(
                        "SELECT id FROM restore_changes WHERE cleared_at IS NULL LIMIT 1"
                    ).fetchone()
                    if pending is None:
                        conn.execute(
                            "INSERT INTO restore_changes "
                            "(previous_backend_instance_id, previous_restore_epoch, previous_watermark, "
                            "observed_backend_instance_id, observed_restore_epoch, detected_at, source) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (
                                previous["backend_instance_id"],
                                previous["restore_epoch"],
                                previous["watermark"],
                                backend_instance_id,
                                restore_epoch,
                                moment,
                                source,
                            ),
                        )
                conn.execute(
                    "INSERT INTO remote_meta (id, backend_instance_id, restore_epoch, watermark, updated_at) "
                    "VALUES (1, ?, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET "
                    "backend_instance_id = COALESCE(excluded.backend_instance_id, remote_meta.backend_instance_id), "
                    "restore_epoch = COALESCE(excluded.restore_epoch, remote_meta.restore_epoch), "
                    "watermark = COALESCE(excluded.watermark, remote_meta.watermark), "
                    "updated_at = excluded.updated_at",
                    (backend_instance_id, restore_epoch, watermark, moment),
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def remote_meta(self) -> dict:
        with self.read() as conn:
            row = conn.execute("SELECT * FROM remote_meta WHERE id = 1").fetchone()
        if row is None:
            return {"backend_instance_id": None, "restore_epoch": None, "watermark": None}
        return dict(row)

    def pending_restore_change(self) -> dict | None:
        """Latest un-reconciled restore detection (survives normal deliveries)."""
        with self.read() as conn:
            row = conn.execute(
                "SELECT * FROM restore_changes WHERE cleared_at IS NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row is not None else None

    def clear_restore_change(self, change_id: int) -> None:
        with _WRITE_LOCK, self.read() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE restore_changes SET cleared_at = ? WHERE id = ? AND cleared_at IS NULL",
                    (utc_now(self.clock), int(change_id)),
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def restore_changes(self) -> list[dict]:
        with self.read() as conn:
            rows = conn.execute(
                "SELECT * FROM restore_changes ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]

    def stats(self) -> dict:
        with self.read() as conn:
            rows = conn.execute("SELECT state, COUNT(*) AS c FROM outbox GROUP BY state").fetchall()
            streams = conn.execute("SELECT stream, position FROM cursor").fetchall()
        by_state = {row["state"]: row["c"] for row in rows}
        return {
            "by_state": by_state,
            "total": sum(by_state.values()),
            "cursors": {row["stream"]: row["position"] for row in streams},
            "remote": self.remote_meta(),
            "db_path": str(self.db_path),
        }


def _shift(moment: str, seconds: int) -> str:
    parsed = _dt.datetime.fromisoformat(moment.replace("Z", "+00:00"))
    return (parsed + _dt.timedelta(seconds=seconds)).astimezone(_dt.timezone.utc).replace(
        microsecond=0
    ).isoformat().replace("+00:00", "Z")
