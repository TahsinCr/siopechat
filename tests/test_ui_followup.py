"""Reported wrapped cursor failure and global sidebar and model-scoped project picker."""

import tempfile
import unittest
from pathlib import Path

from prompt_toolkit.data_structures import Point
from prompt_toolkit.layout.controls import FormattedTextControl

from cli.widgets import BoundedTextControl
from tests.account_fixture import configured_application as create_application
from tests.test_cli_interaction import terminal


class CursorTests(unittest.TestCase):
    def test_stale_wrapped_cursor_is_reproduced_and_clamped_to_actual_snapshot(self):
        content = "short"
        old = FormattedTextControl(lambda: content, get_cursor_position=lambda: Point(0, 599))
        old_content = old.create_content(8, 20)
        with self.assertRaises(IndexError):
            old_content.get_height_for_line(old_content.cursor_position.y, 8, None)
        control = BoundedTextControl(lambda: content, get_cursor_position=lambda: Point(99, 599))
        current = control.create_content(8, 20)
        self.assertEqual(current.cursor_position, Point(5, 0))
        self.assertEqual(current.get_height_for_line(current.cursor_position.y, 8, None), 1)

    def test_sidebar_keeps_all_projects_while_picker_tracks_model_account(self):
        with tempfile.TemporaryDirectory() as folder:
            api = create_application(Path(folder))
            try:
                accounts = api.get("accounts")
                gemini = accounts.add("gemini", "Gemini")
                with terminal(account_service=accounts) as (view, _):
                    view.project_items = {
                        "@chatgpt/default/gpt-project": {
                            "name": "GPT project",
                            "provider_id": "chatgpt",
                        },
                        "@" + gemini + "/notebooks%2Fabcdef": {
                            "name": "Gemini notebook",
                            "provider_id": "gemini",
                            "kind": "notebook",
                            "metadata": {"read_only": True, "supports_chat": True},
                        },
                    }
                    view.sidebar_index = 599
                    view._build_sidebar_rows()
                    self.assertEqual(
                        [r["title"] for r in view.sidebar_rows if r["kind"] == "project"],
                        ["Gemini notebook", "GPT project"],
                    )
                    self.assertLess(view.sidebar_index, len(view.sidebar_rows))
                    view.open_project_picker()
                    self.assertEqual(
                        [
                            r["title"]
                            for r in view.palette_items
                            if r["id"].startswith("project.select:")
                        ],
                        ["Projesiz sohbet", "GPT project"],
                    )
                    view.bind_account(gemini)
                    view._build_sidebar_rows()
                    self.assertEqual(
                        [r["title"] for r in view.sidebar_rows if r["kind"] == "project"],
                        ["Gemini notebook", "GPT project"],
                    )
                    view.open_project_picker()
                    self.assertEqual(
                        [
                            r["title"]
                            for r in view.palette_items
                            if r["id"].startswith("project.select:")
                        ],
                        ["Projesiz sohbet", "Gemini notebook"],
                    )
                    view.choose_project("@" + gemini + "/notebooks%2Fabcdef")
                    self.assertEqual(view.current_project["name"], "Gemini notebook")
            finally:
                api.runtime.close()
