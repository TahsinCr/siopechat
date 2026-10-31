"""SidebarComponent: sidebar behavior for ChatView."""

from __future__ import annotations

from typing import Any

from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.utils import get_cwidth

from accounts import origin
from providers import Capability

from ..library import build_sidebar_rows, merge_chat_listing
from ..styles import ICONS
from ..widgets import (
    fit_text,
    on_click,
)


class SidebarComponent:
    def _sidebar_sync_states(self):
        archive = self.archive_service()
        if not archive:
            return {}
        result = {}
        for item in self.sync_states(archive):
            error = str(item.get("error") or "").casefold()
            if (
                item["kind"] == "container"
                and item["state"] == "error"
                and any(
                    marker in error
                    for marker in (
                        "kayıtlı oturum yok",
                        "http 401",
                        "unauthorized",
                        "authentication required",
                    )
                )
            ):
                # Authentication is account-wide. Repeating the same ! on every
                # cached project suggests independent project failures.
                continue
            result[item["account"], item["kind"], item["remote"]] = item["state"]
        return result

    def _sidebar_provider_capabilities(self, provider_id):
        registry = getattr(getattr(self.account_service, "api", None), "providers", None)
        if registry is None:
            return frozenset()
        capabilities = self._sidebar_capabilities_cache.get(provider_id)
        if capabilities is not None:
            return capabilities
        try:
            capabilities = frozenset(registry.get(provider_id).capabilities)
        except (KeyError, AttributeError):
            return frozenset()
        self._sidebar_capabilities_cache[provider_id] = capabilities
        return capabilities

    def render_sidebar_row(self, index, states=None, open_ids=None):
        if not 0 <= index < len(self.sidebar_rows):
            return []
        row = self.sidebar_rows[index]
        kind = row["kind"]
        if kind == "message":
            return [
                (
                    "class:sidebar.muted",
                    "   " + fit_text(row["title"], self.sidebar_width() - 3) + "\n",
                )
            ]
        states = states if states is not None else {}
        if open_ids is None:
            open_ids = {tab.get("conversation_id") for tab in self.tabs}
        selected = index == self.sidebar_index
        click = on_click(lambda i=index: self.click_sidebar(i))
        style = "class:sidebar.selected" if selected else "class:sidebar.item"
        if row.get("id") in open_ids:
            style += " class:sidebar.open"
        marker = (
            (ICONS["collapsed"].icon if row["collapsed"] else ICONS["dropdown"].icon)
            if kind in ("section", "project")
            else (ICONS["forward"].icon if selected else " ")
        )
        indent = "   " if row.get("nested") else " "
        fragments = [(style, indent + marker + " ", click)]
        prefix_width = get_cwidth(indent + marker + " ")
        if kind in ("chat", "project"):
            provider = row.get("provider_id", "chatgpt")
            provider_style = ICONS.get(f"provider.{provider}", ICONS["provider.generic"])
            provider_icon = provider_style.icon
            fragments.append(
                (
                    f"class:{provider_style.name}",
                    provider_icon + " ",
                    click,
                )
            )
            prefix_width += get_cwidth(provider_icon + " ")
        suffix = " " + str(row.get("count", "")) if kind in ("section", "project") else ""
        if kind == "project":
            suffix += f" {ICONS['info'].icon}"
        section_action = None
        if kind == "section" and row.get("id") in ("projects", "chats"):
            section_action = self.open_project_create if row["id"] == "projects" else self.new_tab
        key, remote = origin(row.get("id"), self.account_key)
        state = states.get((key, "conversation" if kind == "chat" else "container", remote))
        live_tab = row.get("live_tab")
        if (
            kind == "chat"
            and live_tab is not None
            and (live_tab.get("generation_complete") or live_tab.get("completed_pending"))
        ):
            suffix += f" {ICONS['complete'].icon}"
        elif kind == "chat" and live_tab is not None and live_tab.get("busy"):
            suffix += f" {ICONS['running'].icon}"
            style += " class:sidebar.running"
        if state == "syncing":
            suffix += f" {ICONS['retry'].icon}"
        elif state == "error":
            suffix += " !"
        provider_capabilities = self._sidebar_provider_capabilities(
            row.get("provider_id", "chatgpt")
        )
        can_rename = (
            kind == "chat"
            and self.rename_conversation is not None
            and Capability.RENAME in provider_capabilities
        )
        can_delete = kind == "chat" and Capability.DELETE in provider_capabilities
        action_suffix = (f" {ICONS['edit'].icon}" if can_rename else "") + (
            f" {ICONS['delete'].icon}" if can_delete else ""
        )
        trailing_gutter = " " if kind == "project" else ""
        reserve_suffix = (
            suffix + action_suffix + ("  +" if section_action is not None else "") + trailing_gutter
        )
        width = self.sidebar_width() - prefix_width - get_cwidth(reserve_suffix)
        title = fit_text(row["title"], max(1, width))
        fragments.append(
            (
                style + (" bold" if kind == "section" else ""),
                title,
                click,
            )
        )
        if kind == "project":
            fragments.append(
                (
                    "class:sidebar.muted",
                    suffix,
                    on_click(lambda project_id=row["id"]: self.show_project_details(project_id)),
                )
            )
            fragments.append((style, trailing_gutter + "\n"))
        elif section_action is not None:
            padding = max(0, width - get_cwidth(title))
            if padding:
                fragments.append((style, " " * padding, click))
            fragments.append((style, suffix, click))
            fragments.append((style, "  +", on_click(section_action)))
            fragments.append((style, "\n", click))
        elif kind == "chat":
            padding = max(0, width - get_cwidth(title))
            if padding:
                fragments.append((style, " " * padding, click))
            if suffix:
                fragments.append((style, suffix, click))
            chat_id = row.get("id")
            if can_rename:
                fragments.append(
                    (
                        "class:button",
                        f" {ICONS['edit'].icon}",
                        on_click(
                            lambda identifier=chat_id, name=row["title"]: (
                                self.open_conversation_rename(identifier, name)
                            )
                        ),
                    )
                )
            if can_delete:
                fragments.append(
                    (
                        "class:button",
                        f" {ICONS['delete'].icon}",
                        on_click(lambda identifier=chat_id: self.delete_record(value=identifier)),
                    )
                )
            fragments.append((style, "\n", click))
        else:
            fragments.append((style, suffix + "\n", click))
        return fragments

    def render_sidebar(self) -> FormattedText:
        self._build_sidebar_rows()
        states = self._sidebar_sync_states()
        open_ids = {tab.get("conversation_id") for tab in self.tabs}
        fragments = []
        for index in range(len(self.sidebar_rows)):
            fragments.extend(self.render_sidebar_row(index, states, open_ids))
        return FormattedText(fragments)

    def click_sidebar(self, index):
        self.sidebar_index = index
        self.activate_sidebar_item()

    def click_palette(self, index):
        self.palette_index = index
        self.execute_palette_item()

    def _invalidate_sidebar_rows(self):
        self._sidebar_rows_dirty = True

    def _build_sidebar_rows(self, force=True):
        if force or self._sidebar_rows_dirty:
            build_sidebar_rows(self)
            self._sidebar_rows_dirty = False
        return self.sidebar_rows

    def _chat_row(self, item: dict[str, Any], nested: bool = False) -> dict[str, Any]:
        return {
            "kind": "chat",
            "id": item.get("id"),
            "project_id": item.get("project_id"),
            "title": str(item.get("title") or self.tr("Adsız sohbet")),
            "provider_id": item.get("provider_id") or origin(item.get("id"))[0].split("/")[0],
            "nested": nested,
            "live_tab": item.get("live_tab"),
        }

    def toggle_sidebar(self) -> None:
        self.close_overlays()
        self.sidebar_visible = not self.sidebar_visible
        self.app.layout.focus(self.sidebar_control if self.sidebar_visible else self.input)
        self.app.invalidate()

    def focus_sidebar(self) -> None:
        self.close_overlays()
        if not self.sidebar_visible:
            self.sidebar_visible = True
        self.app.layout.focus(self.sidebar_control)
        self.status = self.tr("Sohbetler")
        self.app.invalidate()

    def toggle_sidebar_group(self) -> None:
        self._build_sidebar_rows()
        if not self.sidebar_rows:
            return
        row = self.sidebar_rows[self.sidebar_index]
        if row["kind"] not in ("section", "project"):
            return
        key = f"project:{row['id']}" if row["kind"] == "project" else f"section:{row['id']}"
        if key in self.sidebar_collapsed:
            self.sidebar_collapsed.remove(key)
            if row["kind"] == "project" and (
                row["id"] not in self.project_chats_loaded or row["id"] in self.project_chat_errors
            ):
                self.load_project_chats(row["id"])
        else:
            self.sidebar_collapsed.add(key)
        self._build_sidebar_rows()
        self.ensure_sidebar_selection_visible()
        self.app.invalidate()

    def ensure_sidebar_selection_visible(self) -> None:
        info = self.sidebar_window.render_info
        height = (
            info.window_height if info is not None else max(1, self.viewport_height().preferred - 5)
        )
        first = self.sidebar_window.vertical_scroll
        if self.sidebar_index < first:
            self.sidebar_window.vertical_scroll = self.sidebar_index
        elif self.sidebar_index >= first + height:
            self.sidebar_window.vertical_scroll = self.sidebar_index - height + 1

    def load_more_history(self, *, generation=None):
        if not self.list_history_page or not self.history_cursor or self.history_loading:
            return
        generation = self.sidebar_refresh_generation if generation is None else generation
        self.history_loading = True
        self._history_page_token += 1
        token = self._history_page_token
        self._invalidate_sidebar_rows()
        self.app.invalidate()
        cursor = self.history_cursor
        self._history_seen_cursors.add(cursor)

        def loaded(page):
            if token != self._history_page_token or generation != self.sidebar_refresh_generation:
                return
            by_id = {item["id"]: item for item in self.sidebar_items if item.get("id")}
            for item in page["items"]:
                if item.get("id"):
                    by_id[item["id"]] = merge_chat_listing(by_id.get(item["id"]), item)
            self.sidebar_items = list(by_id.values())
            next_cursor = page.get("cursor")
            if next_cursor == cursor or next_cursor in self._history_seen_cursors:
                next_cursor = None
            self.history_cursor = next_cursor
            self.history_loading = False
            self.filter_sidebar()
            if next_cursor:
                self.load_more_history(generation=generation)

        def failed(reason):
            if token != self._history_page_token or generation != self.sidebar_refresh_generation:
                return
            self.history_loading = False
            self._invalidate_sidebar_rows()
            self.status = str(reason)
            self.app.invalidate()

        self._read_in_background(lambda: self.list_history_page(cursor), loaded, failed)

    def refresh_sidebar(self) -> None:
        self.refresh_projects()
        if self.sidebar_loading:
            self.sidebar_refresh_pending = True
            return
        self.sidebar_refresh_generation += 1
        generation = self.sidebar_refresh_generation
        self._history_page_token += 1
        self._history_seen_cursors.clear()
        self.history_cursor = None
        self.history_loading = False
        self.sidebar_loading = True
        self.app.invalidate()

        def load() -> None:
            conversations: list[dict[str, Any]] = []
            cursor = None
            workspaces: list[dict[str, Any]] = []
            sidebar_error = None
            partial_error = None
            workspace_error = None
            try:
                page = self.list_history_page(None) if self.list_history_page else None
                conversations = [
                    item
                    for item in (page["items"] if page is not None else self.list_conversations())
                    if isinstance(item, dict)
                ]
                cursor = page.get("cursor") if page is not None else None
                if page and page.get("errors"):
                    partial_error = self.tr("Yenilenemeyen hesaplar: ") + ", ".join(page["errors"])
                    failed = set(page.get("failed_accounts", []))
                    conversations.extend(
                        item
                        for item in self.sidebar_items
                        if origin(item.get("id"), self.account_key)[0] in failed
                    )
            except Exception as error:
                sidebar_error = str(error)
            try:
                workspaces = [item for item in self.list_workspaces() if isinstance(item, dict)]
            except Exception as error:
                workspace_error = str(error)

            def apply_results() -> None:
                if generation != self.sidebar_refresh_generation:
                    return
                # Keep the last good list on transient errors instead of
                # blanking the navigation tree while a refresh is in flight.
                if sidebar_error is None:
                    project_chats = [
                        item
                        for item in self.sidebar_items
                        if item.get("project_id") in self.project_chats_loaded
                    ]
                    by_id = {item["id"]: item for item in project_chats if item.get("id")}
                    for item in conversations:
                        if item.get("id"):
                            by_id[item["id"]] = merge_chat_listing(by_id.get(item["id"]), item)
                    chats = list(by_id.values())
                else:
                    chats = [item for item in self.sidebar_items if item.get("kind") != "workspace"]
                saved_workspaces = (
                    [dict(item, kind="workspace") for item in workspaces]
                    if workspace_error is None
                    else [item for item in self.sidebar_items if item.get("kind") == "workspace"]
                )
                if sidebar_error is None:
                    self.history_cursor = cursor
                self.sidebar_items = chats + saved_workspaces
                self.sidebar_error = sidebar_error or partial_error
                self.workspace_error = workspace_error
                self.sidebar_loading = False
                if sidebar_error or workspace_error or partial_error:
                    self.status = partial_error or self.tr(
                        "Sol panel yenilenemedi; son kayıtlar korunuyor"
                    )
                self.filter_sidebar()
                if sidebar_error is None and cursor:
                    self.load_more_history(generation=generation)
                if getattr(self, "sidebar_refresh_pending", False):
                    self.sidebar_refresh_pending = False
                    self.refresh_sidebar()

            self._dispatch_ui(apply_results)

        if not self._reads.submit(load):
            self.sidebar_loading = False
            self.status = self.tr("Arka plan iş kuyruğu dolu; yeniden deneyin")

    def _read_in_background(self, read, success, failure, session_bound=True, *, cpu=False) -> None:
        if self.event_loop is None:
            self.event_loop = getattr(self.app, "loop", None)
        epoch = self.session_epoch
        original_success, original_failure = success, failure

        def success(result):
            if not session_bound or epoch == self.session_epoch:
                original_success(result)

        def failure(reason):
            if not session_bound or epoch == self.session_epoch:
                original_failure(reason)

        def work():
            try:
                result = read()
            except Exception as error:
                reason = str(error)
                self._dispatch_ui(lambda: failure(reason))
            else:

                def deliver():
                    try:
                        success(result)
                    except Exception as error:
                        failure(str(error))

                self._dispatch_ui(deliver)

        pool = self._render_reads if cpu else self._reads
        if not pool.submit(work):
            self._dispatch_ui(
                lambda: failure(self.tr("Arka plan iş kuyruğu dolu; yeniden deneyin"))
            )

    def filter_sidebar(self) -> None:
        if hasattr(self, "app"):
            self._invalidate_sidebar_rows()
            self._build_sidebar_rows()
            self.ensure_sidebar_selection_visible()
            self.app.invalidate()

    def open_sidebar_search(self) -> None:
        self.close_overlays()
        self.sidebar_visible = True
        self.sidebar_search.text = ""
        self.refresh_sidebar()
        self.app.layout.focus(self.sidebar_search)
        self.app.invalidate()
