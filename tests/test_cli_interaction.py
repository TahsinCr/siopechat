"""Regression tests through Prompt Toolkit's actual input/key processor."""

import asyncio
import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from cli.chat import ChatView
from providers.chatgpt.discovery import model_catalog
from tests.test_provider_discovery import CATALOG


@contextmanager
def terminal(output=None, **callbacks):
    with (
        create_pipe_input() as pipe,
        patch(
            "cli.chat.Application",
            side_effect=lambda **kw: Application(input=pipe, output=output or DummyOutput(), **kw),
        ),
    ):
        view = ChatView(lambda: [], lambda _: {}, lambda *_: {}, **callbacks)
        yield view, pipe


def immediate(read, success, failure, **kwargs):
    try:
        success(read())
    except Exception as error:
        failure(str(error))


class InputTests(unittest.IsolatedAsyncioTestCase):
    async def settle(self, predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait(), 1)

    async def test_search_editing_backspace_delete_space_home_end_and_ctrl_d(self):
        with terminal() as (view, pipe):
            run = asyncio.create_task(view.app.run_async())
            try:
                await self.settle(lambda: view.app.is_running)
                pipe.send_bytes(b"\x10")  # Ctrl+P
                await self.settle(lambda: view.palette_visible)
                pipe.send_text("alpha beta")
                await self.settle(lambda: view.palette_search.text == "alpha beta")
                pipe.send_bytes(b"\x08")  # Backspace on terminals emitting Ctrl+H
                await self.settle(lambda: view.palette_search.text == "alpha bet")
                pipe.send_bytes(b"\x7f")  # Backspace on terminals emitting DEL
                await self.settle(lambda: view.palette_search.text == "alpha be")
                pipe.send_bytes(b"\x1b[H\x1b[3~")  # Home + Delete
                await self.settle(lambda: view.palette_search.text == "lpha be")
                pipe.send_bytes(b"\x04")  # Ctrl+D must delete in the search buffer, not quit
                await self.settle(lambda: view.palette_search.text == "pha be")
                pipe.send_bytes(b"\x1b[F")  # End belongs to the search field
                await self.settle(
                    lambda: view.palette_search.buffer.cursor_position == len("pha be")
                )
                self.assertTrue(view.app.is_running)
            finally:
                if view.app.is_running:
                    view.app.exit()
                await asyncio.wait_for(run, 2)

    async def test_sidebar_and_composer_text_fields_do_not_lose_spaces_or_backspaces(self):
        with terminal(keybindings={"chat.reload": "c-h"}) as (view, pipe):
            run = asyncio.create_task(view.app.run_async())
            try:
                await self.settle(lambda: view.app.is_running)
                pipe.send_text("hello world")
                await self.settle(lambda: view.input.text == "hello world")
                pipe.send_bytes(b"\x08")
                await self.settle(lambda: view.input.text == "hello worl")
                # Focus the existing search widget without causing network activity.
                view.app.layout.focus(view.sidebar_search)
                pipe.send_text("project notes")
                await self.settle(lambda: view.sidebar_search.text == "project notes")
                pipe.send_bytes(b"\x08")
                await self.settle(lambda: view.sidebar_search.text == "project note")
                self.assertEqual(view.input.text, "hello worl")
            finally:
                if view.app.is_running:
                    view.app.exit()
                await asyncio.wait_for(run, 2)

    async def test_long_sidebar_and_palette_keep_keyboard_selection_in_view(self):
        with terminal() as (view, pipe):
            view.sidebar_items = [{"id": str(i), "title": f"Conversation {i}"} for i in range(80)]
            view._build_sidebar_rows()
            run = asyncio.create_task(view.app.run_async())
            try:
                await self.settle(lambda: view.app.is_running)
                view.focus_sidebar()
                pipe.send_bytes(b"\x1b[B" * 60)
                await self.settle(lambda: view.sidebar_index == 60)
                await self.settle(
                    lambda: (
                        view.sidebar_window.render_info is not None
                        and view.sidebar_window.render_info.first_visible_line() > 0
                    )
                )
                self.assertIn(view.sidebar_index, view.sidebar_window.render_info.displayed_lines)
                view.list_actions = lambda: [
                    {"id": f"test.action.{i}", "title": f"Action {i}"} for i in range(80)
                ]
                view.open_palette()
                pipe.send_bytes(b"\x1b[B" * 60)
                await self.settle(lambda: view.palette_index == 60)
                await self.settle(
                    lambda: (
                        view.palette_window.render_info is not None
                        and view.palette_window.render_info.first_visible_line() > 0
                    )
                )
                self.assertIn(view.palette_index, view.palette_window.render_info.displayed_lines)
            finally:
                if view.app.is_running:
                    view.app.exit()
                await asyncio.wait_for(run, 2)

    async def test_navigation_shortcuts_close_overlays_before_changing_focus(self):
        with terminal() as (view, pipe):
            view._read_in_background = immediate
            run = asyncio.create_task(view.app.run_async())
            try:
                await self.settle(lambda: view.app.is_running)
                view.open_palette()
                pipe.send_bytes(b"\x06")  # Ctrl+F
                await self.settle(
                    lambda: view.app.layout.current_control is view.sidebar_search.control
                )
                self.assertFalse(view.palette_visible)
                view.open_palette()
                pipe.send_bytes(b"\x07")  # Ctrl+G
                await self.settle(lambda: view.app.layout.current_control is view.sidebar_control)
                self.assertFalse(view.palette_visible)
                view.open_palette()
                pipe.send_bytes(b"\x14")  # Ctrl+T
                await self.settle(lambda: len(view.tabs) == 2)
                self.assertFalse(view.palette_visible)
                self.assertIs(view.app.layout.current_control, view.input.control)
            finally:
                if view.app.is_running:
                    view.app.exit()
                await asyncio.wait_for(run, 2)

    async def test_resize_recomputes_sidebar_transcript_and_tab_widths(self):
        from prompt_toolkit.utils import get_cwidth

        class Output(DummyOutput):
            size = Size(rows=24, columns=80)

            def get_size(self):
                return self.size

        output = Output()
        with terminal(output=output) as (view, _pipe):
            view.messages = [{"role": "assistant", "text": "A readable response. " * 20}]
            view.title = "日本語 🚀 Çok uzun bir sohbet başlığı"
            run = asyncio.create_task(view.app.run_async())
            try:
                await self.settle(lambda: view.app.is_running)
                for columns, rows in ((80, 24), (60, 18), (120, 40)):
                    output.size = Size(rows=rows, columns=columns)
                    view.app.invalidate()
                    expected = columns - view.sidebar_width() - 1
                    await self.settle(
                        lambda: (
                            view.transcript_window.render_info is not None
                            and view.transcript_window.render_info.window_width == expected - 4
                        )
                    )
                    self.assertEqual(view.transcript_width(), min(100, expected - 5))
                    self.assertLessEqual(
                        sum(get_cwidth(fragment[1]) for fragment in view.render_tabs()), columns
                    )
                view.toggle_sidebar()
                await self.settle(lambda: view.transcript_window.render_info.window_width == 116)
                self.assertEqual(view.transcript_width(), 100)
            finally:
                if view.app.is_running:
                    view.app.exit()
                await asyncio.wait_for(run, 2)


class ProviderUiTests(unittest.TestCase):
    def test_work_model_is_preserved_as_provenance_but_not_offered_or_restored_as_chat_selection(
        self,
    ):
        raw = dict(
            CATALOG,
            models=CATALOG["models"] + [{"slug": "internal-sol-wm", "title": "Internal Sol"}],
        )
        with terminal(model="internal-sol-wm", load_catalog=lambda: model_catalog(raw)) as (
            view,
            _pipe,
        ):
            view._read_in_background = immediate
            view.input.text = "Keep the draft"
            view.submit_prompt()
            self.assertEqual(view.input.text, "Keep the draft")
            self.assertFalse(view.messages)
            view.refresh_provider_catalog()
            self.assertEqual(view.model, "account-thinking")
            self.assertEqual(view.last_response_model, "internal-sol-wm")
            view.open_palette("models")
            self.assertNotIn(
                "model.select:internal-sol-wm", {item["id"] for item in view.palette_items}
            )
            view.select_model("internal-sol-wm")
            self.assertEqual(view.model, "account-thinking")
            self.assertIn("kataloğunda yok", view.status)

    def test_model_and_effort_pickers_use_only_account_catalog_and_keep_selection_per_tab(self):
        with terminal(load_catalog=lambda: model_catalog(CATALOG)) as (view, _pipe):
            view._read_in_background = immediate
            view.refresh_provider_catalog()
            self.assertEqual(view.model, "account-thinking")
            self.assertEqual(view.effort, "standard")
            view.sidebar_items = [{"id": "old", "default_model_slug": "deleted-model"}]
            view.handle_command("/model")
            self.assertEqual(
                {i["id"] for i in view.palette_items},
                {"model.select:account-thinking", "model.select:account-instant"},
            )
            view.close_palette()
            view.handle_command("/effort extended")
            self.assertEqual(view.effort, "extended")
            view.input.text = "first draft"
            view.new_tab()
            view.send_mode = "chrome"
            view.handle_command("/model account-instant")
            self.assertEqual(view.model, "account-instant")
            self.assertIsNone(view.effort)
            view.input.text = "second draft"
            view.switch_tab(-1)
            self.assertEqual(
                (view.model, view.effort, view.input.text),
                ("account-thinking", "extended", "first draft"),
            )
            view.switch_tab(1)
            self.assertEqual(view.input.text, "second draft")

    def test_projects_load_lazily_page_chats_and_show_details_in_their_own_tab(self):
        detail = {
            "id": "g-p-work",
            "name": "Work",
            "instructions": "Be precise",
            "resources": [{"name": "Design.pdf", "type": "application/pdf"}],
        }
        list_chats = Mock(
            side_effect=[
                {"items": [{"id": "one", "title": "One"}], "cursor": "page2"},
                {"items": [{"id": "two", "title": "Two"}], "cursor": None},
            ]
        )
        with terminal(
            list_projects=lambda cursor: {"items": [detail], "cursor": None},
            get_project=lambda _: detail,
            list_project_chats=list_chats,
        ) as (view, _pipe):
            view._read_in_background = immediate
            view.refresh_projects()
            list_chats.assert_not_called()
            view._build_sidebar_rows()
            view.sidebar_index = next(
                i for i, row in enumerate(view.sidebar_rows) if row["kind"] == "project"
            )
            view.toggle_sidebar_group()
            list_chats.assert_called_once_with("g-p-work", None)
            self.assertIn("One", str(view.render_sidebar()))
            self.assertIn("Daha fazla sohbet", str(view.render_sidebar()))
            view.load_project_chats("g-p-work", more=True)
            self.assertEqual(list_chats.call_args.args, ("g-p-work", "page2"))
            self.assertIn("Two", str(view.render_sidebar()))
            view.messages = [{"role": "user", "text": "Keep this chat"}]
            view.show_project_details("g-p-work")
            self.assertEqual(view.tab_kind, "project")
            self.assertIn("Be precise", view.render_transcript())
            self.assertIn("Design.pdf", view.render_transcript())
            view.switch_tab(-1)
            self.assertIn("Keep this chat", view.render_transcript())
            self.assertEqual(view.tab_kind, "chat")

    def test_catalog_and_project_failures_keep_last_good_data_and_offer_retry(self):
        with terminal(
            load_catalog=Mock(side_effect=[model_catalog(CATALOG), RuntimeError("offline")]),
            list_projects=Mock(
                side_effect=[
                    {"items": [{"id": "g-p-work", "name": "Work"}], "cursor": None},
                    RuntimeError("HTTP 403"),
                ]
            ),
        ) as (view, _pipe):
            view._read_in_background = immediate
            view.refresh_provider_catalog()
            view.refresh_projects()
            view.refresh_provider_catalog()
            view.refresh_projects()
            self.assertEqual(len(view.catalog.models), 2)
            self.assertEqual(view.catalog_error, "offline")
            self.assertIn("g-p-work", view.project_items)
            self.assertIn("HTTP 403", str(view.render_sidebar()))
            self.assertFalse(view.projects_loading)

    def test_cached_model_and_project_metadata_are_restored_when_opening_chat(self):
        with terminal() as (view, _pipe):
            view.catalog = model_catalog(CATALOG)
            view.project_items["g-p-work"] = {"id": "g-p-work", "name": "Work"}
            view.load_conversation = lambda _: {
                "id": "c1",
                "model": "account-thinking",
                "project_id": "g-p-work",
                "messages": [],
            }
            with patch("cli.chat.threading.Thread") as thread:
                thread.side_effect = lambda target, daemon: Mock(start=target)
                view.resume_conversation("c1")
            self.assertEqual(view.model, "account-thinking")
            self.assertEqual(view.current_project["name"], "Work")

    def test_cached_project_survives_startup_and_catalog_fills_its_name(self):
        with terminal(
            conversation_id="c1",
            initial_messages=[{"role": "user", "text": "cached"}],
            initial_project={"id": "g-p-work"},
            list_projects=lambda cursor: {"items": [{"id": "g-p-work", "name": "Work"}]},
            refresh_on_start=True,
        ) as (view, _pipe):
            view._read_in_background = immediate
            self.assertTrue(view.load_on_start)
            view.refresh_projects()
            self.assertEqual(view.current_project, {"id": "g-p-work", "name": "Work"})
            self.assertIn("cached", view.render_transcript())

    def test_response_metadata_does_not_overwrite_the_next_selected_model(self):
        with terminal() as (view, _pipe):
            view.model = "account-thinking"
            view.messages = [{"role": "assistant", "text": ""}]
            view._save_active_tab()
            view.send_prompt = lambda *_: {
                "conversation_id": "chat",
                "conversation": {
                    "default_model_slug": "old-chat-default",
                    "messages": [
                        {
                            "id": "reply",
                            "author": {"role": "assistant"},
                            "metadata": {"model_slug": "actual-engine"},
                            "content": {"parts": ["answer"]},
                        }
                    ],
                },
            }
            view.run_send("question", [], 0, view.messages, None, "account-thinking", "standard")
            self.assertEqual(view.model, "account-thinking")
            self.assertEqual(view.last_response_model, "actual-engine")
            self.assertIn("answer", view.render_transcript())

    def test_background_content_loading_does_not_steal_search_focus(self):
        detail = {"id": "g-p-work", "name": "Work", "instructions": "Notes"}
        with terminal(get_project=lambda _: detail) as (view, _pipe):
            pending = []
            view._read_in_background = lambda read, success, failure: pending.append(
                (read, success)
            )
            view.show_project_details("g-p-work")
            view.open_palette()
            read, success = pending.pop()
            success(read())
            self.assertIs(view.app.layout.current_control, view.palette_search.control)
            self.assertTrue(view.palette_visible)
            view.close_palette()
            view.new_tab()
            view.load_conversation = lambda _: {"id": "chat", "messages": []}
            with patch("cli.chat.threading.Thread") as thread:
                view.resume_conversation("chat")
                load = thread.call_args.kwargs["target"]
            view.open_palette()
            load()
            self.assertIs(view.app.layout.current_control, view.palette_search.control)
