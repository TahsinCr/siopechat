"""UI navigation must not wait for local storage, Markdown rendering or pointer fsync."""

import asyncio
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests.account_fixture import configured_application as create_application
from tests.tui_support import terminal
from tui.markdown import MessageRenderer


class OpeningPerformanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_cached_open_does_not_block_ui_and_prepares_viewport_off_thread(self):
        with tempfile.TemporaryDirectory() as folder:
            api = create_application(Path(folder))
            accounts = api.get("accounts")
            entered, release = threading.Event(), threading.Event()
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
                    patch.object(MessageRenderer, "render", render),
                ):
                    run = asyncio.create_task(v.app.run_async())
                    try:
                        await self.settle(lambda: v.app.is_running)
                        start = time.monotonic()
                        v.resume_conversation("@chatgpt/default/one")
                        self.assertLess(time.monotonic() - start, 0.15)
                        await self.settle(entered.is_set)
                        v.input.text = "Still typing"
                        release.set()
                        await self.settle(
                            lambda: (
                                bool(v.tabs[0].get("messages"))
                                and v.tabs[0]["messages"][0].get("text") == "**Saved answer**"
                            )
                        )
                        await self.settle(
                            lambda: getattr(v, "_virtual_transcript", None) is not None
                        )
                        self.assertEqual(v.input.text, "Still typing")
                        self.assertTrue(threads)
                        self.assertNotIn(ui_thread, threads)
                        self.assertLessEqual(v._virtual_transcript.cache_bytes, 1024 * 1024)
                    finally:
                        release.set()
                        v.app.exit()
                        await run
            finally:
                release.set()
                await api.runtime.aclose()

    async def settle(self, predicate):
        deadline = asyncio.get_running_loop().time() + 3
        while not predicate():
            if asyncio.get_running_loop().time() >= deadline:
                self.fail("background UI operation did not complete")
            await asyncio.sleep(0.005)

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

        from tests import tui_support as interaction

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
                        await interaction.wait_until(lambda: v.app.is_running)
                        v.apply_archive_updates()
                        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                        v.input.text = "Typing during synchronization"
                        await asyncio.sleep(0.03)
                        self.assertEqual(v.input.text, "Typing during synchronization")
                        release.set()
                        await interaction.wait_until(lambda: not v._archive_read_pending)
                        self.assertEqual(v.messages[0]["text"], "Synced")
                    finally:
                        release.set()
                        v.app.exit()
                        await run
            finally:
                release.set()
                await api.runtime.aclose()


class SidebarRenderingPerformanceTests(unittest.TestCase):
    def test_sidebar_content_is_lazy_and_reuses_rows_until_invalidated(self):
        from tui.library import build_sidebar_rows

        with terminal() as (view, _):
            view.sidebar_items = [
                {
                    "id": f"@chatgpt/default/chat-{number}",
                    "title": f"Conversation {number:05d}",
                    "provider_id": "chatgpt",
                }
                for number in range(3000)
            ]
            view._invalidate_sidebar_rows()
            render_calls = []
            render_row = view.render_sidebar_row

            def track_row(*args):
                render_calls.append(args[0])
                return render_row(*args)

            with patch.object(view, "render_sidebar_row", side_effect=track_row):
                content = view.sidebar_control.create_content(80, 24)
                self.assertEqual(content.line_count, len(view.sidebar_rows))
                self.assertEqual(render_calls, [])
                content.get_line(0)
                content.get_line(1)
                content.get_line(1)
                self.assertEqual(render_calls, [0, 1])

            rows = view.sidebar_rows
            with patch("tui.chat.build_sidebar_rows", wraps=build_sidebar_rows) as build:
                view.sidebar_control.create_content(80, 24)
                self.assertIs(view.sidebar_rows, rows)
                build.assert_not_called()

                view._invalidate_sidebar_rows()
                view.sidebar_control.create_content(80, 24)
                build.assert_called_once()
                self.assertIsNot(view.sidebar_rows, rows)

    def test_lazy_sidebar_routes_chat_and_project_info_clicks(self):
        from prompt_toolkit.data_structures import Point
        from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
        from prompt_toolkit.utils import get_cwidth

        with terminal() as (view, _):
            project_id = "@chatgpt/default/project"
            view.project_items = {project_id: {"id": project_id, "name": "Project"}}
            view._invalidate_sidebar_rows()
            content = view.sidebar_control.create_content(80, 24)
            project_row = next(
                i for i, row in enumerate(view.sidebar_rows) if row["kind"] == "project"
            )
            view.click_sidebar = Mock()
            view.show_project_details = Mock()
            fragments = view.sidebar_control._fragments.get(project_row)
            if fragments is None:
                content.get_line(project_row)
                fragments = view.sidebar_control._fragments[project_row]
            project_info_x = sum(get_cwidth(part[1]) for part in fragments[:-1])
            event = MouseEvent(
                position=Point(x=project_info_x, y=project_row),
                event_type=MouseEventType.MOUSE_UP,
                button=MouseButton.LEFT,
                modifiers=frozenset(),
            )
            view.sidebar_control.mouse_handler(event)
            view.show_project_details.assert_called_once_with(project_id)

            chat_id = "@chatgpt/default/chat"
            view.sidebar_items = [{"id": chat_id, "title": "Chat", "provider_id": "chatgpt"}]
            view._invalidate_sidebar_rows()
            content = view.sidebar_control.create_content(80, 24)
            chat_row = next(i for i, row in enumerate(view.sidebar_rows) if row["id"] == chat_id)
            content.get_line(chat_row)
            view.sidebar_control.mouse_handler(
                MouseEvent(
                    position=Point(x=1, y=chat_row),
                    event_type=MouseEventType.MOUSE_UP,
                    button=MouseButton.LEFT,
                    modifiers=frozenset(),
                )
            )
            view.click_sidebar.assert_called_once_with(chat_row)
