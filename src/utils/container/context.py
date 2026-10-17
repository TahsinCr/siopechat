"""Default and context-local dependency-container selection."""

from __future__ import annotations

import contextvars
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .container import DependencyContainer


_default_container: DependencyContainer | None = None
_default_container_lock = threading.Lock()
_current_container: contextvars.ContextVar[DependencyContainer | None] = (
    contextvars.ContextVar("current_dependency_container", default=None)
)


def enter_container(
    container: DependencyContainer,
) -> contextvars.Token[DependencyContainer | None]:
    return _current_container.set(container)


def exit_container(token: contextvars.Token[DependencyContainer | None]) -> None:
    _current_container.reset(token)


def set_default_container(container: DependencyContainer) -> None:
    global _default_container
    with _default_container_lock:
        _default_container = container


def get_default_container() -> DependencyContainer:
    global _default_container
    if _default_container is None:
        with _default_container_lock:
            if _default_container is None:
                from .container import DependencyContainer

                _default_container = DependencyContainer()
    return _default_container


def get_current_container() -> DependencyContainer:
    return _current_container.get() or get_default_container()
