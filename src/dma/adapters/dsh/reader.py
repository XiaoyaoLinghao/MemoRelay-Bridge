"""Read-only DSH session-event adapter (T10).

DSH persists an append-only, event-sourced session log: every entry is
``{type, seq, time, data, ignorable?, surfaceOp?, sourceEventSeqs?}`` (see
``reference/host-snapshots/dsh-packages/dsh-session`` types). This adapter reads
that log through a JSONL hand-off (the host integration feeds SessionEvent
objects; the reader never writes to a DSH profile):

* ``user/message`` → user input unless the message source marks an injection
  (runtime context, cron, goal continuation, memory injection), in which case it
  is classified ``memory_injection`` so KW stores an exclusion receipt;
* ``assistant/message`` → assistant reply; ``tool/call`` is not captured
  (raw arguments can contain secrets), ``tool/result`` is captured as tool
  evidence with its call id and error flag;
* ``system/message`` / ``developer/message`` are never captured as shared facts
  (host identity/skills stay with the host);
* runtime-context events are recorded as exclusion receipts;
* compaction boundaries and failed-attempt stream records are skipped, but user
  input and tool results from failed turns are still captured on the
  turn-stopping rescan;
* unknown event types without ``ignorable: true`` are refused, exactly like a
  required DSH session event;
* cursor = last contiguous session ``seq``; no timestamp cursor and no line
  numbers, so append-only logs and restarts cannot skip or duplicate events.

The reader is read-only and never prints event content.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import io
from pathlib import Path

from dma.errors import SourceReadError
from dma.ids import sha256_hex
from dma.reader import NativeRecord

SKIP_TYPES = {
    # Native 0.2.1-rc.2 configuration/inbox bookkeeping. The surfaced
    # user/message is authoritative; inbox splices must not duplicate it.
    "permission/preset",
    "sandbox/mode",
    "approval/policy",
    "agent/inbox/spliced",
    "session/title",
    'session/title-llm-request',
    'job/registered',
    'workspace/changes',
    'llm/retry',
    'llm/retry-started',
    'command/run',
    'command/done',
    'approval/asked',
    'approval/decided',
    'subagent/descriptor',
    'agent-preset/selected',
    'model/selection',
    'session-log-deepseek/delivery-accepted',
    'web/deepseek-search-llm-request',
    'team/member',
    'subagent/catalog',
    'working-directory/change',

    "turn/start",
    "turn/end",
    "step/start",
    "step/end",
    "request/header",
    "request/context",
    "assistant/attempt",
    "tool/call",
    "compaction/start",
    "compaction/end",
    "compaction/summary",
    "compaction/prune",
    "session/end-seed",
}
SYSTEM_TYPES = {"system/message", "developer/message"}
INJECTION_SOURCES = {
    'agent-instructions',
    'skill-catalog',
    'time-context',
    'compact-checkpoint',
    'tool-jobs',
    'repeat-tool-reminder',
    'user-approval',
    'agent-message',
    'model-selection',
    'subagent-settled',
    'tool-goal',

    "inject",
    "injected",
    "runtime-context",
    "runtime_context",
    "memory",
    "memory-injection",
    "cron",
    "goal-continuation",
    "goal",
    "skill",
    "file-change",
}


def _iso_from_millis(value) -> dict:
    try:
        moment = _dt.datetime.fromtimestamp(float(value) / 1000.0, tz=_dt.timezone.utc)
    except (TypeError, ValueError, OSError):
        return {"value": None, "precision": "unknown", "timezone": "UTC"}
    return {
        "value": moment.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "precision": "instant",
        "timezone": "UTC",
    }


def _text_of_content(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content["text"]
    return ""


def _message_source(data: dict) -> str:
    source = data.get("source")
    if isinstance(source, dict):
        for key in ("kind", "type", "origin"):
            value = source.get(key)
            if isinstance(value, str):
                return value
    if isinstance(source, str):
        return source
    for key in ("kind", "origin", "source"):
        value = data.get(key)
        if isinstance(value, str):
            return value
    return "human"


class DshSessionReader:
    def __init__(
        self,
        log_path: str | Path | None,
        *,
        instance_id: str,
        session_id: str,
        actor_id: str = "actor:dsh",
        session_log: str | None = None,
        ancestor_logs: dict[str, str] | None = None,
        project_ids: tuple[str, ...] = (),
    ) -> None:
        self.log_path = Path(log_path) if log_path is not None else None
        self.session_log = session_log
        self.ancestor_logs = dict(ancestor_logs or {})
        self._header: dict = {}
        self._events: list[dict] = []
        self._parent_readers: dict[str, DshSessionReader] = {}
        self.instance_id = instance_id
        self.session_id = session_id
        self.actor_id = actor_id
        self.project_ids = tuple(project_ids)

    def _parse(self) -> list[dict]:
        if self.session_log is None and (self.log_path is None or not self.log_path.is_file()):
            raise SourceReadError(f"DSH session log not found: {self.log_path}")
        events: list[dict] = []
        self._header = {}
        # The native export API can stream decompressed JSONL directly through
        # stdin. Never materialize an extra unredacted session copy on disk.
        handle_context = io.StringIO(self.session_log) if self.session_log is not None else self.log_path.open("r", encoding="utf-8")
        with handle_context as handle:
            for line_number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    raise SourceReadError(f"unparsable DSH session event at line {line_number}") from None
                if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                    raise SourceReadError(f"malformed DSH session event at line {line_number}")
                if line_number == 1 and event.get("type") == "session":
                    if event.get("version") != 4 or event.get("id") != self.session_id:
                        raise SourceReadError("unsupported or mismatched DSH native session header")
                    self._header = event
                    # Native plain JSONL has one file header outside the event
                    # sequence; hand-off streams may omit it. Never renumber.
                    continue
                if type(event.get("seq")) is not int or event["seq"] < 0:
                    raise SourceReadError(f"DSH session event without stable seq at line {line_number}")
                events.append(event)
        for expected, event in enumerate(events):
            if event["seq"] != expected:
                raise SourceReadError(
                    f"DSH session log is not contiguous: expected seq {expected}, got {event['seq']}"
                )
        self._events = events
        return events

    def _origin(self, seq: int, visited: tuple[str, ...] = ()):
        """Resolve the native owner of an inherited event, including nested forks.

        Never invent a new event for copied history. An unavailable/mismatching
        ancestor leaves the native log and capture cursor untouched for retry.
        """
        if self.session_id in visited or len(visited) >= 64:
            raise SourceReadError("cyclic or excessive DSH fork lineage")
        if not self._header.get("isSeeded"):
            return self, self._events[seq]
        markers = [event["seq"] for event in self._events
                   if event["type"] == "session/end-seed"
                   and (event.get("data") or {}).get("inherited") is True]
        if not markers:
            raise SourceReadError("DSH fork lacks an inherited boundary")
        if seq >= markers[-1]:
            return self, self._events[seq]
        parent_id = self._header.get("parentSession")
        if not isinstance(parent_id, str) or parent_id not in self.ancestor_logs:
            raise SourceReadError("DSH fork ancestor log is required before capture")
        parent = self._parent_readers.get(parent_id)
        if parent is None:
            parent = DshSessionReader(None, session_log=self.ancestor_logs[parent_id],
                ancestor_logs=self.ancestor_logs, instance_id=self.instance_id,
                session_id=parent_id, actor_id=self.actor_id, project_ids=self.project_ids)
            parent._parse()
            self._parent_readers[parent_id] = parent
        if seq >= len(parent._events) or parent._events[seq] != self._events[seq]:
            # A subagent seed may append its own cwd/role/runtime scaffolding
            # before end-seed. It is not a copied parent fact. Preserve only
            # an exclusion identity for such non-business context; genuine
            # inherited human/assistant/tool evidence must still match exactly.
            own_event = self._events[seq]
            own = self._record_for(own_event["type"], seq,
                                   own_event.get("data") or {}, own_event)
            if own is None or own.origin_kind in {"memory_injection", "maintenance"}:
                return self, own_event
            raise SourceReadError("DSH fork inherited prefix does not match its ancestor")
        return parent._origin(seq, (*visited, self.session_id))

    def read_since(self, stream: str, position: str | None):
        events = self._parse()
        try:
            start = int(position) if position else -1
        except (TypeError, ValueError):
            raise SourceReadError("DSH checkpoint is not a session seq") from None
        pairs: list[tuple[NativeRecord, str]] = []
        last_seq = start
        for event in events:
            seq = int(event["seq"])
            if seq <= start:
                continue
            event_type = event["type"]
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            last_seq = seq
            origin, original_event = self._origin(seq)
            original_data = original_event.get("data") if isinstance(original_event.get("data"), dict) else {}
            record = origin._record_for(original_event["type"], seq, original_data, original_event)
            if record is not None:
                pairs.append((record, str(seq)))
        return pairs, str(last_seq)

    def _record_for(self, event_type: str, seq: int, data: dict, event: dict) -> NativeRecord | None:
        if event_type in SKIP_TYPES:
            return None
        if event_type in SYSTEM_TYPES:
            # Host identity / skills / system instructions are not shared facts.
            return None
        role: str | None = None
        origin: str | None = None
        text = ""
        locator_suffix = f"seq/{seq}"
        aliases: list[str] = []
        if event_type == "user/message":
            role = "user"
            source_kind = _message_source(data)
            if source_kind.lower().replace("_", "-") in INJECTION_SOURCES or "inject" in source_kind.lower():
                origin = "memory_injection"
            elif source_kind.lower().replace("_", "-") in {"human", "user"}:
                origin = "user_input"
            else:
                raise SourceReadError("unsupported DSH user-message source")
            text = _text_of_content(data.get("content") or data.get("message", {}).get("content"))
            locator_suffix = f"user/{seq}"
        elif event_type == "assistant/message":
            role = "assistant"
            origin = "assistant_reply"
            message = data.get("message") if isinstance(data.get("message"), dict) else {}
            text = _text_of_content(message.get("content") or data.get("content"))
            if data.get("interrupted"):
                aliases.append("interrupted")
            locator_suffix = f"assistant/{seq}"
        elif event_type == "tool/result":
            role = "tool"
            origin = "tool_execution"
            message = data.get("message") if isinstance(data.get("message"), dict) else {}
            text = _text_of_content(message.get("content") or data.get("content"))
            call_id = message.get("toolCallId") or message.get("callId") or data.get("callId")
            locator_suffix = f"tool/{call_id or seq}"
            if message.get("isError") or data.get("error"):
                aliases.append("tool-error")
            if call_id:
                aliases.append(f"call:{call_id}")
        elif event_type in {"job/settled","todo/write","team/task",
                            "deliverables/presented","goal/change"}:
            role = "tool"
            # Native job lifecycle duplicates tool/result evidence; retain an
            # exclusion receipt instead of buying another semantic extraction.
            origin = "maintenance" if event_type == "job/settled" else "tool_execution"
            # State bookkeeping is scoped evidence, not whole-task verification.
            fields = {
                "job/settled": ("jobId","owner","kind","label","startedAt",
                                "finishedAt","status","cause","producerReported","detail"),
                "todo/write": ("todos",),
                "team/task": ("teamId","task"),
                "deliverables/presented": ("files",),
                "goal/change": ("kind","operation","goal","roundsStarted",
                                "createdAt","updatedAt"),
            }[event_type]
            text = json.dumps({
                "native_event": event_type,
                "verification_scope": "native state update only, not verification of the whole task",
                "state": {key:data[key] for key in fields if key in data},
            }, ensure_ascii=False, sort_keys=True)
            aliases.append("native-state:"+event_type.replace("/", "-"))
        elif event_type == "runtime-context":
            role = "user"
            origin = "memory_injection"
            text = _text_of_content(data.get("text") or data.get("snapshot") or data.get("content"))
            locator_suffix = f"runtime-context/{seq}"
        elif event.get("ignorable") is True:
            return None
        elif event_type.startswith("plugin:"):
            return None
        else:
            raise SourceReadError(
                f"unknown required DSH session event type {event_type!r}; refusing to reconstruct"
            )
        if not text.strip():
            return None
        if event.get("sourceEventSeqs"):
            aliases.extend(f"source:{source_seq}" for source_seq in event["sourceEventSeqs"])
        surface_op = event.get("surfaceOp")
        # A native tool result cites its tool/call event; that reference is not
        # a compaction summary. Memory-search results remain recall injection.
        refs = event.get("sourceEventSeqs") or []
        calls = [self._events[i] for i in refs if type(i) is int and 0 <= i < len(self._events)]
        tool_result_refs = (event_type == "tool/result" and bool(refs) and len(calls) == len(refs)
            and all(call["type"] == "tool/call" for call in calls))
        if tool_result_refs and any((call.get("data") or {}).get("name") == "unified_memory_search" for call in calls):
            origin = "memory_injection"
        if (refs and not tool_result_refs) or isinstance(surface_op, dict):
            # Host compaction / surface replacement: a derived summary, never a
            # new user fact (SRC-02). KW records an exclusion receipt for it.
            if origin in ("user_input", "assistant_reply", "tool_execution"):
                origin = "maintenance"
                aliases.append("derived")
        content_hash = sha256_hex(text)
        return NativeRecord(
            native_event_id=f"{self.session_id}:seq:{seq}",
            text=text,
            role=role or "user",
            origin_kind=origin or "user_input",
            session_id=data.get("sessionId") or self.session_id,
            actor_id=self.actor_id,
            native_schema="dsh-session-events-0.2.1",
            content_revision=f"native:{content_hash[:16]}",
            locator=f"dsh://{self.session_id}/{locator_suffix}",
            event_time=_iso_from_millis(event.get("time") or data.get("time")),
            project_ids=self.project_ids,
            turn_id=str(data.get("turn")) if data.get("turn") is not None else None,
            aliases=tuple(aliases),
        )


def sync_on_turn_stopping(reader: DshSessionReader, adapter, outbox, *, stream: str | None = None) -> dict:
    """Rescan the native session on turn-stopping; durable outbox rows are proof.

    A failed/empty turn still runs this rescan: user input and tool results from
    failed turns are captured, while assistant stream attempts are ignored.
    """
    from dma import pipeline

    return pipeline.capture(
        reader, outbox, stream=stream or f"dsh:{reader.session_id}", convert=adapter.to_events
    )
