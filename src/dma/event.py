"""Build contract-shaped source events from sanitized host messages."""

from __future__ import annotations

from .errors import InvalidEventError
from .ids import EMPTY_BODY_SHA256, EMPTY_TEXT, body_digest, split_text
from .validation import assert_valid_event

REDACT_PASSED = {"policy_version": "redact:1", "status": "passed", "replacements": 0, "credential_refs": []}


def build_part_event(
    *,
    workspace_id: str,
    source: dict,
    event_time: dict,
    text: str,
    part_index: int,
    part_count: int,
    content_revision: str,
    redaction: dict,
    project_ids,
    operation: str = "create",
    event_revision: int = 1,
    parent_revision: int | None = None,
    derived_from=(),
    event_id: str | None = None,
) -> dict:
    from .ids import canonical_event_id

    resolved_event_id = event_id or canonical_event_id(
        workspace_id, source["instance_id"], source["native_event_id"]
    )
    event = {
        "schema_version": "1.0",
        "event_id": resolved_event_id,
        "event_revision": event_revision,
        "workspace_id": workspace_id,
        "operation": operation,
        "source": dict(source),
        "event_time": dict(event_time),
        "body": {
            "text": text,
            "sha256": body_digest(text),
            "part_index": part_index,
            "part_count": part_count,
            "content_revision": content_revision,
            "format": "text/plain",
        },
        "redaction": dict(redaction),
        "project_ids": list(project_ids),
        "parent_revision": parent_revision,
        "derived_from": list(derived_from),
    }
    assert_valid_event(event)
    return event


def build_message_events(
    *,
    redactor,
    workspace_id: str,
    source: dict,
    event_time: dict,
    text: str,
    content_revision: str,
    project_ids,
    operation: str = "create",
    event_revision: int = 1,
    parent_revision: int | None = None,
    derived_from=(),
) -> list[dict]:
    """Redact the full message, then split it into complete contract part events."""
    result = redactor.redact(text)
    if result.status == "withheld":
        return [
            build_part_event(
                workspace_id=workspace_id,
                source=source,
                event_time=event_time,
                text=EMPTY_TEXT,
                part_index=0,
                part_count=1,
                content_revision=content_revision,
                redaction=result.contract(),
                project_ids=project_ids,
                operation=operation,
                event_revision=event_revision,
                parent_revision=parent_revision,
                derived_from=derived_from,
            )
        ]
    parts = split_text(result.text)
    return [
        build_part_event(
            workspace_id=workspace_id,
            source=source,
            event_time=event_time,
            text=part,
            part_index=index,
            part_count=len(parts),
            content_revision=content_revision,
            redaction=result.contract(),
            project_ids=project_ids,
            operation=operation,
            event_revision=event_revision,
            parent_revision=parent_revision,
            derived_from=derived_from,
        )
        for index, part in enumerate(parts)
    ]


def build_retract_events(
    *,
    workspace_id: str,
    source: dict,
    event_time: dict,
    parent_revision: int,
    content_revision: str,
    project_ids,
) -> list[dict]:
    return [
        build_part_event(
            workspace_id=workspace_id,
            source=source,
            event_time=event_time,
            text=EMPTY_TEXT,
            part_index=0,
            part_count=1,
            content_revision=content_revision,
            redaction=dict(REDACT_PASSED),
            project_ids=project_ids,
            operation="retract",
            event_revision=parent_revision + 1,
            parent_revision=parent_revision,
        )
    ]


def build_redact_events(
    *,
    redactor,
    workspace_id: str,
    source: dict,
    event_time: dict,
    parent_revision: int,
    content_revision: str,
    project_ids,
    text: str,
) -> list[dict]:
    result = redactor.redact(text)
    parts = split_text(result.text if result.status != "withheld" else EMPTY_TEXT)
    return [
        build_part_event(
            workspace_id=workspace_id,
            source=source,
            event_time=event_time,
            text=part,
            part_index=index,
            part_count=len(parts),
            content_revision=content_revision,
            redaction=result.contract(),
            project_ids=project_ids,
            operation="redact",
            event_revision=parent_revision + 1,
            parent_revision=parent_revision,
        )
        for index, part in enumerate(parts)
    ]
