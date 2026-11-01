"""Import v1 DMA daily-memory markdown as **derived** history (T16).

The v1 daily files are summaries/projections produced by the old DMA, not the
original user sessions. They are imported with origin ``derived_import`` so KW
never treats them as raw user evidence: authority becomes ``legacy_unknown`` and
the original native sessions remain the only source of truth.

Preserved/limited provenance:

* locator keeps file + line + section; aliases keep the source path;
* a line with ``HH:MM`` gets an instant business time in the configured time
  zone (the date comes from the file frontmatter); a line without a time keeps
  **day** precision; no date means unknown time (import time never substitutes);
* lines that cannot be parsed are counted and reported, never silently coerced;
* project mapping is explicit (date/file/default); unmatched files are reported
  as unresolved rather than guessed;
* re-import is idempotent via stable native ids + event ids;
* sanitization happens before the outbox (same redact:1 path).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from dma.adapters.base import NativeMessageAdapter
from dma.errors import SourceReadError
from dma.reader import NativeRecord
from dma.redaction import Redactor

SECTION_HEADINGS = {
    "核心要点": "fact",
    "决策与结论": "decision",
    "已完成事项": "task",
    "待办与计划": "task",
    "用户偏好与习惯": "preference",
    "技术/项目要点": "tech",
    "风险与注意事项": "risk",
    "创意与想法": "idea",
}
_FRONTMATTER_DATE = re.compile(r"^date:\s*[\"']?(?P<date>\d{4}-\d{2}-\d{2})")
_ITEM = re.compile(r"^-\s+(?P<body>.+)$")
_TIME_ITEM = re.compile(r"^(?P<hh>\d{1,2}):(?P<mm>\d{2})\s*-\s*(?P<text>.*)$")
_FILE_DATE = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})")


@dataclass
class DailyParseResult:
    records: list[NativeRecord] = field(default_factory=list)
    skipped_lines: int = 0
    unresolved_files: list[str] = field(default_factory=list)
    section_counts: dict[str, int] = field(default_factory=dict)


class DailyMemoryImporter:
    def __init__(
        self,
        *,
        workspace_id: str,
        instance_id: str = "instance:legacy-import",
        timezone: str = "Asia/Shanghai",
        project_map: dict[str, str] | None = None,
        default_project_id: str | None = None,
        redactor=None,
    ) -> None:
        self.workspace_id = workspace_id
        self.instance_id = instance_id
        self.timezone = ZoneInfo(timezone)
        self.project_map = dict(project_map or {})
        self.default_project_id = default_project_id
        self.redactor = redactor or Redactor()
        self.adapter = NativeMessageAdapter(
            self.redactor,
            workspace_id=workspace_id,
            instance_id=instance_id,
            host="legacy",
        )

    def parse_file(self, path: Path) -> DailyParseResult:
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        result = DailyParseResult()
        date_value: str | None = None
        section: str | None = None
        frontmatter = False
        for index, line in enumerate(lines, start=1):
            stripped = line.strip()
            if index == 1 and stripped == "---":
                frontmatter = True
                continue
            if frontmatter:
                if stripped == "---":
                    frontmatter = False
                    continue
                match = _FRONTMATTER_DATE.match(stripped)
                if match:
                    date_value = match.group("date")
                continue
            if stripped.startswith("## "):
                section = stripped[3:].strip()
                result.section_counts[section] = result.section_counts.get(section, 0) + 1
                continue
            if not stripped:
                continue
            item = _ITEM.match(stripped)
            if item is None:
                result.skipped_lines += 1
                continue
            body = item.group("body").strip()
            event_time, content = self._split_time(date_value, body)
            if not content.strip():
                result.skipped_lines += 1
                continue
            project_ids: tuple[str, ...] = ()
            project = self._project_for(path, date_value)
            if project:
                project_ids = (project,)
            else:
                result.unresolved_files.append(path.name)
            alias_bits = [f"file:{path.name}", f"line:{index}"]
            if section:
                # Aliases must be contract tokens; keep a stable hash of the
                # (non-ASCII) section heading instead of the raw title.
                alias_bits.append(
                    "section:" + hashlib.sha256(section.encode("utf-8")).hexdigest()[:8]
                )
            record = NativeRecord(
                native_event_id=f"daily:{date_value or path.stem}:L{index}",
                text=content,
                role="user",
                origin_kind="derived_import",
                session_id=f"legacy:{date_value or path.stem}",
                actor_id="actor:legacy-dma",
                native_schema="dma-daily-v1",
                content_revision="native:" + hashlib.sha256(content.encode("utf-8")).hexdigest()[:16],
                locator=f"legacy-daily://{path.name}#L{index}",
                event_time=event_time,
                project_ids=project_ids,
                aliases=tuple(alias_bits),
            )
            result.records.append(record)
        return result

    def _project_for(self, path: Path, date_value: str | None) -> str | None:
        if path.name in self.project_map:
            return self.project_map[path.name]
        if date_value and date_value in self.project_map:
            return self.project_map[date_value]
        return self.default_project_id

    def _split_time(self, date_value: str | None, body: str):
        match = _TIME_ITEM.match(body)
        if match and date_value:
            try:
                moment = _dt.datetime(
                    int(date_value[0:4]),
                    int(date_value[5:7]),
                    int(date_value[8:10]),
                    int(match.group("hh")),
                    int(match.group("mm")),
                    tzinfo=self.timezone,
                )
                return (
                    {
                        "value": moment.isoformat(),
                        "precision": "instant",
                        "timezone": str(self.timezone),
                    },
                    body,
                )
            except ValueError:
                pass
        if date_value:
            return (
                {"value": date_value, "precision": "day", "timezone": str(self.timezone)},
                body,
            )
        return ({"value": None, "precision": "unknown", "timezone": str(self.timezone)}, body)

    def import_path(self, path: str | Path, outbox, *, stream: str | None = None) -> dict:
        root = Path(path)
        if root.is_file():
            files = [root]
        elif root.is_dir():
            files = sorted(root.glob("*.md"))
        else:
            raise SourceReadError(f"daily memory source not found: {root}")
        if not files:
            raise SourceReadError(f"no daily memory files found: {root}")
        events: list[dict] = []
        totals = DailyParseResult()
        for file in files:
            parsed = self.parse_file(file)
            totals.records.extend(parsed.records)
            totals.skipped_lines += parsed.skipped_lines
            totals.unresolved_files.extend(parsed.unresolved_files)
        for record in totals.records:
            events.extend(self.adapter.to_events(record))
        digest = hashlib.sha256()
        for file in files:
            digest.update(file.name.encode("utf-8"))
            digest.update(hashlib.sha256(file.read_bytes()).digest())
        position = "digest:" + digest.hexdigest()[:32]
        stored = outbox.capture(events, stream=stream or "import:daily-memory", position=position)
        return {
            "files": len(files),
            "records": len(totals.records),
            "imported_events": stored["inserted"],
            "duplicates": stored["duplicates"],
            "skipped_lines": totals.skipped_lines,
            "unresolved_projects": sorted(set(totals.unresolved_files)),
            "position": position,
            "origin_kind": "derived_import",
            "source_kind": "dma-daily-v1-derived-summary",
        }
