"""Composable type and predicate filters for event subscriptions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar


T = TypeVar("T")
EventType = type[Any] | tuple[type[Any], ...]


@dataclass(frozen=True)
class EventFilter(Generic[T]):
    event_type: EventType | None = None
    predicate: Callable[[T], bool] | None = None

    def __post_init__(self) -> None:
        event_types = self.event_type
        if event_types is not None:
            values = event_types if isinstance(event_types, tuple) else (event_types,)
            if not values or any(not isinstance(value, type) for value in values):
                raise TypeError("Event filter types must contain one or more classes")
        if self.predicate is not None and not callable(self.predicate):
            raise TypeError("Event filter predicate must be callable")

    def matches(self, event: T) -> bool:
        if self.event_type is not None and not isinstance(event, self.event_type):
            return False
        return self.predicate(event) if self.predicate is not None else True


def coerce_event_filter(
    event_filter: EventFilter[T] | None = None,
    *,
    event_type: EventType | None = None,
    predicate: Callable[[T], bool] | None = None,
) -> EventFilter[T]:
    if event_filter is not None and (event_type is not None or predicate is not None):
        raise ValueError("Use event_filter or event_type/predicate, not both")
    return event_filter or EventFilter(event_type=event_type, predicate=predicate)
