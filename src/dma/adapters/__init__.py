"""Host adapters: read-only capture into contract-shaped source events."""

from .base import NativeMessageAdapter
from .hermes.reader import HermesSqliteReader, checkpoint_before_compression
from .dsh.reader import DshSessionReader

__all__ = [
    "DshSessionReader",
    "HermesSqliteReader",
    "NativeMessageAdapter",
    "checkpoint_before_compression",
]
