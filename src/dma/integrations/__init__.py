"""Host integration surfaces: recall delivery plugins and shared formatting."""

from .formatting import format_recall_context, is_trivial_prompt
from .recall_client import RecallClient, RecallUnavailable

__all__ = ["RecallClient", "RecallUnavailable", "format_recall_context", "is_trivial_prompt"]
