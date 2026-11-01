"""T16: v1 daily-memory import as derived history with explicit time precision."""
from __future__ import annotations

import json

import pytest

from dma.errors import SourceReadError
from dma.importers.chat_history import ChatHistoryImporter
from dma.importers.daily_memory import DailyMemoryImporter
from dma.importers.openclaw import OpenClawMemoryReader
from dma.validation import validate_event

from .conftest import write_chat_jsonl, write_daily


def _payloads(outbox):
    with outbox.read() as conn:
        return [json.loads(row["payload_json"]) for row in conn.execute("SELECT payload_json FROM outbox").fetchall()]


def test_daily_import_derived_origin_and_time_precision(tmp_path, outbox, redactor):
    directory = write_daily(tmp_path)
    importer = DailyMemoryImporter(
        workspace_id="owner:demo",
        default_project_id="project:songta",
        redactor=redactor,
    )
    report = importer.import_path(directory, outbox)
    assert report["origin_kind"] == "derived_import"
    assert report["imported_events"] == 4
    payloads = _payloads(outbox)
    assert all(validate_event(event) == [] for event in payloads)
    assert {event["source"]["host"] for event in payloads} == {"legacy"}
    assert {event["source"]["origin_kind"] for event in payloads} == {"derived_import"}
    instant = [event for event in payloads if event["event_time"]["precision"] == "instant"]
    day = [event for event in payloads if event["event_time"]["precision"] == "day"]
    assert instant and day
    assert instant[0]["event_time"]["value"].startswith("2026-05-22T04:00:00")
    assert day[0]["event_time"]["value"] == "2026-05-22"
    assert all(event["project_ids"] == ["project:songta"] for event in payloads)
    assert all("daily:2026-05-22:" in event["source"]["native_event_id"] for event in payloads)
    # Idempotent re-import.
    again = importer.import_path(directory, outbox)
    assert again["duplicates"] == 4
    assert outbox.stats()["total"] == 4


def test_unresolved_projects_are_reported_not_guessed(tmp_path, outbox, redactor):
    directory = write_daily(tmp_path)
    importer = DailyMemoryImporter(workspace_id="owner:demo", redactor=redactor)
    report = importer.import_path(directory, outbox)
    assert report["unresolved_projects"] == ["2026-05-22.md"]
    payloads = _payloads(outbox)
    assert all(event["project_ids"] == [] for event in payloads)


def test_unparsable_lines_are_counted(tmp_path, outbox, redactor):
    directory = write_daily(tmp_path, extra="这不是条目行\n另一个自由文本\n")
    importer = DailyMemoryImporter(workspace_id="owner:demo", redactor=redactor)
    report = importer.import_path(directory, outbox)
    assert report["skipped_lines"] == 2
    assert report["records"] == 4


def test_daily_import_redacts_before_outbox(tmp_path, outbox, redactor):
    directory = write_daily(tmp_path, extra="## 核心要点\n- 06:00 - 数据库密码: CANARY-PW-7f3a\n")
    importer = DailyMemoryImporter(workspace_id="owner:demo", redactor=redactor)
    importer.import_path(directory, outbox)
    assert b"CANARY-PW-7f3a" not in outbox.db_path.read_bytes()
    payloads = _payloads(outbox)
    redacted = [event for event in payloads if event["redaction"]["status"] == "redacted"]
    assert redacted
    assert "[REDACTED:PASSWORD_ASSIGNMENT]" in redacted[0]["body"]["text"]


def test_chat_history_import(tmp_path, outbox, redactor):
    path = write_chat_jsonl(tmp_path, host="codex")
    importer = ChatHistoryImporter(
        host="codex",
        workspace_id="owner:demo",
        instance_id="instance:codex-import",
        project_ids=("project:songta",),
        redactor=redactor,
    )
    report = importer.import_path(path, outbox)
    assert report["skipped_system"] == 1
    assert report["imported_events"] == 4
    payloads = _payloads(outbox)
    assert all(event["source"]["host"] == "codex" for event in payloads)
    assert all(event["source"]["origin_kind"] == "derived_import" for event in payloads)
    assert {event["event_time"]["precision"] for event in payloads} == {"instant", "unknown"}
    again = importer.import_path(path, outbox)
    assert again["duplicates"] == 4


def test_unknown_chat_history_schema_is_refused(tmp_path, outbox, redactor):
    path = tmp_path / "bad.jsonl"
    path.write_text(json.dumps({"unexpected": "shape"}) + "\n", encoding="utf-8")
    importer = ChatHistoryImporter(
        host="claude_code",
        workspace_id="owner:demo",
        instance_id="instance:claude-import",
        redactor=redactor,
    )
    with pytest.raises(SourceReadError):
        importer.import_path(path, outbox)


def test_openclaw_reader_is_read_only_and_derived(tmp_path, outbox, redactor):
    directory = write_daily(tmp_path)
    reader = OpenClawMemoryReader(
        directory,
        workspace_id="owner:demo",
        default_project_id="project:songta",
        redactor=redactor,
    )
    pairs, position = reader.read_since("openclaw:memory", None)
    assert len(pairs) == 4
    assert position.startswith("files:1:")
    records = [record for record, _ in pairs]
    assert {record.origin_kind for record in records} == {"derived_import"}
    report = reader.import_path(outbox)
    assert report["boundary"] == "openclaw-read-only-derived"
    assert outbox.stats()["total"] == 4
    with pytest.raises(SourceReadError):
        OpenClawMemoryReader(tmp_path / "missing", workspace_id="owner:demo").read_since("s", None)
