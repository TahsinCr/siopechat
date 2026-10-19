"""Deletion, tombstone recovery and cached-history merging without remote traffic."""

import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from accounts import identity
from bootstrap import create_application
from extensions.archive.history import merge_conversation
from extensions.archive.repository import Files, SQLiteRepository
from extensions.archive.sync import Archive
from providers import Capability
from providers.errors import RemoteNotFound
from providers.gemini import GeminiProvider
from tests.test_cli_interaction import terminal


def message(role, text, **extra):
    return {"role": role, "text": text, **extra}


class HistoryTests(unittest.TestCase):
    def test_older_remote_history_is_prepended_and_new_local_turns_remain(self):
        remote = {
            "messages": [message("user", "old", id="u1"), message("assistant", "answer", id="a1")]
        }
        local = {
            "messages": [
                message("user", "new", local_pending=True),
                message("assistant", "new answer", local_pending=True),
            ]
        }
        merged = merge_conversation(remote, local)
        self.assertEqual(
            [m["text"] for m in merged["messages"]], ["old", "answer", "new", "new answer"]
        )
        self.assertEqual(merge_conversation(remote, merged), merged)

    def test_confirmed_turns_are_not_duplicated_and_local_paths_and_thoughts_survive(self):
        remote = {
            "messages": [message("user", "hello", id="u"), message("assistant", "answer", id="a")]
        }
        local = {
            "messages": [
                message("user", "hello", local_pending=True, resources=[{"path": "files/a"}]),
                message("reasoning", "summary", local_pending=True),
                message("assistant", "answer", local_pending=True),
            ]
        }
        merged = merge_conversation(remote, local)
        self.assertEqual([m["text"] for m in merged["messages"]], ["hello", "summary", "answer"])
        self.assertEqual(merged["messages"][0]["resources"][0]["path"], "files/a")
        self.assertNotIn("local_pending", merged["messages"][-1])
        self.assertEqual(merge_conversation(remote, merged), merged)

    def test_repeated_prompt_occurrences_and_edited_provider_message_keep_order(self):
        remote = {
            "messages": [
                message("user", "again", id="1"),
                message("assistant", "one", id="2"),
                message("user", "again", id="3"),
                message("assistant", "edited", id="4"),
            ]
        }
        local = {
            "messages": [
                message("user", "again", id="1"),
                message("assistant", "one", id="2"),
                message("user", "again", id="3"),
                message("assistant", "old", id="4"),
                message("user", "next", local_pending=True),
            ]
        }
        result = merge_conversation(remote, local)
        self.assertEqual(
            [x["text"] for x in result["messages"]], ["again", "one", "again", "edited", "next"]
        )

    def test_partially_visible_remote_answer_is_not_duplicated_with_complete_local_answer(self):
        remote = {
            "messages": [message("user", "question", id="u"), message("assistant", "part", id="a")]
        }
        local = {
            "messages": [
                message("user", "question", local_pending=True),
                message("assistant", "partial completed answer", local_pending=True),
            ]
        }
        merged = merge_conversation(remote, local)
        self.assertEqual(
            [m["text"] for m in merged["messages"]], ["question", "partial completed answer"]
        )

    def test_partial_remote_window_does_not_discard_older_archived_messages(self):
        local = {"messages": [message("user", str(i), id="m" + str(i)) for i in range(1200)]}
        remote = {
            "history_complete": False,
            "messages": [message("user", str(i), id="m" + str(i)) for i in range(600, 1220)],
        }
        merged = merge_conversation(remote, local)
        self.assertEqual([m["text"] for m in merged["messages"]], [str(i) for i in range(1220)])


class DeleteAndCacheTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.api = create_application(self.root)
        self.addCleanup(self.api.runtime.close)
        self.accounts = self.api.get("accounts")
        self.archive = self.accounts.archive()
        self.key = self.accounts.add("gemini", "Fixture")
        self.provider = Mock(
            capabilities={Capability.DELETE, Capability.PROJECT_DELETE},
            close=AsyncMock(),
            delete_chat=AsyncMock(),
            delete_project=AsyncMock(),
        )
        self.accounts._providers[self.key] = self.provider

    def seed(self):
        chat = {
            "id": identity(self.key, "c_abc"),
            "project_id": identity(self.key, "notebooks/abcdef"),
            "messages": [message("user", "saved")],
            "title": "saved",
        }
        self.archive.put(self.key, "conversation", "c_abc", chat)
        self.archive.put(self.key, "chats", "", {"items": [chat], "cursor": None})
        self.archive.put(
            self.key,
            "containers",
            "",
            {"items": [{"id": identity(self.key, "notebooks/abcdef"), "name": "Notebook"}]},
        )
        return chat

    def test_remote_chat_delete_removes_all_page_references_and_blocks_legacy_resurrection(self):
        chat = self.seed()
        self.accounts.cache(self.key).cache_conversation(dict(chat, id="c_abc"))
        result = self.accounts.delete(chat["id"], self.key)
        self.provider.delete_chat.assert_awaited_once_with("c_abc")
        self.assertIn(chat["id"], result["removed"])
        self.assertIsNone(self.accounts.cached(self.key, "c_abc"))
        self.assertIsNone(self.accounts.cache(self.key).load_cached_conversation("c_abc"))
        self.assertTrue(
            self.accounts.cache(self.key)
            .conversation_cache_path("c_abc")
            .with_suffix(".deleted")
            .is_file()
        )
        self.assertEqual(self.archive.get(self.key, "chats", "")["items"], [])
        self.archive.put(self.key, "chats", "", {"items": [chat]})
        self.assertEqual(self.archive.get(self.key, "chats", "")["items"], [])
        cold = self.archive.read(self.key, "chats", "next", lambda: {"items": [chat]})
        self.assertEqual(cold["items"], [])
        reopened = Archive(
            SQLiteRepository(self.root / "data/archive.sqlite3"), Files(self.root / "data/files")
        )
        try:
            self.assertTrue(reopened.deleted(self.key, "conversation", "c_abc"))
        finally:
            reopened.close()

    def test_project_delete_cascades_known_members_and_preserves_other_accounts(self):
        chat = self.seed()
        self.archive.put("chatgpt/default", "conversation", "c_abc", {"title": "other"})
        result = self.accounts.delete(identity(self.key, "notebooks/abcdef"), self.key, "container")
        self.assertIn(chat["id"], result["removed"])
        self.assertIsNone(self.archive.get(self.key, "conversation", "c_abc"))
        self.assertEqual(
            self.archive.get("chatgpt/default", "conversation", "c_abc")["title"], "other"
        )

    def test_server_confirmed_missing_chat_is_tombstoned_but_network_failure_is_not(self):
        self.seed()
        self.provider.get_chat = AsyncMock(side_effect=RemoteNotFound("deleted upstream"))
        with self.assertRaises(RemoteNotFound):
            self.accounts.load(identity(self.key, "c_missing"), self.key, force=True)
        self.assertTrue(self.archive.deleted(self.key, "conversation", "c_missing"))
        self.provider.get_chat.side_effect = RuntimeError("offline")
        with self.assertRaisesRegex(RuntimeError, "offline"):
            self.accounts.load(identity(self.key, "c_network"), self.key, force=True)
        self.assertFalse(self.archive.deleted(self.key, "conversation", "c_network"))

    def test_delete_already_missing_upstream_still_cleans_the_archive(self):
        chat = self.seed()
        self.provider.delete_chat.side_effect = RemoteNotFound("already deleted")
        self.accounts.delete(chat["id"], self.key)
        self.assertIsNone(self.accounts.cached(self.key, "c_abc"))

    def test_remote_failure_does_not_remove_local_history(self):
        chat = self.seed()
        self.provider.delete_chat.side_effect = RuntimeError("offline")
        with self.assertRaisesRegex(RuntimeError, "offline"):
            self.accounts.delete(chat["id"], self.key)
        self.assertEqual(self.accounts.cached(self.key, "c_abc")["title"], "saved")
        self.assertFalse(self.archive.deleted(self.key, "conversation", "c_abc"))

    def test_inflight_history_fetch_merges_new_send_instead_of_discarding_older_history(self):
        entered, release = threading.Event(), threading.Event()

        def fetch():
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test barrier")
            return {"messages": [message("user", "older", id="old")]}

        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.archive.read, self.key, "conversation", "c_abc", fetch)
            try:
                self.assertTrue(entered.wait(1))
                self.archive.put(
                    self.key,
                    "conversation",
                    "c_abc",
                    {"messages": [message("user", "new", local_pending=True)]},
                )
            finally:
                release.set()
            self.assertEqual([x["text"] for x in future.result(2)["messages"]], ["older", "new"])

    def test_deleted_record_cannot_return_from_inflight_fetch(self):
        self.seed()
        entered, release = threading.Event(), threading.Event()

        def fetch():
            entered.set()
            release.wait(3)
            return {"messages": [message("user", "stale")]}

        self.archive.read(self.key, "conversation", "c_abc", fetch)
        try:
            self.assertTrue(entered.wait(1))
            self.accounts.delete(identity(self.key, "c_abc"), self.key)
        finally:
            release.set()
        with self.archive._lock:
            pending = list(self.archive._pending.values())
        for future in pending:
            future.result(2)
        self.assertIsNone(self.archive.get(self.key, "conversation", "c_abc"))

    def test_cached_resume_shows_history_and_keeps_composer_usable_before_network(self):
        self.provider.capabilities = frozenset()
        chat = self.seed()
        with terminal(account_service=self.accounts) as (view, _):
            with (
                patch.object(view, "_read_in_background") as background,
                patch.object(view, "refresh_provider_catalog"),
            ):
                view.resume_conversation(chat["id"])
                self.assertFalse(view.loading_conversation)
                deadline = time.monotonic() + 2
                while (not view.messages or not background.called) and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(view.messages[0]["text"], "saved")
                self.assertEqual(view.conversation_id, chat["id"])
                background.assert_called_once()
                self.assertTrue(view.tabs[view.active_tab]["syncing"])

    def test_cold_history_does_not_replace_an_inflight_response_or_a_typed_draft(self):
        entered, release = threading.Event(), threading.Event()
        self.provider.capabilities = frozenset()

        def load(value, key, force=False):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test barrier")
            return {"messages": [message("assistant", "old history")], "title": "Hydrated"}

        with terminal(account_service=self.accounts) as (view, _):
            with (
                patch.object(self.accounts, "load", side_effect=load),
                patch.object(view, "refresh_provider_catalog"),
            ):
                view.resume_conversation(identity(self.key, "c_abc"))
                try:
                    self.assertTrue(entered.wait(1))
                    self.assertFalse(view.loading_conversation)
                    self.assertEqual(view.conversation_id, identity(self.key, "c_abc"))
                    view.input.text = "typed draft"
                    view.busy = True
                    view.tabs[view.active_tab]["busy"] = True
                    view.messages = [
                        message("user", "new question"),
                        message("assistant", "streaming"),
                    ]
                    view.tabs[view.active_tab]["messages"] = view.messages
                finally:
                    release.set()
                until = time.monotonic() + 2
                while view.tabs[view.active_tab].get("syncing") and time.monotonic() < until:
                    time.sleep(0.005)
                self.assertEqual(view.messages[-1]["text"], "streaming")
                self.assertEqual(view.input.text, "typed draft")
                view.busy = False
                view.tabs[view.active_tab]["busy"] = False

    def test_delete_confirmation_defaults_to_cancel(self):
        self.provider.capabilities = {Capability.DELETE}
        chat = self.seed()
        with terminal(account_service=self.accounts) as (view, _):
            view.conversation_id = chat["id"]
            with patch.object(view.forms, "open") as opened:
                view.delete_record()
                self.assertEqual(opened.call_args.args[1][0][2], "cancel")
                opened.call_args.args[2]({"confirm": "cancel"})
                self.provider.delete_chat.assert_not_awaited()


class GeminiHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_listing_refreshes_sdk_cache_and_returns_real_pagination(self):
        provider = GeminiProvider(Path("unused"))
        client = SimpleNamespace(
            _fetch_recent_chats=AsyncMock(),
            list_chats=lambda: [
                SimpleNamespace(cid="c_" + str(i), title=str(i), timestamp=i) for i in range(5)
            ],
        )
        provider._factory = lambda *_: client
        provider._connect = AsyncMock(return_value=client)
        first = await provider.list_chats(limit=2)
        second = await provider.list_chats(first.next_cursor, limit=2)
        self.assertEqual([x.title for x in first.items], ["0", "1"])
        self.assertEqual([x.title for x in second.items], ["2", "3"])
        self.assertEqual(second.next_cursor, "4")
        self.assertEqual(client._fetch_recent_chats.await_count, 2)

    async def test_message_ids_do_not_shift_when_older_history_arrives(self):
        provider = GeminiProvider(Path("unused"))
        output = SimpleNamespace(thoughts=None, metadata=["c_abc", "r_stable"])
        user = SimpleNamespace(role="user", text="question", model_output=None)
        assistant = SimpleNamespace(role="model", text="answer", model_output=output)
        client = SimpleNamespace(
            read_chat=AsyncMock(return_value=SimpleNamespace(turns=[assistant, user])),
            list_chats=lambda: [],
        )
        provider._factory = lambda *_: client
        provider._connect = AsyncMock(return_value=client)
        first = await provider.get_chat("c_abc")
        old_output = SimpleNamespace(thoughts=None, metadata=["c_abc", "r_old"])
        client.read_chat.return_value = SimpleNamespace(
            turns=[
                assistant,
                user,
                SimpleNamespace(role="model", text="old answer", model_output=old_output),
                SimpleNamespace(role="user", text="old question", model_output=None),
            ]
        )
        second = await provider.get_chat("c_abc")
        self.assertEqual([m.id for m in first.messages], [m.id for m in second.messages[-2:]])


class NativeChatDeletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_deletion_requires_matching_acknowledgement(self):
        from providers.errors import ProviderError
        from providers.gemini.browser import NativeBrowser

        browser = NativeBrowser("http://127.0.0.1:9231")

        async def events(mode, **options):
            self.assertEqual(mode, "delete_chat")
            self.assertEqual(options["conversation_id"], "c_abc")
            yield {"type": "deleted", "id": "c_abc"}

        browser.events = events
        await browser.delete_chat("c_abc")
        self.assertTrue(browser.authenticated)

        async def missing_ack(*args, **kwargs):
            yield {"type": "deleted", "id": "c_other"}

        browser.events = missing_ack
        with self.assertRaises(ProviderError):
            await browser.delete_chat("c_abc")


class NotebookDeleteVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_index_may_lag_deletion_without_resubmitting_the_write(self):
        project = SimpleNamespace(
            name="Fixture", origin=SimpleNamespace(remote_id="notebooks/abcdef")
        )
        notebooks = SimpleNamespace(
            cdp_url="http://127.0.0.1:9231",
            get=AsyncMock(return_value=project),
            list=AsyncMock(
                side_effect=[SimpleNamespace(items=[project]), SimpleNamespace(items=[])]
            ),
        )
        provider = GeminiProvider(Path("unused"), notebooks=notebooks)
        with (
            patch.object(provider.editor, "write", new_callable=AsyncMock) as delete,
            patch("providers.gemini.adapter.asyncio.sleep", new_callable=AsyncMock) as wait,
        ):
            await provider.delete_project("notebooks/abcdef")
            delete.assert_awaited_once()
            wait.assert_awaited_once_with(0.25)
        self.assertEqual(notebooks.list.await_count, 2)
