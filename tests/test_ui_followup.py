"""Reported wrapped cursor failure and global sidebar and model-scoped project picker."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from prompt_toolkit.data_structures import Point
from prompt_toolkit.layout.controls import FormattedTextControl

from cli.widgets import BoundedTextControl
from tests.account_fixture import configured_application as create_application
from tests.cli_support import terminal


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

    def test_loading_next_project_page_keeps_previous_pages_and_other_accounts(self):
        with tempfile.TemporaryDirectory() as folder:
            api = create_application(Path(folder))
            try:
                accounts = api.get("accounts")
                first_id = "@chatgpt/default/project-1"
                next_id = "@chatgpt/default/project-2"
                other_id = "@gemini/personal/notebook-1"
                with terminal(account_service=accounts) as (view, _):
                    view.project_items = {
                        first_id: {"id": first_id, "name": "Page one"},
                        other_id: {"id": other_id, "name": "Other account"},
                    }
                    view.project_cursor = json.dumps({"chatgpt/default": "page-2"})
                    view._read_in_background = lambda read, success, _failure, **_kwargs: success(
                        read()
                    )

                    def load_next(cursor, *, account_keys=None, on_account=None):
                        self.assertEqual(json.loads(cursor), {"chatgpt/default": "page-2"})
                        page = {"items": [{"id": next_id, "name": "Page two"}], "cursor": None}
                        on_account({"key": "chatgpt/default"}, page)
                        return page | {"errors": [], "failed_accounts": []}

                    with patch.object(accounts, "all_projects", side_effect=load_next):
                        view.refresh_projects(more=True)

                    self.assertEqual(set(view.project_items), {first_id, next_id, other_id})
            finally:
                api.runtime.close()
