"""T10: DSH session-event capture, derived exclusions, cursor and failure turns."""
from __future__ import annotations

import json

import pytest

from dma import contracts
from dma.adapters.dsh.reader import SourceReadError
from dma.adapters.dsh.reader import sync_on_turn_stopping
from dma.pipeline import capture
from dma.validation import validate_event

from .conftest import dsh_adapter, dsh_event, dsh_reader, write_dsh_log


def _base_log(tmp_path):
    events = [
        dsh_event(0, "turn/start", {"turn": 1}),
        dsh_event(1, "user/message", {"turn": 1, "content": "松塔项目端口改为4200"}),
        dsh_event(2, "assistant/message", {"turn": 1, "step": 1, "message": {"content": [{"type": "text", "text": "已记录4200"}]}}),
        dsh_event(3, "tool/call", {"turn": 1, "step": 1, "callId": "call:1", "name": "bash", "arguments": "{\"cmd\":\"curl -H 'token: SECRET'\"}"}),
        dsh_event(4, "tool/result", {"turn": 1, "step": 1, "message": {"content": [{"type": "text", "text": "端口 4200 监听正常"}], "toolCallId": "call:1"}}),
        dsh_event(5, "runtime-context", {"text": "DSH injected runtime context snapshot"}),
        dsh_event(6, "system/message", {"turn": 1, "step": 1, "message": {"content": "You are DSH."}}),
        dsh_event(7, "developer/message", {"turn": 1, "step": 1, "message": {"content": "skill instructions"}}),
        dsh_event(8, "plugin:custom", {"note": "unknown but ignorable"}, ignorable=True),
        dsh_event(9, "compaction/end"),
        dsh_event(10, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
    ]
    return write_dsh_log(tmp_path / "session.jsonl", events)


def test_reads_surface_events_and_excludes_system_and_derived(tmp_path):
    reader = dsh_reader(_base_log(tmp_path))
    pairs, final = reader.read_since("dsh:session:dsh-test", None)
    records = [record for record, _position in pairs]
    assert final == "10"
    assert [record.native_event_id for record in records] == [
        "session:dsh-test:seq:1",
        "session:dsh-test:seq:2",
        "session:dsh-test:seq:4",
        "session:dsh-test:seq:5",
    ]
    by_seq = {record.native_event_id: record for record in records}
    assert by_seq["session:dsh-test:seq:1"].origin_kind == "user_input"
    assert by_seq["session:dsh-test:seq:2"].origin_kind == "assistant_reply"
    tool = by_seq["session:dsh-test:seq:4"]
    assert tool.role == "tool" and tool.origin_kind == "tool_execution"
    assert "call:call:1" in tool.aliases
    context = by_seq["session:dsh-test:seq:5"]
    assert context.origin_kind == "memory_injection"


def test_injected_user_message_is_classified_as_memory_injection(tmp_path):
    events = [
        dsh_event(0, "turn/start", {"turn": 1}),
        dsh_event(1, "user/message", {"turn": 1, "content": "cron reminder text", "source": {"kind": "inject"}}),
        dsh_event(2, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
    ]
    log = write_dsh_log(tmp_path / "session.jsonl", events)
    records = [record for record, _ in dsh_reader(log).read_since("dsh:s", None)[0]]
    assert records[0].origin_kind == "memory_injection"


def test_unknown_required_event_is_refused(tmp_path):
    events = [dsh_event(0, "user/message", {"content": "x"}), dsh_event(1, "future/required/event", {})]
    log = write_dsh_log(tmp_path / "session.jsonl", events)
    with pytest.raises(SourceReadError):
        dsh_reader(log).read_since("dsh:s", None)


def test_non_contiguous_seq_is_refused(tmp_path):
    events = [dsh_event(0, "user/message", {"content": "x"}), dsh_event(2, "user/message", {"content": "y"})]
    log = write_dsh_log(tmp_path / "session.jsonl", events)
    with pytest.raises(SourceReadError):
        dsh_reader(log).read_since("dsh:s", None)


def test_turn_stopping_sync_is_incremental_and_idempotent(tmp_path, redactor, outbox):
    log = _base_log(tmp_path)
    reader = dsh_reader(log)
    adapter = dsh_adapter(redactor)
    first = sync_on_turn_stopping(reader, adapter, outbox)
    assert first["inserted"] == 4
    second = sync_on_turn_stopping(reader, adapter, outbox)
    assert second["inserted"] == 0
    with outbox.read() as conn:
        roles = {
            json.loads(row["payload_json"])["source"]["origin_kind"]
            for row in conn.execute("SELECT payload_json FROM outbox").fetchall()
        }
    assert {"user_input", "assistant_reply", "tool_execution", "memory_injection"} <= roles


def test_failed_turn_still_captures_user_input_and_tool_error(tmp_path, redactor, outbox):
    events = [
        dsh_event(0, "turn/start", {"turn": 1}),
        dsh_event(1, "user/message", {"turn": 1, "content": "运行备份脚本"}),
        dsh_event(2, "assistant/attempt", {"turn": 1, "step": 1, "stream": [{"kind": "reasoning", "text": "private chain"}]}),
        dsh_event(3, "tool/result", {"turn": 1, "step": 1, "message": {"content": [{"type": "text", "text": "permission denied"}], "toolCallId": "call:9", "isError": True}}),
        dsh_event(4, "turn/end", {"turn": 1, "reason": {"kind": "error", "error": {"message": "tool failed", "code": "TOOL_ERROR"}}}),
    ]
    log = write_dsh_log(tmp_path / "failed.jsonl", events)
    reader = dsh_reader(log)
    adapter = dsh_adapter(redactor)
    result = sync_on_turn_stopping(reader, adapter, outbox)
    assert result["inserted"] == 2  # user + tool result, no reasoning stream attempt
    with outbox.read() as conn:
        payloads = [json.loads(row["payload_json"]) for row in conn.execute("SELECT payload_json FROM outbox").fetchall()]
    assert {payload["source"]["role"] for payload in payloads} == {"user", "tool"}
    tool_event = next(payload for payload in payloads if payload["source"]["role"] == "tool")
    assert "tool-error" in tool_event["source"]["aliases"]


def test_adapter_events_are_valid_and_redacted(tmp_path, redactor, outbox):
    events = [
        dsh_event(0, "user/message", {"content": "数据库密码: CANARY-PW-7f3a"}),
        dsh_event(1, "tool/result", {"message": {"content": [{"type": "text", "text": "token sk-CANARY-API-91b2c3d4e5f6g7h8"}]}}),
    ]
    log = write_dsh_log(tmp_path / "secret.jsonl", events)
    reader = dsh_reader(log)
    adapter = dsh_adapter(redactor)
    capture(reader, outbox, stream="dsh:s", convert=adapter.to_events)
    assert b"CANARY-PW-7f3a" not in outbox.db_path.read_bytes()
    assert b"CANARY-API-91b2c3d4e5f6g7h8" not in outbox.db_path.read_bytes()
    with outbox.read() as conn:
        payloads = [json.loads(row["payload_json"]) for row in conn.execute("SELECT count(*) AS c, payload_json FROM outbox").fetchall() if False]
    with outbox.read() as conn:
        payloads = [json.loads(row["payload_json"]) for row in conn.execute("SELECT payload_json FROM outbox").fetchall()]
    for payload in payloads:
        assert validate_event(payload) == []
        assert payload["redaction"]["status"] == "redacted"
        assert payload["source"]["host"] == "dsh"


def test_empty_session_log_advances_nothing(tmp_path, redactor, outbox):
    log = write_dsh_log(tmp_path / "empty.jsonl", [dsh_event(0, "turn/start", {"turn": 1}), dsh_event(1, "turn/end", {"turn": 1, "reason": {"kind": "blocked"}})])
    reader = dsh_reader(log)
    result = sync_on_turn_stopping(reader, dsh_adapter(redactor), outbox)
    assert result["captured_events"] == 0
    assert outbox.cursor("dsh:session:dsh-test") is None



def test_compaction_summary_is_marked_derived(tmp_path):
    events = [
        dsh_event(0, "user/message", {"turn": 1, "content": "原始用户消息"}),
        dsh_event(
            1,
            "assistant/message",
            {"turn": 2, "step": 1, "message": {"content": [{"type": "text", "text": "压缩后的摘要"}]}},
            surfaceOp={"op": "replace", "startSeq": 0, "endSeq": 0},
            sourceEventSeqs=[0],
        ),
        dsh_event(2, "turn/end", {"turn": 2, "reason": {"kind": "completed"}}),
    ]
    log = write_dsh_log(tmp_path / "compacted.jsonl", events)
    records = [record for record, _ in dsh_reader(log).read_since("dsh:s", None)[0]]
    by_id = {record.native_event_id: record for record in records}
    assert by_id["session:dsh-test:seq:1"].origin_kind == "maintenance"
    assert "derived" in by_id["session:dsh-test:seq:1"].aliases
