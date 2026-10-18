"""Archive isolation, revalidation, bounded memory and account/model UX."""

import asyncio
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from bootstrap import create_application
from extensions.archive.repository import Files, SQLiteRepository
from extensions.archive.sync import Archive
from providers.models import Choice, FeatureState, Model, ModelCatalog
from tests.test_cli_interaction import terminal
from tests.test_ui_controls import immediate


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.repository = SQLiteRepository(self.root / "archive.sqlite3")
        self.archive = Archive(self.repository, Files(self.root / "files"), memory_bytes=2048)
        self.addCleanup(self.archive.close)

    def wait(self):
        until = time.monotonic() + 3
        while any(x["state"] == "syncing" for x in self.archive.states()):
            self.assertLess(time.monotonic(), until)
            time.sleep(0.005)

    def test_account_isolation_roundtrip_reasoning_and_resources(self):
        path, digest = self.archive.files.save(b"file contents")
        value = {
            "id": "one",
            "title": "A",
            "project_id": "@gpt/a/project",
            "messages": [
                {
                    "id": "1",
                    "role": "assistant",
                    "channel": "analysis",
                    "text": "Visible summary",
                    "metadata": {"source": "provider"},
                },
                {
                    "id": "2",
                    "role": "assistant",
                    "text": "Answer",
                    "content": [
                        {
                            "kind": "image",
                            "resource": {
                                "id": "img",
                                "name": "image",
                                "path": path,
                                "sha256": digest,
                            },
                        }
                    ],
                },
            ],
        }
        self.archive.put("chatgpt/a", "conversation", "one", value)
        self.assertEqual(self.archive.get("chatgpt/a", "conversation", "one"), value)
        self.assertIsNone(self.archive.get("gemini/a", "conversation", "one"))
        self.assertEqual((self.root / "files" / path).read_bytes(), b"file contents")
        with closing(sqlite3.connect(self.repository.path)) as db:
            self.assertEqual(
                db.execute("SELECT channel FROM messages ORDER BY ordinal").fetchone()[0],
                "analysis",
            )
            self.assertEqual(db.execute("SELECT path FROM resources").fetchone()[0], path)
            self.assertEqual(db.execute("SELECT count(*) FROM memberships").fetchone()[0], 1)
        self.assertFalse(self.repository.put("chatgpt/a", "conversation", "one", value))
        fresh = SQLiteRepository(self.repository.path)
        self.assertEqual(fresh.get("chatgpt/a", "conversation", "one"), value)

    def test_cached_read_returns_before_remote_and_single_flight(self):
        self.archive.put("gemini/a", "conversation", "one", {"messages": [], "title": "old"})
        entered, release = threading.Event(), threading.Event()

        def fetch():
            entered.set()
            self.assertTrue(release.wait(3))
            return {"messages": [], "title": "new"}

        start = time.monotonic()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(
                    lambda _: self.archive.read("gemini/a", "conversation", "one", fetch), range(16)
                )
            )
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertTrue(entered.wait(1))
        self.assertEqual({v["title"] for v in results}, {"old"})
        self.assertEqual(len(self.archive._pending), 1)
        release.set()
        self.wait()
        self.assertEqual(self.archive.get("gemini/a", "conversation", "one")["title"], "new")

    def test_late_read_cannot_overwrite_completed_send(self):
        self.archive.put("chatgpt/a", "conversation", "one", {"title": "old"})
        release = threading.Event()
        self.archive.read(
            "chatgpt/a", "conversation", "one", lambda: release.wait(3) and {"title": "stale"}
        )
        self.archive.put("chatgpt/a", "conversation", "one", {"title": "sent"})
        release.set()
        self.wait()
        self.assertEqual(self.archive.get("chatgpt/a", "conversation", "one")["title"], "sent")

    def test_errors_preserve_saved_value_and_explicit_refresh_retries(self):
        self.archive.put("gemini/a", "container", "n", {"name": "Notebook"})
        calls = Mock(side_effect=ValueError("offline"))
        value = self.archive.read("gemini/a", "container", "n", calls)
        self.assertEqual(value["name"], "Notebook")
        self.wait()
        self.assertEqual(self.archive.states()[-1]["state"], "error")
        calls.side_effect = None
        calls.return_value = {"name": "Updated"}
        self.archive.refresh()
        self.wait()
        self.assertEqual(self.archive.get("gemini/a", "container", "n")["name"], "Updated")
        with closing(sqlite3.connect(self.repository.path)) as db:
            self.assertEqual(db.execute("SELECT kind FROM containers").fetchone()[0], "notebook")

    def test_refresh_of_a_failed_cold_record_never_waits_in_the_caller(self):
        release = threading.Event()
        fetch = Mock(side_effect=ValueError("offline"))
        with self.assertRaises(ValueError):
            self.archive.read("gemini/a", "conversation", "missing", fetch)
        self.wait()
        fetch.side_effect = lambda: release.wait(2) and {"title": "Recovered"}
        started = time.monotonic()
        try:
            self.archive.refresh()
            self.assertLess(time.monotonic() - started, 0.1)
        finally:
            release.set()
            self.wait()

    def test_hot_memory_is_bounded_and_temporary_data_is_not_archived(self):
        for i in range(25):
            self.archive.put("gemini/a", "conversation", str(i), {"text": "x" * 500})
        self.assertLessEqual(self.archive._size, 2048)
        self.assertLess(len(self.archive._hot), 25)
        self.assertEqual(self.archive.get("gemini/a", "conversation", "0")["text"], "x" * 500)
        self.archive.put(
            "gemini/a", "conversation", "private", {"temporary": True, "text": "secret"}
        )
        self.assertIsNone(self.repository.get("gemini/a", "conversation", "private"))


class ArchiveIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.api = create_application(Path(self.folder.name))
        self.addCleanup(self.api.runtime.close)
        self.accounts = self.api.get("accounts")
        self.gemini = self.accounts.add("gemini", "Gemini")

    def test_active_account_is_chosen_independently_per_provider(self):
        second = self.accounts.add("chatgpt", "Other GPT")
        self.accounts.select(second)
        self.accounts.select(self.gemini)
        self.assertEqual({x["key"] for x in self.accounts.active_accounts()}, {second, self.gemini})
        self.accounts.select("chatgpt/default")
        self.assertEqual(
            {x["key"] for x in self.accounts.active_accounts()}, {"chatgpt/default", self.gemini}
        )
        self.accounts.set_enabled("gemini", False)
        self.assertEqual([x["key"] for x in self.accounts.active_accounts()], ["chatgpt/default"])

    def test_new_chat_models_include_selected_accounts_but_existing_chat_does_not(self):
        self.accounts.select(self.gemini)
        for key, model in [("chatgpt/default", "gpt"), (self.gemini, "flash")]:
            self.accounts._catalogs[key] = (
                time.monotonic(),
                ModelCatalog(
                    FeatureState.AVAILABLE, (Model(model, model, (Choice("high", "High"),)),)
                ),
            )
        with terminal(account_service=self.accounts) as (view, _):
            view._read_in_background = immediate
            view.open_palette("models")
            self.assertEqual(
                len([x for x in view.palette_items if x["id"].startswith("model.")]), 2
            )
            view.palette_filtered = [
                next(x for x in view.palette_items if x["id"].startswith("model.account:"))
            ]
            view.execute_palette_item()
            self.assertEqual((view.account_key, view.model), ("chatgpt/default", "gpt"))
            view.conversation_id = "@chatgpt/default/one"
            view.open_palette("models")
            self.assertEqual([x["detail"].split(" · ")[0] for x in view.palette_items], ["chatgpt"])

    def test_sync_updates_keep_draft_and_do_not_restore_deleted_first_page_rows(self):
        archive = self.api.get("archive")
        with terminal(account_service=self.accounts) as (view, _):
            view.input.text = "keep my draft"
            archive.put(
                "chatgpt/default",
                "chats",
                "",
                {"items": [{"id": "@chatgpt/default/one", "title": "Old"}]},
            )
            view.apply_archive_updates()
            self.assertTrue(view.sidebar_items)
            archive.put("chatgpt/default", "chats", "", {"items": []})
            view.apply_archive_updates()
            self.assertEqual(view.sidebar_items, [])
            self.assertEqual(view.input.text, "keep my draft")

    def test_opening_other_provider_chat_uses_its_preference_for_cached_and_cold_history(self):
        self.accounts.select(self.gemini)
        key = "chatgpt/default"
        self.accounts.remember(key, "preferred-gpt", "high")
        self.accounts._catalogs[key] = (
            time.monotonic(),
            ModelCatalog(FeatureState.AVAILABLE, (
                Model("old-gpt", "Old"),
                Model("preferred-gpt", "Preferred", (Choice("high", "High"),)),
            )),
        )
        body = {"id": "@chatgpt/default/chat", "model": "old-gpt", "messages": []}
        for cached in (body, None):
            with self.subTest(cached=cached is not None), terminal(account_service=self.accounts) as (view, _):
                view._read_in_background = immediate
                with patch.object(self.accounts, "cached", return_value=cached), patch.object(
                    self.accounts, "load", return_value=body
                ), patch("cli.chat.threading.Thread", side_effect=lambda target, daemon: Mock(start=target)):
                    view.resume_conversation(body["id"])
                self.assertEqual(view.account_key, key)
                self.assertEqual(view.tabs[view.active_tab]["account_key"], key)
                self.assertEqual((view.model, view.effort), ("preferred-gpt", "high"))
                view.open_palette("models")
                self.assertEqual({i["id"] for i in view.palette_items}, {
                    "model.select:old-gpt", "model.select:preferred-gpt"
                })

    def test_unrelated_background_operation_does_not_block_sending(self):
        with terminal() as (view, _):
            view.background_jobs = 1
            view.input.text = "Hello"
            sent = Mock(return_value={"conversation_id": "new"})
            view.send_prompt = sent
            with patch("cli.chat.threading.Thread", side_effect=lambda **kw: Mock(
                start=lambda: kw["target"](*kw.get("args", ()), **kw.get("kwargs", {}))
            )):
                view.submit_prompt()
            sent.assert_called_once()
            self.assertEqual(view.input.text, "")

    def test_extension_disable_preserves_archive_and_other_services(self):
        archive = self.api.get("archive")
        archive.put("chatgpt/default", "conversation", "one", {"title": "Saved"})
        self.api.get("extensions").disable("archive")
        self.assertIsNone(self.accounts.archive())
        self.assertTrue(self.api.get("workspaces"))
        self.api.get("extensions").enable("archive")
        self.assertEqual(
            self.api.get("archive").get("chatgpt/default", "conversation", "one")["title"], "Saved"
        )

    def test_cold_load_allows_another_tab_and_applies_only_to_its_owner(self):
        entered, release = threading.Event(), threading.Event()
        with terminal(account_service=self.accounts) as (view, _):
            owner = view.tabs[0]

            def load(remote):
                entered.set()
                release.wait(2)
                return {
                    "title": "Loaded",
                    "messages": [{"role": "assistant", "text": "Original tab"}],
                }

            view.load_conversation = load
            view.resume_conversation("@chatgpt/default/one")
            self.assertTrue(entered.wait(1))
            self.assertFalse(view.loading_conversation)
            self.assertTrue(owner["syncing"])
            self.assertEqual(view.conversation_id, "@chatgpt/default/one")
            view.new_tab()
            self.assertEqual(view.active_tab, 1)
            self.assertFalse(view.loading_conversation)
            view.input.text = "new draft"
            release.set()
            until = time.monotonic() + 3
            while owner.get("syncing") and time.monotonic() < until:
                time.sleep(0.01)
            self.assertFalse(owner["loading"])
            self.assertEqual(owner["title"], "Loaded")
            self.assertEqual(view.input.text, "new draft")
            self.assertIsNone(view.conversation_id)
            self.assertEqual(view.messages, [])

    def test_completed_reply_does_not_turn_into_request_failure_if_archive_write_fails(self):
        from providers import ConversationBound, ReplaceText

        async def stream(request):
            yield ReplaceText("Answer")
            yield ConversationBound("one")

        self.accounts._providers[self.gemini] = Mock(stream=stream, close=AsyncMock())
        archive = self.api.get("archive")
        archive.repository.put = Mock(side_effect=OSError("disk full"))
        result = self.accounts.send(self.gemini, None, "Question", [], Mock(), Mock())
        self.assertTrue(result["conversation_id"].endswith("/one"))
        self.assertTrue(result["cache_warning"])

    def test_successful_file_send_archives_content_without_forwarding_local_paths(self):
        from providers import ConversationBound, ReplaceText

        root = Path(self.folder.name)
        source = root / "notes.txt"
        source.write_text("Research source")

        async def stream(request):
            self.assertNotIn("path", request.attachments[0])
            yield ReplaceText("Answer")
            yield ConversationBound("one")

        self.accounts._providers["chatgpt/default"] = Mock(stream=stream, close=AsyncMock())
        self.accounts._attachment_paths["chatgpt/default", "file1"] = str(source)
        self.accounts.send(
            "chatgpt/default",
            None,
            "Question",
            [{"id": "file1", "name": "notes.txt"}],
            Mock(),
            Mock(),
        )
        archive = self.api.get("archive")
        resource = archive.resources("chatgpt/default", "conversation", "one")[0]
        self.assertFalse(Path(resource["path"]).is_absolute())
        self.assertEqual((archive.files.root / resource["path"]).read_text(), "Research source")
        self.assertTrue(source.exists())

    def test_cached_startup_revalidates_active_chat_without_resetting_draft(self):
        self.accounts.load = Mock(return_value={"id": "one", "messages": []})
        with terminal(account_service=self.accounts) as (view, _):
            view._read_in_background = immediate
            view.conversation_id = "@chatgpt/default/one"
            view.input.text = "Continue drafting"
            view.refresh_open_conversation()
            self.accounts.load.assert_called_once_with("@chatgpt/default/one", "chatgpt/default")
            self.assertEqual(view.input.text, "Continue drafting")
            self.assertFalse(view.loading_conversation)

    def test_active_archive_pointer_survives_restart_and_can_be_cleared(self):
        archive = self.api.get("archive")
        archive.put(
            self.gemini, "conversation", "one", {"id": "one", "title": "Saved", "messages": []}
        )
        self.accounts.set_active(self.gemini, "one")
        self.assertEqual(self.accounts.cached(self.gemini)["title"], "Saved")
        self.accounts.set_active(self.gemini, None)
        self.assertIsNone(self.accounts.cached(self.gemini))
        self.assertEqual(archive.conversations(self.gemini)[0]["title"], "Saved")

    def test_sidebar_icons_open_labels_and_project_names_are_separate_fragments(self):
        with terminal(account_service=self.accounts) as (view, _):
            view.sidebar_items = [
                {"id": "@chatgpt/default/one", "provider_id": "chatgpt", "title": "GPT chat"},
                {"id": "@" + self.gemini + "/two", "provider_id": "gemini", "title": "Gemini chat"},
            ]
            view.project_items = {
                "@chatgpt/default/project": {"name": "Research", "provider_id": "chatgpt"}
            }
            view.tabs[0]["conversation_id"] = "@chatgpt/default/one"
            fragments = list(view.render_sidebar())
            self.assertTrue(
                any(
                    style == "class:provider.gemini" and text == "✦ "
                    for style, text, *_ in fragments
                )
            )
            self.assertTrue(
                any(
                    style == "class:provider.chatgpt" and text == "◎ "
                    for style, text, *_ in fragments
                )
            )
            self.assertTrue(
                any(
                    "class:sidebar.open" in style and "GPT chat" in text
                    for style, text, *_ in fragments
                )
            )
            self.assertFalse(any("@chatgpt/" in text for _, text, *_ in fragments))
            self.assertTrue(any("class:tab.open" in style for style, _, *_ in view.render_tabs()))

    def test_read_returns_saved_conversation_while_remote_is_slow(self):
        archive = self.api.get("archive")
        archive.put(
            self.gemini, "conversation", "one", {"id": "one", "title": "Saved", "messages": []}
        )
        release = threading.Event()

        async def get_chat(remote):
            await asyncio.to_thread(release.wait, 2)
            from providers.models import Conversation, Origin

            return Conversation(Origin("gemini", self.gemini.split("/")[1], remote), "Remote")

        self.accounts._providers[self.gemini] = Mock(get_chat=get_chat, close=AsyncMock())
        started = time.monotonic()
        try:
            self.assertEqual(self.accounts.load("one", self.gemini)["title"], "Saved")
            self.assertLess(time.monotonic() - started, 0.1)
        finally:
            release.set()
            until = time.monotonic() + 3
            while archive._pending and time.monotonic() < until:
                time.sleep(0.01)
