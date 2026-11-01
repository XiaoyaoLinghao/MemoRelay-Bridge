"""T12: DSH dynamic-context plugin against the installed SystemPrompt component.

Skips when Node or the installed DSH release is unavailable. Uses an isolated KW
service/socket and synthetic claims; no user profile or model call.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

pytest.importorskip("knowledge_weaver")

from knowledge_weaver import contracts  # noqa: E402
from knowledge_weaver.service import ServiceApp  # noqa: E402
from knowledge_weaver.store import Store, receive_batch, register_client  # noqa: E402
from knowledge_weaver.store.claims import record_claim_revision  # noqa: E402

NODE = shutil.which("node")
HARNESS = Path(__file__).resolve().parent / "node" / "dsh_plugin_harness.mjs"
RELEASE_ROOT = Path.home() / ".local/share/deepseek-harness/releases/0.2.1-rc.2/node_modules"

pytestmark = pytest.mark.skipif(
    NODE is None or not HARNESS.is_file(),
    reason="Node.js or the DSH plugin harness is unavailable",
)


def _valid_event(native_id: str, text: str) -> dict:
    event_id = contracts.canonical_event_id("owner:demo", "instance:dsh-test", native_id)
    event = {
        "schema_version": "1.0",
        "event_id": event_id,
        "event_revision": 1,
        "workspace_id": "owner:demo",
        "operation": "create",
        "source": {
            "instance_id": "instance:dsh-test",
            "host": "dsh",
            "actor_id": "actor:dsh-test",
            "session_id": "session:node-test",
            "native_event_id": native_id,
            "native_schema": "dsh-session-events-0.2.1",
            "role": "user",
            "origin_kind": "user_input",
            "locator": f"dsh://session:node-test/{native_id}",
        },
        "event_time": {"value": "2026-10-30T01:00:00Z", "precision": "instant", "timezone": "UTC"},
        "body": {
            "text": text,
            "sha256": contracts.body_digest(text),
            "part_index": 0,
            "part_count": 1,
            "content_revision": "native:1",
            "format": "text/plain",
        },
        "redaction": {"policy_version": "redact:1", "status": "passed", "replacements": 0, "credential_refs": []},
        "project_ids": ["project:songta"],
        "parent_revision": None,
        "derived_from": [],
    }
    return event


def _claim(store, event, value, predicate, text):
    contract = contracts.validate_event(event)
    assert contract == [], [str(issue) for issue in contract]
    receive_batch(store, {"schema_version": "1.0", "batch_id": f"batch:{event['event_id'][-8:]}", "events": [event]})
    encoded = text.encode("utf-8")
    record_claim_revision(
        store,
        workspace_id="owner:demo",
        project_id="project:songta",
        subject_id="subject:songta-server",
        predicate=predicate,
        facet="desired" if predicate == "server.port" else "decision",
        qualifiers={},
        value=value,
        display_text=text,
        authority="user_confirmed",
        assertion_kind="explicit_statement",
        event_time=event["event_time"],
        evidence=[
            {
                "event_id": event["event_id"],
                "event_revision": 1,
                "part_index": 0,
                "start_byte": 0,
                "end_byte": len(encoded),
                "body_sha256": event["body"]["sha256"],
                "availability": "full",
            }
        ],
    )


@pytest.fixture()
def kw_service(tmp_path):
    store = Store(tmp_path / "kw" / "knowledge.db")
    register_client(
        store,
        client_id="client:dsh-main",
        workspace_id="owner:demo",
        host="dsh",
        instance_id="instance:dsh-test",
        actor_ids=["*"],
        project_ids=["project:songta"],
    )
    port_text = "松塔项目端口为4200，字面量 {{do_not_interpolate}} 保持不变"
    backup_text = "松塔项目备份周期为每周"
    _claim(store, _valid_event("session:node-test:seq:1", port_text), 4200, "server.port", port_text)
    _claim(
        store,
        _valid_event("session:node-test:seq:2", backup_text),
        "weekly",
        "backup_interval",
        backup_text,
    )
    socket_dir = Path(tempfile.mkdtemp(prefix="umdsn-", dir="/tmp"))
    socket_path = socket_dir / "kw.sock"
    app = ServiceApp(store, socket_path, embedder=None).start()
    try:
        yield store, app
    finally:
        app.stop()
        shutil.rmtree(socket_dir, ignore_errors=True)


def test_dsh_plugin_dynamic_context(kw_service):
    store, app = kw_service
    completed = subprocess.run(
        [NODE, str(HARNESS), str(app.socket_path), "client:dsh-main"],
        capture_output=True,
        text=True,
        timeout=120,
        env={**__import__("os").environ, "DSH_RELEASE_ROOT": str(RELEASE_ROOT)},
    )
    if completed.returncode == 3:
        pytest.skip("installed DSH release not available for the component harness")
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert payload["passed"] is True, payload
    assert payload["externalLlmCalls"] == 0
    assert len(payload["checks"]) >= 5
