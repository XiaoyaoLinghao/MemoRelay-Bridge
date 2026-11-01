"""Capture pipeline: read-only source -> sanitize -> durable outbox (same tx)."""

from __future__ import annotations

import hashlib

from .outbox import Outbox


def capture(
    reader,
    outbox: Outbox,
    *,
    stream: str,
    convert,
) -> dict:
    """Pull new source records, convert them to events, commit outbox+cursor.

    ``convert(record) -> list[dict]`` is supplied by a host adapter; it must
    already have sanitized the text. If conversion fails, no cursor movement or
    outbox write happens for the failed record; earlier records in this call are
    also not committed (the whole batch is re-read next time, and event ids make
    the replay idempotent).
    """
    position = outbox.cursor(stream)
    pairs, final_position = reader.read_since(stream, position)
    if not pairs:
        return {
            "captured_events": 0,
            "inserted": 0,
            "duplicates": 0,
            "cursor": position,
            "source_position": final_position,
        }
    events: list[dict] = []
    last_position = position
    for record, checkpoint in pairs:
        events.extend(convert(record))
        last_position = checkpoint
    if last_position is None:
        return {
            "captured_events": 0,
            "inserted": 0,
            "duplicates": 0,
            "cursor": position,
            "source_position": final_position,
        }
    stored = outbox.capture(events, stream=stream, position=last_position)
    return {
        "captured_events": len(events),
        **stored,
        "cursor": last_position,
        "source_position": final_position,
    }


def fixture_batch_id(stream: str, position: str) -> str:
    return "batch:" + hashlib.sha256(f"{stream}|{position}".encode("utf-8")).hexdigest()[:24]
