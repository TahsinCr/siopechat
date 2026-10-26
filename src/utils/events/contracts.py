"""Small shared contracts used by synchronous and asynchronous event buses."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ErrorPolicy(StrEnum):
    """Define how subscriber failures are reported by publish operations."""

    ISOLATE = "isolate"
    RAISE = "raise"


class DeliveryMode(StrEnum):
    """Define ordering for asynchronous subscriber delivery."""

    SEQUENTIAL = "sequential"
    CONCURRENT = "concurrent"


@dataclass(frozen=True, slots=True)
class PublishResult:
    matched: int = 0
    delivered: int = 0
    failed: int = 0
    errors: tuple[Exception, ...] = ()

    @property
    def successful(self) -> bool:
        return self.failed == 0


@dataclass(frozen=True, slots=True)
class EventBusStats:
    published: int = 0
    delivered: int = 0
    failed: int = 0
