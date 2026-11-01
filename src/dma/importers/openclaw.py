"""OpenClaw read-only memory compatibility (T16).

OpenClaw keeps workspace memory as daily markdown (the same v1 DMA output shape
KW 1.x consumed). The reader is read-only and keeps synthetic contract tests so
the OpenClaw boundary can be verified without installing OpenClaw this round.
Native/derived distinction is preserved: these files are derived history.
"""

from __future__ import annotations

from pathlib import Path

from dma.errors import SourceReadError
from dma.importers.daily_memory import DailyMemoryImporter
from dma.reader import NativeRecord
from dma.redaction import Redactor


class OpenClawMemoryReader:
    def __init__(
        self,
        memory_dir: str | Path,
        *,
        workspace_id: str,
        instance_id: str = "instance:openclaw-import",
        timezone: str = "Asia/Shanghai",
        default_project_id: str | None = None,
        redactor=None,
    ) -> None:
        self.memory_dir = Path(memory_dir)
        self.importer = DailyMemoryImporter(
            workspace_id=workspace_id,
            instance_id=instance_id,
            timezone=timezone,
            default_project_id=default_project_id,
            redactor=redactor or Redactor(),
        )

    def read_since(self, stream: str, position: str | None):
        if not self.memory_dir.is_dir():
            raise SourceReadError(f"OpenClaw memory directory not found: {self.memory_dir}")
        pairs: list[tuple[NativeRecord, str]] = []
        files = sorted(self.memory_dir.glob("*.md"))
        final_position = position
        for file in files:
            parsed = self.importer.parse_file(file)
            for index, record in enumerate(parsed.records, start=1):
                pairs.append((record, f"{file.name}:{index}"))
            final_position = f"files:{len(files)}:{file.name}"
        return pairs, final_position or "files:0"

    def import_path(self, outbox, *, stream: str | None = None) -> dict:
        report = self.importer.import_path(
            self.memory_dir, outbox, stream=stream or "import:openclaw-memory"
        )
        return {**report, "boundary": "openclaw-read-only-derived"}
