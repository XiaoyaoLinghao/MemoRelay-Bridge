"""Shared adapter logic: native record -> sanitized contract events."""

from __future__ import annotations

from dma.event import build_message_events
from dma.reader import NativeRecord, source_envelope


class NativeMessageAdapter:
    """Convert read-only native records into redacted v1 source events."""

    def __init__(self, redactor, *, workspace_id: str, instance_id: str, host: str) -> None:
        self.redactor = redactor
        self.workspace_id = workspace_id
        self.instance_id = instance_id
        self.host = host

    def to_events(self, record: NativeRecord) -> list[dict]:
        return build_message_events(
            redactor=self.redactor,
            workspace_id=self.workspace_id,
            source=source_envelope(record, host=self.host, instance_id=self.instance_id),
            event_time=record.event_time,
            text=record.text,
            content_revision=record.content_revision,
            project_ids=record.project_ids,
        )
