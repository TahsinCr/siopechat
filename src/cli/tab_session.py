"""Private, temporary persistence for the terminal tab session."""

import json
from pathlib import Path

from utils.files import atomic_json_write

_VERSION = 1
_TAB_KINDS = frozenset({"chat", "project", "settings", "downloads"})


class TabSessionStore:
    """Persist tab references and drafts without duplicating conversation history."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict | None:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if (
            not isinstance(value, dict)
            or value.get("version") != _VERSION
            or not isinstance(value.get("tabs"), list)
            or type(value.get("active_tab")) is not int
        ):
            return None

        tabs = [tab for raw in value["tabs"] if (tab := self._tab_record(raw)) is not None]
        active_tab = min(max(value["active_tab"], 0), max(len(tabs) - 1, 0))
        layout = value.get("layout")
        return {
            "tabs": tabs,
            "active_tab": active_tab,
            "layout": layout if isinstance(layout, dict) else None,
        }

    def save(self, tabs: list[dict], active_tab: int, layout: dict | None = None) -> None:
        saved = [
            (index, descriptor)
            for index, tab in enumerate(tabs)
            if (descriptor := self._tab_record(tab, exclude_temporary=True)) is not None
        ]
        if saved:
            active_index = min(
                range(len(saved)),
                key=lambda index: (
                    abs(saved[index][0] - active_tab),
                    saved[index][0] > active_tab,
                ),
            )
        else:
            active_index = 0
        atomic_json_write(
            self.path,
            {
                "version": _VERSION,
                "tabs": [descriptor for _, descriptor in saved],
                "active_tab": active_index,
                "layout": layout,
            },
            separators=(",", ":"),
            allow_nan=False,
            prefix=".session-",
            directory_mode=0o700,
        )

    @staticmethod
    def _tab_record(value: object, *, exclude_temporary: bool = False) -> dict | None:
        if not isinstance(value, dict):
            return None
        kind = value.get("kind")
        account_key = value.get("account_key")
        tab_id = value.get("tab_id")
        conversation_id = value.get("conversation_id")
        effort = value.get("effort")
        if (
            not isinstance(kind, str)
            or kind not in _TAB_KINDS
            or (exclude_temporary and value.get("temporary"))
            or not isinstance(account_key, str)
            or not isinstance(tab_id, str)
            or not tab_id
            or (conversation_id is not None and not isinstance(conversation_id, str))
            or (effort is not None and not isinstance(effort, str))
        ):
            return None
        project = value.get("project")
        project = project if isinstance(project, dict) else {}
        return {
            "kind": kind,
            "account_key": account_key,
            "tab_id": tab_id,
            "conversation_id": conversation_id,
            "title": value.get("title") if isinstance(value.get("title"), str) else "",
            "model": value.get("model") if isinstance(value.get("model"), str) else "",
            "effort": effort,
            "effort_preference": (
                value.get("effort_preference")
                if isinstance(value.get("effort_preference"), str)
                else ""
            ),
            "draft": value.get("draft") if isinstance(value.get("draft"), str) else "",
            "project": {
                key: project[key] for key in ("id", "name") if isinstance(project.get(key), str)
            },
        }
