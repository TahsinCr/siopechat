"""Thread-safe and asyncio-native in-process event buses."""

# Python 3.10 unittest discovery probes source directories as top-level
# packages. The supported package is <parent>.core.events, so avoid eager
# imports only for that probe.
if __package__ == "events":
    __all__: list[str] = []
else:
    from .async_event_bus import AsyncEventBus
    from .actions import (
        AsyncEventBusActions,
        EventBusActions,
        EventErrorContext,
        EventTimeoutContext,
    )
    from .base import BaseEventBus
    from .contracts import DeliveryMode, ErrorPolicy, EventBusStats, PublishResult
    from .errors import (
        CallbackTimeoutError,
        EventBusClosedError,
        EventBusError,
        EventShutdownTimeoutError,
        InvalidEventHandlerError,
    )
    from .engine import AsyncEventEngine, EventEngine
    from .event_bus import EventBus
    from .filtering import EventFilter
    from .history import EventHistory, MemoryEventHistory
    from .subscription import AsyncSubscription, Subscription
    from .callback import CallbackContext, CallbackSubscription

    __all__ = [
        "AsyncEventBus",
        "AsyncEventBusActions",
        "AsyncEventEngine",
        "AsyncSubscription",
        "BaseEventBus",
        "DeliveryMode",
        "ErrorPolicy",
        "EventBus",
        "EventBusActions",
        "EventBusClosedError",
        "EventBusError",
        "EventBusStats",
        "EventShutdownTimeoutError",
        "EventEngine",
        "EventFilter",
        "EventErrorContext",
        "EventHistory",
        "EventTimeoutContext",
        "InvalidEventHandlerError",
        "MemoryEventHistory",
        "PublishResult",
        "Subscription",
        "CallbackContext",
        "CallbackTimeoutError",
        "CallbackSubscription",
    ]
