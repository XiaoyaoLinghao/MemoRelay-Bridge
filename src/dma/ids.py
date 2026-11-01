"""Canonical identifiers, digests and chunking for the DMA/KW contract v1.

Copied semantics from the frozen contract (ID-01): ``event_id`` is
``evt:`` + SHA-256 of the compact UTF-8 JSON array
``[workspace_id, instance_id, native_event_id]``. Splitting happens on whole
logical messages *after* redaction and never truncates content.
"""

from __future__ import annotations

import hashlib
import json

from .errors import InvalidEventError

MAX_PART_CHARS = 250_000
MAX_PART_BYTES = 1024 * 1024
MAX_BATCH_EVENTS = 100
MAX_BATCH_BYTES = 5 * 1024 * 1024

EMPTY_TEXT = ""
EMPTY_BODY_SHA256 = hashlib.sha256(b"").hexdigest()


def compact_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def body_digest(text: str) -> str:
    return sha256_hex(text.encode("utf-8"))


def canonical_event_id(workspace_id: str, instance_id: str, native_event_id: str) -> str:
    return "evt:" + sha256_hex(
        compact_json([workspace_id, instance_id, native_event_id])
    )


def split_text(
    text: str,
    *,
    max_chars: int = MAX_PART_CHARS,
    max_bytes: int = MAX_PART_BYTES,
) -> list[str]:
    """Split one full message into contract-limited parts (never head-truncate).

    Parts contain every input code point exactly once, in order; an empty text
    yields a single empty part (the retract/empty-body shape).
    """
    if not isinstance(text, str):
        raise InvalidEventError("body text must be a string")
    if text == "":
        return [EMPTY_TEXT]
    parts: list[str] = []
    current: list[str] = []
    current_bytes = 0
    for char in text:
        char_bytes = len(char.encode("utf-8"))
        if current and (len(current) + 1 > max_chars or current_bytes + char_bytes > max_bytes):
            parts.append("".join(current))
            current = []
            current_bytes = 0
        current.append(char)
        current_bytes += char_bytes
    if current:
        parts.append("".join(current))
    if "".join(parts) != text:
        raise InvalidEventError("internal chunking error: content would be lost")
    return parts


def part_limits_ok(text: str) -> list[str]:
    problems: list[str] = []
    if len(text) > MAX_PART_CHARS:
        problems.append(f"part exceeds {MAX_PART_CHARS} characters")
    if len(text.encode("utf-8")) > MAX_PART_BYTES:
        problems.append(f"part exceeds {MAX_PART_BYTES} UTF-8 bytes")
    return problems
