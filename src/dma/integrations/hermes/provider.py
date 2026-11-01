"""Hermes memory provider plugin (T11): current-turn recall + durable capture.

Implements the duck-typed ``agent.memory_provider.MemoryProvider`` surface from
the installed Hermes snapshot (no host import, no host files touched):

* ``prefetch(query, session_id)`` recalls **for the current query** before the
  model request, bounded by the plugin deadline; a previous query's result is
  never reused when the backend fails (RECALL-01);
* output is a provenance-labelled quoted-evidence block. It never frames the
  memory as identity or authoritative instructions, and items that try to
  become system/identity instructions are dropped (the host's own
  ``<memory-context>`` wrapper adds an "authoritative reference" note — a host
  packaging risk tracked for T19/review, not something this plugin can remove);
* ``sync_turn`` only *triggers a native read-only rescan*; the host-supplied
  strings are not trusted as evidence and never replace native event ids;
* ``on_pre_compress`` persists a durable outbox checkpoint before returning and
  is fail-closed (API v2) when a durable checkpoint cannot be produced;
* current-turn operational notices distinguish unavailable, empty and incomplete
  recall; raw backend diagnostics are never interpolated into those notices;
* no model call is made here.
"""

from __future__ import annotations

import time
import json
import uuid
from dataclasses import dataclass

from dma.adapters.hermes.reader import checkpoint_before_compression
from dma.errors import DmaError
from dma.integrations.formatting import format_recall_context, format_recall_notice, is_trivial_prompt
from dma.integrations.recall_client import RecallUnavailable
from dma.pipeline import capture

PRE_COMPRESS_CHECKPOINT_API_VERSION = 2


@dataclass(frozen=True)
class RecallStatus:
    """Mirror of the host's RecallStatus shape (duck-typed, no host import)."""

    provider_label: str
    count: int
    glyph: str = "🧠"


class DmaMemoryProvider:
    name = "dma-memory-v2"
    pre_compress_checkpoint_api_version = PRE_COMPRESS_CHECKPOINT_API_VERSION

    def __init__(
        self,
        *,
        recall_client,
        client_id: str,
        reader=None,
        adapter=None,
        outbox=None,
        project_ids=(),
        deadline_ms: int = 6500,
        budget_tokens: int = 2000,
        limit: int = 8,
        stream: str | None = None,
        enable_search_tool: bool = False,
        query_project_aliases: dict | None = None,
        allowed_project_ids: tuple | None = None,
        allow_workspace_search: bool = False,
    ) -> None:
        self.recall_client = recall_client
        self.client_id = client_id
        self.reader = reader
        self.adapter = adapter
        self.outbox = outbox
        self.project_ids = tuple(project_ids)
        self.deadline_ms = int(deadline_ms)
        self.budget_tokens = int(budget_tokens)
        self.limit = int(limit)
        self.stream = stream
        self.enable_search_tool = bool(enable_search_tool)
        from dma.integrations.scopes import QueryScopes
        self.query_scopes=QueryScopes(self.project_ids,aliases=query_project_aliases,allowed=allowed_project_ids,allow_workspace=allow_workspace_search)
        self._session_id = ""
        self._agent_context = "primary"
        self._last_status: RecallStatus | None = None
        self.last_error_code: str | None = None

    # -- core lifecycle -----------------------------------------------------
    def is_available(self) -> bool:
        return self.recall_client is not None

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id or ""
        self._agent_context = str(kwargs.get("agent_context") or "primary")
        self._last_status = None

    def get_tool_schemas(self) -> list[dict]:
        from copy import deepcopy
        from dma.integrations.search import TOOL_SCHEMA
        if not self.enable_search_tool:
            return []
        schema=deepcopy(TOOL_SCHEMA)
        if self.query_scopes.allowed:
            schema["parameters"]["properties"]["project_ids"]["items"]["enum"]=sorted(self.query_scopes.allowed)
        if not self.query_scopes.allow_workspace:
            schema["parameters"]["properties"]["intent"]["enum"].remove("all_projects")
        return [schema]

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        from dma.integrations.search import ReadOnlySearch, TOOL_NAME
        if not self.enable_search_tool or tool_name != TOOL_NAME:
            return json.dumps({"ok": False, "error": {"code": "UNKNOWN_TOOL"}})
        result = ReadOnlySearch(self.recall_client, project_ids=self.project_ids, deadline_ms=self.deadline_ms, scopes=self.query_scopes).call(
            args, session_id=kwargs.get("session_id") or self._session_id,
            turn_id=kwargs.get("turn_id") or "turn:active-search")
        return json.dumps(result, ensure_ascii=False)

    def system_prompt_block(self) -> str:
        return ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self.recall_client or is_trivial_prompt(query):
            self._last_status = None
            self.last_error_code = None
            return ""
        resolved_session = session_id or self._session_id or "session:unknown"
        request_id = "req:" + uuid.uuid4().hex[:24]
        turn_id = f"turn:prefetch:{int(time.time() * 1000)}"
        try:
            result = self.recall_client.recall(
                query=query,
                session_id=resolved_session,
                turn_id=turn_id,
                request_id=request_id,
                project_ids=self.query_scopes.resolve(query),
                deadline_ms=self.deadline_ms,
                budget_tokens=self.budget_tokens,
                limit=self.limit,
            )
        except RecallUnavailable as exc:
            self._last_status = None
            self.last_error_code = exc.code
            return format_recall_notice(unavailable=True)
        self.last_error_code = None
        text, dropped = format_recall_context(result, max_items=self.limit)
        notice = format_recall_notice(result)
        if notice:
            text = notice + ("\n" + text if text else "")
        if not text:
            self._last_status = RecallStatus(provider_label="unified-memory", count=0)
            return ""
        item_count = len([line for line in text.splitlines() if line.startswith("- ")])
        self._last_status = RecallStatus(provider_label="unified-memory", count=item_count)
        return text

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        # Current-turn recall is synchronous in prefetch(); no next-turn cache is
        # produced because a previous query/cache must never substitute for the
        # current one (RECALL-01).
        return None

    def recall_status(self) -> RecallStatus | None:
        return self._last_status

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages=None,
        turn_author=None,
    ) -> None:
        """Trigger a native rescan; host strings are not evidence (native ids only)."""
        if self._agent_context not in ("primary", ""):
            return
        self._capture()

    # -- Hermes optional hooks ---------------------------------------------
    def on_pre_compress(self, messages, *, require_checkpoint: bool = False) -> str:
        if self.adapter is None or self.reader is None or self.outbox is None:
            if require_checkpoint:
                raise RuntimeError("durable memory checkpoint is not configured")
            return ""
        try:
            checkpoint_before_compression(
                self.reader, self.adapter, self.outbox, stream=self.stream
            )
        except DmaError as exc:
            self.last_error_code = getattr(exc, "code", "STORE_UNAVAILABLE")
            if require_checkpoint:
                raise
            return ""
        return ""

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        return None

    def on_session_end(self, messages) -> None:
        # Session switches must not strand a native completed/failed turn.
        self._capture()

    def on_delegation(self, task: str, result: str, **kwargs) -> None:
        # These host strings are observations, not native source records.
        return None

    def on_memory_write(self, action: str, target: str, content: str, metadata=None) -> None:
        # Do not mirror an old backend or create a second authoritative store.
        return None

    def on_session_switch(self, new_session_id: str, *, parent_session_id: str = "", reset: bool = False, rewound: bool = False, **kwargs) -> None:
        self._session_id = new_session_id or ""
        self._last_status = None

    def identity_signature(self) -> dict:
        return {
            "dma:client": self.client_id,
            "dma:instance": getattr(self.reader, "instance_id", None) or "",
        }

    def backup_paths(self) -> list[str]:
        return [str(self.outbox.db_path)] if self.outbox is not None else []

    def shutdown(self) -> None:
        self._last_status = None

    # -- internals ----------------------------------------------------------
    def _capture(self) -> dict | None:
        if self.adapter is None or self.reader is None or self.outbox is None:
            return None
        try:
            return capture(
                self.reader,
                self.outbox,
                stream=self.stream or f"hermes:{getattr(self.reader, 'profile', 'main')}",
                convert=self.adapter.to_events,
            )
        except (DmaError, OSError) as exc:
            self.last_error_code = getattr(exc, "code", "STORE_UNAVAILABLE")
            return None
