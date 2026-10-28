"""Deterministic races and slow-storage probes; no remote provider traffic."""

import asyncio
import hashlib
import tempfile
import threading
import unittest
from concurrent.futures import CancelledError, ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from accounts import identity
from extensions.archive.repository import Files, SQLiteRepository, encode
from extensions.archive.sync import Archive
from providers import ConversationBound, ReplaceText
from providers.models import FeatureState, Model, ModelCatalog
from tests.account_fixture import configured_application as create_application
from tests.tui_support import terminal


class SyncConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        self.repo = SQLiteRepository(root / "archive.sqlite3")
        self.archive = Archive(self.repo, Files(root / "files"))
        self.addCleanup(self.archive.close)

    def test_rename_survives_older_history_after_listing_confirms_new_title(self):
        account, remote = "claude/a", "chat"
        history = {
            "id": remote,
            "title": "Old",
            "messages": [{"id": "one", "role": "assistant", "text": "Existing answer"}],
        }
        self.archive.put(account, "conversation", remote, history)
        entered, release = threading.Event(), threading.Event()

        def fetch():
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test barrier")
            return dict(
                history,
                messages=history["messages"]
                + [{"id": "two", "role": "assistant", "text": "New remote answer"}],
            )

        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(
                self.archive.read, account, "conversation", remote, fetch, fresh=True
            )
            try:
                self.assertTrue(entered.wait(1))
                self.archive.rename_conversation(account, remote, "Renamed")
                self.archive.put(
                    account, "chats", "", {"items": [{"id": remote, "title": "Renamed"}]}
                )
            finally:
                release.set()
            result = pending.result(3)
        self.assertEqual(result["title"], "Renamed")
        self.assertEqual([message["id"] for message in result["messages"]], ["one", "two"])
        self.assertEqual(self.repo.get(account, "conversation", remote)["title"], "Renamed")

    def test_save_turn_preserves_history_hydrated_after_the_sender_read(self):
        self.archive.put(
            "gemini/a",
            "conversation",
            "one",
            {"messages": [{"id": "old", "role": "user", "text": "Hydrated older turn"}]},
        )
        self.archive.save_turn(
            "gemini/a",
            "one",
            {
                "messages": [
                    {"role": "user", "text": "New question", "local_pending": True},
                    {"role": "assistant", "text": "Accepted answer", "local_pending": True},
                ]
            },
        )
        self.assertEqual(
            [m["text"] for m in self.archive.get("gemini/a", "conversation", "one")["messages"]],
            ["Hydrated older turn", "New question", "Accepted answer"],
        )

    def test_cold_waiter_receives_new_local_value_instead_of_stale_remote_result(self):
        entered, release = threading.Event(), threading.Event()

        def fetch():
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test barrier")
            return {"title": "old remote"}

        with ThreadPoolExecutor(1) as pool:
            waiting = pool.submit(self.archive.read, "gemini/a", "conversation", "one", fetch)
            try:
                self.assertTrue(entered.wait(1))
                self.archive.put("gemini/a", "conversation", "one", {"title": "new local"})
            finally:
                release.set()
            self.assertEqual(waiting.result(2)["title"], "new local")

    def test_long_history_refresh_merges_only_the_changed_sql_tail(self):
        account, remote = "gemini/a", "long-chat"
        key = account, "conversation", remote
        history = [{"id": f"m{i}", "role": "user", "text": f"archived-{i}"} for i in range(1000)]
        self.archive.put(
            account,
            "conversation",
            remote,
            {"history_complete": True, "messages": history},
        )
        entered, release = threading.Event(), threading.Event()

        def fetch():
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test barrier")
            return {"history_complete": True, "messages": history}

        with (
            patch.object(self.repo, "get", wraps=self.repo.get) as full_read,
            patch.object(self.repo, "messages", wraps=self.repo.messages) as page_read,
        ):
            self.archive.read(account, "conversation", remote, fetch, force=True, window=True)
            self.assertTrue(entered.wait(1))
            future = self.archive._pending[key]
            try:
                self.archive.save_turn(
                    account,
                    remote,
                    {
                        "messages": [
                            {"role": "user", "text": "new question", "local_pending": True},
                            {
                                "role": "assistant",
                                "text": "accepted answer",
                                "local_pending": True,
                            },
                        ]
                    },
                )
            finally:
                release.set()
            future.result(3)

        self.assertEqual(full_read.call_count, 0)
        self.assertTrue(page_read.called)
        args, kwargs = page_read.call_args_list[0]
        self.assertEqual(kwargs.get("offset", args[3] if len(args) > 3 else None), 920)
        window = self.archive.conversation_window(account, remote, limit=3)
        self.assertEqual(
            [item["text"] for item in window["messages"]],
            ["archived-999", "new question", "accepted answer"],
        )

    def test_partial_provider_refresh_preserves_sql_prefix_and_concurrent_send(self):
        account, remote = "gemini/a", "partial-chat"
        key = account, "conversation", remote
        history = [{"id": f"m{i}", "role": "user", "text": f"archived-{i}"} for i in range(5000)]
        self.archive.put(
            account,
            "conversation",
            remote,
            {"history_complete": True, "messages": history},
        )
        entered, release = threading.Event(), threading.Event()

        def fetch():
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test barrier")
            return {"history_complete": False, "messages": history[2500:]}

        with (
            patch.object(self.repo, "get", wraps=self.repo.get) as full_read,
            patch.object(
                self.repo, "partial_history_window", wraps=self.repo.partial_history_window
            ) as merge_window,
        ):
            self.archive.read(account, "conversation", remote, fetch, force=True, window=True)
            self.assertTrue(entered.wait(1))
            future = self.archive._pending[key]
            try:
                self.archive.save_turn(
                    account,
                    remote,
                    {
                        "messages": [
                            {"role": "user", "text": "new question", "local_pending": True},
                            {
                                "role": "assistant",
                                "text": "accepted answer",
                                "local_pending": True,
                            },
                        ]
                    },
                )
            finally:
                release.set()
            future.result(3)

        self.assertEqual(full_read.call_count, 0)
        merge_window.assert_called_once_with(account, remote, 2420, prefix=81, tail=512)
        first = self.archive.conversation_window(account, remote, start=0, limit=1)
        tail = self.archive.conversation_window(account, remote, limit=3)
        self.assertEqual(first["messages"][0]["text"], "archived-0")
        self.assertEqual(tail["history_window"]["total"], 5002)
        self.assertEqual(
            [item["text"] for item in tail["messages"]],
            ["archived-4999", "new question", "accepted answer"],
        )
        value = self.repo.get(account, "conversation", remote)
        with self.repo._connect() as db:
            digest = db.execute(
                "SELECT digest FROM records WHERE account=? AND kind='conversation' AND remote=?",
                (account, remote),
            ).fetchone()[0]
        self.assertEqual(digest, hashlib.sha256(encode(value).encode()).hexdigest())

    def test_gapped_partial_provider_page_cannot_delete_archived_tail(self):
        account, remote = "gemini/a", "gapped-partial-chat"
        key = account, "conversation", remote
        history = [{"id": f"m{i}", "role": "user", "text": f"archived-{i}"} for i in range(5000)]
        self.archive.put(
            account,
            "conversation",
            remote,
            {"history_complete": True, "messages": history},
        )
        entered, release = threading.Event(), threading.Event()

        def fetch():
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test barrier")
            return {"history_complete": False, "messages": history[2500:2580]}

        with patch.object(
            self.repo, "replace_conversation_tail", wraps=self.repo.replace_conversation_tail
        ) as replace_tail:
            self.archive.read(account, "conversation", remote, fetch, force=True, window=True)
            self.assertTrue(entered.wait(1))
            future = self.archive._pending[key]
            release.set()
            future.result(3)

        replace_tail.assert_not_called()
        first = self.archive.conversation_window(account, remote, start=0, limit=1)
        tail = self.archive.conversation_window(account, remote, limit=1)
        self.assertEqual(first["messages"][0]["text"], "archived-0")
        self.assertEqual(tail["messages"][0]["text"], "archived-4999")
        self.assertEqual(tail["history_window"]["total"], 5000)

    def test_cold_snapshot_cannot_replace_a_concurrent_new_write_in_hot_cache(self):
        self.archive.put("gemini/a", "conversation", "one", {"title": "old"})
        self.archive._hot.clear()
        self.archive._size = 0
        read, release = threading.Event(), threading.Event()
        original = self.repo.get

        def blocked(*key):
            value = original(*key)
            read.set()
            if not release.wait(3):
                raise TimeoutError("test barrier")
            return value

        with patch.object(self.repo, "get", side_effect=blocked), ThreadPoolExecutor(2) as pool:
            reading = pool.submit(self.archive.get, "gemini/a", "conversation", "one")
            try:
                self.assertTrue(read.wait(1))
                writing = pool.submit(
                    self.archive.put, "gemini/a", "conversation", "one", {"title": "new"}
                )
            finally:
                release.set()
            reading.result(2)
            writing.result(2)
        self.assertEqual(self.archive.get("gemini/a", "conversation", "one")["title"], "new")

    def test_failed_cold_read_is_throttled_and_explicit_refresh_recovers(self):
        fetch = Mock(side_effect=ValueError("offline"))
        for _ in range(12):
            with self.assertRaisesRegex((ValueError, RuntimeError), "offline"):
                self.archive.read("gemini/a", "conversation", "one", fetch)
        self.assertEqual(fetch.call_count, 1)
        fetch.side_effect = None
        fetch.return_value = {"title": "recovered"}
        value = self.archive.read("gemini/a", "conversation", "one", fetch, force=True)
        self.assertEqual(value["title"], "recovered")
        self.assertEqual(fetch.call_count, 2)

    def test_repository_remains_open_until_running_fetch_finishes_after_close(self):
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()

        def fetch():
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test barrier")
            return {"title": "remote"}

        with patch.object(self.repo, "close", side_effect=closed.set):
            self.archive.read("gemini/a", "conversation", "one", fetch, wait=False)
            try:
                self.assertTrue(entered.wait(1))
                self.archive.close()
                self.assertFalse(closed.is_set())
            finally:
                release.set()
            self.assertTrue(closed.wait(2))
            self.assertIsNone(self.repo.get("gemini/a", "conversation", "one"))

    def test_async_close_waits_for_running_fetches(self):
        entered, release, repository_closed = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )

        def fetch():
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test barrier")
            return {"title": "remote"}

        real_close = self.repo.close

        def close_repository():
            repository_closed.set()
            real_close()

        with patch.object(self.repo, "close", side_effect=close_repository):
            self.archive.read("gemini/a", "conversation", "async-close", fetch, wait=False)
            self.assertTrue(entered.wait(1))
            timer = threading.Timer(0.05, release.set)
            timer.start()
            try:
                asyncio.run(self.archive.aclose())
            finally:
                release.set()
                timer.join()

        self.assertTrue(repository_closed.is_set())
        self.assertFalse(self.archive._pending)

    def test_worker_and_queue_limits_hold_during_a_refresh_burst(self):
        release = threading.Event()
        counter_lock = threading.Lock()
        active = peak = 0

        def fetch():
            nonlocal active, peak
            with counter_lock:
                active += 1
                peak = max(peak, active)
            try:
                if not release.wait(3):
                    raise TimeoutError("test barrier")
                return {"title": "remote"}
            finally:
                with counter_lock:
                    active -= 1

        try:
            for i in range(40):
                self.archive.put("gemini/a", "conversation", str(i), {"title": "saved"})
                self.archive.read("gemini/a", "conversation", str(i), fetch)
            with self.archive._lock:
                waiting = list(self.archive._pending.values())
                self.assertEqual(len(waiting), 32)
            self.archive.refresh()
            self.assertLessEqual(peak, 3)
        finally:
            release.set()
        for future in waiting:
            future.result(3)
        self.assertLessEqual(peak, 3)

    def test_hot_snapshots_are_isolated_from_caller_mutations(self):
        value = {"messages": [{"text": "original"}]}
        self.archive.put("gemini/a", "conversation", "one", value)
        value["messages"][0]["text"] = "changed"
        first = self.archive.get("gemini/a", "conversation", "one")
        first["messages"].clear()
        self.assertEqual(
            self.archive.get("gemini/a", "conversation", "one")["messages"][0]["text"], "original"
        )


class AccountConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.api = create_application(Path(self.folder.name))
        self.addCleanup(self.api.runtime.close)
        self.accounts = self.api.get("accounts")
        self.gemini = self.accounts.add("gemini", "Concurrency fixture")
        self.entered = {key: threading.Event() for key in (self.gemini, "chatgpt/default")}
        for key in self.entered:

            async def stream(request, key=key):
                self.entered[key].set()
                yield ConversationBound("c_fixture")
                yield ReplaceText("answer", "final")

            self.accounts._providers[key] = Mock(stream=stream, close=AsyncMock(), browser=None)

    def send(self, key):
        return self.accounts.send(
            key, identity(key, "c_fixture"), "question", [], lambda _: None, lambda _: None
        )

    def test_sidebar_rename_uses_conversation_account_instead_of_active_account(self):
        target = self.accounts._providers[self.gemini]
        target.rename_chat = AsyncMock()
        active = self.accounts._providers["chatgpt/default"]
        active.rename_chat = AsyncMock()
        archive = self.accounts.archive()
        for key in (self.gemini, "chatgpt/default"):
            value = {"id": "same-id", "title": "Original", "messages": []}
            archive.put(key, "conversation", "same-id", value)
            self.accounts.repository.cache_put(key, "conversation", "same-id", value)

        self.accounts.callbacks("chatgpt/default")["rename_conversation"](
            identity(self.gemini, "same-id"), "Renamed"
        )

        target.rename_chat.assert_awaited_once_with("same-id", "Renamed")
        active.rename_chat.assert_not_called()
        for key, title in ((self.gemini, "Renamed"), ("chatgpt/default", "Original")):
            self.assertEqual(archive.get(key, "conversation", "same-id")["title"], title)
            self.assertEqual(
                self.accounts.repository.cache_get(key, "conversation", "same-id")["title"],
                title,
            )

    def test_background_catalog_verification_does_not_resume_a_paused_send_account(self):
        account = tuple(self.gemini.split("/", 1))

        async def block():
            self.api.tasks._block_account(account, None)

        async def paused():
            return account in self.api.tasks._blocked

        self.accounts.call(block())
        self.accounts._remember_verification(self.gemini)
        self.assertTrue(self.accounts.call(paused()))
        self.accounts._remember_verification(self.gemini, resume=True)
        self.assertFalse(self.accounts.call(paused()))

    def test_unexpected_local_archive_error_does_not_reject_an_accepted_turn(self):
        with patch.object(
            self.accounts.archive(), "save_turn", side_effect=RuntimeError("local corruption")
        ):
            result = self.send(self.gemini)
        self.assertEqual(result["conversation_id"], identity(self.gemini, "c_fixture"))
        self.assertTrue(result["cache_warning"])

    def test_download_rejects_another_accounts_conversation_before_provider_access(self):
        with patch.object(self.accounts, "provider") as provider:
            with self.assertRaises(ValueError):
                self.accounts.resolve_download(
                    self.gemini, "https://example.com/file", identity("chatgpt/default", "chat")
                )
            provider.assert_not_called()

    def test_cancel_keeps_account_busy_until_provider_cleanup_finishes(self):
        started, draining, release = threading.Event(), threading.Event(), threading.Event()
        handles = []

        async def stream(request):
            started.set()
            try:
                yield ConversationBound("c_fixture")
                await asyncio.Event().wait()
            finally:
                draining.set()
                await asyncio.to_thread(release.wait, 3)

        self.accounts._providers[self.gemini].stream = stream
        with ThreadPoolExecutor(1) as pool:
            send = pool.submit(
                self.accounts.send,
                self.gemini,
                None,
                "Question",
                [],
                lambda _: None,
                handles.append,
            )
            try:
                self.assertTrue(started.wait(1))
                handles[0].close()
                self.assertTrue(draining.wait(1))
                handles[0].close()
                self.assertFalse(send.done())
                self.assertGreater(self.accounts._sending[self.gemini], 0)
                with self.assertRaises((RuntimeError, ValueError)):
                    self.accounts.set_enabled("gemini", False)
            finally:
                release.set()
            with self.assertRaises(CancelledError):
                send.result(2)
        self.assertEqual(self.accounts._sending[self.gemini], 0)

    def test_slow_archive_write_does_not_block_another_provider_stream_or_loop(self):
        archive = self.accounts.archive()
        entered, release = threading.Event(), threading.Event()
        original = archive.save_turn

        def slow(account, remote, value):
            if account == self.gemini:
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test barrier")
            return original(account, remote, value)

        async def pulse():
            await asyncio.sleep(0)
            return "responsive"

        with patch.object(archive, "save_turn", side_effect=slow), ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.send, self.gemini)
            try:
                self.assertTrue(entered.wait(1))
                second = pool.submit(self.send, "chatgpt/default")
                self.assertTrue(self.entered["chatgpt/default"].wait(1))
                self.assertEqual(self.accounts.call(pulse(), timeout=1), "responsive")
            finally:
                release.set()
            first.result(2)
            second.result(2)

    def test_same_account_waits_for_persistence_and_preserves_both_turns(self):
        archive = self.accounts.archive()
        entered, release = threading.Event(), threading.Event()
        original = archive.save_turn
        calls = []

        def slow(account, remote, value):
            calls.append(1)
            if len(calls) == 1:
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test barrier")
            return original(account, remote, value)

        with patch.object(archive, "save_turn", side_effect=slow), ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.send, self.gemini)
            try:
                self.assertTrue(entered.wait(1))
                second = pool.submit(self.send, self.gemini)
            finally:
                release.set()
            first.result(2)
            second.result(2)
        self.assertEqual(len(self.accounts.cached(self.gemini, "c_fixture")["messages"]), 4)

    def test_runtime_drains_accepted_response_before_closing_archive_extension(self):
        archive = self.accounts.archive()
        entered, release, closing = threading.Event(), threading.Event(), threading.Event()
        original_put, original_close = archive.save_turn, self.accounts.close

        def slow(account, remote, value):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test barrier")
            return original_put(account, remote, value)

        def close_accounts():
            closing.set()
            return original_close()

        with (
            patch.object(archive, "save_turn", side_effect=slow),
            patch.object(self.accounts, "close", side_effect=close_accounts),
            ThreadPoolExecutor(2) as pool,
        ):
            sending = pool.submit(self.send, self.gemini)
            try:
                self.assertTrue(entered.wait(1))
                shutdown = pool.submit(self.api.runtime.close)
                self.assertTrue(closing.wait(1))
                self.assertFalse(archive.closed)
            finally:
                release.set()
            shutdown.result(3)
            try:
                sending.result(2)
            except CancelledError:
                pass
        stored = archive.repository.get(self.gemini, "conversation", "c_fixture")
        self.assertEqual(len(stored["messages"]), 2)

    def test_refresh_command_schedules_archive_work_outside_the_ui_thread(self):
        with terminal(account_service=self.accounts) as (view, _):
            with (
                patch.object(self.accounts, "synchronize") as refresh,
                patch.object(view, "_read_in_background") as background,
                patch.object(view, "refresh_sidebar"),
                patch.object(view, "refresh_provider_catalog"),
            ):
                view.handle_command("/refresh")
                refresh.assert_not_called()
                background.call_args.args[0]()
                refresh.assert_called_once()
                self.assertFalse(background.call_args.kwargs["session_bound"])

    def test_catalog_disk_write_does_not_block_the_provider_loop(self):
        entered, release = threading.Event(), threading.Event()
        original = self.accounts.repository.put
        self.accounts._providers[self.gemini].models = AsyncMock(
            return_value=ModelCatalog(FeatureState.AVAILABLE, (Model("fixture", "Fixture"),))
        )

        def slow(key, kind, value):
            if kind == "catalog":
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test barrier")
            return original(key, kind, value)

        async def pulse():
            await asyncio.sleep(0)
            return "responsive"

        with (
            patch.object(self.accounts.repository, "put", side_effect=slow),
            ThreadPoolExecutor(1) as pool,
        ):
            discovering = pool.submit(self.accounts.models, self.gemini, True)
            try:
                self.assertTrue(entered.wait(1))
                self.assertEqual(self.accounts.call(pulse(), timeout=1), "responsive")
            finally:
                release.set()
            self.assertEqual(discovering.result(2).models[0].id, "fixture")

    def test_http_discovery_for_one_account_does_not_block_another(self):
        from providers.models import FeatureState, Model, ModelCatalog

        release = threading.Event()
        catalog = ModelCatalog(FeatureState.AVAILABLE, (Model("fixture", "Fixture"),))
        for key in self.entered:

            async def models(key=key, *, force=False):
                self.entered[key].set()
                await asyncio.to_thread(release.wait, 3)
                return catalog

            self.accounts._providers[key] = SimpleNamespace(models=models, close=AsyncMock())
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.accounts.models, self.gemini, True)
            try:
                self.assertTrue(self.entered[self.gemini].wait(1))
                second = pool.submit(self.accounts.models, "chatgpt/default", True)
                self.assertTrue(self.entered["chatgpt/default"].wait(1))
            finally:
                release.set()
            self.assertEqual(first.result(2).models[0].id, "fixture")
            self.assertEqual(second.result(2).models[0].id, "fixture")

    def test_closed_account_service_cannot_restart_its_background_loop(self):
        self.accounts.close()
        with self.assertRaisesRegex(RuntimeError, "kapalı"):
            self.accounts._start()
        with self.assertRaisesRegex(RuntimeError, "kapalı"):
            self.accounts.call(asyncio.sleep(0))
