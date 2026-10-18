"""Deterministic races and slow-storage probes; no remote provider traffic."""

import asyncio
import tempfile
import threading
import unittest
from concurrent.futures import CancelledError, ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from accounts import identity
from bootstrap import create_application
from extensions.archive.repository import Files, SQLiteRepository
from extensions.archive.sync import Archive
from providers import ConversationBound, ReplaceText
from providers.models import FeatureState, Model, ModelCatalog
from tests.test_cli_interaction import terminal


class SyncConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        self.repo = SQLiteRepository(root / "archive.sqlite3")
        self.archive = Archive(self.repo, Files(root / "files"))
        self.addCleanup(self.archive.close)

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
        self.accounts.ensure_browser = Mock()
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

    def test_slow_archive_write_does_not_block_another_provider_stream_or_loop(self):
        archive = self.accounts.archive()
        entered, release = threading.Event(), threading.Event()
        original = archive.put

        def slow(account, kind, remote, value):
            if account == self.gemini and kind == "conversation":
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test barrier")
            return original(account, kind, remote, value)

        async def pulse():
            await asyncio.sleep(0)
            return "responsive"

        with patch.object(archive, "put", side_effect=slow), ThreadPoolExecutor(2) as pool:
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
        original = archive.put
        calls = []

        def slow(account, kind, remote, value):
            if kind == "conversation":
                calls.append(1)
                if len(calls) == 1:
                    entered.set()
                    if not release.wait(3):
                        raise TimeoutError("test barrier")
            return original(account, kind, remote, value)

        with patch.object(archive, "put", side_effect=slow), ThreadPoolExecutor(2) as pool:
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
        original_put, original_close = archive.put, self.accounts.close

        def slow(account, kind, remote, value):
            if kind == "conversation":
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test barrier")
            return original_put(account, kind, remote, value)

        def close_accounts():
            closing.set()
            return original_close()

        with (
            patch.object(archive, "put", side_effect=slow),
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
        archive = self.accounts.archive()
        with terminal(account_service=self.accounts) as (view, _):
            with (
                patch.object(archive, "refresh") as refresh,
                patch.object(view, "_read_in_background") as background,
                patch.object(view, "refresh_sidebar"),
                patch.object(view, "refresh_provider_catalog"),
            ):
                view.handle_command("/refresh")
                refresh.assert_not_called()
                self.assertIs(background.call_args.args[0], refresh)
                self.assertFalse(background.call_args.kwargs["session_bound"])

    def test_catalog_disk_write_does_not_block_the_provider_loop(self):
        entered, release = threading.Event(), threading.Event()
        backend = self.api.get("backend")
        original = backend.atomic_json_write
        self.accounts._providers[self.gemini].models = AsyncMock(
            return_value=ModelCatalog(FeatureState.AVAILABLE, (Model("fixture", "Fixture"),))
        )

        def slow(path, value):
            if path.name == "models.json":
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test barrier")
            return original(path, value)

        async def pulse():
            await asyncio.sleep(0)
            return "responsive"

        with (
            patch.object(backend, "atomic_json_write", side_effect=slow),
            ThreadPoolExecutor(1) as pool,
        ):
            discovering = pool.submit(self.accounts.models, self.gemini, True)
            try:
                self.assertTrue(entered.wait(1))
                self.assertEqual(self.accounts.call(pulse(), timeout=1), "responsive")
            finally:
                release.set()
            self.assertEqual(discovering.result(2).models[0].id, "fixture")

    def test_browser_startup_for_one_account_does_not_block_another(self):
        release = threading.Event()
        sessions = {}
        for key in self.entered:
            path = self.accounts.directory(key) / "cookies.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}")

            def launch(_url, key=key):
                self.entered[key].set()
                if not release.wait(3):
                    raise TimeoutError("test barrier")
                return {"browser": "started"}

            sessions[key] = SimpleNamespace(paths=SimpleNamespace(session_file=path), launch=launch)
        with patch.object(
            self.accounts,
            "ensure_browser",
            wraps=type(self.accounts).ensure_browser.__get__(self.accounts),
        ):
            with (
                patch.object(self.accounts, "session", side_effect=sessions.__getitem__),
                ThreadPoolExecutor(2) as pool,
            ):
                first = pool.submit(self.accounts.ensure_browser, self.gemini)
                try:
                    self.assertTrue(self.entered[self.gemini].wait(1))
                    second = pool.submit(self.accounts.ensure_browser, "chatgpt/default")
                    self.assertTrue(self.entered["chatgpt/default"].wait(1))
                finally:
                    release.set()
                first.result(2)
                second.result(2)

    def test_closed_account_service_cannot_restart_its_background_loop(self):
        self.accounts.close()
        with self.assertRaisesRegex(RuntimeError, "kapalı"):
            self.accounts._start()
        with self.assertRaisesRegex(RuntimeError, "kapalı"):
            self.accounts.call(asyncio.sleep(0))
