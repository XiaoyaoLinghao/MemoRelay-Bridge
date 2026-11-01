"""Hermes adapter identity/project configuration."""

from __future__ import annotations

from dma.adapters.base import NativeMessageAdapter


class HermesAdapter(NativeMessageAdapter):
    def __init__(self, redactor, *, workspace_id: str, instance_id: str) -> None:
        super().__init__(redactor, workspace_id=workspace_id, instance_id=instance_id, host="hermes")
