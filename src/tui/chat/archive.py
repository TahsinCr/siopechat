"""ArchiveComponent: archive behavior for ChatView."""

from __future__ import annotations

import asyncio

from accounts import origin

from ..history import (
    prepare_archive_snapshot,
    read_history,
)
from ..library import merge_chat_listing


class ArchiveComponent:
    async def animate_activity(self) -> None:
        """Refresh animated status only while an operation is active.

        A permanent prompt_toolkit refresh interval repaints the terminal even
        when the UI is idle. Those writes clear native terminal selections,
        making copied text lose its highlight almost immediately.
        """
        last_account_check = 0.0
        last_download_check = 0.0
        while True:
            await asyncio.sleep(0.12)
            now = asyncio.get_running_loop().time()
            if self.account_service and now - last_account_check >= 5:
                last_account_check = now
                key = self.account_key
                try:
                    state = await asyncio.to_thread(self.account_service.status, key)
                except ValueError:
                    # A local account can be removed between the timer and UI callback.
                    continue
                if key == self.account_key and state != self.session_state:
                    self.session_state = state
                    self.app.invalidate()
            downloads_changed = self.downloads_panel.update()
            if now - last_download_check >= 10:
                last_download_check = now
                self.downloads_panel.refresh()
            if downloads_changed:
                self.app.invalidate()
            active_tab_busy = bool(self.tabs and self.tabs[self.active_tab].get("busy"))
            self.apply_archive_updates()
            if active_tab_busy or self.loading_conversation or self.uploading_image:
                self.app.invalidate()

    def archive_service(self):
        if self.account_service and hasattr(self.account_service, "archive"):
            return self.account_service.archive()
        return None

    def sync_states(self, archive):
        signature = (id(archive), archive.revision)
        if signature != getattr(self, "_sync_state_signature", None):
            self._sync_state_rows = archive.states()
            self._sync_state_signature = signature
        return self._sync_state_rows

    def sync_failure_detail(self, failures):
        if not failures or not all(
            "provider rate limit" in str(item.get("error") or "").casefold() for item in failures
        ):
            return None
        providers = {
            str(item.get("account", "")).split("/", 1)[0]
            for item in failures
            if item.get("account")
        }
        if len(providers) == 1:
            provider = next(iter(providers))
            name = {"chatgpt": "ChatGPT", "claude": "Claude", "gemini": "Gemini"}.get(
                provider, provider
            )
            return self.tr("{0} hız sınırı").format(name)
        return self.tr("Sağlayıcı hız sınırı")

    def synchronize(self, scope="all"):
        if getattr(self, "_synchronize_pending", False):
            self.status = self.tr("Eşitleme zaten sürüyor")
            return
        if not self.account_service or not hasattr(self.account_service, "synchronize"):
            self.refresh_sidebar()
            self.refresh_provider_catalog(force=True)
            return
        self._synchronize_pending = True
        key, cid = self.account_key, self.conversation_id
        self.status = self.tr("Eşitleniyor…")

        def finish(error=None):
            self._synchronize_pending = False
            self.status = (
                self.tr("Eşitleme başlatıldı") if error is None else self.tr("Eşitleme: ") + error
            )
            self.refresh_sidebar()
            self.apply_archive_updates()
            if scope == "all":
                self.refresh_provider_catalog()
            self.app.invalidate()

        self._read_in_background(
            lambda: self.account_service.synchronize(
                scope, key if scope == "conversation" else None, cid
            ),
            lambda _: finish(),
            lambda reason: finish(reason),
            session_bound=False,
        )

    def refresh_open_conversation(self):
        if not self.account_service or not self.conversation_id or not self.archive_service():
            return
        conversation_id, key = self.conversation_id, self.account_key

        def refreshed(_):
            self.apply_archive_updates()

        self._read_in_background(
            lambda: self.account_service.load(conversation_id, key, force=True),
            refreshed,
            lambda reason: self._show_refresh_error(reason),
            session_bound=False,
        )

    def _show_refresh_error(self, reason):
        self.status = self.tr("Sohbet senkronize edilemedi: ") + str(reason)
        self.app.invalidate()

    def apply_archive_updates(self, snapshot=None):
        source = self.archive_service()
        archive = snapshot or source
        revision = (id(source), archive.revision) if archive is not None else None
        if archive is None:
            return
        pending_tabs = [
            tab
            for tab in self.tabs
            if tab.get("archive_refresh_pending") is not None
            and not tab.get("busy")
            and tab.get("conversation_id")
        ]
        pending_history_tabs = [
            tab
            for tab in self.tabs
            if tab.get("history_load_complete") and tab.get("conversation_id")
        ]
        if revision == self._archive_revision and not pending_tabs and not pending_history_tabs:
            return
        state_rows = self.sync_states(archive)
        states = {(x["account"], x["kind"], x["remote"]): x for x in state_rows}
        pending_refresh = False
        for tab in pending_tabs:
            key, remote = origin(tab["conversation_id"], tab.get("account_key", self.account_key))
            state = states.get((key, "conversation", remote))
            if state and state["state"] == "ready":
                pending_refresh = True
                break
        if revision == self._archive_revision and not pending_refresh and not pending_history_tabs:
            return
        if snapshot is None and self.app.is_running:
            if getattr(self, "_archive_read_pending", False):
                return
            self._archive_read_pending = True
            for tab in self.tabs:
                if not tab.get("conversation_id") or not tab.get("busy"):
                    continue
                key, remote = origin(
                    tab["conversation_id"], tab.get("account_key", self.account_key)
                )
                state = states.get((key, "conversation", remote))
                if (
                    state
                    and state["state"] == "ready"
                    and tab.get("archive_version") != state.get("version")
                ):
                    tab["archive_refresh_pending"] = state["version"]
            accounts = self.account_service.active_accounts()
            tabs = [
                (
                    tab,
                    {
                        key: tab.get(key)
                        for key in (
                            "conversation_id",
                            "account_key",
                            "busy",
                            "archive_version",
                            "archive_refresh_pending",
                            "history_window",
                        )
                    },
                )
                for tab in self.tabs
            ]
            known_chats, known_projects = list(self.sidebar_items), list(self.project_items)
            default_account = self.account_key

            def read():
                return prepare_archive_snapshot(
                    source,
                    accounts,
                    tabs,
                    known_chats,
                    known_projects,
                    default_account,
                    revision[1],
                )

            def completed(result):
                self._archive_read_pending = False
                if self.archive_service() is source:
                    if result.revision != source.revision:
                        self._archive_revision = -1
                        self.apply_archive_updates()
                    else:
                        self.apply_archive_updates(result)

            def failed(reason):
                self._archive_read_pending = False
                self.status = self.tr("Arşiv okunamadı: ") + str(reason)
                self.app.invalidate()

            self._read_in_background(read, completed, failed, session_bound=False)
            return
        self._archive_revision = revision
        accounts = self.account_service.active_accounts()
        chats = {item["id"]: item for item in self.sidebar_items if item.get("id")}
        for account in accounts:
            key = account["key"]
            page = archive.get(key, "chats", "")
            if page:
                new_ids = {item["id"] for item in page["items"]}
                prior_ids = self._archive_page_ids.get((key, "chats"), set())
                if page.get("complete"):
                    prior_ids = prior_ids | {
                        cid
                        for cid, item in chats.items()
                        if item.get("context") == key
                        and not item.get("project_id")
                        and item.get("kind") != "workspace"
                    }
                for removed in prior_ids - new_ids:
                    chats.pop(removed, None)
                self._archive_page_ids[key, "chats"] = new_ids
                for item in page["items"]:
                    chats[item["id"]] = merge_chat_listing(chats.get(item["id"]), item)
            page = archive.get(key, "containers", "")
            if page:
                new_ids = {item["id"] for item in page["items"]}
                for removed in self._archive_page_ids.get((key, "containers"), set()) - new_ids:
                    self.project_items.pop(removed, None)
                    for projects in self._account_projects.values():
                        projects.pop(removed, None)
                self._archive_page_ids[key, "containers"] = new_ids
                for item in page["items"]:
                    self.project_items[item["id"]] = item
        for cid in list(chats):
            key, remote = origin(cid, self.account_key)
            if archive.deleted(key, "conversation", remote):
                chats.pop(cid, None)
        for pid in list(self.project_items):
            key, remote = origin(pid, self.account_key)
            if archive.deleted(key, "container", remote):
                self.project_items.pop(pid, None)
        self.sidebar_items = list(chats.values())
        for i, tab in enumerate(self.tabs):
            if not tab.get("conversation_id"):
                continue
            key, remote = origin(tab["conversation_id"], tab.get("account_key", self.account_key))
            if archive.deleted(key, "conversation", remote):
                tab["syncing"] = False
                tab.pop("history_load_pending", None)
                tab.pop("history_load_complete", None)
                tab["load_error"] = self.tr("Sağlayıcıdan silinmiş sohbet")
                tab.pop("archive_refresh_pending", None)
                if i == self.active_tab:
                    self.status = self.tr("Bu sohbet sağlayıcıdan silinmiş · yeni sohbet açın")
                continue
            state = states.get((key, "conversation", remote))
            if tab.get("history_load_complete") and not (state and state["state"] == "syncing"):
                tab.pop("history_load_pending", None)
                tab.pop("history_load_complete", None)
                if i == self.active_tab and not tab.get("messages"):
                    self.status = ""
            tab["syncing"] = bool(state and state["state"] == "syncing") or bool(
                tab.get("history_load_pending")
            )
            if tab.get("busy"):
                if (
                    state
                    and state["state"] == "ready"
                    and tab.get("archive_version") != state.get("version")
                ):
                    # The archive revision is global. Remember this tab's update
                    # so a concurrent response cannot consume it permanently.
                    tab["archive_refresh_pending"] = state["version"]
                continue
            if (
                not state
                or state["state"] != "ready"
                or (
                    tab.get("archive_version") == state.get("version")
                    and tab.get("archive_refresh_pending") is None
                )
            ):
                if state and state.get("state") == "error":
                    tab.pop("archive_refresh_pending", None)
                continue
            if snapshot is not None and snapshot.tab_windows.get(id(tab)) is not tab.get(
                "history_window"
            ):
                self._archive_revision = -1
                continue
            if snapshot is not None and id(tab) in snapshot.deferred_tabs:
                tab["archive_refresh_pending"] = state["version"]
                continue
            window = tab.get("history_window") or {}
            has_window = window.get("end", 0) > window.get("start", 0)
            value = (
                snapshot.get(key, "conversation", remote)
                if snapshot is not None
                else read_history(
                    archive,
                    key,
                    remote,
                    start=window.get("start") if has_window else None,
                    complete=has_window,
                    until=window["end"] if has_window else None,
                )
            )
            if not value:
                tab["archive_version"] = state["version"]
                tab.pop("archive_refresh_pending", None)
                continue
            tab["archive_version"] = state["version"]
            tab.pop("archive_refresh_pending", None)
            from ..paging import visible_window

            messages = visible_window(value, tab.get("messages", ()))
            if tab.get("messages") != messages:
                if i != self.active_tab:
                    cached_layout = tab.pop("render_layout", None)
                    tab.pop("render_signature", None)
                    if cached_layout is not None:
                        cached_layout.dispose()
                if i == self.active_tab and not self.follow_output:
                    tab["history_anchor"] = self.history_pager.anchor(tab)
                tab["messages"] = messages
                tab["title"] = value.get("title") or tab.get("title")
                if i == self.active_tab:
                    self.messages = messages
                    self.title = tab["title"]
            tab["history_window"] = value.get("history_window")
            tab["history_version"] = value.get("history_version")
        self._invalidate_sidebar_rows()
        self.app.invalidate()
