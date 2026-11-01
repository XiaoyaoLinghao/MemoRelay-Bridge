"""Restore-epoch reconciliation and redaction-required recovery hooks (T17/T19/T20).

AC-46: after the KW backend is restored from a snapshot, its watermark can move
backwards and its ``restore_epoch`` changes. The DMA outbox keeps its durable
payloads and the restore epoch each row was acknowledged in, so the reconciler
can:

1. detect the epoch change via KW ``/v1/health`` (or a durable marker written by
   a delivery that already observed the new epoch);
2. re-queue every delivered row whose recorded ack epoch differs from the
   current backend epoch. This is the safe direction: a *global* backend
   watermark is not a trusted snapshot baseline, because another client may
   already have advanced the restored backend before this reconciler runs;
3. re-deliver those rows in bounded rounds until the replay drains or stops
   making progress. Each requeued row durably carries the restore epoch it is
   owed to (``outbox.replay_epoch``), so an interrupted, window-limited or
   partially failed replay is resumed by the next pass instead of being claimed
   complete;
4. report unrecoverable gaps (purged payloads), legacy rows without a recorded
   ack epoch, an unfinished replay, and native-reread recovery obligations
   explicitly on *every* pass — including passes where no epoch change is
   detected. ``changed=false`` never turns the same unresolved gap into
   ``ok=true``. A restore marker is cleared only for an epoch whose replay
   actually drained.

F15 round-5: a native re-read replaces a legacy/unknown-epoch delivered row (or a
``gap`` row) with a fresh ``pending`` row that durably carries
``outbox.recovery_owed``. Re-reading the source is not recovery: the obligation
stays visible (``recovery_owed_rows`` + limitation, ``ok=false``) across pending/
retry_wait/failed/gap and process restarts, and on a default pass it is *not*
auto-replayed (legacy rows are never mass-replayed). Only an explicit delivery
that obtains a durable receipt naming the current recovery target epoch clears
the flag; the reconciler then reports ``ok=true`` and the real CLI exits 0.
``--no-deliver`` reports the obligation and leaves the row's attempts/replay state
untouched.

F15 round-6: an HTTP 200, a non-NULL epoch or a cached older-epoch durable receipt
are *not* sufficient proof. The recovery target epoch is durably established by a
reconcile ``/v1/health`` observation (``meta.recovery_target_epoch``) and a
receipt discharges an obligation only when it is durable (``durable_remote``) and
names exactly that epoch, checked in the same outbox transaction that records the
ack. Each pass also atomically binds the newly observed target and discharges rows
whose durable ack already names it (e.g. a delivery that ran before this pass
adopted the post-restore epoch), so the strict rule cannot deadlock a genuinely
recovered row.

``ok`` is true only when the replay drained and no persistent gap/unverifiable
row/recovery obligation remains. The reconciler only touches the adapter's own
outbox; it never edits the KW database, host files, or production configuration.
"""

from __future__ import annotations

import json

from .delivery import DeliveryClient, deliver_once
from .errors import DeliveryError
from .outbox import Outbox

# A replay is delivered in bounded rounds. The default is deliberately larger
# than the number of rounds needed to drain the default pending_rows window
# (2000 rows / at most 100 events per batch), while still bounding a pass whose
# deliveries keep failing.
DEFAULT_MAX_REPLAY_ROUNDS = 16

LIMITATIONS = [
    "ack_epoch_replay: on a restore-epoch change every delivered row acknowledged under a different epoch is replayed; KW returns duplicate for rows the snapshot already contains, so a replay is idempotent",
    "unrecoverable_gaps_are_reported: if the outbox payload/body was purged and the native source no longer provides it, the gap is listed by event key instead of being silently skipped; the row is kept in the durable 'gap' state and reported by every later reconcile pass",
    "deletions_after_snapshot_unknown: a restore cannot reveal deletes that happened after the snapshot was taken",
    "unknown_ack_epoch_rows_are_not_mass_replayed: rows written by an outbox that predates ack-epoch tracking (or a first contact without a recorded pre-restore epoch) are counted in unverifiable_epoch_rows instead of being re-sent blindly; re-read the native source if a restore may have lost them",
]

NO_BASELINE_LIMITATION = (
    "no_trusted_baseline: no pre-restore epoch was recorded for this outbox, so a "
    "blanket replay would resend every delivered row on first contact; the rows "
    "that cannot be proven to belong to the current epoch are durably marked "
    "unverified; they are reported in unverifiable_epoch_rows on every pass and "
    "the observed identity is adopted for later passes. If data may have been "
    "lost, re-read the native source explicitly."
)

UNVERIFIABLE_LIMITATION = (
    "unverifiable_epoch_rows: delivered rows without a verifiable ack epoch cannot be "
    "attributed to the lost timeline automatically; they are left untouched and "
    "reported, and they keep ok=false. Re-read the native source if a restore may "
    "have lost them."
)

RECOVERY_OWED_LIMITATION = (
    "recovery_owed_rows: rows re-read from the native source after an unverifiable "
    "legacy/unknown ack epoch or a gap still owe a durable receipt from the current "
    "restore target epoch; re-reading alone, a plain HTTP 200, a non-durable ack or a "
    "cached older-epoch receipt is not recovery. They are reported on every pass "
    "and keep ok=false; the reconciler does not mass-replay legacy rows, so each one is "
    "resolved only when an explicit delivery (the normal delivery worker or "
    "'dma-memory deliver') persists a durable current-target-epoch receipt, after which a "
    "later reconcile reports ok=true."
)

REPLAY_INCOMPLETE_LIMITATION = (
    "replay_not_complete: the restore replay has not drained yet (rows are still "
    "pending/retry_wait, a round limit was reached, or delivery is disabled); the "
    "restore marker is kept and the next reconcile pass resumes the replay"
)


class ReconcileError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _payload_is_reusable(row: dict) -> tuple[bool, str]:
    try:
        payload = json.loads(row["payload_json"])
    except (TypeError, ValueError):
        return False, "payload_unreadable"
    body = payload.get("body") or {}
    redaction = payload.get("redaction") or {}
    if not body.get("text") and redaction.get("status") != "withheld":
        # Body purged (e.g. withheld/redaction path) and no redacted content is
        # available in the outbox; only a native re-read could recover it.
        return False, "body_unavailable_requires_native_reread"
    return True, "ok"


def _gap(row: dict, reason: str) -> dict:
    return {
        "event_id": row["event_id"],
        "event_revision": row["revision"],
        "part_index": row["part_index"],
        "reason": reason,
        "state": row.get("state"),
        "note": "source must be re-read by the adapter; if the native record is gone this gap is unrecoverable",
    }


def _key(row: dict) -> tuple[str, int, int]:
    return (row["event_id"], int(row["revision"]), int(row["part_index"]))


def _state_counts(rows: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for row in rows:
        state = row["state"]
        counts[state] = counts.get(state, 0) + 1
    return counts


def _aggregate_delivery(total: dict | None, counters: dict) -> dict:
    if total is None:
        total = {name: 0 for name in counters}
    for name, value in counters.items():
        total[name] = int(total.get(name, 0)) + int(value or 0)
    return total


def _add_limitation(report: dict, message: str) -> None:
    if message not in report["limitations"]:
        report["limitations"].append(message)


def reconcile_after_restore(
    outbox: Outbox,
    client: DeliveryClient,
    *,
    deliver: bool = True,
    now: str | None = None,
    max_replay_rounds: int = DEFAULT_MAX_REPLAY_ROUNDS,
) -> dict:
    try:
        health = client.health()
    except DeliveryError as exc:
        raise ReconcileError(exc.code, exc.message) from None
    new_backend = health.get("backend_instance_id")
    new_epoch = health.get("restore_epoch")
    observed_watermark = int(health.get("watermark") or 0)
    previous = outbox.remote_meta()
    pending_change = outbox.pending_restore_change()
    # F15: a normal delivery may have already observed the new epoch before this
    # reconciler ran; the durable marker keeps that pre-restore baseline alive.
    # Otherwise the epoch recorded in the outbox before the restore is the only
    # trustworthy baseline available (a *current* global watermark is not one).
    baseline_epoch = (
        pending_change.get("previous_restore_epoch")
        if pending_change is not None
        else previous.get("restore_epoch")
    )
    changed = pending_change is not None or (
        previous.get("restore_epoch") != new_epoch
        or previous.get("backend_instance_id") != new_backend
    )
    report: dict = {
        "ok": True,
        "changed": changed,
        "changed_source": (
            "pending_restore_marker"
            if pending_change is not None
            else ("epoch_compare" if changed else "none")
        ),
        "pending_restore_change": pending_change,
        "baseline_restore_epoch": baseline_epoch,
        "previous": {
            "backend_instance_id": previous.get("backend_instance_id"),
            "restore_epoch": previous.get("restore_epoch"),
            "watermark": previous.get("watermark"),
        },
        "current": {
            "backend_instance_id": new_backend,
            "restore_epoch": new_epoch,
            "watermark": observed_watermark,
        },
        "requeue_basis": "none",
        # requeued = rows the backend accepted as *new* data during this replay.
        # replayed_rows/duplicate_rows expose the full replay accounting so a
        # no-op replay cannot be mistaken for recovery (F15 reporting honesty).
        "requeued": 0,
        "replayed_rows": 0,
        "duplicate_rows": 0,
        "unverifiable_epoch_rows": 0,
        # F15 round-5: rows native-re-read from a legacy/unknown ack epoch or a
        # gap that still owe a current-epoch durable receipt (never auto-replayed
        # by the reconciler; reported on every pass until explicitly delivered).
        "recovery_owed_rows": 0,
        "recovery_owed_keys": [],
        "recovery_owed_by_state": {},
        # F15 round-6: rows discharged because their durable ack names the epoch
        # this pass authoritatively observed as the current recovery target.
        "recovery_target_epoch": None,
        "resolved_recovery_keys": [],
        "requeued_keys": [],
        "gaps": [],
        "delivery": None,
        "replay": {
            "epoch": new_epoch,
            "round_limit": max(1, int(max_replay_rounds)),
            "rounds": [],
            "stopped_reason": "not_started",
            "complete": False,
            "outstanding_rows": 0,
            "outstanding_by_state": {},
        },
        "limitations": list(LIMITATIONS),
    }

    requeue_keys: list[tuple[str, int, int]] = []
    if changed:
        if baseline_epoch is None or new_epoch is None:
            # First contact with no recorded pre-restore epoch. Replaying every
            # row whose epoch cannot be compared would be a blanket resend of the
            # whole outbox, so keep the rows untouched and make the uncertainty
            # durable (reported by every later pass).
            unconfirmed = outbox.delivered_rows_unconfirmed(new_epoch)
            if unconfirmed:
                outbox.mark_epoch_unverified([_key(row) for row in unconfirmed])
            report["requeue_basis"] = "no_trusted_baseline"
            _add_limitation(report, NO_BASELINE_LIMITATION)
        else:
            # F15: replay per ack epoch, never by the current global watermark.
            # Rows acknowledged in another epoch may have been lost by the
            # restore; KW dedupes the ones the snapshot already has.
            report["requeue_basis"] = "ack_epoch_mismatch"
            for row in outbox.delivered_rows_epoch_mismatch(new_epoch):
                key = _key(row)
                reusable, reason = _payload_is_reusable(row)
                if not reusable:
                    # Keep the unrecoverable gap visible across passes instead of
                    # only in the pass that happened to detect it.
                    outbox.mark_gap(key, reason)
                    continue
                requeue_keys.append(key)
            if requeue_keys:
                report["replayed_rows"] = int(
                    outbox.requeue(requeue_keys, replay_epoch=new_epoch)
                )
                report["requeued_keys"] = [list(key) for key in requeue_keys]
        outbox.set_remote_meta(
            backend_instance_id=new_backend,
            restore_epoch=new_epoch,
            watermark=observed_watermark,
            source="reconcile",
        )

    # F15 round-6: this pass just observed the backend identity authoritatively.
    # Durably bind that epoch as the current recovery target and atomically
    # discharge obligations already proven by a durable ack naming it (e.g. a
    # normal delivery that ran before this reconcile pass adopted the
    # post-restore epoch, or a delivery interrupted between apply_receipt and
    # set_remote_meta). A stale/non-durable ack cannot be discharged here; it
    # stays visible as recovery_owed_rows and keeps ok=false.
    resolved_recovery = outbox.observe_recovery_target(new_epoch)
    report["recovery_target_epoch"] = outbox.recovery_target_epoch()
    report["resolved_recovery_keys"] = [list(key) for key in resolved_recovery]

    # -- Bounded replay rounds (also resumes a replay left by an earlier pass) --
    rounds: list[dict] = []
    delivery_total: dict | None = None
    stopped_reason = "drained"
    outstanding = outbox.outstanding_replay_rows()
    if outstanding:
        if new_epoch is None:
            stopped_reason = "no_target_epoch"
        elif not deliver:
            stopped_reason = "deliver_disabled"
        else:
            stopped_reason = "round_limit"
            for index in range(1, max(1, int(max_replay_rounds)) + 1):
                before = len(outbox.outstanding_replay_rows())
                counters = deliver_once(outbox, client, now=now)
                delivery_total = _aggregate_delivery(delivery_total, counters)
                remaining = outbox.outstanding_replay_rows()
                rounds.append(
                    {
                        "round": index,
                        "delivery": counters,
                        "replay_outstanding_rows": len(remaining),
                        "remaining": outbox.stats()["by_state"],
                    }
                )
                if not remaining:
                    stopped_reason = "drained"
                    break
                # No receipt at all (nothing ready, e.g. retry backoff): further
                # rounds cannot make progress without the clock advancing.
                if not any(int(counters.get(name) or 0) for name in counters):
                    stopped_reason = "no_progress"
                    break
                if len(remaining) >= before and not (
                    int(counters.get("accepted") or 0)
                    + int(counters.get("duplicate") or 0)
                    + int(counters.get("withheld") or 0)
                    + int(counters.get("excluded") or 0)
                ):
                    stopped_reason = "no_progress"
                    break
            else:
                stopped_reason = "round_limit"
    if delivery_total is not None:
        report["delivery"] = delivery_total
        report["requeued"] = int(delivery_total.get("accepted") or 0)
        report["duplicate_rows"] = int(delivery_total.get("duplicate") or 0)

    # -- Persistent unresolved state, re-derived from the durable outbox on
    # every pass (changed=false included) --------------------------------------
    unverifiable = outbox.unverifiable_rows()
    report["unverifiable_epoch_rows"] = len(unverifiable)
    unknown_epoch_rows = [row for row in unverifiable if row.get("ack_restore_epoch") is None]
    if unknown_epoch_rows:
        _add_limitation(report, UNVERIFIABLE_LIMITATION)
    report["gaps"] = [
        _gap(row, row.get("last_error_code") or "unrecoverable_payload")
        for row in outbox.gap_rows()
    ]
    outstanding = outbox.outstanding_replay_rows()
    replay_complete = not outstanding
    if not replay_complete and stopped_reason in (
        "round_limit",
        "no_progress",
        "deliver_disabled",
        "no_target_epoch",
    ):
        _add_limitation(report, REPLAY_INCOMPLETE_LIMITATION)
    # F15 round-5/6: durable recovery responsibility left by native re-reads. It
    # is re-derived from the outbox on every pass (changed=false included) and is
    # only gone once a real delivery persisted a *durable* receipt naming the
    # current recovery target epoch (see observe_recovery_target above). The
    # reconciler deliberately does not auto-deliver these rows: ordinary pending
    # rows are not gaps, and un-reread legacy delivered rows must not be
    # mass-replayed.
    owed = outbox.recovery_owed_rows()
    report["recovery_owed_rows"] = len(owed)
    report["recovery_owed_keys"] = [list(_key(row)) for row in owed]
    report["recovery_owed_by_state"] = _state_counts(owed)
    if owed:
        _add_limitation(report, RECOVERY_OWED_LIMITATION)

    # Only clear detections for the epoch whose replay actually drained; a marker
    # for a different epoch stays pending for the pass that owns it.
    if replay_complete and new_epoch is not None:
        for change in outbox.restore_changes():
            if change["cleared_at"] is None and change["observed_restore_epoch"] == new_epoch:
                outbox.clear_restore_change(int(change["id"]))

    report["pending_restore_change_after"] = outbox.pending_restore_change()
    report["remaining"] = outbox.stats()["by_state"]
    report["replay"] = {
        "epoch": new_epoch,
        "round_limit": max(1, int(max_replay_rounds)),
        "rounds": rounds,
        "stopped_reason": stopped_reason,
        "complete": replay_complete,
        "outstanding_rows": len(outstanding),
        "outstanding_by_state": _state_counts(outstanding),
        "outstanding_epoch_rows": sum(
            1 for row in outstanding if new_epoch is not None and row.get("replay_epoch") == new_epoch
        ),
    }
    # "ok" must not claim success while a known gap, an unverifiable epoch row, a
    # native-reread recovery obligation or an unfinished replay still exists
    # (Codex F15: changed=false/ok=true must never hide lost data).
    report["ok"] = bool(
        replay_complete and not report["gaps"] and not unverifiable and not owed
    )
    return report
