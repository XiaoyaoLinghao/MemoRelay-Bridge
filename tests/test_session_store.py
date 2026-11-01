#!/usr/bin/env python3
"""Contract tests for the read-only native session snapshot adapter.

The adapter is intentionally exercised as a subprocess.  This keeps the test
at the public CLI boundary used by archive-engine.sh and catches diagnostics
leaking into stdout, partial JSON on failure, and accidental source writes.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
READER = ROOT / "scripts" / "session-store.py"
AGENT = "main"
KEY = "agent:main:main"
OTHER_KEY = "agent:main:other"
TS0 = "2026-09-08T10:00:00.000Z"
TS1 = "2026-09-08T10:01:00.000Z"


SQLITE_SCHEMA = """
CREATE TABLE schema_meta (
    meta_key TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    agent_id TEXT,
    app_version TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE session_nodes (
    session_key TEXT PRIMARY KEY,
    current_session_id TEXT NOT NULL,
    entry_json TEXT NOT NULL,
    entry_valid INTEGER NOT NULL DEFAULT 1,
    updated_at INTEGER NOT NULL
);
CREATE TABLE session_windows (
    session_id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL,
    previous_session_id TEXT,
    reason TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    transcript_updated_at INTEGER,
    transcript_observed_at INTEGER
);
CREATE TABLE transcript_events (
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (session_id, seq)
);
CREATE TABLE transcript_event_identities (
    session_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event_type TEXT,
    parent_id TEXT,
    message_idempotency_key TEXT,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (session_id, event_id)
);
"""


def message_event(
    event_id: str,
    timestamp: str,
    role: str,
    content: Any,
) -> dict[str, Any]:
    return {
        "type": "message",
        "id": event_id,
        "timestamp": timestamp,
        "message": {"role": role, "content": content},
    }


def session_event(session_id: str) -> dict[str, Any]:
    return {"type": "session", "version": 3, "id": session_id}


def required_message_rows(payload: dict[str, Any]) -> list[tuple[str, str, str, str]]:
    return [
        (row["sk"], row["ts"], row["role"], row["content"])
        for row in payload["data"]
    ]


class SessionStoreContractTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="dma-session-store-")
        self.root = Path(self.tmp.name)
        self.home = self.root / "openclaw"
        self.home.mkdir()
        self.sessions_dir = self.home / "agents" / AGENT / "sessions"
        self.sessions_dir.mkdir(parents=True)
        self.legacy_store = self.sessions_dir / "sessions.json"
        self.checkpoint = self.root / "checkpoint.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_snapshot(
        self,
        *,
        backend: str,
        keys: Iterable[str] = (KEY,),
        sqlite_path: Path | None = None,
        legacy_store: Path | None = None,
        checkpoint: Path | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["DAILY_MEMORY_SESSION_BACKEND"] = backend
        env.pop("DAILY_MEMORY_SQLITE_PATH", None)
        if sqlite_path is not None:
            env["DAILY_MEMORY_SQLITE_PATH"] = str(sqlite_path)
        if env_extra:
            env.update(env_extra)
        args = [
            sys.executable,
            str(READER),
            "snapshot",
            "--agent",
            AGENT,
            "--home",
            str(self.home),
            "--legacy-store",
            str(legacy_store or self.legacy_store),
            "--checkpoint",
            str(checkpoint or self.checkpoint),
            "--keys-json",
            json.dumps(list(keys), separators=(",", ":")),
        ]
        return subprocess.run(
            args,
            cwd=ROOT,
            env=env,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )

    def run_list(
        self,
        *,
        backend: str,
        sqlite_path: Path | None = None,
        legacy_store: Path | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["DAILY_MEMORY_SESSION_BACKEND"] = backend
        env.pop("DAILY_MEMORY_SQLITE_PATH", None)
        if sqlite_path is not None:
            env["DAILY_MEMORY_SQLITE_PATH"] = str(sqlite_path)
        if env_extra:
            env.update(env_extra)
        args = [
            sys.executable,
            str(READER),
            "list",
            "--agent",
            AGENT,
            "--home",
            str(self.home),
            "--legacy-store",
            str(legacy_store or self.legacy_store),
        ]
        return subprocess.run(
            args,
            cwd=ROOT,
            env=env,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )

    def successful_snapshot(self, **kwargs: Any) -> dict[str, Any]:
        result = self.run_snapshot(**kwargs)
        self.assertEqual(
            result.returncode,
            0,
            msg=f"snapshot failed\nstdout={result.stdout!r}\nstderr={result.stderr!r}",
        )
        self.assertTrue(result.stdout.strip(), "successful snapshot must emit JSON")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:  # pragma: no cover - assertion context
            self.fail(f"stdout is not one JSON document: {exc}: {result.stdout!r}")
        self.assertEqual(
            set(payload),
            {"backend", "sessions", "count", "data", "stripped", "checkpoint"},
        )
        self.assertIsInstance(payload["checkpoint"], dict)
        for session in payload["sessions"]:
            self.assertEqual(
                set(session),
                {"key", "inputTokens", "totalTokens", "totalTokensFresh"},
            )
        for row in payload["data"]:
            self.assertEqual(set(row), {"sk", "ts", "role", "content"})
        self.assertEqual(
            payload["stripped"],
            [{"role": row["role"], "content": row["content"]} for row in payload["data"]],
        )
        self.assertEqual(payload["count"], len(payload["data"]))
        return payload

    def write_jsonl_store(
        self,
        events: list[dict[str, Any]],
        *,
        key: str = KEY,
        session_id: str = "jsonl-session",
        input_tokens: int = 17,
        total_tokens: int = 29,
        total_tokens_fresh: bool = True,
    ) -> Path:
        transcript = self.sessions_dir / f"{session_id}.jsonl"
        transcript.write_text(
            "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
            encoding="utf-8",
        )
        store = {
            key: {
                "sessionId": session_id,
                "updatedAt": 1_757_320_000_000,
                "inputTokens": input_tokens,
                "totalTokens": total_tokens,
                "totalTokensFresh": total_tokens_fresh,
                "sessionFile": str(transcript),
            }
        }
        self.legacy_store.write_text(
            json.dumps(store, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return transcript

    def create_sqlite_store(
        self,
        *,
        version: int = 19,
        current_entry: dict[str, Any] | None = None,
        windows: list[tuple[str, str, list[dict[str, Any]]]] | None = None,
        include_other: bool = True,
        wal: bool = False,
    ) -> Path:
        db_path = self.root / "agents" / AGENT / "openclaw-agent.sqlite"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        current_entry = current_entry or {
            "sessionId": "sqlite-current",
            "updatedAt": 1_757_320_000_000,
            "inputTokens": 101,
            "totalTokens": 202,
            "totalTokensFresh": True,
        }
        if windows is None:
            windows = [
                (
                    "sqlite-old",
                    KEY,
                    [message_event("old-1", TS0, "user", "from older generation")],
                ),
                (
                    "sqlite-current",
                    KEY,
                    [
                        message_event("cur-1", TS0, "assistant", "same timestamp"),
                        message_event("cur-2", TS0, "assistant", "same timestamp"),
                        message_event("cur-3", TS1, "toolResult", "must be stripped"),
                        message_event("cur-4", TS1, "assistant", "current answer"),
                    ],
                ),
            ]

        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(SQLITE_SCHEMA)
            if wal:
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(f"PRAGMA user_version = {int(version)}")
            conn.execute(
                "INSERT INTO schema_meta "
                "(meta_key, role, schema_version, agent_id, app_version, created_at, updated_at) "
                "VALUES ('primary', 'agent', ?, ?, '2026.9.2', 1, 1)",
                (version, AGENT),
            )
            current_id = str(current_entry["sessionId"])
            conn.execute(
                "INSERT INTO session_nodes "
                "(session_key, current_session_id, entry_json, entry_valid, updated_at) "
                "VALUES (?, ?, ?, 1, ?)",
                (KEY, current_id, json.dumps(current_entry), int(current_entry["updatedAt"])),
            )
            previous: str | None = None
            for window_index, (session_id, session_key, events) in enumerate(windows):
                conn.execute(
                    "INSERT INTO session_windows "
                    "(session_id, session_key, previous_session_id, reason, created_at, updated_at, "
                    "transcript_updated_at, transcript_observed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        session_id,
                        session_key,
                        previous,
                        "initial" if window_index == 0 else "rollover",
                        100 + window_index,
                        100 + window_index,
                        100 + window_index,
                        100 + window_index,
                    ),
                )
                previous = session_id
                for seq, event in enumerate(events, start=1):
                    event_json = json.dumps(event, ensure_ascii=False)
                    event_id = str(event["id"])
                    conn.execute(
                        "INSERT INTO transcript_events (session_id, seq, event_json, created_at) "
                        "VALUES (?, ?, ?, ?)",
                        (session_id, seq, event_json, 1000 + seq),
                    )
                    conn.execute(
                        "INSERT INTO transcript_event_identities "
                        "(session_id, event_id, seq, event_type, parent_id, "
                        "message_idempotency_key, created_at) VALUES (?, ?, ?, ?, NULL, NULL, ?)",
                        (session_id, event_id, seq, event.get("type"), 1000 + seq),
                    )
            if include_other:
                other_entry = {
                    "sessionId": "sqlite-other",
                    "updatedAt": 1_757_320_000_001,
                    "inputTokens": 1,
                    "totalTokens": 2,
                    "totalTokensFresh": True,
                }
                conn.execute(
                    "INSERT INTO session_nodes "
                    "(session_key, current_session_id, entry_json, entry_valid, updated_at) "
                    "VALUES (?, ?, ?, 1, ?)",
                    (
                        OTHER_KEY,
                        "sqlite-other",
                        json.dumps(other_entry),
                        int(other_entry["updatedAt"]),
                    ),
                )
                conn.execute(
                    "INSERT INTO session_windows "
                    "(session_id, session_key, reason, created_at, updated_at) "
                    "VALUES ('sqlite-other', ?, 'initial', 1, 1)",
                    (OTHER_KEY,),
                )
                other_event = message_event("other-1", TS1, "user", "unselected")
                conn.execute(
                    "INSERT INTO transcript_events (session_id, seq, event_json, created_at) "
                    "VALUES ('sqlite-other', 1, ?, 1)",
                    (json.dumps(other_event),),
                )
                conn.execute(
                    "INSERT INTO transcript_event_identities "
                    "(session_id, event_id, seq, event_type, created_at) "
                    "VALUES ('sqlite-other', 'other-1', 1, 'message', 1)"
                )
            conn.commit()
            if wal:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        return db_path

    def append_sqlite_event(
        self,
        db_path: Path,
        *,
        session_id: str,
        seq: int,
        event: dict[str, Any],
        commit: bool = True,
    ) -> sqlite3.Connection:
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.execute(
            "INSERT INTO transcript_events (session_id, seq, event_json, created_at) VALUES (?, ?, ?, ?)",
            (session_id, seq, json.dumps(event, ensure_ascii=False), 2000 + seq),
        )
        conn.execute(
            "INSERT INTO transcript_event_identities "
            "(session_id, event_id, seq, event_type, created_at) VALUES (?, ?, ?, 'message', ?)",
            (session_id, event["id"], seq, 2000 + seq),
        )
        if commit:
            conn.commit()
        return conn

    def sqlite_logical_rows(self, db_path: Path) -> dict[str, list[tuple[Any, ...]]]:
        """Capture source rows without treating WAL/SHM coordination as a write."""
        uri = f"file:{db_path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            return {
                "schema_meta": conn.execute(
                    "SELECT * FROM schema_meta ORDER BY meta_key"
                ).fetchall(),
                "session_nodes": conn.execute(
                    "SELECT * FROM session_nodes ORDER BY session_key"
                ).fetchall(),
                "session_windows": conn.execute(
                    "SELECT * FROM session_windows ORDER BY session_id"
                ).fetchall(),
                "transcript_events": conn.execute(
                    "SELECT * FROM transcript_events ORDER BY session_id, seq"
                ).fetchall(),
                "transcript_event_identities": conn.execute(
                    "SELECT * FROM transcript_event_identities ORDER BY session_id, event_id"
                ).fetchall(),
            }
        finally:
            conn.close()

    def test_jsonl_long_text_and_metadata_are_lossless(self) -> None:
        long_text = ("0123456789abcdef" * 900) + "\n最后一行"
        events = [
            session_event("jsonl-session"),
            message_event("long-user", TS0, "user", long_text),
            message_event(
                "array-assistant",
                TS1,
                "assistant",
                [{"type": "text", "text": "第一段"}, {"type": "text", "text": "第二段"}],
            ),
            message_event("tool", TS1, "toolResult", "not a user/assistant message"),
            message_event("system", TS1, "system", "ignored"),
        ]
        transcript = self.write_jsonl_store(events)
        before_store = self.legacy_store.read_bytes()
        before_transcript = transcript.read_bytes()

        payload = self.successful_snapshot(backend="jsonl")

        self.assertEqual(payload["backend"], "jsonl")
        self.assertEqual(
            payload["sessions"],
            [{"key": KEY, "inputTokens": 17, "totalTokens": 29, "totalTokensFresh": True}],
        )
        self.assertEqual(
            required_message_rows(payload),
            [
                (KEY, TS0, "user", long_text),
                (KEY, TS1, "assistant", "第一段\n第二段"),
            ],
        )
        self.assertEqual(self.legacy_store.read_bytes(), before_store)
        self.assertEqual(transcript.read_bytes(), before_transcript)
        self.assertFalse(self.checkpoint.exists(), "reader must not publish checkpoints")

    def test_jsonl_identity_cursor_handles_equal_timestamps_repeated_text_and_rerun(self) -> None:
        repeated = "同一条文字，不能按正文去重"
        initial = [
            session_event("identity-session"),
            message_event("m-1", TS0, "user", repeated),
            message_event("m-2", TS0, "assistant", repeated),
            message_event("m-3", TS0, "user", repeated),
        ]
        transcript = self.write_jsonl_store(initial, session_id="identity-session")
        first = self.successful_snapshot(backend="jsonl")
        self.assertEqual(first["count"], 3)
        self.assertEqual([row[3] for row in required_message_rows(first)], [repeated] * 3)
        self.checkpoint.write_text(json.dumps(first["checkpoint"]) + "\n", encoding="utf-8")

        with transcript.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(message_event("m-4", TS0, "assistant", repeated), ensure_ascii=False)
                + "\n"
            )
            stream.write(
                json.dumps(message_event("m-5", TS1, "user", "newer"), ensure_ascii=False) + "\n"
            )
        before_checkpoint = self.checkpoint.read_bytes()
        second = self.successful_snapshot(backend="jsonl")
        self.assertEqual(
            required_message_rows(second), [(KEY, TS0, "assistant", repeated), (KEY, TS1, "user", "newer")]
        )
        self.assertEqual(self.checkpoint.read_bytes(), before_checkpoint)
        self.checkpoint.write_text(json.dumps(second["checkpoint"]) + "\n", encoding="utf-8")

        third = self.successful_snapshot(backend="jsonl")
        self.assertEqual(third["count"], 0)
        self.assertEqual(third["data"], [])
        self.assertEqual(third["stripped"], [])

    def test_filtered_noise_is_consumed_without_forwarding_and_cursor_stays_stable(self) -> None:
        noise = "[heartbeat poll] internal scheduler message"
        events = [
            session_event("noise-session"),
            message_event("noise-1", TS0, "user", noise),
            message_event("real-1", TS1, "user", "keep this message"),
        ]
        transcript = self.write_jsonl_store(events, session_id="noise-session")

        first = self.successful_snapshot(backend="jsonl")
        self.assertEqual(
            required_message_rows(first), [(KEY, TS1, "user", "keep this message")]
        )
        self.assertNotIn(noise, [row[3] for row in required_message_rows(first)])
        self.checkpoint.write_text(json.dumps(first["checkpoint"]) + "\n", encoding="utf-8")
        before_checkpoint = self.checkpoint.read_bytes()

        second = self.successful_snapshot(backend="jsonl")
        self.assertEqual(second["count"], 0)
        self.assertEqual(second["data"], [])
        self.assertEqual(second["checkpoint"], first["checkpoint"])
        self.assertEqual(self.checkpoint.read_bytes(), before_checkpoint)

        with transcript.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    message_event("real-2", TS1, "assistant", "second real message"),
                    ensure_ascii=False,
                )
                + "\n"
            )
        third = self.successful_snapshot(backend="jsonl")
        self.assertEqual(
            required_message_rows(third), [(KEY, TS1, "assistant", "second real message")]
        )

    def test_old_timestamp_checkpoint_is_consumed_and_upgraded(self) -> None:
        events = [
            session_event("legacy-cursor-session"),
            message_event("at-old-cursor", TS0, "user", "already archived"),
            message_event("after-old-cursor", TS1, "assistant", "must be returned"),
        ]
        self.write_jsonl_store(events, session_id="legacy-cursor-session")
        self.checkpoint.write_text(json.dumps({KEY: TS0}) + "\n", encoding="utf-8")

        payload = self.successful_snapshot(backend="jsonl")

        self.assertEqual(
            required_message_rows(payload), [(KEY, TS1, "assistant", "must be returned")]
        )
        self.assertIn(KEY, payload["checkpoint"])
        self.assertNotEqual(payload["checkpoint"][KEY], TS0)

        # A v1 timestamp is only a bootstrap boundary.  After the reader has
        # emitted its v2 identity cursor, a distinct event appended at that
        # same timestamp must be visible instead of being hidden forever by a
        # strict ``ts > checkpoint`` filter.
        self.checkpoint.write_text(json.dumps(payload["checkpoint"]) + "\n", encoding="utf-8")
        transcript = self.sessions_dir / "legacy-cursor-session.jsonl"
        with transcript.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    message_event("late-equal", TS0, "user", "late at old timestamp"),
                    ensure_ascii=False,
                )
                + "\n"
            )
        late = self.successful_snapshot(backend="jsonl")
        self.assertEqual(
            required_message_rows(late), [(KEY, TS0, "user", "late at old timestamp")]
        )

    def test_malformed_selected_jsonl_fails_closed_without_partial_json_or_checkpoint(self) -> None:
        transcript = self.sessions_dir / "malformed.jsonl"
        transcript.write_text(
            "\n".join(
                [
                    json.dumps(session_event("malformed")),
                    json.dumps(message_event("valid", TS0, "user", "must not publish")),
                    '{"type":"message","id":"broken"',
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        self.legacy_store.write_text(
            json.dumps(
                {
                    KEY: {
                        "sessionId": "malformed",
                        "updatedAt": 1,
                        "inputTokens": 1,
                        "totalTokens": 1,
                        "totalTokensFresh": True,
                        "sessionFile": str(transcript),
                    }
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.checkpoint.write_text(json.dumps({KEY: TS0}) + "\n", encoding="utf-8")
        before_checkpoint = self.checkpoint.read_bytes()

        result = self.run_snapshot(backend="jsonl")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "", "failed snapshot must not emit partial JSON")
        self.assertIn("malformed JSONL record", result.stderr)
        self.assertEqual(self.checkpoint.read_bytes(), before_checkpoint)

        with self.subTest("legacy header id must match index sessionId"):
            mismatch_transcript = self.sessions_dir / "identity-mismatch.jsonl"
            mismatch_transcript.write_text(
                "".join(
                    json.dumps(event, ensure_ascii=False) + "\n"
                    for event in [
                        session_event("header-session"),
                        message_event("mismatch-message", TS0, "user", "must fail closed"),
                    ]
                ),
                encoding="utf-8",
            )
            self.legacy_store.write_text(
                json.dumps(
                    {
                        KEY: {
                            "sessionId": "index-session",
                            "updatedAt": 1,
                            "inputTokens": 1,
                            "totalTokens": 1,
                            "totalTokensFresh": True,
                            "sessionFile": str(mismatch_transcript),
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            self.checkpoint.write_text(json.dumps({KEY: ""}) + "\n", encoding="utf-8")
            mismatch_checkpoint = self.checkpoint.read_bytes()
            mismatch_result = self.run_snapshot(backend="jsonl")
            self.assertNotEqual(mismatch_result.returncode, 0)
            self.assertEqual(mismatch_result.stdout.strip(), "")
            self.assertEqual(self.checkpoint.read_bytes(), mismatch_checkpoint)

        with self.subTest("SQLite entry sessionId must match current window"):
            db_path = self.create_sqlite_store(
                current_entry={
                    "sessionId": "entry-json-id",
                    "updatedAt": 1_757_320_000_000,
                    "inputTokens": 1,
                    "totalTokens": 1,
                    "totalTokensFresh": True,
                },
                windows=[
                    (
                        "sqlite-current",
                        KEY,
                        [message_event("sqlite-mismatch", TS0, "user", "must fail closed")],
                    )
                ],
                include_other=False,
            )
            self.checkpoint.write_text(json.dumps({KEY: ""}) + "\n", encoding="utf-8")
            with sqlite3.connect(db_path) as connection:
                connection.execute("UPDATE session_nodes SET current_session_id = 'sqlite-current' WHERE session_key = ?", (KEY,))
            connection.close()
            mismatch_checkpoint = self.checkpoint.read_bytes()
            mismatch_db = db_path.read_bytes()
            mismatch_result = self.run_snapshot(
                backend="sqlite",
                sqlite_path=db_path,
                legacy_store=self.root / "missing-sessions.json",
            )
            self.assertNotEqual(mismatch_result.returncode, 0)
            self.assertEqual(mismatch_result.stdout.strip(), "")
            self.assertEqual(self.checkpoint.read_bytes(), mismatch_checkpoint)
            self.assertEqual(db_path.read_bytes(), mismatch_db)

    def test_malformed_legacy_checkpoint_fails_closed_without_partial_json_or_write(self) -> None:
        transcript = self.write_jsonl_store(
            [
                session_event("malformed-cursor"),
                message_event("cursor-message", TS0, "user", "must not be selected"),
            ],
            session_id="malformed-cursor",
        )
        self.checkpoint.write_text(json.dumps({KEY: "not-a-timestamp"}) + "\n", encoding="utf-8")
        before_checkpoint = self.checkpoint.read_bytes()
        before_transcript = transcript.read_bytes()

        result = self.run_snapshot(backend="jsonl")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "")
        self.assertEqual(self.checkpoint.read_bytes(), before_checkpoint)
        self.assertEqual(transcript.read_bytes(), before_transcript)

    def test_sqlite_reader_reads_all_generations_and_is_read_only(self) -> None:
        db_path = self.create_sqlite_store(wal=True)
        before_bytes = db_path.read_bytes()
        before_rows = self.sqlite_logical_rows(db_path)

        payload = self.successful_snapshot(
            backend="sqlite",
            sqlite_path=db_path,
            legacy_store=self.root / "missing-sessions.json",
        )

        self.assertEqual(payload["backend"], "sqlite")
        self.assertEqual(
            payload["sessions"],
            [{"key": KEY, "inputTokens": 101, "totalTokens": 202, "totalTokensFresh": True}],
        )
        self.assertEqual(
            required_message_rows(payload),
            [
                (KEY, TS0, "user", "from older generation"),
                (KEY, TS0, "assistant", "same timestamp"),
                (KEY, TS0, "assistant", "same timestamp"),
                (KEY, TS1, "assistant", "current answer"),
            ],
        )
        self.assertTrue(all(row[0] == KEY for row in required_message_rows(payload)))
        self.assertEqual(db_path.read_bytes(), before_bytes)
        self.assertEqual(self.sqlite_logical_rows(db_path), before_rows)
        self.assertFalse(self.checkpoint.exists())

    def test_sqlite_list_reads_metadata_without_transcript_reads(self) -> None:
        db_path = self.create_sqlite_store()
        connection = sqlite3.connect(db_path)
        try:
            connection.execute(
                "UPDATE transcript_events SET event_json = '{' WHERE session_id = 'sqlite-current'"
            )
            connection.commit()
        finally:
            connection.close()
        before_db = db_path.read_bytes()

        result = self.run_list(
            backend="sqlite",
            sqlite_path=db_path,
            legacy_store=self.root / "missing-sessions.json",
        )

        self.assertEqual(
            result.returncode,
            0,
            msg=f"metadata list failed\nstdout={result.stdout!r}\nstderr={result.stderr!r}",
        )
        payload = json.loads(result.stdout)
        self.assertEqual(set(payload), {"agent_id", "count", "sessions"})
        self.assertEqual(payload["agent_id"], AGENT)
        self.assertEqual(payload["count"], len(payload["sessions"]))
        self.assertGreaterEqual(payload["count"], 2)
        listed = {entry["key"]: entry for entry in payload["sessions"]}
        self.assertIn(KEY, listed)
        self.assertEqual(listed[KEY]["sessionFile"], "")
        self.assertEqual(db_path.read_bytes(), before_db)

    def test_auto_with_explicit_sqlite_path_works_without_sessions_json_or_openclaw_cli(self) -> None:
        db_path = self.create_sqlite_store()
        payload = self.successful_snapshot(
            backend="auto",
            sqlite_path=db_path,
            legacy_store=self.root / "does-not-exist" / "sessions.json",
            env_extra={"PATH": str(self.root / "empty-bin")},
        )
        self.assertEqual(payload["backend"], "sqlite")
        self.assertEqual(payload["count"], 4)

    def test_unknown_sqlite_schema_fails_closed_without_source_or_checkpoint_writes(self) -> None:
        db_path = self.create_sqlite_store(version=20)
        before_db = db_path.read_bytes()
        self.checkpoint.write_text(json.dumps({KEY: TS0}) + "\n", encoding="utf-8")
        before_checkpoint = self.checkpoint.read_bytes()

        result = self.run_snapshot(
            backend="sqlite",
            sqlite_path=db_path,
            legacy_store=self.root / "missing-sessions.json",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "")
        self.assertIn("unsupported OpenClaw agent SQLite schema", result.stderr)
        self.assertEqual(db_path.read_bytes(), before_db)
        self.assertEqual(self.checkpoint.read_bytes(), before_checkpoint)

    def test_official_cli_json_failure_fallback_is_narrow_and_scoped(self) -> None:
        import importlib.util
        from types import SimpleNamespace
        from unittest.mock import patch

        self.write_jsonl_store([session_event("cli"), message_event("one", TS0, "user", "legacy retained")], session_id="cli")
        spec = importlib.util.spec_from_file_location("dma_store_test", READER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        args = SimpleNamespace(agent=AGENT, home=str(self.home), legacy_store=str(self.legacy_store), checkpoint=str(self.checkpoint), keys_json=json.dumps([KEY]))
        env = dict(os.environ, DAILY_MEMORY_SESSION_BACKEND="auto")
        env.pop("DAILY_MEMORY_SQLITE_PATH", None)
        envelope = {"ok": False, "error": {"message": "Session store target does not exist: selected database"}}
        response = subprocess.CompletedProcess([], 1, json.dumps(envelope), "")
        with patch.dict(os.environ, env, clear=True), patch.object(module.subprocess, "run", return_value=response) as cli:
            result = module.snapshot(args)
        self.assertEqual(result["backend"], "jsonl")
        self.assertEqual(result["count"], 1)
        command = cli.call_args.args[0]
        self.assertEqual(command[command.index("--limit") + 1], "all")
        self.assertEqual(command[command.index("--store") + 1], str(self.legacy_store))
        for stdout, stderr in [(json.dumps({"ok": False, "error": {"message": "permission denied"}}), ""), ("not JSON", "unrelated failure")]:
            with self.subTest(stdout=stdout), patch.dict(os.environ, env, clear=True), patch.object(module.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, stdout, stderr)):
                with self.assertRaises(module.AdapterError):
                    module.snapshot(args)
        self.assertFalse(self.checkpoint.exists())

    def test_unselected_legacy_cursor_is_preserved_and_remains_readable(self) -> None:
        self.write_jsonl_store([session_event("preserve"), message_event("old", TS0, "user", "already archived")], session_id="preserve")
        self.checkpoint.write_text(json.dumps({KEY: TS0, OTHER_KEY: TS1}), encoding="utf-8")
        first = self.successful_snapshot(backend="jsonl")
        self.assertEqual(first["count"], 0)
        self.assertEqual(first["checkpoint"][OTHER_KEY], TS1)
        self.checkpoint.write_text(json.dumps(first["checkpoint"]), encoding="utf-8")
        self.assertEqual(self.successful_snapshot(backend="jsonl")["count"], 0)

    def test_missing_current_window_fails_before_empty_checkpoint_proposal(self) -> None:
        db = self.create_sqlite_store()
        with sqlite3.connect(db) as connection:
            connection.execute("DELETE FROM session_windows WHERE session_id = 'sqlite-current'")
        connection.close()
        result = self.run_snapshot(backend="sqlite", sqlite_path=db)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("current window is missing", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse(self.checkpoint.exists())

    def test_jsonl_to_sqlite_migration_reuses_event_ids_without_duplicates(self) -> None:
        events = [
            session_event("migration-jsonl"),
            message_event("same-1", TS0, "user", "migrate me"),
            message_event("same-2", TS0, "assistant", "already seen"),
        ]
        self.write_jsonl_store(events, session_id="migration-jsonl")
        jsonl = self.successful_snapshot(backend="jsonl")
        self.checkpoint.write_text(json.dumps(jsonl["checkpoint"]) + "\n", encoding="utf-8")

        db_path = self.create_sqlite_store(
            current_entry={
                "sessionId": "migration-jsonl",
                "updatedAt": 1_757_320_000_000,
                "inputTokens": 17,
                "totalTokens": 29,
                "totalTokensFresh": True,
            },
            windows=[
                (
                    "migration-jsonl",
                    KEY,
                    [
                        message_event("same-1", TS0, "user", "migrate me"),
                        message_event("same-2", TS0, "assistant", "already seen"),
                    ],
                )
            ],
            include_other=False,
        )
        migrated = self.successful_snapshot(
            backend="sqlite",
            sqlite_path=db_path,
            legacy_store=self.root / "missing-sessions.json",
        )
        self.assertEqual(migrated["count"], 0)
        self.assertEqual(migrated["data"], [])

        self.append_sqlite_event(
            db_path,
            session_id="migration-jsonl",
            seq=3,
            event=message_event("same-3", TS0, "user", "late equal timestamp"),
        ).close()
        appended = self.successful_snapshot(
            backend="sqlite",
            sqlite_path=db_path,
            legacy_store=self.root / "missing-sessions.json",
        )
        self.assertEqual(
            required_message_rows(appended), [(KEY, TS0, "user", "late equal timestamp")]
        )

    def test_sqlite_wal_reader_sees_committed_snapshot_while_writer_is_active(self) -> None:
        db_path = self.create_sqlite_store(wal=True)
        writer = sqlite3.connect(db_path, isolation_level=None, timeout=1)
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("BEGIN IMMEDIATE")
            writer.execute(
                "INSERT INTO transcript_events (session_id, seq, event_json, created_at) "
                "VALUES ('sqlite-current', 99, ?, 2099)",
                (json.dumps(message_event("uncommitted", TS1, "user", "not visible yet")),),
            )
            writer.execute(
                "INSERT INTO transcript_event_identities "
                "(session_id, event_id, seq, event_type, created_at) "
                "VALUES ('sqlite-current', 'uncommitted', 99, 'message', 2099)"
            )
            snapshot = self.successful_snapshot(
                backend="sqlite",
                sqlite_path=db_path,
                legacy_store=self.root / "missing-sessions.json",
            )
            self.assertNotIn("uncommitted", {row[3] for row in required_message_rows(snapshot)})
            self.assertEqual(snapshot["count"], 4)
            self.checkpoint.write_text(json.dumps(snapshot["checkpoint"]) + "\n", encoding="utf-8")
            writer.commit()
        finally:
            writer.close()

        after_commit = self.successful_snapshot(
            backend="sqlite",
            sqlite_path=db_path,
            legacy_store=self.root / "missing-sessions.json",
        )
        self.assertEqual(
            required_message_rows(after_commit), [(KEY, TS1, "user", "not visible yet")]
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
