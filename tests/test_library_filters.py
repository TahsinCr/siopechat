"""Account-scoped library browsing and stable native-date sorting."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from bootstrap import create_application
from cli.library import sort_records
from providers.gemini.history import conversations
from providers.gemini.notebooks import project_from_native
from providers.models import Conversation, Origin, Page
from tests.test_account_ui import Accounts
from tests.test_cli_interaction import terminal
from tests.test_gemini_notebooks import notebook


class LibraryTests(unittest.TestCase):
    def test_all_sort_directions_keep_missing_dates_last(self):
        rows = [
            {
                "id": "b",
                "title": "Beta",
                "metadata": {"native": {"updated_at": "2026-10-10T00:00:00Z"}},
            },
            {"id": "a", "name": "Alpha", "update_time": 100},
            {"id": "c", "title": "Zulu"},
        ]
        for mode, expected in [
            ("time_desc", "bac"),
            ("time_asc", "abc"),
            ("name_asc", "abc"),
            ("name_desc", "cba"),
        ]:
            self.assertEqual("".join(x["id"] for x in sort_records(rows, mode)), expected)
        self.assertEqual([x["id"] for x in rows], list("bac"))

    def test_gemini_native_dates_are_normalized_for_both_record_types(self):
        chat = conversations(
            [[None, None, [["c_x", "Title", None, None, None, [100, 500000000]]]]],
            lambda cid: Origin("gemini", "a", cid),
        )[0]
        self.assertEqual(chat.metadata["updated_at"], 100.5)
        row = notebook()
        row[1][14] = [None, [200, 250000000]]
        self.assertEqual(project_from_native(row, "a").metadata["updated_at"], 200.25)

    def test_sidebar_filters_projects_and_chats_by_account_and_excludes_disabled_providers(self):
        service = Accounts()
        with terminal(account_service=service) as (view, _):
            view.sidebar_collapsed = set()
            view.sidebar_items = [
                {"id": "@chatgpt/default/c", "title": "GPT", "provider_id": "chatgpt"},
                {"id": "@gemini/personal/c", "title": "Gemini", "provider_id": "gemini"},
            ]
            view.project_items = {
                "@chatgpt/default/p": {"name": "GPT project", "provider_id": "chatgpt"},
                "@gemini/personal/p": {"name": "Gemini project", "provider_id": "gemini"},
            }
            view.account_filter = {"gemini/personal"}
            view._build_sidebar_rows()
            self.assertEqual(
                [x["title"] for x in view.sidebar_rows if x["kind"] == "project"],
                ["Gemini project"],
            )
            self.assertEqual(
                [x["title"] for x in view.sidebar_rows if x["kind"] == "chat"], ["Gemini"]
            )
            view.account_filter = None
            service.entries[1]["enabled"] = False
            view._build_sidebar_rows()
            self.assertEqual(
                [x["title"] for x in view.sidebar_rows if x["kind"] == "project"], ["GPT project"]
            )
            self.assertEqual(
                [x["title"] for x in view.sidebar_rows if x["kind"] == "chat"], ["GPT"]
            )

    def test_library_can_browse_inactive_account_without_changing_sender(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_application(Path(directory))
            try:
                accounts = app.get("accounts")
                second = accounts.add("chatgpt", "Second")
                accounts.status = lambda _: {"saved_snapshot": True}
                accounts.browser_connected = lambda _: True
                for key in ("chatgpt/default", second):
                    provider = accounts.provider(key)
                    provider.list_chats = AsyncMock(
                        return_value=Page((Conversation(Origin(*key.split("/"), "c"), key),))
                    )
                before = accounts.active()
                page = accounts.list_chats(account_keys={second})
                self.assertEqual(len(page["items"]), 1)
                self.assertEqual(page["items"][0]["account_id"], second.split("/")[1])
                self.assertEqual(accounts.active(), before)
                accounts._providers["chatgpt/default"].list_chats.assert_not_called()
            finally:
                app.runtime.close()
