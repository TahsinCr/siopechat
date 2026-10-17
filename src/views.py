"""Extension-facing view registry and view-owned tab state."""

from dataclasses import dataclass
from typing import Any, Callable

from utils.events import EventBus


@dataclass(frozen=True)
class ViewChanged:
    id: str
    state: str


@dataclass(frozen=True)
class View:
    id: str
    title: str
    factory: Callable[[], Any]


class Views:
    def __init__(self, events: EventBus | None = None):
        self.events = events
        self._views: dict[str, tuple[str, View]] = {}
        self._tabs: dict[str, Any] = {}
        self.active: str | None = None

    def register(self, owner: str, view: View) -> None:
        if not view.id or view.id in self._views:
            raise ValueError("view ID must be unique")
        self._views[view.id] = (owner, view)

    def open(self, view_id: str) -> Any:
        if view_id not in self._tabs:
            self._tabs[view_id] = self._views[view_id][1].factory()
        self.active = view_id
        if self.events is not None:
            self.events.publish(ViewChanged(view_id, "opened"))
        return self._tabs[view_id]

    def close_tab(self, view_id: str) -> None:
        existed = view_id in self._tabs
        self._tabs.pop(view_id, None)
        if self.active == view_id:
            self.active = next(reversed(self._tabs), None)
        if existed and self.events is not None:
            self.events.publish(ViewChanged(view_id, "closed"))

    def unregister(self, owner: str) -> None:
        for key in [key for key, value in self._views.items() if value[0] == owner]:
            self.close_tab(key)
            del self._views[key]

    def tabs(self) -> tuple[str, ...]:
        return tuple(self._tabs)

    def close(self) -> None:
        for view_id in tuple(self._tabs):
            self.close_tab(view_id)
        self._views.clear()
        self.active = None
