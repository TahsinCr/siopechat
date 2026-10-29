"""Stable action grouping and locale-aware action titles."""

from collections.abc import Mapping

_GROUP_ORDER = (
    "chat",
    "model",
    "projects",
    "layout",
    "accounts",
    "appearance",
    "editing",
    "navigation",
    "application",
    "extensions",
)


def action_group(action_id: str) -> str:
    if action_id in ("command.model", "command.effort"):
        return "model"
    if action_id in ("command.project", "command.projects") or action_id.startswith("projects."):
        return "projects"
    if action_id == "command.session" or action_id.startswith("accounts."):
        return "accounts"
    if action_id.startswith("layout."):
        return "layout"
    if action_id.startswith("navigation."):
        return "navigation"
    if action_id.startswith("edit."):
        return "editing"
    if action_id.startswith("chat.") or action_id == "command.rename":
        return "chat"
    if action_id.startswith("ui.") and action_id != "ui.command_palette":
        return "appearance"
    if action_id.startswith(("app.", "command.", "ui.")):
        return "application"
    return "extensions"


def action_title(action_id: str, messages: Mapping[str, str], fallback: str | None = None) -> str:
    group = action_group(action_id)
    group_title = messages.get(f"application.action_groups.{group}", group.title())
    title = messages.get(
        f"application.action_titles.{action_id}",
        fallback or action_id.partition(".")[2].replace("_", " ").capitalize(),
    )
    return f"{group_title} · {title}"


def action_order(action_id: str) -> tuple[int, int, str]:
    return (
        _GROUP_ORDER.index(action_group(action_id)),
        0 if action_id == "chat.new" else 1,
        action_id,
    )
