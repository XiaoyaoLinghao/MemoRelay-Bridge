"""DMA adapter error types; no secret value is ever embedded in messages."""


class DmaError(Exception):
    """Base class for adapter errors."""


class InvalidEventError(DmaError):
    """A to-be-captured event violates the frozen contract (field path only)."""


class SourceReadError(DmaError):
    """A read-only host source could not be read or parsed."""


class DeliveryError(DmaError):
    """A delivery attempt failed before a durable receipt was obtained."""

    def __init__(self, code: str, message: str = "", retryable: bool = True) -> None:
        super().__init__(message or code)
        self.code = code
        self.retryable = retryable
