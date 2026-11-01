"""DMA 2.0 adapter core (Python, standard library only).

The adapter layer owns host differences, read-only capture, sanitization,
reliable outbox delivery and recall delivery. It does **not** perform semantic
fact extraction: KW is the single semantic center.
"""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("memorelay-bridge")
except PackageNotFoundError:
    try:
        __version__ = _pkg_version("daily-memory-archiver-core")
    except PackageNotFoundError:
        __version__ = "0.0.0+unknown"
