"""UI navigation must not wait for local storage, Markdown rendering or pointer fsync."""

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bootstrap import create_application
from cli.markdown import MessageRenderer
from tests.test_cli_interaction import terminal


class OpeningPerformanceTests(unittest.TestCase):
    def test_cached_open_does_not_block_ui_and_prepares_the_complete_transcript(self):
        with tempfile.TemporaryDirectory() as folder:
            api = create_application(Path(folder))
            accounts = api.get("accounts")
            entered, release, completed = threading.Event(), threading.Event(), threading.Event()
            ui_thread = threading.get_ident()
            threads = []
            original = MessageRenderer.render

            def render(renderer, *args):
                threads.append(threading.get_ident())
                return original(renderer, *args)

            def cached(*_):
                self.assertNotEqual(threading.get_ident(), ui_thread)
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test barrier")
                return {
                    "title": "Saved",
                    "messages": [{"role": "assistant", "text": "**Saved answer**"}],
                }

            try:
                with (
                    terminal(account_service=accounts) as (v, _),
                    patch.object(accounts, "cached", cached),
                    patch.object(v, "refresh_provider_catalog"),
                    patch.object(
                        v, "_read_in_background", side_effect=lambda *a, **k: completed.set()
                    ),
                    patch.object(MessageRenderer, "render", render),
                ):
                    start = time.monotonic()
                    v.resume_conversation("@chatgpt/default/one")
                    self.assertLess(time.monotonic() - start, 0.15)
                    self.assertTrue(entered.wait(1))
                    v.input.text = "Still typing"
                    release.set()
                    self.assertTrue(completed.wait(3))
                    self.assertEqual(v.input.text, "Still typing")
                    self.assertIn("Saved answer", v.render_transcript())
                    self.assertTrue(threads)
                    self.assertNotIn(ui_thread, threads)
                    v._save_active_tab()
                    v.new_tab()
                    v.render_transcript()
                    v.active_tab = 0
                    v._restore_active_tab(focus=False)
                    with patch.object(
                        v.renderer, "render", side_effect=AssertionError("UI Markdown render")
                    ):
                        self.assertIn("Saved answer", v.render_transcript())
            finally:
                release.set()
                api.runtime.close()

    def test_pointer_writes_are_off_thread_coalesced_and_drained_on_close(self):
        with tempfile.TemporaryDirectory() as folder:
            api = create_application(Path(folder))
            accounts = api.get("accounts")
            entered, release = threading.Event(), threading.Event()
            calls = []

            def write(key, value):
                calls.append(value)
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test barrier")

            accounts.set_active = Mock(side_effect=write)
            try:
                start = time.monotonic()
                accounts.queue_active("chatgpt/default", "one")
                self.assertLess(time.monotonic() - start, 0.15)
                self.assertTrue(entered.wait(1))
                for _ in range(50):
                    accounts.queue_active("chatgpt/default", "two")
                release.set()
                api.runtime.close()
                self.assertEqual(calls, ["one", "two"])
            finally:
                release.set()
                api.runtime.close()


class ArchivePollingPerformanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_archive_polling_cannot_wait_for_storage_on_the_ui_thread(self):
        import asyncio

        from tests import test_cli_interaction as interaction

        with tempfile.TemporaryDirectory() as folder:
            api = create_application(Path(folder))
            archive = api.get("archive")
            archive.put("chatgpt/default", "chats", "", {"items": []})
            archive.put(
                "chatgpt/default",
                "conversation",
                "one",
                {"messages": [{"id": "answer", "role": "assistant", "text": "Synced"}]},
            )
            archive._state(("chatgpt/default", "conversation", "one"), "ready")
            entered, release = threading.Event(), threading.Event()
            ui_thread, original = threading.get_ident(), archive.get

            def read(*args):
                self.assertNotEqual(threading.get_ident(), ui_thread)
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test barrier")
                return original(*args)

            try:
                with (
                    terminal(account_service=api.get("accounts")) as (v, _),
                    patch.object(archive, "get", read),
                ):
                    v.conversation_id = "@chatgpt/default/one"
                    v._save_active_tab()
                    run = asyncio.create_task(v.app.run_async())
                    try:
                        await interaction.InputTests.settle(self, lambda: v.app.is_running)
                        v.apply_archive_updates()
                        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                        v.input.text = "Typing during synchronization"
                        await asyncio.sleep(0.03)
                        self.assertEqual(v.input.text, "Typing during synchronization")
                        release.set()
                        await interaction.InputTests.settle(
                            self, lambda: not v._archive_read_pending
                        )
                        self.assertEqual(v.messages[0]["text"], "Synced")
                    finally:
                        release.set()
                        v.app.exit()
                        await run
            finally:
                release.set()
                await api.runtime.aclose()
