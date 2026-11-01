"""T03/T04: event builders, withheld flow and capture pipeline atomicity."""
from __future__ import annotations

import json

import pytest

from dma.errors import InvalidEventError
from dma.event import build_message_events, build_retract_events
from dma.redaction import Redactor

from .conftest import build_events, make_source


def test_build_message_events_contract_shape(redactor):
    events = build_events(text="松塔项目端口改为4200。", redactor=redactor)
    assert len(events) == 1
    event = events[0]
    assert event["schema_version"] == "1.0"
    assert event["operation"] == "create"
    assert event["body"]["part_index"] == 0
    assert event["body"]["part_count"] == 1
    assert event["redaction"]["status"] == "passed"


def test_builder_redacts_before_outbox(redactor):
    events = build_events(text="数据库密码: CANARY-PW-7f3a", redactor=redactor)
    assert "CANARY-PW-7f3a" not in json.dumps(events)
    assert "[REDACTED:PASSWORD_ASSIGNMENT]" in events[0]["body"]["text"]
    assert events[0]["redaction"]["status"] == "redacted"
    assert events[0]["redaction"]["replacements"] == 1


def test_withheld_message_builds_empty_body_event(redactor):
    raw = "-----BEGIN PRIVATE KEY-----\nCANARY\n-----END PRIVATE KEY-----"
    events = build_events(text=raw, redactor=redactor)
    assert len(events) == 1
    assert events[0]["redaction"]["status"] == "withheld"
    assert events[0]["body"]["text"] == ""
    assert events[0]["redaction"]["credential_refs"]


def test_builder_refuses_unredacted_secret(redactor):
    from dma.redaction import RedactionResult

    class PassthroughRedactor:
        """Simulated adapter bug: claims passed but leaves the secret in place."""

        def redact(self, text):
            return RedactionResult(text=text, status="passed", replacements=0)

    source = make_source("message:1")
    with pytest.raises(InvalidEventError):
        build_message_events(
            redactor=PassthroughRedactor(),
            workspace_id="owner:demo",
            source=source,
            event_time={"value": "2026-10-30T01:00:00+00:00", "precision": "instant", "timezone": "UTC"},
            text="数据库密码: CANARY-PW-7f3a",
            content_revision="native:1",
            project_ids=("project:songta",),
        )


def test_build_retract_events_shape(redactor):
    source = make_source("message:1")
    events = build_retract_events(
        workspace_id="owner:demo",
        source=source,
        event_time={"value": "2026-10-30T01:00:00+00:00", "precision": "instant", "timezone": "UTC"},
        parent_revision=1,
        content_revision="native:1",
        project_ids=("project:songta",),
    )
    assert events[0]["operation"] == "retract"
    assert events[0]["event_revision"] == 2
    assert events[0]["parent_revision"] == 1
    assert events[0]["body"]["text"] == ""
    assert events[0]["body"]["part_count"] == 1


def _capture(reader, outbox, convert):
    from dma.pipeline import capture

    return capture(reader, outbox, stream="hermes:main", convert=convert)


def test_pipeline_capture_advances_cursor_and_skips_replay(tmp_path, outbox):
    from dma.reader import JsonlReader

    source = tmp_path / "native.jsonl"
    source.write_text(
        "\n".join(
            json.dumps(
                {
                    "native_event_id": f"message:{index}",
                    "text": f"第{index}条记录，端口4200。",
                    "event_time": {
                        "value": "2026-10-30T01:00:00+00:00",
                        "precision": "instant",
                        "timezone": "UTC",
                    },
                },
                ensure_ascii=False,
            )
            for index in range(3)
        )
        + "\n",
        encoding="utf-8",
    )
    reader = JsonlReader(source)

    def convert(record):
        return build_events(native_id=record.native_event_id, text=record.text)

    first = _capture(reader, outbox, convert)
    assert first["inserted"] == 3
    assert outbox.cursor("hermes:main") == "3"
    second = _capture(reader, outbox, convert)
    assert second["inserted"] == 0
    assert second["captured_events"] == 0
    assert outbox.stats()["total"] == 3


def test_pipeline_failure_leaves_no_cursor_or_rows(tmp_path, outbox):
    from dma.reader import JsonlReader

    source = tmp_path / "native.jsonl"
    source.write_text(
        "\n".join(
            json.dumps(
                {
                    "native_event_id": f"message:{index}",
                    "text": f"记录{index}",
                },
                ensure_ascii=False,
            )
            for index in range(2)
        )
        + "\n",
        encoding="utf-8",
    )
    reader = JsonlReader(source)

    def convert(record):
        if record.native_event_id == "message:1":
            raise RuntimeError("adapter bug")
        return build_events(native_id=record.native_event_id, text=record.text)

    with pytest.raises(RuntimeError):
        _capture(reader, outbox, convert)
    assert outbox.cursor("hermes:main") is None
    assert outbox.stats()["total"] == 0
