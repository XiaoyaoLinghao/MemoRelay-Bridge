"""Read-only Hermes source adapter (T09).

Hermes stores conversations in a per-profile SQLite ``state.db`` (schema
verified against ``reference/host-snapshots/hermes-dma/hermes_dma.py``):

* ``messages`` required columns: ``id, session_id, role, content, timestamp,
  active, compacted, _compressed_summary``;
* ``sessions`` rows carry ``parent_session_id`` / ``end_reason`` so compression
  rotations can be joined into one logical session;
* compressed pre-rotation originals (``compacted=1``) are retained; compressed
  summaries, hidden scaffolding and inactive non-compacted rows are excluded;
* the checkpoint is the last processed row ``id`` (insertion order), **not** the
  maximum timestamp: late-arriving or clock-skewed rows are never skipped, and
  rows inside the settle window are retried later;
* unknown/incompatible schema is refused rather than silently ignored.

The adapter never writes to the native database (``mode=ro`` + ``query_only``)
and never logs message content.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import sqlite3
from pathlib import Path

from dma import pipeline
from dma.errors import SourceReadError
from dma.ids import sha256_hex
from dma.reader import NativeRecord

REQUIRED_COLUMNS = {
    "id",
    "session_id",
    "role",
    "content",
    "timestamp",
    "active",
    "compacted",
    "_compressed_summary",
}
INCLUDED_ROLES = {"user", "assistant", "tool"}
ROLE_ORIGIN = {
    "user": "user_input",
    "assistant": "assistant_reply",
    "tool": "tool_execution",
}


def text_content(content) -> str:
    """Extract visible text while preserving the encoded content only in hashes."""
    if content is None:
        return ""
    if isinstance(content, str):
        try:
            decoded = json.loads(content)
        except (ValueError, TypeError):
            return content
        if isinstance(decoded, str):
            return decoded
        if isinstance(decoded, list):
            return text_content(decoded)
        return content
    if isinstance(content, list):
        return "\n".join(
            part["text"]
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


def _iso_from_epoch(value) -> dict:
    try:
        moment = _dt.datetime.fromtimestamp(float(value), tz=_dt.timezone.utc)
    except (TypeError, ValueError, OSError):
        return {"value": None, "precision": "unknown", "timezone": "UTC"}
    return {
        "value": moment.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "precision": "instant",
        "timezone": "UTC",
    }


def _compression_parent(session_id: str, sessions: dict[str, dict]) -> str | None:
    child = sessions.get(session_id) or {}
    parent = child.get("parent_session_id")
    if not parent or (sessions.get(parent) or {}).get("end_reason") != "compression":
        return None
    # Native forks/resets/delegations remain independent conversations even if
    # their parent was later ended by compression. Inherited markers only count
    # when they point to this direct parent (the native SessionDB convention).
    config = child.get("model_config")
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except ValueError:
            config = {}
    if child.get("source") == "tool" or (
        isinstance(config, dict)
        and parent in [config.get(k) for k in ("_branched_from", "_delegate_from", "_reset_from")]
    ):
        return None
    return parent


def _logical_session(session_id: str, sessions: dict[str, dict]) -> str:
    current, visited = session_id, set()
    while current not in visited:
        visited.add(current)
        parent = _compression_parent(current, sessions)
        if not parent:
            break
        current = parent
    return current


def _canonical_generations(rows: list[dict], sessions: dict[str, dict]) -> dict[int, dict]:
    """Resolve native retained-tail copies to their original row identity.

    SessionDB copies rows during compression, preserving the exact original
    timestamp/role/content/tool metadata but assigning a fresh row id. Only
    verified compression lineage or an in-place summary boundary creates a new
    generation. Identical content in ordinary turns/sessions is never enough.
    Occurrence indexes preserve distinct equal rows within one generation.
    """
    generations: dict[str, int] = {}
    occurrences: dict[tuple, int] = {}
    originals: dict[tuple, list[dict]] = {}
    result = {}
    for row in rows:
        sid = str(row["session_id"])
        if row.get("_compressed_summary"):
            generations[sid] = generations.get(sid, 0) + 1
            continue
        generation = (sid, generations.get(sid, 0))
        signature = (_logical_session(sid, sessions),) + tuple(
            row.get(k) for k in ("role", "content", "timestamp", "tool_call_id", "tool_calls", "tool_name")
        )
        key = (generation, signature)
        index = occurrences.get(key, 0)
        occurrences[key] = index + 1
        candidates = originals.setdefault(signature, [])
        if index < len(candidates):
            original = candidates[index]
            # Same-generation equal rows are distinct native messages.
            if original["_dma_generation"] != generation:
                result[int(row["id"])] = original
                continue
        original = dict(row, _dma_generation=generation)
        candidates.append(original)
        result[int(row["id"])] = original
    return result


class HermesSqliteReader:
    def __init__(
        self,
        db_path: str | Path,
        *,
        instance_id: str,
        actor_id: str = "actor:hermes",
        profile: str = "main",
        settle_seconds: float = 0.0,
        project_ids: tuple[str, ...] = (),
        clock=None,
    ) -> None:
        self.db_path = Path(db_path)
        self.instance_id = instance_id
        self.actor_id = actor_id
        self.profile = profile
        self.settle_seconds = float(settle_seconds)
        self.project_ids = tuple(project_ids)
        self.clock = clock

    def _connect(self) -> sqlite3.Connection:
        if not self.db_path.is_file():
            raise SourceReadError(f"Hermes state database not found: {self.db_path}")
        connection = sqlite3.connect(
            self.db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=15.0
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection

    def read_since(self, stream: str, position: str | None):
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(messages)")}
            if not REQUIRED_COLUMNS <= columns:
                missing = sorted(REQUIRED_COLUMNS - columns)
                raise SourceReadError(
                    f"unsupported Hermes messages schema (missing {missing}); refusing to guess"
                )
            sessions = {
                row["id"]: dict(row) for row in connection.execute("SELECT * FROM sessions").fetchall()
            }
            try:
                start = int(position) if position else 0
            except (TypeError, ValueError):
                raise SourceReadError("Hermes checkpoint is not a message id") from None
            rows = [dict(row) for row in connection.execute(
                "SELECT * FROM messages WHERE id > ? ORDER BY id", (start,)
            ).fetchall()]
            # Canonicalize from the same read snapshot, including originals below
            # the incremental cursor and archived retained-tail rows. Restrict the
            # history read to logical conversations touched by this batch.
            roots = {_logical_session(str(row["session_id"]), sessions) for row in rows}
            history_sessions = [sid for sid in sessions if _logical_session(sid, sessions) in roots]
            history = []
            for offset in range(0, len(history_sessions), 500):
                batch = history_sessions[offset:offset + 500]
                marks = ",".join("?" for _ in batch)
                history.extend(dict(row) for row in connection.execute(
                    f"SELECT * FROM messages WHERE session_id IN ({marks}) ORDER BY id", batch
                ).fetchall())
            canonical = _canonical_generations(sorted(history, key=lambda row: row["id"]), sessions)
        finally:
            connection.close()
        now = (self.clock() if self.clock else _dt.datetime.now(_dt.timezone.utc)).timestamp()
        settle_before = now - self.settle_seconds
        pairs: list[tuple[NativeRecord, str]] = []
        last_id = start
        emitted_ids: set[int] = set()
        for raw in rows:
            row = dict(raw)
            message_id = int(row["id"])
            try:
                timestamp = float(row["timestamp"])
            except (TypeError, ValueError):
                timestamp = 0.0
            if timestamp > settle_before:
                # Not settled yet: do not advance the checkpoint past this row.
                break
            last_id = message_id
            if row.get("_compressed_summary"):
                continue  # model-generated summary is not original user evidence
            if row.get("display_kind") == "hidden":
                continue
            if not (row.get("active") == 1 or row.get("compacted") == 1):
                continue
            role = str(row.get("role") or "")
            if role not in INCLUDED_ROLES:
                continue
            text = text_content(row.get("content"))
            if not text.strip():
                continue
            # Keep the *new physical row* as cursor progress, while the event
            # envelope uses byte-identical original identity/source/time.
            row = canonical.get(message_id, row)
            original_id = int(row["id"])
            if original_id in emitted_ids:
                continue
            emitted_ids.add(original_id)
            logical = _logical_session(str(row["session_id"]), sessions)
            aliases = [f"content:{sha256_hex(text)[:16]}"]
            if logical != row["session_id"]:
                aliases.append(f"session:{logical}")
            content_hash = sha256_hex(str(row.get("content") or ""))
            session = sessions.get(str(row["session_id"])) or {}
            generated_kickoff = role == "user" and session.get("source") in {
                "cron", "kanban", "subagent", "tool",
            }
            if generated_kickoff:
                aliases.append("native-generated:"+str(session["source"]))
            record = NativeRecord(
                native_event_id=f"msg:{original_id}",
                text=text,
                role=role,
                origin_kind="memory_injection" if generated_kickoff or (role == "tool" and row.get("tool_name") == "unified_memory_search") else ROLE_ORIGIN[role],
                session_id=logical,
                actor_id=self.actor_id,
                native_schema=f"hermes-state-{self.profile}",
                content_revision=f"native:{content_hash[:16]}",
                locator=f"hermes://{self.profile}/{row['session_id']}/messages/{original_id}",
                event_time=_iso_from_epoch(timestamp),
                project_ids=self.project_ids,
                turn_id=None,
                aliases=tuple(aliases),
            )
            pairs.append((record, str(message_id)))
        final_position = str(last_id)
        if pairs:
            pairs[-1] = (pairs[-1][0], final_position)
        return pairs, final_position


def checkpoint_before_compression(reader: HermesSqliteReader, adapter, outbox, *, stream: str | None = None) -> dict:
    """Persist new sanitized Hermes events to the durable outbox before compaction.

    The caller must not report the compression path as safely archived if this
    raises; a durable outbox row (and only then a committed receipt later) is the
    proof, not a log line or an in-memory queue.
    """
    resolved_stream = stream or f"hermes:{reader.profile}"
    return pipeline.capture(reader, outbox, stream=resolved_stream, convert=adapter.to_events)
