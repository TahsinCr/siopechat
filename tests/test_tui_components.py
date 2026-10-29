import json
import threading
import unittest
from unittest.mock import Mock, patch

from prompt_toolkit.application import Application
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from providers.models import Choice, FeatureState, Model, ModelCatalog
from settings import DEFAULTS, Settings, keybinding_conflicts
from tui.chat import ChatView
from tui.markdown import MessageRenderer, detect_code_language, visible_chat_message
from utils import clipboard


class CliComponentTests(unittest.TestCase):
    def test_chat_view_renders_markdown_and_transcript_messages(self):
        with create_pipe_input() as pipe:

            def application(**kwargs):
                return Application(input=pipe, output=DummyOutput(), **kwargs)

            with patch("tui.chat.view.Application", side_effect=application):
                view = ChatView(lambda: [], lambda _: {}, lambda *_: {})
                rendered = view.renderer.render("assistant", "**hello**", 60, False, False)
                self.assertIn("hello", rendered)
                view.messages = [{"role": "user", "text": "question"}]
                self.assertIn("question", view.render_transcript())

    def test_chat_tabs_preserve_conversations_and_allow_switch_during_send(self):
        with create_pipe_input() as pipe:
            with patch(
                "tui.chat.view.Application",
                side_effect=lambda **kwargs: Application(
                    input=pipe, output=DummyOutput(), **kwargs
                ),
            ):
                view = ChatView(lambda: [], lambda _: {}, lambda *_: {})
                view.messages = [{"role": "user", "text": "first tab"}]
                view.new_tab()
                self.assertEqual(len(view.tabs), 2)
                self.assertEqual(view.messages, [])
                view.messages = [{"role": "user", "text": "second tab"}]
                view.switch_tab(-1)
                self.assertEqual(view.messages[0]["text"], "first tab")
                view.busy = True
                view.busy_tab_index = 0
                view.tabs[0]["busy"] = True
                view.switch_tab(1)
                self.assertEqual(view.active_tab, 1)
                self.assertIn("sürüyor", view.status)
                self.assertEqual(view.messages[0]["text"], "second tab")
                view.busy = False
                view.busy_tab_index = None
                view.tabs[0]["busy"] = False
                self.assertEqual(view.messages[0]["text"], "second tab")

    def test_tab_strip_keeps_active_chat_visible_without_overflow(self):
        with create_pipe_input() as pipe:
            with patch(
                "tui.chat.view.Application",
                side_effect=lambda **kwargs: Application(
                    input=pipe, output=DummyOutput(), **kwargs
                ),
            ):
                view = ChatView(lambda: [], lambda _: {}, lambda *_: {})
                view.tabs = [
                    {"title": f"Conversation {index}", "busy": False} for index in range(10)
                ]
                view.active_tab = 7
                view.title = "Current conversation"
                fragments = view.render_tabs()
                text = "".join(fragment[1] for fragment in fragments)
                self.assertIn("Current", text)
                self.assertTrue(any(len(fragment) == 3 for fragment in fragments))
                self.assertLessEqual(len(text), view.app.output.get_size().columns)

    def test_sidebar_keyboard_shortcuts_focus_search_and_navigate(self):
        with create_pipe_input() as pipe:
            with patch(
                "tui.chat.view.Application",
                side_effect=lambda **kwargs: Application(
                    input=pipe, output=DummyOutput(), **kwargs
                ),
            ):
                view = ChatView(lambda: [], lambda _: {}, lambda *_: {})
                self.assertEqual(view.keybindings["navigation.focus_sidebar"], "c-g")
                view.sidebar_visible = False
                view.focus_sidebar()
                self.assertTrue(view.sidebar_visible)
                self.assertIs(view.app.layout.current_control, view.sidebar_control)
                with patch.object(
                    view._reads, "submit", side_effect=lambda target: target() or True
                ):
                    view.open_sidebar_search()
                self.assertIs(view.app.layout.current_control, view.sidebar_search.control)
                view.sidebar_items = [
                    {"id": str(index), "title": f"Chat {index}"} for index in range(40)
                ]
                view.sidebar_search.text = ""
                view._build_sidebar_rows()
                view.sidebar_index = len(view.sidebar_rows) - 1
                view.ensure_sidebar_selection_visible()
                self.assertGreater(view.sidebar_window.vertical_scroll, 0)

    def test_no_effort_is_invented_before_catalog_discovery(self):
        with create_pipe_input() as pipe:
            with patch(
                "tui.chat.view.Application",
                side_effect=lambda **kwargs: Application(
                    input=pipe, output=DummyOutput(), **kwargs
                ),
            ):
                view = ChatView(
                    lambda: [],
                    lambda _: {},
                    lambda *_: {},
                    effort_preference="medium",
                )
                self.assertIsNone(view.effort)
                self.assertIn("Sağlayıcı varsayılanı", str(view.render_chat_header()))

    def test_transcript_keeps_entire_conversation_in_fullscreen_ui(self):
        with create_pipe_input() as pipe:
            with patch(
                "tui.chat.view.Application",
                side_effect=lambda **kwargs: Application(
                    input=pipe, output=DummyOutput(), **kwargs
                ),
            ):
                view = ChatView(lambda: [], lambda _: {}, lambda *_: {})
                view.messages = [
                    {"role": "user", "text": "first message"},
                    {"role": "assistant", "text": "last message"},
                ]
                transcript = view.render_transcript()
                self.assertTrue(view.app.full_screen)
                self.assertIn("first message", transcript)
                self.assertIn("last message", transcript)

    def test_sidebar_refresh_keeps_last_good_rows_on_provider_failure(self):
        with create_pipe_input() as pipe:
            with patch(
                "tui.chat.view.Application",
                side_effect=lambda **kwargs: Application(
                    input=pipe, output=DummyOutput(), **kwargs
                ),
            ):
                view = ChatView(
                    lambda: (_ for _ in ()).throw(RuntimeError("offline")),
                    lambda _: {},
                    lambda *_: {},
                    list_workspaces=lambda: [],
                )
                previous = {"id": "known", "title": "Keep visible"}
                view.sidebar_items = [previous]
                with patch.object(
                    view._reads, "submit", side_effect=lambda target: target() or True
                ):
                    view.refresh_sidebar()
                self.assertEqual(view.sidebar_items, [previous])
                self.assertEqual(view.sidebar_error, "offline")
                self.assertFalse(view.sidebar_loading)

    def test_response_continues_in_its_origin_tab_after_switch(self):
        with create_pipe_input() as pipe:
            with patch(
                "tui.chat.view.Application",
                side_effect=lambda **kwargs: Application(
                    input=pipe, output=DummyOutput(), **kwargs
                ),
            ):
                started = threading.Event()
                release = threading.Event()

                def send(_conversation_id, _prompt, _attachments, on_event, *_args):
                    on_event({"type": "delta", "text": "answer"})
                    started.set()
                    self.assertTrue(release.wait(2))
                    return {
                        "conversation_id": "chat-1",
                        "conversation": {
                            "title": "First chat",
                            "default_model_slug": "model-a",
                            "messages": [
                                {
                                    "id": "answer-1",
                                    "author": {"role": "assistant"},
                                    "content": {"parts": ["answer"]},
                                }
                            ],
                        },
                        "previous_ids": [],
                    }

                view = ChatView(lambda: [], lambda _: {}, send)
                first_messages = [
                    {"role": "user", "text": "question"},
                    {"role": "assistant", "text": ""},
                ]
                view.messages = first_messages
                view._save_active_tab()
                view.busy = True
                view.busy_tab_index = 0
                view.tabs[0]["busy"] = True
                worker = threading.Thread(
                    target=view.run_send,
                    args=("question", [], 0, first_messages, None, "model-a", "extended"),
                )
                worker.start()
                self.assertTrue(started.wait(2))
                view.new_tab()
                self.assertEqual(view.active_tab, 1)
                release.set()
                worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(view.tabs[0]["messages"][-1]["text"], "answer")
                self.assertFalse(view.tabs[0]["busy"])
                self.assertTrue(view.tabs[0]["completed_pending"])
                self.assertEqual(view.messages, [])
                self.assertIn("tamamlandı", view.status)

    def test_duplicate_shortcuts_are_valid_and_report_every_conflicting_action(self):
        collided = json.loads(json.dumps(DEFAULTS))
        collided["keybindings"]["extra.action"] = DEFAULTS["keybindings"]["ui.command_palette"]
        Settings._validate_document(collided)
        self.assertEqual(
            keybinding_conflicts(collided["keybindings"]),
            frozenset(("ui.command_palette", "extra.action")),
        )

    def test_sidebar_groups_projects_unassigned_chats_and_layout_workspaces(self):
        with create_pipe_input() as pipe:
            with patch(
                "tui.chat.view.Application",
                side_effect=lambda **kwargs: Application(
                    input=pipe, output=DummyOutput(), **kwargs
                ),
            ):
                view = ChatView(
                    lambda: [],
                    lambda _: {},
                    lambda *_: {},
                    keybindings={"chat.new": "f11"},
                    list_actions=lambda: [{"id": "layout.list", "title": "List workspaces"}],
                    list_workspaces=lambda: [{"id": "w1", "name": "Focus setup"}],
                )
                self.assertEqual(view.keybindings["chat.new"], "f11")
                view.sidebar_items = [
                    {
                        "id": "1",
                        "title": "Network notes",
                        "project_id": "p1",
                        "project_name": "Work",
                        "default_model_slug": "gpt-history",
                    },
                    {"id": "2", "title": "Other"},
                    {"id": "w1", "name": "Focus setup", "kind": "workspace"},
                ]
                view.sidebar_search.text = "network"
                view._build_sidebar_rows()
                self.assertIn("project:p1", view.sidebar_collapsed)
                self.assertFalse(any(row["kind"] == "chat" for row in view.sidebar_rows))
                view.sidebar_index = next(
                    i for i, row in enumerate(view.sidebar_rows) if row["kind"] == "project"
                )
                view.toggle_sidebar_group()
                self.assertNotIn("project:p1", view.sidebar_collapsed)
                self.assertEqual(
                    [row["id"] for row in view.sidebar_rows if row["kind"] == "chat"], ["1"]
                )
                view.sidebar_search.text = ""
                view._build_sidebar_rows()
                self.assertEqual(
                    [row["title"] for row in view.sidebar_rows if row["kind"] == "section"],
                    ["Projeler", "Sohbetler", "Düzenler"],
                )
                view.sidebar_index = next(
                    i for i, row in enumerate(view.sidebar_rows) if row["kind"] == "project"
                )
                view.toggle_sidebar_group()
                self.assertIn("project:p1", view.sidebar_collapsed)
                view.sidebar_index = 0
                view.toggle_sidebar_group()
                self.assertIn("section:projects", view.sidebar_collapsed)
                view.catalog = ModelCatalog(
                    FeatureState.AVAILABLE,
                    (
                        Model(
                            "gpt-account-model",
                            "Account model",
                            (Choice("standard", "Standard"), Choice("extended", "Extended")),
                        ),
                        Model("another-model", "Another model"),
                    ),
                )
                view.open_palette()
                ids = {item["id"] for item in view.palette_filtered}
                self.assertIn("ui.search", ids)
                self.assertIn("command.projects", ids)
                self.assertNotIn("model.select:gpt-history", ids)
                self.assertIn("model.select:gpt-account-model", ids)
                self.assertIn("layout.list", ids)
                rendered = "".join(fragment[1] for fragment in view.render_sidebar())
                self.assertIn("Sohbetler", rendered)
                self.assertIn("Düzenler", rendered)
                self.assertIn("Focus setup", rendered)
                view.sidebar_error = "provider offline"
                rendered = "".join(fragment[1] for fragment in view.render_sidebar())
                self.assertIn("provider off", rendered)
                self.assertEqual(view.sidebar_error, "provider offline")
                self.assertIn("Focus setup", rendered)
                view.sidebar_error = None
                self.assertTrue(view.sidebar_visible)
                view.sidebar_visible = False
                self.assertFalse(view.sidebar_visible)
                view.handle_command("/model gpt-account-model")
                self.assertEqual(view.model, "gpt-account-model")
                view.handle_command("/effort")
                self.assertIn("effort.select:extended", {item["id"] for item in view.palette_items})
                view.close_palette()
                view.handle_command("/effort medium")
                self.assertIn("kataloğunda yok", view.status)
                view.handle_command("/model another-model")
                self.assertEqual(view.model, "another-model")

    def test_markdown_conversion_preserves_visible_messages_and_ignores_tool_nodes(self):
        self.assertEqual(detect_code_language("def hello():\n    return 1"), "python")
        message = visible_chat_message({"role": "assistant", "text": "Visible answer"})
        self.assertEqual(message["text"], "Visible answer")
        self.assertIsNone(visible_chat_message({"role": "tool", "text": "Internal tool message"}))

        renderer = MessageRenderer()
        renderer.cache_byte_limit = 1
        renderer.render("assistant", "cached markdown", 60, False, False)
        self.assertEqual(renderer.cache, {})
        self.assertEqual(renderer.cache_bytes, 0)

    def test_clipboard_uses_platform_helper_and_retains_terminal_fallback(self):
        with patch.object(clipboard.shutil, "which", return_value="/bin/tool"):
            with patch.object(clipboard.subprocess, "run", return_value=Mock()) as run:
                self.assertTrue(clipboard.copy_text("hello"))
                self.assertEqual(run.call_args.kwargs["input"], "hello")
        with patch.object(clipboard.shutil, "which", return_value=None):
            self.assertFalse(clipboard.copy_text("hello"))
        self.assertEqual(clipboard.terminal_sequence("hello"), "\x1b]52;c;aGVsbG8=\x07")
