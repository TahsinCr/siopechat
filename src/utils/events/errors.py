"""Errors raised by the in-process event buses."""


class EventBusError(RuntimeError):
    """Base error for event bus operations."""


class EventBusClosedError(EventBusError):
    """Raised when an operation requires an open event bus."""


class InvalidEventHandlerError(EventBusError, TypeError):
    """Raised when a handler is not compatible with the selected bus."""


class CallbackTimeoutError(EventBusError, TimeoutError):
    """Raised when a callback exceeds its configured execution budget."""


class EventShutdownTimeoutError(EventBusError, TimeoutError):
    """Raised when a synchronous schedule cannot stop within its budget."""
