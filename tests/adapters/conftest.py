"""Host adapter fixtures: synthetic Hermes SQLite and DSH session logs."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3

import pytest

from dma.adapters.dsh.adapter import DshAdapter
from dma.adapters.dsh.reader import DshSessionReader
from dma.adapters.hermes.adapter import HermesAdapter
from dma.adapters.hermes.reader import HermesSqliteReader
from dma.outbox import Outbox
from dma.redaction import Redactor

INSTANCE_HERMES = "instance:hermes-test"
INSTANCE_DSH = "instance:dsh-test"
WORKSPACE = "owner:demo"


class FixedClock:
    def __init__(self) -> None:
        self.now = dt.datetime(2026, 10, 30, 2, 0, 0, tzinfo=dt.timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + dt.timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return FixedClock()


@pytest.fixture
def redactor():
    return Redactor()


@pytest.fixture
def outbox(tmp_path, clock):
    return Outbox(
        tmp_path / "dma" / "outbox.db",
        clock=clock,
        workspace_id=WORKSPACE,
        instance_id=INSTANCE_HERMES,
    )


def make_hermes_db(path, sessions, messages):
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
            "content TEXT, timestamp REAL, active INTEGER, compacted INTEGER, "
            "_compressed_summary INTEGER, display_kind TEXT)"
        )
        connection.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT, end_reason TEXT)"
        )
        connection.executemany(
            "INSERT INTO sessions (id, parent_session_id, end_reason) VALUES (?, ?, ?)",
            sessions,
        )
        for message in messages:
            connection.execute(
                "INSERT INTO messages (id, session_id, role, content, timestamp, active, "
                "compacted, _compressed_summary, display_kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    message["id"],
                    message.get("session_id", "session:root"),
                    message.get("role", "user"),
                    json.dumps(message.get("text", ""), ensure_ascii=False)
                    if not message.get("raw_content")
                    else message["raw_content"],
                    message["timestamp"],
                    message.get("active", 1),
                    message.get("compacted", 0),
                    message.get("summary", 0),
                    message.get("display_kind"),
                ),
            )
        connection.commit()
    finally:
        connection.close()
    return path


def hermes_reader(path, *, settle_seconds=0.0, clock=None, profile="main"):
    return HermesSqliteReader(
        path,
        instance_id=INSTANCE_HERMES,
        profile=profile,
        settle_seconds=settle_seconds,
        clock=clock,
    )


def hermes_adapter(redactor):
    return HermesAdapter(redactor, workspace_id=WORKSPACE, instance_id=INSTANCE_HERMES)


def dsh_event(seq, event_type, data=None, *, time_ms=None, **fields):
    event = {
        "type": event_type,
        "seq": seq,
        "time": time_ms if time_ms is not None else 1761789600000 + seq * 1000,
        "data": data or {},
    }
    event.update(fields)
    return event


def write_dsh_log(path, events):
    path.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
        encoding="utf-8",
    )
    return path


def dsh_reader(path, *, session_id="session:dsh-test"):
    return DshSessionReader(
        path, instance_id=INSTANCE_DSH, session_id=session_id, actor_id="actor:dsh-test"
    )


def dsh_adapter(redactor):
    return DshAdapter(redactor, workspace_id=WORKSPACE, instance_id=INSTANCE_DSH)
