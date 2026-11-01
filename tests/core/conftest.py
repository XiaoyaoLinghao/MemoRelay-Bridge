"""Fixtures for DMA core tests: synthetic hosts, fake KW UDS server, fake clock."""
from __future__ import annotations

import datetime as dt
import json
import shutil
import socketserver
import tempfile
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from dma.event import build_message_events
from dma.outbox import Outbox
from dma.reader import source_envelope
from dma.redaction import Redactor


class FakeClock:
    def __init__(self) -> None:
        self.now = dt.datetime(2026, 10, 30, 1, 0, 0, tzinfo=dt.timezone.utc)

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + dt.timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def outbox(tmp_path, clock):
    return Outbox(
        tmp_path / "dma" / "outbox.db",
        clock=clock,
        workspace_id="owner:demo",
        instance_id="instance:hermes-test",
        max_attempts=3,
        backoff_delays=(10, 60, 300),
    )


@pytest.fixture
def redactor():
    return Redactor()


def make_source(
    native_id="message:1",
    *,
    session_id="session:test",
    actor_id="jun-test",
    role="user",
    origin_kind="user_input",
    turn_id=None,
    aliases=(),
    parent_event_id=None,
):
    record = type(
        "Record",
        (),
        {
            "native_event_id": native_id,
            "session_id": session_id,
            "actor_id": actor_id,
            "role": role,
            "origin_kind": origin_kind,
            "native_schema": "test-host-1",
            "locator": f"test://{session_id}/{native_id}",
            "turn_id": turn_id,
            "aliases": tuple(aliases),
            "parent_native_event_id": parent_event_id,
        },
    )()
    return source_envelope(record, host="hermes", instance_id="instance:hermes-test")


def build_events(
    text="松塔项目端口改为4200。",
    *,
    native_id="message:1",
    redactor=None,
    source=None,
    event_time=None,
    project_ids=("project:songta",),
    operation="create",
    event_revision=1,
    parent_revision=None,
    content_revision="native:1",
):
    return build_message_events(
        redactor=redactor or Redactor(),
        workspace_id="owner:demo",
        source=source or make_source(native_id),
        event_time=event_time
        or {"value": "2026-10-30T01:00:00+00:00", "precision": "instant", "timezone": "UTC"},
        text=text,
        content_revision=content_revision,
        project_ids=project_ids,
        operation=operation,
        event_revision=event_revision,
        parent_revision=parent_revision,
    )


class _FakeKwHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            batch = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            batch = {}
        server = self.server
        server.requests.append(batch)
        try:
            status, payload = server.handler(batch)
        except Exception:  # noqa: BLE001 - stub
            status, payload = 500, {
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "stub failure",
                    "retryable": True,
                    "request_id": "req:stub",
                }
            }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):  # client timed out
            pass
        self.close_connection = True


class FakeKwServer:
    def __init__(self, handler):
        self.handler = handler
        self.requests: list[dict] = []
        self.directory = Path(tempfile.mkdtemp(prefix="dma-kw-", dir="/tmp"))
        self.socket_path = self.directory / "kw.sock"
        import socketserver as _socketserver

        class _Server(_socketserver.ThreadingMixIn, _socketserver.UnixStreamServer):
            daemon_threads = True

        self.server = _Server(str(self.socket_path), _FakeKwHandler)
        self.server.handler = handler
        self.server.requests = self.requests
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        shutil.rmtree(self.directory, ignore_errors=True)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc_info):
        self.stop()


def accept_all(batch, *, watermark=1):
    receipts = []
    for event in batch.get("events", []):
        body = event.get("body") or {}
        receipts.append(
            {
                "event_id": event["event_id"],
                "event_revision": event["event_revision"],
                "part_index": body.get("part_index", 0),
                "disposition": "accepted",
                "durable_remote": True,
                "processing": "pending",
                "searchability": "none",
                "error_code": None,
                "receipt_id": "rcpt:" + "a" * 32,
            }
        )
    return 200, {
        "schema_version": "1.0",
        "backend_instance_id": "kw:stub",
        "restore_epoch": "epoch:1",
        "watermark": watermark,
        "receipts": receipts,
    }
