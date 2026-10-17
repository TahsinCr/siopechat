"""One action dispatch path for command palette, keyboard and extension calls."""

import inspect
from dataclasses import dataclass
from typing import Any, Callable

from utils.events import EventBus


@dataclass(frozen=True)
class Action:
    id: str
    title: str
    callback: Callable[..., Any]
    keybinding: str | None = None
    available: Callable[[], bool] = lambda: True


@dataclass(frozen=True)
class ActionChanged:
    id: str
    state: str


class ActionService:
    def __init__(self, events: EventBus | None = None):
        self.events = events
        self._actions: dict[str, tuple[str, Action]] = {}

    def register(self, owner: str, action: Action) -> None:
        if not action.id or action.id in self._actions:
            raise ValueError("action ID must be unique")
        self._actions[action.id] = (owner, action)

    def unregister(self, owner: str) -> None:
        self._actions = {key: value for key, value in self._actions.items() if value[0] != owner}

    @staticmethod
    def _available(action: Action) -> bool:
        try:
            return bool(action.available())
        except Exception:
            return False

    def search(self, query: str = "") -> tuple[Action, ...]:
        query = query.casefold()
        return tuple(
            action
            for _, action in self._actions.values()
            if query in f"{action.id} {action.title}".casefold() and self._available(action)
        )

    async def invoke(self, action_id: str, **arguments: Any) -> Any:
        action = self._actions[action_id][1]
        if not self._available(action):
            raise ValueError("action is currently unavailable")
        self._emit(action_id, "running")
        try:
            result = action.callback(**arguments)
            result = await result if inspect.isawaitable(result) else result
        except BaseException:
            self._emit(action_id, "failed")
            raise
        self._emit(action_id, "completed")
        return result

    def _emit(self, action_id: str, state: str) -> None:
        if self.events is not None and not self.events.closed:
            self.events.publish(ActionChanged(action_id, state))

    def keymap(self, overrides: dict[str, str] | None = None) -> dict[str, str]:
        overrides = overrides or {}
        result = {}
        for _, action in self._actions.values():
            key = overrides.get(action.id, action.keybinding)
            if key:
                if key in result:
                    raise ValueError(f"keybinding collision: {key}")
                result[key] = action.id
        return result
