import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from actions import Action, ActionService
from bootstrap import Application, create_application
from runtime import Runtime
from settings import Settings
from tui.application import register_workspace_actions
from tui.lang import Catalog
from tui.settings import SettingsPanel
from tui.workspaces import Workspaces


class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.runtime = Runtime()
        self.addCleanup(self.runtime.close)
        self.settings = Settings(self.root / "config.json", events=self.runtime.events)
        self.actions = ActionService(self.runtime.events)
        self.api = Application(self.settings, self.runtime, self.actions)

    def test_failing_availability_does_not_hide_unrelated_actions(self):
        def broken():
            raise RuntimeError("broken availability")

        self.api.actions.register(
            "broken", Action("broken.run", "Broken", lambda: None, available=broken)
        )
        self.api.actions.register("healthy", Action("healthy.run", "Healthy", lambda: 42))
        self.assertEqual([action.id for action in self.api.actions.search()], ["healthy.run"])
        self.assertEqual(asyncio.run(self.api.actions.invoke("healthy.run")), 42)

    def test_settings_validate_without_losing_other_sections(self):
        self.api.settings.update(
            "accounts",
            {
                "items": [{"provider": "chatgpt", "id": "work", "label": "Work", "port": 9222}],
                "active": "chatgpt/work",
            },
        )
        self.api.settings.update("application", {"mouse": False})
        previous = self.api.settings.path.read_bytes()
        with self.assertRaises(ValueError):
            self.api.settings.update("application", {"mouse": "not-a-bool"})
        self.assertEqual(self.api.settings.path.read_bytes(), previous)
        reopened = Settings(self.api.settings.path)
        self.assertFalse(reopened.snapshot()["application"]["mouse"])
        self.assertEqual(reopened.snapshot()["accounts"]["active"], "chatgpt/work")

    def test_shared_settings_panel_routes_provider_toggle(self):
        api = create_application(self.root / "app")
        try:
            panel = SettingsPanel(api)
            asyncio.run(panel.change("providers.gemini", "enabled", False))
            self.assertFalse(api.accounts.enabled("gemini"))
            self.assertTrue(any(row["section"] == "providers.gemini" for row in panel.rows()))
            asyncio.run(panel.change("providers.gemini", "enabled", True))
            self.assertTrue(api.accounts.enabled("gemini"))
        finally:
            api.runtime.close()

    def test_failed_atomic_settings_write_preserves_file_and_memory(self):
        self.api.settings.save()
        before = self.api.settings.snapshot()
        with patch("utils.files.os.replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                self.api.settings.update("application", {"language": "tr"})
        self.assertEqual(self.api.settings.snapshot(), before)
        self.assertEqual(json.loads(self.api.settings.path.read_text()), before)
        self.assertFalse(list(self.root.glob(".config-*.tmp")))

    def test_language_packages_merge_namespaces_and_fallback(self):
        languages = Catalog()
        languages.register("first", "en", {"title": "First", "fallback": "English"})
        languages.register("first", "tr", {"title": "Birinci"})
        languages.register("second", "tr", {"title": "İkinci"})
        self.assertEqual(languages.translate("first.title", "tr-TR"), "Birinci")
        self.assertEqual(languages.translate("first.fallback", "tr"), "English")
        self.assertEqual(languages.translate("second.title", "tr"), "İkinci")
        languages.unregister("first")
        self.assertEqual(languages.translate("second.title", "tr"), "İkinci")
        self.assertEqual(languages.translate("first.title", "tr"), "first.title")

    def test_actions_share_dispatch_context_and_shortcut_overrides(self):
        callback = Mock(return_value=42)
        enabled = False
        self.api.actions.register(
            "sample", Action("sample.run", "Run", callback, "f2", lambda: enabled)
        )
        self.assertFalse(self.api.actions.search("run"))
        self.assertEqual([action.id for action in self.api.actions.catalog()], ["sample.run"])
        self.assertFalse(self.api.actions.is_available("sample.run"))
        with self.assertRaises(ValueError):
            asyncio.run(self.api.actions.invoke("sample.run"))
        callback.assert_not_called()
        enabled = True
        self.assertTrue(self.api.actions.is_available("sample.run"))
        self.assertEqual(asyncio.run(self.api.actions.invoke("sample.run", value=1)), 42)
        callback.assert_called_once_with(value=1)
        self.assertEqual(self.api.actions.keymap({"sample.run": "f3"}), {"f3": "sample.run"})

    def test_shipped_application_wires_workspaces_languages_settings_and_data(self):
        api = create_application(self.root / "shipped")
        try:
            workspaces = Workspaces(api.get("data_root") / "workspaces.sqlite3")
            register_workspace_actions(api, workspaces)
            api.settings.update("application", {"language": "tr"})
            rows = SettingsPanel(api).rows()
            self.assertTrue(any(row.get("title") == "Dil" for row in rows))
            created = asyncio.run(api.actions.invoke("layout.create", name="Research"))
            loaded = asyncio.run(api.actions.invoke("layout.show", workspace_id=created["id"]))
            self.assertEqual(created, loaded)
        finally:
            api.runtime.close()

    def test_application_uses_independent_storage_path_overrides(self):
        root = self.root.resolve()
        overrides = {
            "SIOPECHAT_CONFIG_PATH": str(root / "settings.json"),
            "SIOPECHAT_DATA_DIR": str(root / "account-data"),
            "SIOPECHAT_STATE_DIR": str(root / "state"),
            "SIOPECHAT_DOWNLOADS_DIR": str(root / "saved-files"),
            "SIOPECHAT_CACHE_DIR": str(root / "cache"),
            "SIOPECHAT_RUNTIME_DIR": str(root / "runtime"),
        }
        with patch.dict(os.environ, overrides):
            api = create_application(root)
            try:
                data_root = root / "account-data"
                self.assertEqual(api.settings.path, root / "settings.json")
                self.assertEqual(api.get("data_root"), data_root)
                self.assertEqual(api.get("state_root"), root / "state")
                self.assertEqual(api.get("accounts").data_root, data_root)
                self.assertEqual(api.get("downloads").directory(), root / "saved-files")
                self.assertEqual(api.get("cache_root"), root / "cache")
                self.assertEqual(api.get("runtime_root"), root / "runtime")
                workspaces = Workspaces(data_root / "workspaces.sqlite3")
                self.assertEqual(workspaces.path, data_root / "workspaces.sqlite3")
            finally:
                api.runtime.close()
