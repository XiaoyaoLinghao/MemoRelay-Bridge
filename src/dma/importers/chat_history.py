"""Import Codex / Claude Code chat transcripts as derived history (T16).

Transcript files are imported as ``derived_import`` on host ``codex`` or
``claude_code``. They are historical exports, not the original native session
records, so KW marks authority ``legacy_unknown`` and requires explicit
correction before any fact is treated as current user intent.

The importer requires a recognisable schema (role + content/text per JSONL
record) and refuses an unknown required schema instead of guessing. System
prompts are not imported as facts; tool content is kept only when it is a
visible transcript message. Timestamps are optional but never replaced by
import time (missing => unknown precision).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
from pathlib import Path

from dma.adapters.base import NativeMessageAdapter
from dma.errors import SourceReadError
from dma.reader import NativeRecord
from dma.redaction import Redactor

SUPPORTED_HOSTS = {"codex", "claude_code"}
ROLE_MAP = {
    "user": "user_input",
    "human": "user_input",
    "assistant": "assistant_reply",
    "ai": "assistant_reply",
    "tool": "tool_execution",
}
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "\n".join(parts)
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content["text"]
    return ""


def _parse_time(value) -> dict:
    if value is None or value == "":
        return {"value": None, "precision": "unknown", "timezone": "UTC"}
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = value / 1000.0 if value > 1e11 else float(value)
        moment = _dt.datetime.fromtimestamp(seconds, tz=_dt.timezone.utc)
        return {
            "value": moment.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "precision": "instant",
            "timezone": "UTC",
        }
    text = str(value).strip()
    if _DATE.match(text):
        return {"value": text, "precision": "day", "timezone": "UTC"}
    if _ISO.match(text):
        try:
            moment = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=_dt.timezone.utc)
            return {
                "value": moment.isoformat(),
                "precision": "instant",
                "timezone": "UTC",
            }
        except ValueError:
            pass
    return {"value": None, "precision": "unknown", "timezone": "UTC"}


class ChatHistoryImporter:
    def __init__(
        self,
        *,
        host: str,
        workspace_id: str,
        instance_id: str,
        project_ids=(),
        redactor=None,
    ) -> None:
        if host not in SUPPORTED_HOSTS:
            raise ValueError(f"unsupported history host {host!r}")
        self.host = host
        self.project_ids = tuple(project_ids)
        self.redactor = redactor or Redactor()
        self.adapter = NativeMessageAdapter(
            self.redactor,
            workspace_id=workspace_id,
            instance_id=instance_id,
            host=host,
        )

    def import_path(self, path: str | Path, outbox, *, stream: str | None = None) -> dict:
        source = Path(path)
        if not source.is_file():
            raise SourceReadError(f"history transcript not found: {source}")
        records: list[NativeRecord] = []
        skipped_system = 0
        for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                raise SourceReadError(f"unparsable history record at line {line_number}") from None
            if not isinstance(item, dict):
                raise SourceReadError(f"unknown history schema at line {line_number}")
            if "schema_version" in item and str(item["schema_version"]) not in ("1", "1.0"):
                raise SourceReadError("unsupported history schema_version")
            role = str(item.get("role") or item.get("type") or "")
            content = item.get("content", item.get("text"))
            if role not in ROLE_MAP and role not in ("system", "developer"):
                raise SourceReadError(f"unknown history role/schema at line {line_number}")
            if role in ("system", "developer"):
                skipped_system += 1
                continue
            text = _text(content)
            if not text.strip():
                continue
            native_id = str(item.get("native_event_id") or item.get("id") or f"line:{line_number}")
            session_id = str(item.get("session_id") or item.get("conversation_id") or source.stem)
            timestamp = item.get("timestamp", item.get("time", item.get("created_at")))
            records.append(
                NativeRecord(
                    native_event_id=f"{self.host}:{session_id}:{native_id}",
                    text=text,
                    role=role if role in ("user", "assistant", "tool") else "user",
                    origin_kind="derived_import",
                    session_id=f"legacy:{session_id}",
                    actor_id=f"actor:{self.host}-history",
                    native_schema=f"{self.host}-history-export-1",
                    content_revision="native:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
                    locator=f"{self.host}-history://{source.name}#L{line_number}",
                    event_time=_parse_time(timestamp),
                    project_ids=self.project_ids,
                    aliases=(f"file:{source.name}", f"line:{line_number}"),
                )
            )
        events: list[dict] = []
        for record in records:
            events.extend(self.adapter.to_events(record))
        digest = "digest:" + hashlib.sha256(source.read_bytes()).hexdigest()[:32]
        stored = outbox.capture(
            events,
            stream=stream or f"import:{self.host}-history",
            position=digest,
        )
        return {
            "host": self.host,
            "records": len(records),
            "imported_events": stored["inserted"],
            "duplicates": stored["duplicates"],
            "skipped_system": skipped_system,
            "position": digest,
            "origin_kind": "derived_import",
        }
