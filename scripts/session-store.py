#!/usr/bin/env python3
"""Read a bounded set of OpenClaw sessions without mutating their stores.

The archive engine deliberately talks to this module through one small JSON
boundary.  The module owns backend discovery, native SQLite/JSONL decoding,
message normalization, and the v2 identity cursor.  It never writes an
OpenClaw store or the DMA checkpoint.
"""

from __future__ import annotations

import argparse
import datetime as _datetime
import hashlib
import json
import math
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


for _stream in (sys.stdout, sys.stderr):
    # DMA consumes stdout as one UTF-8 JSON document.  Do not let the host
    # locale choose a legacy code page for long Chinese or non-ASCII messages.
    try:
        _stream.reconfigure(encoding="utf-8", errors="strict")
    except (AttributeError, OSError, ValueError):
        pass


SUPPORTED_AGENT_SCHEMA_VERSION = 19
NOISE_MARKERS = (
    "[heartbeat",
    "heartbeat poll",
    "HEARTBEAT",
    "[tool",
    "toolCall",
    "tool_call_id",
    "Sender (untrusted",
    "[system",
    "[SYSTEM",
    "[MCP",
    "[Spinner",
    "<<<",
    ">>>",
)
NOISE_JSON_PREFIX = re.compile(r'^\s*\{"')


class AdapterError(RuntimeError):
    """A user-actionable storage or input error."""


class CliMissingStoreError(AdapterError):
    """The official CLI resolved a selector whose physical target is absent."""


def _absolute_path(value: str) -> pathlib.Path:
    if not isinstance(value, str) or not value.strip():
        raise AdapterError("path arguments must be non-empty strings")
    return pathlib.Path(os.path.abspath(os.path.expanduser(value)))


def _read_json_file(path: pathlib.Path, label: str) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise AdapterError(f"{label} not found: {path}")
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise AdapterError(f"cannot read {label} {path}: {error}")


def _parse_keys_json(raw: str) -> List[str]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise AdapterError(f"--keys-json is not valid JSON: {error}")
    if not isinstance(value, list):
        raise AdapterError("--keys-json must be a JSON array of session keys")
    keys: List[str] = []
    seen = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise AdapterError("--keys-json contains a blank or non-string session key")
        key = item.strip()
        if key not in seen:
            keys.append(key)
            seen.add(key)
    if not keys:
        raise AdapterError("--keys-json must contain at least one session key")
    return keys


def _validate_seen(value: Any, key: str) -> List[str]:
    if not isinstance(value, list):
        raise AdapterError(f"checkpoint cursor for {key!r} has non-array seen identities")
    result: List[str] = []
    seen = set()
    for identity in value:
        if not isinstance(identity, str) or not identity:
            raise AdapterError(f"checkpoint cursor for {key!r} has an invalid identity")
        if identity not in seen:
            result.append(identity)
            seen.add(identity)
    return result


def _parse_cursor(value: Any, key: str) -> Dict[str, Any]:
    """Parse one legacy timestamp or one canonical v2 cursor.

    The old archive wrote a bare ISO timestamp for each key.  It is accepted
    only as an input migration format.  The next snapshot always proposes a
    v2 object and materializes all currently visible messages at or before the
    old boundary into ``seen``.
    """

    if isinstance(value, str):
        # An empty value is how the old shell represented an uninitialised
        # cursor.  Treat it as an empty boundary while still converting it to
        # the v2 identity ledger on the first read.
        if value and _timestamp_epoch(value) is None:
            raise AdapterError(f"checkpoint cursor for {key!r} has an invalid legacy timestamp")
        return {
            "version": 1,
            "seen": [],
            "legacyTimestamp": value if value else None,
        }
    if not isinstance(value, dict):
        raise AdapterError(f"checkpoint cursor for {key!r} is neither a timestamp nor an object")
    version = value.get("version")
    if version != 2:
        raise AdapterError(f"checkpoint cursor for {key!r} has unsupported version {version!r}")
    seen = _validate_seen(value.get("seen"), key)
    legacy_timestamp = value.get("legacyTimestamp")
    if legacy_timestamp is not None and not isinstance(legacy_timestamp, str):
        raise AdapterError(f"checkpoint cursor for {key!r} has an invalid legacyTimestamp")
    if legacy_timestamp == "":
        legacy_timestamp = None
    if legacy_timestamp is not None and _timestamp_epoch(legacy_timestamp) is None:
        raise AdapterError(f"checkpoint cursor for {key!r} has an invalid legacyTimestamp")
    return {"version": 2, "seen": seen, "legacyTimestamp": legacy_timestamp}


def _load_checkpoint(path: pathlib.Path) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return (all original cursors, optional envelope metadata).

    Current DMA checkpoints are a top-level key map.  A small envelope form is
    accepted for forward compatibility, but output is normalized back to the
    top-level map because the shell owns the file and already expects that
    shape.
    """

    if not path.exists():
        return {}, {}
    if not path.is_file():
        raise AdapterError(f"checkpoint path is not a file: {path}")
    raw = _read_json_file(path, "checkpoint")
    if not isinstance(raw, dict):
        raise AdapterError("checkpoint root must be a JSON object")

    if "version" in raw or "sessions" in raw:
        if raw.get("version") != 2 or not isinstance(raw.get("sessions"), dict):
            raise AdapterError("checkpoint envelope must contain version 2 and sessions object")
        values = raw["sessions"]
        envelope = {"version": 2}
    else:
        values = raw
        envelope = {}

    # Keep the original representation for keys that are not selected by this
    # snapshot.  In particular, a v1 timestamp must remain a string in the
    # published top-level map; emitting the internal parsed object would make
    # a subsequent run reject that unselected cursor as a malformed v2 value.
    parsed: Dict[str, Any] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not key.strip():
            raise AdapterError("checkpoint contains a blank or non-string session key")
        _parse_cursor(value, key)  # validate without changing the wire form
        parsed[key] = value
    return parsed, envelope


def _is_scalar_timestamp(value: Any) -> bool:
    return isinstance(value, (str, int, float)) and not isinstance(value, bool)


def _timestamp_value(value: Any, fallback: Any = "") -> Any:
    if _is_scalar_timestamp(value):
        return value
    if _is_scalar_timestamp(fallback):
        return fallback
    return ""


def _timestamp_epoch(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        if not math.isfinite(numeric):
            return None
        return numeric / 1000.0 if abs(numeric) > 100_000_000_000 else numeric
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        numeric = float(text)
        if not math.isfinite(numeric):
            return None
        return numeric / 1000.0 if abs(numeric) > 100_000_000_000 else numeric
    except ValueError:
        pass
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = _datetime.datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_datetime.timezone.utc)
    return parsed.timestamp()


def _timestamp_leq(left: Any, right: str) -> bool:
    """Match the old strict ``timestamp > checkpoint`` rule when possible."""

    left_epoch = _timestamp_epoch(left)
    right_epoch = _timestamp_epoch(right)
    if left_epoch is not None and right_epoch is not None:
        return left_epoch <= right_epoch
    return str(left) <= right


def _timestamp_sort_key(value: Any, sequence: int, source_order: int) -> Tuple[Any, ...]:
    epoch = _timestamp_epoch(value)
    if epoch is not None:
        return (0, epoch, source_order, sequence)
    return (1, str(value), source_order, sequence)


def _message_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        texts: List[str] = []
        for block in value:
            if not isinstance(block, dict):
                raise AdapterError("message content array contains a non-object block")
            if block.get("type") == "text":
                if not isinstance(block.get("text"), str):
                    raise AdapterError("message content text block has non-string text")
                texts.append(block["text"])
            # Image, audio, tool-result and other structured blocks are valid
            # OpenClaw content but have no text contribution to the archive.
        return "\n".join(texts)
    raise AdapterError("message content must be a string or an array of blocks")


def _is_archive_noise(content: str) -> bool:
    if len(content) <= 3:
        return True
    if any(marker in content for marker in NOISE_MARKERS):
        return True
    return NOISE_JSON_PREFIX.match(content) is not None


def _session_fingerprint(path: pathlib.Path) -> str:
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:24]


def _event_identity(
    session_id: str,
    event_id: Optional[str],
    role: str,
    content: str,
    timestamp: Any,
    occurrence: int,
) -> str:
    if event_id:
        return f"event:{session_id}:{event_id}"
    # Without a source event id, identical role/content/timestamp copies are
    # inherently ambiguous after a transcript is trimmed; occurrence preserves
    # their original order while the rows remain visible in one window.
    canonical_timestamp = _canonical_timestamp(timestamp)
    payload = (role + "\0" + content + "\0" + canonical_timestamp).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    return f"fallback:{session_id}:{digest}:{occurrence}"


def _canonical_timestamp(value: Any) -> str:
    """Normalize timestamp values used by the no-id fallback identity."""

    epoch = _timestamp_epoch(value)
    if epoch is not None:
        return f"epoch:{epoch:.6f}"
    return str(value)


def _normalize_message(
    *,
    event: Dict[str, Any],
    session_id: str,
    fallback_timestamp: Any,
    sequence: int,
    source_order: int,
    occurrence_counts: Dict[Tuple[str, str, str], int],
) -> Optional[Dict[str, Any]]:
    if event.get("type") != "message":
        return None
    message = event.get("message")
    if not isinstance(message, dict):
        raise AdapterError("message event has no object message payload")
    role = message.get("role")
    if role not in ("user", "assistant"):
        return None
    content = _message_content(message.get("content"))
    if not content:
        return None
    identity_timestamp = _timestamp_value(event.get("timestamp"), "")
    fingerprint_key = (role, content, _canonical_timestamp(identity_timestamp))
    occurrence = occurrence_counts.get(fingerprint_key, 0)
    occurrence_counts[fingerprint_key] = occurrence + 1
    event_id = event.get("id") if isinstance(event.get("id"), str) else None
    event_id = event_id.strip() if event_id and event_id.strip() else None
    timestamp = _timestamp_value(event.get("timestamp"), fallback_timestamp)
    return {
        "ts": timestamp,
        "role": role,
        "content": content,
        "identity": _event_identity(
            session_id, event_id, role, content, identity_timestamp, occurrence
        ),
        "sequence": sequence,
        "source_order": source_order,
        "noise": _is_archive_noise(content),
    }


def _cursor_state(cursors: Dict[str, Any], key: str) -> Dict[str, Any]:
    value = cursors.get(key)
    if value is None:
        return {"version": 2, "seen": [], "legacyTimestamp": None}
    return _parse_cursor(value, key)


def _select_messages(
    key: str,
    messages: Iterable[Dict[str, Any]],
    cursor: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    old_boundary = cursor.get("legacyTimestamp") if cursor.get("version") == 1 else None
    seen = list(cursor.get("seen", []))
    seen_set = set(seen)
    selected: List[Dict[str, Any]] = []
    for message in messages:
        identity = message["identity"]
        if identity in seen_set:
            continue
        if old_boundary is not None and _timestamp_leq(message["ts"], old_boundary):
            # A v1 timestamp was only a coarse record of what the old reader
            # consumed. Materialize all visible old-boundary identities into
            # v2 so a later equal-timestamp append is distinguishable.
            seen.append(identity)
            seen_set.add(identity)
            continue
        if message["noise"]:
            # Noise is intentionally consumed in the identity ledger even
            # though it does not enter the archive payload.  Otherwise every
            # snapshot would rescan the same tool/heartbeat records forever.
            seen.append(identity)
            seen_set.add(identity)
            continue
        selected.append(message)
        seen.append(identity)
        seen_set.add(identity)
    return selected, {"version": 2, "seen": seen, "legacyTimestamp": None}


def _load_legacy_records(path: pathlib.Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        raise AdapterError(f"selected JSONL transcript not found: {path}")
    records: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError) as error:
                    raise AdapterError(f"malformed JSONL record {path}:{line_number}: {error}")
                if not isinstance(record, dict):
                    raise AdapterError(f"malformed JSONL record {path}:{line_number}: object required")
                records.append(record)
    except (OSError, UnicodeError) as error:
        raise AdapterError(f"cannot read JSONL transcript {path}: {error}")
    return records


def _legacy_messages(
    key: str, entry: Dict[str, Any], source_order: int, legacy_directory: pathlib.Path
) -> List[Dict[str, Any]]:
    raw_path = entry.get("sessionFile")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise AdapterError(f"selected session {key!r} has no sessionFile")
    transcript_path = pathlib.Path(os.path.expanduser(raw_path))
    if not transcript_path.is_absolute():
        # OpenClaw legacy entries normally store absolute paths; resolving a
        # relative one beside sessions.json keeps fixtures and old installs
        # deterministic without changing the source file.
        transcript_path = legacy_directory / transcript_path
    transcript_path = _absolute_path(str(transcript_path))
    records = _load_legacy_records(transcript_path)
    header_id: Optional[str] = None
    for record in records:
        if record.get("type") == "session" and isinstance(record.get("id"), str):
            candidate = record["id"].strip()
            if candidate:
                header_id = candidate
                break
    entry_session_id = entry.get("sessionId")
    entry_session_id = (
        entry_session_id.strip()
        if isinstance(entry_session_id, str) and entry_session_id.strip()
        else None
    )
    if header_id and entry_session_id and header_id != entry_session_id:
        raise AdapterError(
            f"selected session {key!r} has conflicting session ids: "
            f"header={header_id!r}, entry={entry_session_id!r}"
        )
    session_id = (
        header_id
        or entry_session_id
        or f"legacy:{_session_fingerprint(transcript_path)}"
    )
    # The first tuple element scopes fallback identities to the retained
    # transcript window.  The timestamp component is part of the same scope:
    # when a compacted transcript is later re-read, an identical sentence at
    # a different time must not be renumbered into an old identity.
    occurrence_counts: Dict[Tuple[str, str, str], int] = {}
    messages: List[Dict[str, Any]] = []
    for sequence, event in enumerate(records):
        normalized = _normalize_message(
            event=event,
            session_id=session_id,
            fallback_timestamp="",
            sequence=sequence,
            source_order=source_order,
            occurrence_counts=occurrence_counts,
        )
        if normalized is not None:
            messages.append(normalized)
    return messages


def _entry_usage(key: str, entry: Dict[str, Any]) -> Dict[str, Any]:
    def number(name: str) -> int:
        value = entry.get(name, 0)
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return int(value)
        return 0

    fresh = entry.get("totalTokensFresh")
    return {
        "key": key,
        "inputTokens": number("inputTokens"),
        "totalTokens": number("totalTokens"),
        "totalTokensFresh": fresh if isinstance(fresh, bool) else False,
    }


def _legacy_snapshot(
    keys: Sequence[str],
    legacy_store: pathlib.Path,
    cursors: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    if not legacy_store.is_file():
        raise AdapterError(f"legacy session index not found: {legacy_store}")
    raw = _read_json_file(legacy_store, "legacy session index")
    if not isinstance(raw, dict):
        raise AdapterError("legacy session index root must be a JSON object")
    usage: List[Dict[str, Any]] = []
    all_messages: List[Dict[str, Any]] = []
    next_cursors = dict(cursors)
    for source_order, key in enumerate(keys):
        entry = raw.get(key)
        if not isinstance(entry, dict):
            raise AdapterError(f"selected session key not found in legacy index: {key}")
        usage.append(_entry_usage(key, entry))
        messages = _legacy_messages(key, entry, source_order, legacy_store.parent)
        selected, next_cursor = _select_messages(key, messages, _cursor_state(cursors, key))
        next_cursors[key] = next_cursor
        for item in selected:
            item["sk"] = key
            all_messages.append(item)
    return usage, all_messages, next_cursors


def _sqlite_uri(path: pathlib.Path) -> str:
    # pathlib.as_uri() correctly escapes spaces and non-ASCII characters on
    # Windows and POSIX; mode=ro guarantees the adapter cannot create a DB.
    return path.resolve().as_uri() + "?mode=ro"


def _table_columns(connection: sqlite3.Connection, table: str) -> set:
    try:
        rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    except sqlite3.DatabaseError as error:
        raise AdapterError(f"cannot inspect SQLite table {table}: {error}")
    return {str(row[1]) for row in rows}


def _validate_sqlite_schema(connection: sqlite3.Connection, path: pathlib.Path, agent: str) -> None:
    try:
        version_row = connection.execute("PRAGMA user_version").fetchone()
    except sqlite3.DatabaseError as error:
        raise AdapterError(f"cannot read SQLite schema version from {path}: {error}")
    version = int(version_row[0]) if version_row else -1
    if version != SUPPORTED_AGENT_SCHEMA_VERSION:
        raise AdapterError(
            f"unsupported OpenClaw agent SQLite schema at {path}: user_version={version}, "
            f"supported={SUPPORTED_AGENT_SCHEMA_VERSION}"
        )

    required = {
        "schema_meta": {"meta_key", "role", "schema_version", "agent_id"},
        "session_nodes": {"session_key", "current_session_id", "entry_json"},
        "session_windows": {"session_id", "session_key", "created_at"},
        "transcript_events": {"session_id", "seq", "event_json", "created_at"},
    }
    for table, columns in required.items():
        actual = _table_columns(connection, table)
        if not actual:
            raise AdapterError(f"OpenClaw agent SQLite table missing at {path}: {table}")
        missing = sorted(columns - actual)
        if missing:
            raise AdapterError(
                f"OpenClaw agent SQLite table {table} at {path} is missing columns: {', '.join(missing)}"
            )
    try:
        metadata = connection.execute(
            "SELECT role, schema_version, agent_id FROM schema_meta WHERE meta_key = 'primary'"
        ).fetchone()
    except sqlite3.DatabaseError as error:
        raise AdapterError(f"cannot read OpenClaw schema metadata from {path}: {error}")
    if metadata is None:
        raise AdapterError(f"OpenClaw schema metadata primary row missing at {path}")
    role, metadata_version, owner = metadata
    try:
        metadata_version_number = int(metadata_version)
    except (TypeError, ValueError):
        metadata_version_number = -1
    if role != "agent" or metadata_version_number != SUPPORTED_AGENT_SCHEMA_VERSION:
        raise AdapterError(
            f"OpenClaw schema metadata mismatch at {path}: role={role!r}, "
            f"schema_version={metadata_version!r}, supported={SUPPORTED_AGENT_SCHEMA_VERSION}"
        )
    if not isinstance(owner, str) or owner.strip().lower() != agent.strip().lower():
        raise AdapterError(
            f"OpenClaw SQLite owner mismatch at {path}: owner={owner!r}, requested={agent!r}"
        )


def _sqlite_messages(
    connection: sqlite3.Connection,
    keys: Sequence[str],
    cursors: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    usage: List[Dict[str, Any]] = []
    all_messages: List[Dict[str, Any]] = []
    next_cursors = dict(cursors)
    occurrence_counts: Dict[Tuple[str, str, str, str], int] = {}
    for source_order, key in enumerate(keys):
        row = connection.execute(
            "SELECT current_session_id, entry_json FROM session_nodes WHERE session_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            raise AdapterError(f"selected session key not found in SQLite: {key}")
        current_session_id, entry_json = row
        try:
            entry = json.loads(entry_json)
        except (TypeError, json.JSONDecodeError) as error:
            raise AdapterError(f"invalid entry_json for SQLite session {key!r}: {error}")
        if not isinstance(entry, dict):
            raise AdapterError(f"entry_json for SQLite session {key!r} is not an object")
        entry_session_id = entry.get("sessionId")
        if isinstance(entry_session_id, str) and entry_session_id.strip():
            if str(current_session_id).strip() != entry_session_id.strip():
                raise AdapterError(
                    f"SQLite session {key!r} entry_json sessionId does not match current_session_id"
                )
        usage.append(_entry_usage(key, entry))

        windows = connection.execute(
            "SELECT session_id FROM session_windows WHERE session_key = "
            "? ORDER BY created_at ASC, session_id ASC",
            (key,),
        ).fetchall()
        # A valid node must point at one retained window.  Falling back to the
        # node id would silently archive an incomplete migration and violate
        # the selected-session fail-closed contract.
        session_ids = [str(window[0]) for window in windows if window[0] is not None]
        if not current_session_id or str(current_session_id) not in session_ids:
            raise AdapterError(
                f"SQLite session {key!r} current window is missing from session_windows"
            )
        selected_for_key: List[Dict[str, Any]] = []
        for session_id in session_ids:
            rows = connection.execute(
                "SELECT seq, event_json, created_at FROM transcript_events "
                "WHERE session_id = ? ORDER BY seq ASC",
                (session_id,),
            ).fetchall()
            for seq, event_json, created_at in rows:
                try:
                    event = json.loads(event_json)
                except (TypeError, json.JSONDecodeError) as error:
                    raise AdapterError(
                        f"invalid transcript event {session_id}:{seq} for SQLite session {key!r}: {error}"
                    )
                if not isinstance(event, dict):
                    raise AdapterError(
                        f"invalid transcript event {session_id}:{seq} for SQLite session {key!r}: object required"
                    )
                fallback = created_at if _is_scalar_timestamp(created_at) else ""
                fingerprint_scope = (session_id, "", "", "")
                # Occurrence must be scoped by transcript window and content,
                # so a rolled session does not perturb a later duplicate.
                if isinstance(event.get("message"), dict):
                    role = event["message"].get("role")
                    if role in ("user", "assistant"):
                        content = _message_content(event["message"].get("content"))
                    else:
                        content = ""
                    if content:
                        event_timestamp = _timestamp_value(event.get("timestamp"), "")
                        fingerprint_scope = (
                            session_id,
                            role,
                            content,
                            _canonical_timestamp(event_timestamp),
                        )
                occurrence = occurrence_counts.get(fingerprint_scope, 0)
                if fingerprint_scope[1]:
                    occurrence_counts[fingerprint_scope] = occurrence + 1
                normalized = _normalize_message(
                    event=event,
                    session_id=session_id,
                    fallback_timestamp=fallback,
                    sequence=int(seq),
                    source_order=source_order,
                    occurrence_counts={},
                )
                if normalized is None:
                    continue
                # _normalize_message uses a local occurrence counter for the
                # fallback; replace it with the DB-window-scoped count above.
                if not isinstance(event.get("id"), str) or not event.get("id", "").strip():
                    normalized["identity"] = _event_identity(
                        session_id,
                        None,
                        normalized["role"],
                        normalized["content"],
                        _timestamp_value(event.get("timestamp"), ""),
                        occurrence,
                    )
                selected_for_key.append(normalized)
        selected, next_cursor = _select_messages(key, selected_for_key, _cursor_state(cursors, key))
        next_cursors[key] = next_cursor
        for item in selected:
            item["sk"] = key
            all_messages.append(item)
    return usage, all_messages, next_cursors


def _canonical_sqlite_candidate(
    home: pathlib.Path, agent: str, legacy_store: pathlib.Path
) -> Optional[pathlib.Path]:
    if legacy_store.suffix.lower() == ".sqlite":
        return legacy_store
    if legacy_store.name == "sessions.json" and legacy_store.parent.name == "sessions":
        candidate_agent_dir = legacy_store.parent.parent
        if candidate_agent_dir.name.lower() == agent.lower():
            return candidate_agent_dir / "agent" / "openclaw-agent.sqlite"
    # A custom selector may resolve to a differently named SQLite target, and
    # guessing a sibling database without OpenClaw's resolver could archive a
    # different store.  Let the official CLI resolve it; explicit SQLite mode
    # still accepts a direct .sqlite path.
    return None


def _cli_path(agent: str, home: pathlib.Path, legacy_store: pathlib.Path) -> Optional[pathlib.Path]:
    executable = os.environ.get("OPENCLAW_CLI") or os.environ.get("OPENCLAW_BIN") or "openclaw"
    environment = os.environ.copy()
    # OpenClaw's supported state root is OPENCLAW_STATE_DIR. OPENCLAW_HOME is
    # retained for older installations and for the compact path used by DMA.
    environment["OPENCLAW_STATE_DIR"] = str(home)
    environment["OPENCLAW_HOME"] = str(home)
    command = [
        executable,
        "sessions",
        "--json",
        "--limit",
        "all",
        "--agent",
        agent,
        "--store",
        str(legacy_store),
    ]
    def run_cli(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(argv),
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=float(os.environ.get("DAILY_MEMORY_CLI_TIMEOUT", "30")),
            check=False,
        )

    def failure_diagnostic(completed: subprocess.CompletedProcess[str]) -> str:
        """Extract the CLI error message from stderr or a JSON error envelope."""

        parts: List[str] = []
        if completed.stderr.strip():
            parts.append(completed.stderr.strip())
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and payload.get("ok") is False:
            error = payload.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                parts.append(error["message"].strip())
        if not parts and completed.stdout.strip():
            parts.append(completed.stdout.strip())
        return " ".join(part for part in parts if part)

    try:
        completed = run_cli(command)
    except (FileNotFoundError, PermissionError):
        return None
    except (OSError, UnicodeError, ValueError, subprocess.TimeoutExpired) as error:
        raise AdapterError(f"OpenClaw sessions discovery failed: {error}")
    if completed.returncode != 0:
        diagnostic = failure_diagnostic(completed)
        lowered = diagnostic.lower()
        # OpenClaw's current CLI requires --limit all.  Older installations
        # accepted the same JSON listing but did not know that option; retry
        # only for an option/value parser error, never for an arbitrary store
        # or permission failure.
        limit_option_error = (
            "--limit" in lowered
            or "unknown option" in lowered
            or "unknown argument" in lowered
            or "invalid value" in lowered
        )
        if limit_option_error:
            retry_command = command[:3] + command[5:]
            try:
                completed = run_cli(retry_command)
            except (OSError, UnicodeError, ValueError, subprocess.TimeoutExpired) as error:
                raise AdapterError(f"OpenClaw sessions discovery failed: {error}")
        if completed.returncode != 0:
            final_diagnostic = failure_diagnostic(completed)
            if "Session store target does not exist:" in final_diagnostic:
                raise CliMissingStoreError(
                    "OpenClaw sessions discovery failed: " + final_diagnostic[:500]
                )
            raise AdapterError(
                "OpenClaw sessions discovery failed"
                + (f": {final_diagnostic[:500]}" if final_diagnostic else "")
            )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise AdapterError(f"OpenClaw sessions discovery returned invalid JSON: {error}")
    if not isinstance(payload, dict) or not isinstance(payload.get("sessions"), list):
        raise AdapterError("OpenClaw sessions discovery returned no sessions array")
    raw_path = payload.get("path")
    if isinstance(raw_path, str) and raw_path.strip():
        return _absolute_path(raw_path)
    stores = payload.get("stores")
    if isinstance(stores, list):
        for store in stores:
            if isinstance(store, dict) and store.get("agentId") == agent:
                store_path = store.get("path")
                if isinstance(store_path, str) and store_path.strip():
                    return _absolute_path(store_path)
    raise AdapterError("OpenClaw sessions discovery returned no physical session-store path")


def _choose_backend(
    agent: str, home: pathlib.Path, legacy_store: pathlib.Path
) -> Tuple[str, Optional[pathlib.Path]]:
    requested = os.environ.get("DAILY_MEMORY_SESSION_BACKEND", "auto").strip().lower()
    if requested not in ("auto", "jsonl", "sqlite"):
        raise AdapterError(
            "DAILY_MEMORY_SESSION_BACKEND must be auto, jsonl, or sqlite"
        )
    override = os.environ.get("DAILY_MEMORY_SQLITE_PATH", "").strip()
    explicit_path = _absolute_path(override) if override else None
    if requested == "jsonl":
        return "jsonl", None
    if requested == "sqlite":
        candidate = explicit_path or _canonical_sqlite_candidate(home, agent, legacy_store)
        if candidate is None:
            raise AdapterError(
                "SQLite backend requires DAILY_MEMORY_SQLITE_PATH or a canonical agent sessions.json selector"
            )
        return "sqlite", candidate
    if explicit_path is not None:
        return "sqlite", explicit_path

    # The official CLI is the authoritative selector resolver.  Only a
    # missing executable, or its exact pre-migration "physical target does not
    # exist" diagnostic with a present legacy index, may continue locally;
    # malformed output and other failures are real discovery errors.
    try:
        cli_result = _cli_path(agent, home, legacy_store)
    except CliMissingStoreError:
        # An installed CLI can report the canonical SQLite target as absent on
        # a pre-migration host where the legacy index still exists.  Continue
        # only when this is that exact, known diagnostic and the canonical
        # target is genuinely absent; never mask a malformed/permission error
        # or a broken existing SQLite file with JSONL fallback.
        candidate = _canonical_sqlite_candidate(home, agent, legacy_store)
        if candidate is None:
            raise
        try:
            candidate_missing = not candidate.exists()
        except OSError:
            candidate_missing = False
        if not candidate_missing or not legacy_store.is_file():
            raise
        return "jsonl", legacy_store
    if cli_result is not None:
        if cli_result.suffix.lower() in (".json", ".jsonl"):
            return "jsonl", cli_result
        return "sqlite", cli_result
    candidate = _canonical_sqlite_candidate(home, agent, legacy_store)
    if candidate is not None and candidate.is_file():
        return "sqlite", candidate
    return "jsonl", None


def _finalize_messages(messages: List[Dict[str, Any]], key_count: int) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    messages.sort(
        key=lambda item: _timestamp_sort_key(
            item.get("ts", ""), int(item.get("sequence", 0)), int(item.get("source_order", 0))
        )
    )
    data: List[Dict[str, Any]] = []
    for item in messages:
        content = item["content"]
        if key_count > 1:
            content = f"[{item['sk']}] {content}"
        data.append({"sk": item["sk"], "ts": item["ts"], "role": item["role"], "content": content})
    stripped = [{"role": item["role"], "content": item["content"]} for item in data]
    return data, stripped


def snapshot(args: argparse.Namespace) -> Dict[str, Any]:
    agent = args.agent.strip()
    if not agent:
        raise AdapterError("--agent must be non-blank")
    home = _absolute_path(args.home)
    legacy_store = _absolute_path(args.legacy_store)
    checkpoint_path = _absolute_path(args.checkpoint)
    keys = _parse_keys_json(args.keys_json)
    cursors, _envelope = _load_checkpoint(checkpoint_path)

    backend, selected_path = _choose_backend(agent, home, legacy_store)
    if backend == "jsonl":
        source_store = selected_path or legacy_store
        usage, messages, next_cursors = _legacy_snapshot(keys, source_store, cursors)
    else:
        if selected_path is None or not selected_path.is_file():
            raise AdapterError(f"SQLite store not found: {selected_path}")
        try:
            connection = sqlite3.connect(_sqlite_uri(selected_path), uri=True)
        except (sqlite3.Error, OSError) as error:
            raise AdapterError(f"cannot open SQLite store read-only {selected_path}: {error}")
        try:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA busy_timeout = 30000")
            connection.execute("BEGIN")
            _validate_sqlite_schema(connection, selected_path, agent)
            usage, messages, next_cursors = _sqlite_messages(connection, keys, cursors)
        except sqlite3.DatabaseError as error:
            raise AdapterError(f"SQLite read failed for {selected_path}: {error}")
        finally:
            try:
                connection.rollback()
            except sqlite3.DatabaseError:
                pass
            connection.close()

    data, stripped = _finalize_messages(messages, len(keys))
    return {
        "backend": backend,
        "sessions": usage,
        "count": len(data),
        "data": data,
        "stripped": stripped,
        "checkpoint": next_cursors,
    }


def _list_row(key: str, entry: Dict[str, Any], session_file: str) -> Dict[str, Any]:
    usage = _entry_usage(key, entry)
    return {
        "key": key,
        "sessionFile": session_file,
        "inputTokens": usage["inputTokens"],
        "totalTokens": usage["totalTokens"],
        "totalTokensFresh": usage["totalTokensFresh"],
    }


def _legacy_list(legacy_store: pathlib.Path) -> List[Dict[str, Any]]:
    if not legacy_store.is_file():
        raise AdapterError(f"legacy session index not found: {legacy_store}")
    raw = _read_json_file(legacy_store, "legacy session index")
    if not isinstance(raw, dict):
        raise AdapterError("legacy session index root must be a JSON object")
    rows: List[Dict[str, Any]] = []
    for key, entry in raw.items():
        if not isinstance(key, str) or not key.strip():
            raise AdapterError("legacy session index contains a blank or non-string session key")
        if not isinstance(entry, dict):
            raise AdapterError(f"legacy session index entry for {key!r} is not an object")
        raw_session_file = entry.get("sessionFile", "")
        session_file = raw_session_file if isinstance(raw_session_file, str) else ""
        rows.append(_list_row(key, entry, session_file))
    return rows


def _sqlite_list(connection: sqlite3.Connection) -> List[Dict[str, Any]]:
    try:
        rows = connection.execute(
            "SELECT session_key, entry_json FROM session_nodes ORDER BY session_key ASC"
        ).fetchall()
    except sqlite3.DatabaseError as error:
        raise AdapterError(f"cannot list SQLite sessions: {error}")
    result: List[Dict[str, Any]] = []
    for key, entry_json in rows:
        if not isinstance(key, str) or not key.strip():
            raise AdapterError("SQLite session_nodes contains a blank session key")
        try:
            entry = json.loads(entry_json)
        except (TypeError, json.JSONDecodeError) as error:
            raise AdapterError(f"invalid entry_json for SQLite session {key!r}: {error}")
        if not isinstance(entry, dict):
            raise AdapterError(f"entry_json for SQLite session {key!r} is not an object")
        result.append(_list_row(key, entry, ""))
    return result


def list_metadata(args: argparse.Namespace) -> Dict[str, Any]:
    agent = args.agent.strip()
    if not agent:
        raise AdapterError("--agent must be non-blank")
    home = _absolute_path(args.home)
    legacy_store = _absolute_path(args.legacy_store)
    backend, selected_path = _choose_backend(agent, home, legacy_store)
    if backend == "jsonl":
        rows = _legacy_list(selected_path or legacy_store)
    else:
        if selected_path is None or not selected_path.is_file():
            raise AdapterError(f"SQLite store not found: {selected_path}")
        try:
            connection = sqlite3.connect(_sqlite_uri(selected_path), uri=True)
        except (sqlite3.Error, OSError) as error:
            raise AdapterError(f"cannot open SQLite store read-only {selected_path}: {error}")
        try:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA busy_timeout = 30000")
            connection.execute("BEGIN")
            _validate_sqlite_schema(connection, selected_path, agent)
            rows = _sqlite_list(connection)
        except sqlite3.DatabaseError as error:
            raise AdapterError(f"SQLite read failed for {selected_path}: {error}")
        finally:
            try:
                connection.rollback()
            except sqlite3.DatabaseError:
                pass
            connection.close()
    return {"agent_id": agent, "count": len(rows), "sessions": rows}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read OpenClaw session stores for DMA")
    subparsers = parser.add_subparsers(dest="command", required=True)
    snapshot_parser = subparsers.add_parser("snapshot", help="read one immutable session snapshot")
    snapshot_parser.add_argument("--agent", required=True)
    snapshot_parser.add_argument("--home", required=True)
    snapshot_parser.add_argument("--legacy-store", required=True)
    snapshot_parser.add_argument("--checkpoint", required=True)
    snapshot_parser.add_argument("--keys-json", required=True)
    list_parser = subparsers.add_parser("list", help="list session metadata without transcripts")
    list_parser.add_argument("--agent", required=True)
    list_parser.add_argument("--home", required=True)
    list_parser.add_argument("--legacy-store", required=True)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "snapshot":
            result = snapshot(args)
        elif args.command == "list":
            result = list_metadata(args)
        else:
            raise AdapterError(f"unsupported command: {args.command}")
        sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
        return 0
    except AdapterError as error:
        print(f"session-store: {error}", file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error) as error:
        print(f"session-store: storage error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
