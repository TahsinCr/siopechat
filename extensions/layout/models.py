"""Saved layout metadata. Saving a layout never starts or replays a chat job."""

from dataclasses import dataclass
from typing import Protocol


def _text(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")


@dataclass(frozen=True)
class Tab:
    id: str
    title: str
    kind: str = "chat"
    provider_id: str | None = None
    account_id: str | None = None
    chat_id: str | None = None
    conversation_id: str | None = None


@dataclass(frozen=True)
class TabGroup:
    id: str
    tabs: tuple[Tab, ...] = ()
    active_tab_id: str | None = None
    layout: str = "tabs"


@dataclass(frozen=True)
class Workspace:
    id: str
    name: str
    groups: tuple[TabGroup, ...] = ()
    active_group_id: str | None = None

    def validate(self) -> None:
        _text(self.id, "workspace ID")
        _text(self.name, "workspace name")
        if not isinstance(self.groups, tuple) or not all(
            isinstance(group, TabGroup) for group in self.groups
        ):
            raise ValueError("groups must be a tuple of TabGroup values")
        for group in self.groups:
            _text(group.id, "group ID")
        groups = [group.id for group in self.groups]
        if len(set(groups)) != len(groups):
            raise ValueError("group IDs must be unique")
        if self.active_group_id is not None and self.active_group_id not in groups:
            raise ValueError("active group does not exist")
        tab_ids: set[str] = set()
        for group in self.groups:
            if not isinstance(group.tabs, tuple) or not all(
                isinstance(tab, Tab) for tab in group.tabs
            ):
                raise ValueError("tabs must be a tuple of Tab values")
            if group.layout not in ("tabs", "columns", "rows"):
                raise ValueError("unsupported group layout")
            for tab in group.tabs:
                _text(tab.id, "tab ID")
                _text(tab.title, "tab title")
                _text(tab.kind, "tab kind")
                if tab.id in tab_ids:
                    raise ValueError("tab IDs must be globally unique")
                for label, value in (
                    ("provider ID", tab.provider_id),
                    ("account ID", tab.account_id),
                    ("chat ID", tab.chat_id),
                    ("remote conversation ID", tab.conversation_id),
                ):
                    if value is not None:
                        _text(value, label)
                if tab.kind == "chat":
                    for value in (tab.provider_id, tab.account_id, tab.chat_id):
                        _text(value, "chat reference")
                tab_ids.add(tab.id)
            if group.active_tab_id is not None and group.active_tab_id not in {
                tab.id for tab in group.tabs
            }:
                raise ValueError("active tab does not belong to its group")

    @classmethod
    def from_dict(cls, value: dict) -> "Workspace":
        try:
            return cls._decode(value)
        except (TypeError, AttributeError) as error:
            raise ValueError("invalid workspace document") from error

    @classmethod
    def _decode(cls, value: dict) -> "Workspace":
        if not isinstance(value, dict):
            raise ValueError("workspace document must be an object")
        value = dict(value)
        groups = []
        raw_groups = value.pop("groups", [])
        if not isinstance(raw_groups, list):
            raise ValueError("workspace groups must be an array")
        for raw_group in raw_groups:
            if not isinstance(raw_group, dict):
                raise ValueError("group must be an object")
            group = dict(raw_group)
            raw_tabs = group.pop("tabs", [])
            if not isinstance(raw_tabs, list):
                raise ValueError("group tabs must be an array")
            tabs = tuple(Tab(**tab) for tab in raw_tabs)
            groups.append(TabGroup(tabs=tabs, **group))
        workspace = cls(groups=tuple(groups), **value)
        workspace.validate()
        return workspace


class WorkspaceRepository(Protocol):
    async def save(self, workspace: Workspace) -> None: ...
    async def load(self, workspace_id: str) -> Workspace: ...
    async def list(self) -> tuple[tuple[str, str], ...]: ...
    async def delete(self, workspace_id: str) -> bool: ...
