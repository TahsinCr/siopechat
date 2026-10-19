"""Local account lifecycle and selection without remote mutations."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from bootstrap import create_application
from settings import Settings
from tests.test_cli_interaction import terminal
from tests.test_ui_controls import immediate


class AccountManagementTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.api = create_application(Path(self.folder.name))
        self.addCleanup(self.api.runtime.close)
        self.accounts = self.api.get("accounts")
        self.gemini = self.accounts.add("gemini", "Gemini personal")

    def test_rename_preserves_identity_credentials_and_preferences(self):
        key = self.gemini
        self.accounts.select(key)
        self.accounts.remember(key, "model", "effort")
        path = self.accounts.credential_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("preserved session")
        before = self.api.settings.snapshot()["accounts"]
        renamed = self.accounts.rename(key, "  Work account  ")
        self.assertEqual(renamed["label"], "Work account")
        self.assertEqual(renamed["key"], key)
        self.assertEqual(path.read_text(), "preserved session")
        after = Settings(self.api.settings.path).snapshot()["accounts"]
        self.assertEqual(after["active"], before["active"])
        self.assertEqual(after["selected"], before["selected"])
        self.assertEqual(after["preferences"], before["preferences"])
        item = next(x for x in after["items"] if x["provider"] + "/" + x["id"] == key)
        self.assertEqual(item["label"], "Work account")
        for invalid in ("", "   ", "x" * 81):
            with self.assertRaises(ValueError):
                self.accounts.rename(key, invalid)
        self.assertEqual(self.accounts.item(key)["label"], "Work account")
        with self.assertRaises(ValueError):
            self.accounts.rename("gemini/missing", "Missing")

    def test_edit_button_opens_selected_account_name_form(self):
        with terminal(account_service=self.accounts) as (view, _):
            view._read_in_background = immediate
            view.refresh_sidebar = Mock()
            view.refresh_provider_catalog = Mock()
            view.select_account(self.gemini)
            view.settings_dialog.open("accounts")
            view.forms.open = Mock()
            view.session_dialog.edit_account_button.handler()
            _, fields, submit = view.forms.open.call_args.args
            self.assertEqual(fields[0][0], "label")
            self.assertEqual(fields[0][2], "Gemini personal")
            submit({"label": "Gemini work"})
            self.assertEqual(self.accounts.item(self.gemini)["label"], "Gemini work")
            self.assertEqual(view.session_state["account"], "Gemini work")
            self.assertIn("Gemini work", view.session_dialog.render_accounts()[1][1])

    def test_remove_account_clears_credentials_preferences_and_selection_on_restart(self):
        key = self.gemini
        self.accounts.select(key)
        self.accounts.remember(key, "model", "effort")
        path = self.accounts.credential_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"__Secure-1PSID":"test"}')
        keep = path.parent / "history-to-keep.txt"
        keep.write_text("local history")
        self.accounts._remember_verification(key)
        self.accounts.remove(key)
        self.assertFalse(path.exists())
        self.assertTrue(keep.exists())
        data = Settings(self.api.settings.path).snapshot()["accounts"]
        self.assertNotIn(key, [x["provider"] + "/" + x["id"] for x in data["items"]])
        self.assertNotIn(key, data["selected"].values())
        self.assertNotIn(key, data["preferences"])
        self.assertEqual(self.accounts.active(), "chatgpt/default")

    def test_last_enabled_account_and_busy_account_are_not_removed(self):
        self.accounts._sending[self.gemini] = 1
        with self.assertRaises(RuntimeError):
            self.accounts.remove(self.gemini)
        self.accounts._sending[self.gemini] = 0
        self.accounts.remove(self.gemini)
        with self.assertRaisesRegex(ValueError, "Son etkin hesap"):
            self.accounts.remove("chatgpt/default")
        self.assertEqual(len(self.accounts.items()), 1)

    def test_account_click_updates_index_focus_and_target_for_profile_actions(self):
        with terminal(account_service=self.accounts) as (view, _):
            view._read_in_background = immediate
            view.refresh_sidebar = Mock()
            view.refresh_provider_catalog = Mock()
            view.settings_dialog.open("accounts")
            from prompt_toolkit.data_structures import Point
            from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

            view.session_dialog.render_accounts()[1][2](
                MouseEvent(Point(0, 1), MouseEventType.MOUSE_UP, MouseButton.LEFT, frozenset())
            )
            self.assertEqual(view.account_key, self.gemini)
            self.assertEqual(view.session_dialog.account_index, 1)
            self.assertIs(view.app.layout.current_control, view.session_dialog.account_control)
            self.assertTrue(view.session_dialog.render_accounts()[1][1].startswith("› "))
            self.assertIn("Gemini personal", view.session_state["account"])

    def test_delete_confirmation_removes_local_account_tabs_and_selects_fallback(self):
        with terminal(account_service=self.accounts) as (view, _):
            view._read_in_background = immediate
            view.refresh_sidebar = Mock()
            view.refresh_provider_catalog = Mock()
            view.provider_filter = {"chatgpt"}
            view.account_filter = {"chatgpt/default"}
            view.settings_dialog.open("accounts")
            view.session_dialog.remove()
            self.assertTrue(view.forms.visible)
            self.assertEqual(len(self.accounts.items()), 2)
            view.forms.confirm_buttons[0].handler()
            self.assertEqual([x["key"] for x in self.accounts.items()], [self.gemini])
            self.assertEqual(view.account_key, self.gemini)
            self.assertIsNone(view.provider_filter)
            self.assertIsNone(view.account_filter)
            self.assertTrue(all(x.get("account_key") != "chatgpt/default" for x in view.tabs))
            self.assertIn("silindi", view.session_dialog.message)
