"""Dependency tokens, lifetimes, markers, and provider registrations."""

from __future__ import annotations

import asyncio
import contextvars
import threading
from collections.abc import Callable, Hashable, Mapping
from dataclasses import dataclass, field
from typing import Any, ParamSpec, TypeAlias, TypeVar

from ..compatibility import StrEnum


T = TypeVar("T")
P = ParamSpec("P")
R = TypeVar("R")
Token: TypeAlias = Hashable | type[Any]
ProviderCallable: TypeAlias = Callable[..., Any]
TypedProvider: TypeAlias = type[T] | Callable[..., T]
DependencyMap: TypeAlias = Mapping[str, Token]

DEFAULT_PRIORITY = 100
NONE_TYPE = type(None)

_WAIT_GRAPH_LOCK = threading.Lock()
_RESOLUTION_OWNER: contextvars.ContextVar[object | None] = contextvars.ContextVar(
    "dependency_resolution_owner", default=None
)
_OWNER_WAITS_FOR: dict[object, object] = {}


def begin_resolution_owner() -> contextvars.Token[object | None] | None:
    """Create one logical owner shared by nested sync/async resolution work."""

    current = _RESOLUTION_OWNER.get()
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    if task is not None:
        return None if current is task else _RESOLUTION_OWNER.set(task)
    # asyncio.to_thread() copies the task owner into its worker context.
    if current is not None:
        return None
    return _RESOLUTION_OWNER.set(object())


def end_resolution_owner(
    token: contextvars.Token[object | None] | None,
) -> None:
    if token is not None:
        _RESOLUTION_OWNER.reset(token)


def current_resolution_owner() -> object:
    owner = _RESOLUTION_OWNER.get()
    return owner if owner is not None else ("thread", threading.get_ident())


class _Missing:
    __slots__ = ()

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "MISSING"


MISSING = _Missing()


class InitializationRetiredError(RuntimeError):
    """Raised when a registration is removed during cached resolution."""


class InitializationGate:
    """Coordinate one cached value across sync threads and async tasks."""

    __slots__ = (
        "_condition",
        "_initializing",
        "_owner_async",
        "_owner",
        "_retired",
    )

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._initializing = False
        self._owner_async = False
        self._owner: object | None = None
        self._retired = False

    def claim(self, *, asynchronous: bool = False) -> bool:
        with self._condition:
            if self._retired:
                raise InitializationRetiredError("Dependency registration is retired")
            if self._initializing:
                return False
            self._initializing = True
            self._owner_async = asynchronous
            self._owner = current_resolution_owner()
            return True

    def wait(self) -> None:
        waiter = current_resolution_owner()
        with self._condition:
            if (
                self._initializing
                and self._owner_async
                and self._owner == waiter
            ):
                raise InitializationRetiredError(
                    "Synchronous resolution cannot wait for an async provider "
                    "on its owning event-loop thread"
                )
            owner = self._owner
            if owner is not None:
                with _WAIT_GRAPH_LOCK:
                    current: object | None = owner
                    while current is not None:
                        if current == waiter:
                            raise InitializationRetiredError(
                                "Cross-context dependency initialization cycle detected"
                            )
                        current = _OWNER_WAITS_FOR.get(current)
                    _OWNER_WAITS_FOR[waiter] = owner
            try:
                self._condition.wait_for(lambda: not self._initializing)
                if self._retired:
                    raise InitializationRetiredError("Dependency registration is retired")
            finally:
                with _WAIT_GRAPH_LOCK:
                    _OWNER_WAITS_FOR.pop(waiter, None)

    async def wait_async(self) -> None:
        await asyncio.to_thread(self.wait)

    def release(self) -> None:
        with self._condition:
            self._initializing = False
            self._owner_async = False
            self._owner = None
            self._condition.notify_all()

    def retire(self) -> None:
        with self._condition:
            self._retired = True
            self._condition.wait_for(lambda: not self._initializing)

    async def retire_async(self) -> None:
        self.begin_retire()
        await asyncio.to_thread(self._wait_until_idle)

    def begin_retire(self) -> None:
        with self._condition:
            self._retired = True

    def reactivate(self) -> None:
        with self._condition:
            self._retired = False
            self._condition.notify_all()

    def _wait_until_idle(self) -> None:
        with self._condition:
            self._condition.wait_for(lambda: not self._initializing)


class Lifetime(StrEnum):
    TRANSIENT = "transient"
    SINGLETON = "singleton"
    SCOPED = "scoped"


@dataclass(frozen=True, slots=True)
class Inject:
    """Explicitly mark a parameter for dependency injection."""

    token: Any = MISSING
    optional: bool = False


@dataclass(slots=True)
class Provider:
    token: Token
    factory: Any
    lifetime: Lifetime
    dependencies: dict[str, Token] = field(default_factory=dict)
    priority: int = DEFAULT_PRIORITY
    instance: Any = MISSING
    singleton: Any = MISSING
    initialization: InitializationGate = field(default_factory=InitializationGate)

    @property
    def has_instance(self) -> bool:
        return self.instance is not MISSING

@dataclass(frozen=True, slots=True)
class Registration:
    token: Token
    factory: Any
    lifetime: Lifetime
    dependencies: dict[str, Token]
    priority: int
    instance: Any = MISSING
