"""Role gutters, focus recovery, scroll bursts and project membership regressions."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Point, Size
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from prompt_toolkit.output import DummyOutput

from backend import BackendService
from cli.widgets import TranscriptGutter
from providers import Capability
from providers.chatgpt import BrowserSession, ChatGPTClient, ChatGPTProvider, SessionPaths
from providers.chatgpt.errors import ChatGPTWebError
from tests import test_cli_interaction as interaction
from tests.test_cli_interaction import terminal
from tests.test_ui_controls import click, immediate


class PolishTests(unittest.TestCase):
    def test_palette_and_keyboard_share_canonical_actions(self):
        from settings import DEFAULT_KEYBINDINGS

        with terminal() as (v, _):
            self.assertEqual(set(DEFAULT_KEYBINDINGS), set(v.action_handlers))
            v.open_palette()
            ids = [item["id"] for item in v.palette_items]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertEqual(ids[0], "chat.new")
            v.dispatch_action("chat.new")
            self.assertEqual(len(v.tabs), 2)

    def test_multiple_tabs_are_capped_and_controls_stay_at_right(self):
        from prompt_toolkit.utils import get_cwidth

        with terminal() as (v, _):
            v.new_tab()
            fragments = v.render_tabs()
            self.assertEqual(sum(get_cwidth(f[1]) for f in fragments), v.terminal_width())
            self.assertTrue(
                all(get_cwidth(f[1]) <= 22 for f in fragments if f[0] == "class:tab.active")
            )
            self.assertEqual("".join(f[1] for f in fragments[-3:]), " ‹  ›  + ")

    def test_project_move_detach_failure_and_draft_preserve_context(self):
        move = Mock()
        with terminal(move_conversation=move) as (v, _):
            v._read_in_background = immediate
            v.project_items = {"g-p-work": {"id": "g-p-work", "name": "Work"}}
            v.messages = [{"role": "user", "text": "Keep history"}]
            v.conversation_id = "chat"
            v.input.text = "Keep draft"
            v.sidebar_items = [{"id": "chat", "title": "Chat"}]
            v.handle_command("/proje g-p-work")
            move.assert_called_once_with("chat", "g-p-work")
            self.assertEqual(v.sidebar_items[0]["project_id"], "g-p-work")
            self.assertEqual(v.messages[0]["text"], "Keep history")
            self.assertEqual(v.input.text, "Keep draft")
            self.assertEqual(len(v.tabs), 1)
            move.side_effect = RuntimeError("denied")
            v.choose_project("")
            self.assertEqual(v.current_project["id"], "g-p-work")
            self.assertIn("denied", v.status)
            self.assertEqual(v.background_jobs, 0)
            move.side_effect = None
            v.choose_project("")
            self.assertEqual(v.current_project, {})
            self.assertIsNone(v.sidebar_items[0]["project_id"])
            move.assert_called_with("chat", None)
            v.new_tab()
            move.reset_mock()
            v.choose_project("g-p-work")
            move.assert_not_called()
            self.assertEqual(v.current_project["id"], "g-p-work")

    def test_project_header_picker_and_account_label(self):
        with terminal() as (v, _):
            v.projects_loaded = True
            v.current_project = {"id": "g-p-work", "name": "Work"}
            project = next(f for f in v.render_chat_header() if f[0] == "class:metadata.project")
            self.assertIn("Work", project[1])
            click(project)
            self.assertIn("project.select:", [i["id"] for i in v.palette_items])
            self.assertIn("Hesaplar", "".join(f[1] for f in v.render_footer()))
            style = v.app.style
            self.assertEqual(
                style.get_attrs_for_style_str("class:metadata.project").color, "679bd4"
            )
            self.assertEqual(
                style.get_attrs_for_style_str("class:tab.active").bgcolor,
                style.get_attrs_for_style_str("class:tab").bgcolor,
            )

    def test_temporary_chat_control_sits_after_project_and_respects_provider_capability(self):
        with terminal() as (v, _):
            v.provider_features = frozenset({Capability.PROJECTS, Capability.TEMPORARY})
            fragments = v.render_chat_header()
            project_index = next(
                i for i, f in enumerate(fragments) if f[0] == "class:metadata.project"
            )
            temporary_index = next(
                i for i, f in enumerate(fragments) if f[0] == "class:metadata.temporary"
            )
            self.assertGreater(temporary_index, project_index)
            self.assertIn("Geçici: Kapalı", fragments[temporary_index][1])
            count = len(v.tabs)
            v.input.text = "Draft stays here"
            v.current_project = {"id": "project", "name": "Work"}
            click(fragments[temporary_index])
            self.assertTrue(v.temporary)
            self.assertEqual(len(v.tabs), count)
            self.assertEqual(v.input.text, "Draft stays here")
            self.assertEqual(v.current_project, {})
            self.assertIn("Geçici: Açık", str(v.render_chat_header()))
            v.toggle_temporary_chat()
            self.assertFalse(v.temporary)

            v.conversation_id = "saved-chat"
            v.toggle_temporary_chat()
            self.assertFalse(v.temporary)
            self.assertIn("ilk mesajdan önce", v.status)

            v.provider_features = frozenset({Capability.PROJECTS})
            self.assertIn("Geçici: Desteklenmiyor", str(v.render_chat_header()))
            v.toggle_temporary_chat()
            self.assertFalse(v.temporary)
            self.assertIn("desteklemiyor", v.status)

    def test_role_markers_first_line_only_and_reasoning_before_answer(self):
        with terminal(
            initial_messages=[
                {"role": "user", "text": "Question"},
                {"role": "assistant", "text": "Answer"},
                {"role": "reasoning", "text": "Summary"},
            ]
        ) as (v, _):
            rendered = v.render_transcript()
            self.assertLess(rendered.index("Summary"), rendered.index("Answer"))
            starts = {v.messages[i]["role"]: line for line, _, i in v.message_ranges}
            gutter = TranscriptGutter(v)
            info = SimpleNamespace(
                visible_line_to_row_col={
                    0: (starts["user"], 0),
                    1: (starts["user"], 5),
                    2: (starts["reasoning"], 0),
                    3: (starts["assistant"], 0),
                }
            )
            rows = gutter.create_margin(info, 3, 5)
            self.assertEqual([r[1].strip() for r in rows], [">", "", "🧠", "🤖", ""])
            self.assertIn(
                "48;2;28;39;50", v.renderer.render("reasoning", "Summary", 60, True, False)
            )

    def test_wheel_burst_accumulates_without_waiting_for_render(self):
        with terminal() as (v, _):
            w = v.transcript_window
            w.render_info = SimpleNamespace(
                vertical_scroll=40, content_height=200, window_height=20
            )
            w.vertical_scroll = 40
            for _ in range(4):
                w._scroll_up()
            self.assertEqual(w.vertical_scroll, 24)
            for _ in range(6):
                w._scroll_down()
            self.assertEqual(w.vertical_scroll, 48)
            self.assertFalse(v.follow_output)

    def test_provider_requires_server_membership_confirmation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            session = BrowserSession(
                SessionPaths(root / "session", root / "cache", root / "profile")
            )
            client = ChatGPTClient(session, authenticate=False)
            client.http.close()
            requests = []
            membership = ["g-p-work"]

            def respond(request):
                requests.append(request)
                if request.method == "PATCH":
                    return httpx.Response(200, json={"success": True})
                return httpx.Response(
                    200, json={"id": "chat", "gizmo_id": membership[0], "messages": []}
                )

            client.http = httpx.Client(transport=httpx.MockTransport(respond))
            try:
                client.move_conversation("chat", "g-p-work")
                self.assertEqual(requests[0].method, "PATCH")
                self.assertEqual(json.loads(requests[0].content), {"gizmo_id": "g-p-work"})
                with self.assertRaises(ChatGPTWebError):
                    client.move_conversation("chat", None)
                membership[0] = None
                client.move_conversation("chat", None)
                self.assertEqual(json.loads(requests[-2].content), {"gizmo_id": None})
                with self.assertRaises(ValueError):
                    client.move_conversation("chat", "g-custom")
            finally:
                client.close()

    def test_common_provider_exposes_membership_operation(self):
        client = Mock()
        factory = Mock()
        factory.return_value.__enter__ = Mock(return_value=client)
        factory.return_value.__exit__ = Mock(return_value=False)
        provider = ChatGPTProvider(Mock(), client_factory=factory)
        self.assertIn(Capability.PROJECT_MEMBERSHIP, provider.capabilities)
        asyncio.run(provider.move_chat("chat", None))
        client.move_conversation.assert_called_once_with("chat", None)

    def test_ipc_move_preserves_active_conversation_pointer(self):
        with tempfile.TemporaryDirectory() as folder:
            backend = BackendService()
            root = Path(folder)
            backend.conversation_cache_path = lambda remote: root / (remote + ".json")
            backend.active_conversation_path = lambda: root / "active.json"
            backend.set_active_conversation("other")
            client = Mock()
            client.move_conversation.return_value = {
                "id": "chat",
                "gizmo_id": "g-p-work",
                "messages": [],
            }
            value, _ = backend.handle_daemon_command(
                client,
                {"command": "move_chat", "conversation_id": "chat", "project_id": "g-p-work"},
            )
            self.assertEqual(value["conversation"]["project_id"], "g-p-work")
            self.assertEqual(
                json.loads((root / "active.json").read_text())["conversation_id"], "other"
            )
            client.move_conversation.assert_called_once_with("chat", "g-p-work")


class FocusTests(unittest.IsolatedAsyncioTestCase):
    settle = interaction.InputTests.settle

    async def test_modified_enter_inserts_newline_without_sending(self):
        for sequence in (b"\x1b[13;2u", b"\x1b[27;2;13~", b"\x1b[13;2~", b"\n"):
            with terminal() as (v, pipe):
                run = asyncio.create_task(v.app.run_async())
                try:
                    await self.settle(lambda: v.app.is_running)
                    pipe.send_text("first")
                    pipe.send_bytes(sequence)
                    pipe.send_text("second")
                    await self.settle(lambda: v.input.text == "first\nsecond")
                    self.assertFalse(v.busy)
                finally:
                    v.app.exit()
                    await run

    async def test_mouse_returns_focus_to_composer_then_ctrl_d_works_on_message(self):
        with terminal(initial_messages=[{"role": "assistant", "text": "Answer"}]) as (v, pipe):
            run = asyncio.create_task(v.app.run_async())
            try:
                await self.settle(lambda: v.transcript_window.render_info is not None)
                v.select_message(0)
                # Same handler Prompt Toolkit invokes for a click in the composer.
                with set_app(v.app):
                    v.input.control.mouse_handler(
                        MouseEvent(
                            Point(0, 0), MouseEventType.MOUSE_UP, MouseButton.LEFT, frozenset()
                        )
                    )
                self.assertIs(v.app.layout.current_control, v.input.control)
                pipe.send_text("draft")
                await self.settle(lambda: v.input.text == "draft")
                v.input.text = ""
                v.select_message(0)
                pipe.send_bytes(b"\x04")
                await asyncio.wait_for(run, 1)
            finally:
                if v.app.is_running:
                    v.app.exit()
                await run

    async def test_wheel_position_and_first_line_marker_survive_redraw(self):
        class Output(DummyOutput):
            def get_size(self):
                return Size(rows=24, columns=90)

        with terminal(
            output=Output(),
            initial_messages=[
                {"role": "user" if i % 2 == 0 else "assistant", "text": f"Message {i}"}
                for i in range(80)
            ],
        ) as (v, _):
            run = asyncio.create_task(v.app.run_async())
            try:
                await self.settle(lambda: v.transcript_window.render_info is not None)
                w = v.transcript_window
                bottom = w.vertical_scroll
                for _ in range(5):
                    w._scroll_up()
                v.app.invalidate()
                await self.settle(lambda: w.render_info.vertical_scroll == bottom - 20)
                before = w.vertical_scroll
                v.status = "Changed"
                v.app.invalidate()
                await asyncio.sleep(0.06)
                self.assertEqual(w.vertical_scroll, before)
                info = w.render_info
                rows = TranscriptGutter(v).create_margin(info, 3, info.window_height)
                self.assertTrue(any(">" in f[1] or "🤖" in f[1] for f in rows))
            finally:
                v.app.exit()
                await run
