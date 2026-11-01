"""Read-only native source interface for DMA adapters (T03 forerunner).

Adapters never write to a host's native store: they expose a stable checkpoint
and replay events after it. If a host lacks a stable id, the adapter must persist
its own mapping instead of relying on line numbers in the product path (the JSONL
reader here is a fixture/test reader only).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Protocol

from .errors import SourceReadError


@dataclass(frozen=True)
class NativeRecord:
    native_event_id: str
    text: str
    role: str = "user"
    origin_kind: str = "user_input"
    session_id: str = "session:fixture"
    actor_id: str = "actor:fixture"
    native_schema: str = "fixture-jsonl-1"
    content_revision: str = "native:1"
    locator: str = ""
    event_time: dict = field(
        default_factory=lambda: {
            "value": None,
            "precision": "unknown",
            "timezone": "Asia/Shanghai",
        }
    )
    project_ids: tuple[str, ...] = ()
    turn_id: str | None = None
    parent_native_event_id: str | None = None
    aliases: tuple[str, ...] = ()


class SourceReader(Protocol):
    def read_since(
        self, stream: str, position: str | None
    ) -> tuple[list[tuple[NativeRecord, str]], str | None]:
        """Return (record, checkpoint-after-record) pairs and the final position."""


class JsonlReader:
    """Read-only JSONL fixture reader used by tests and local smoke runs."""

    def __init__(self, path: str | Path, *, host: str = "hermes", instance_id: str = "instance:fixture") -> None:
        self.path = Path(path)
        self.host = host
        self.instance_id = instance_id

    def read_since(
        self, stream: str, position: str | None
    ) -> tuple[list[tuple[NativeRecord, str]], str | None]:
        if not self.path.is_file():
            raise SourceReadError(f"source file not found: {self.path}")
        try:
            start = int(position) if position else 0
        except (TypeError, ValueError):
            raise SourceReadError("source checkpoint is not a line offset") from None
        pairs: list[tuple[NativeRecord, str]] = []
        total = 0
        with self.path.open("r", encoding="utf-8") as handle:
            for total, line in enumerate(handle, start=1):
                if total <= start:
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    raise SourceReadError(f"unparsable source record at offset {total}") from None
                pairs.append((_record_from_json(raw, self), str(total)))
        return pairs, str(total)


def _record_from_json(raw: dict, reader: JsonlReader) -> NativeRecord:
    try:
        event_time = raw.get("event_time") or {
            "value": None,
            "precision": "unknown",
            "timezone": "Asia/Shanghai",
        }
        return NativeRecord(
            native_event_id=str(raw["native_event_id"]),
            text=str(raw.get("text") or ""),
            role=str(raw.get("role") or "user"),
            origin_kind=str(raw.get("origin_kind") or "user_input"),
            session_id=str(raw.get("session_id") or "session:fixture"),
            actor_id=str(raw.get("actor_id") or "actor:fixture"),
            native_schema=str(raw.get("native_schema") or "fixture-jsonl-1"),
            content_revision=str(raw.get("content_revision") or "native:1"),
            locator=str(raw.get("locator") or f"fixture://{reader.path.name}/{raw['native_event_id']}"),
            event_time=event_time,
            project_ids=tuple(raw.get("project_ids") or ()),
            turn_id=raw.get("turn_id"),
            parent_native_event_id=raw.get("parent_native_event_id"),
            aliases=tuple(raw.get("aliases") or ()),
        )
    except (KeyError, TypeError) as exc:
        raise SourceReadError(f"invalid fixture record: {exc}") from None


def source_envelope(record: NativeRecord, *, host: str, instance_id: str) -> dict:
    source = {
        "instance_id": instance_id,
        "host": host,
        "actor_id": record.actor_id,
        "session_id": record.session_id,
        "native_event_id": record.native_event_id,
        "native_schema": record.native_schema,
        "role": record.role,
        "origin_kind": record.origin_kind,
        "locator": record.locator,
    }
    if record.turn_id:
        source["turn_id"] = record.turn_id
    if record.aliases:
        source["aliases"] = list(record.aliases)
    return source
