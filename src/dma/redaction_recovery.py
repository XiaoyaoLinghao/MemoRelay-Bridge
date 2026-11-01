"""SEC-01 recovery: purge suspect outbox bodies, re-read native source, retry.

When KW's independent check returns ``withheld`` with ``REDACTION_REQUIRED``,
the adapter must (1) purge the suspect body from its outbox immediately, (2)
re-read the native source under the updated redaction policy, and (3) only then
retry. The re-read events go through the normal adapter/redact path, so the new
payload carries the post-redaction digest; if the content is still unredactable
the message becomes a receipt-only withheld event. If the native record no
longer exists, the gap is reported explicitly and the row is not retried.
"""

from __future__ import annotations

import json

from .errors import DeliveryError, SourceReadError
from .outbox import Outbox

MAX_SOURCES = 8


class RedactionRecoveryError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _index_sources(sources) -> dict[tuple[str, str], object]:
    """Map (instance_id, native_event_id) -> (reader, adapter, record)."""
    index: dict[tuple[str, str], object] = {}
    for source in sources[:MAX_SOURCES]:
        reader, adapter = source
        stream = f"recovery:{getattr(reader, 'profile', 'source')}"
        try:
            pairs, _position = reader.read_since(stream, None)
        except SourceReadError:
            continue
        instance_id = getattr(reader, "instance_id", "")
        for record, _checkpoint in pairs:
            index[(instance_id, record.native_event_id)] = (reader, adapter, record)
    return index


def reread_after_redaction(
    outbox: Outbox,
    sources,
    *,
    client=None,
    deliver: bool = False,
    now: str | None = None,
) -> dict:
    rows = outbox.redaction_required_rows()
    report: dict = {
        "ok": True,
        "scanned": len(rows),
        "recovered": 0,
        "withheld": 0,
        "gaps": [],
        "failed": [],
        "requeued": 0,
        "delivery": None,
    }
    if not rows:
        return report
    index = _index_sources(sources)
    requeued_keys: list[tuple[str, int, int]] = []
    for row in rows:
        key = (row["event_id"], row["revision"], row["part_index"])
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            payload = {}
        source = payload.get("source") or {}
        instance_id = source.get("instance_id")
        native_id = source.get("native_event_id")
        match = index.get((instance_id, native_id))
        if match is None:
            outbox.mark_gap(key, "native_source_unavailable_after_redaction")
            report["gaps"].append(
                {
                    "event_id": row["event_id"],
                    "event_revision": row["revision"],
                    "part_index": row["part_index"],
                    "instance_id": instance_id,
                    "native_event_id": native_id,
                    "reason": "native_source_unavailable_after_redaction",
                }
            )
            continue
        _reader, adapter, record = match
        events = adapter.to_events(record)
        if not events:
            outbox.mark_gap(key, "native_reread_returned_no_event")
            report["gaps"].append(
                {
                    "event_id": row["event_id"],
                    "event_revision": row["revision"],
                    "part_index": row["part_index"],
                    "instance_id": instance_id,
                    "native_event_id": native_id,
                    "reason": "native_reread_returned_no_event",
                }
            )
            continue
        outbox.replace_row_payload(key, events)
        requeued_keys.append(key)
        if events[0].get("redaction", {}).get("status") == "withheld":
            report["withheld"] += 1
    report["requeued"] = len(requeued_keys)
    if deliver and requeued_keys:
        if client is None:
            raise RedactionRecoveryError("INVALID_REQUEST", "deliver=True requires a KW client")
        try:
            report["delivery"] = __import__("dma.delivery", fromlist=["deliver_once"]).deliver_once(
                outbox, client, now=now
            )
        except DeliveryError as exc:
            report["delivery_error"] = {"code": exc.code, "message": exc.message}
    # Recovered means *durably accepted by KW*, not merely re-read. A retry that
    # KW still rejects must stay visible as failed instead of being counted as a
    # recovery (F03: the old FakeKwServer masked exactly this).
    states = outbox.states_for(requeued_keys)
    report["recovered"] = sum(1 for key in requeued_keys if states.get(key) == "delivered")
    failed = [
        {
            "event_id": key[0],
            "event_revision": key[1],
            "part_index": key[2],
            "state": states.get(key),
        }
        for key in requeued_keys
        if states.get(key) not in ("delivered", "gap")
    ]
    report["failed"] = failed
    report["ok"] = not failed and not report["gaps"] and "delivery_error" not in report
    return report
