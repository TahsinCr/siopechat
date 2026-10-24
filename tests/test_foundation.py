import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bootstrap import create_application
from cli.settings import SettingsPanel
from extension import Action, ExtensionAPI, ExtensionManager, ExtensionSpec, SettingField, View
from settings import Settings


class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.api = ExtensionAPI(Settings(self.root / "config.json"))
        self.manager = ExtensionManager(self.api)
        self.api.provide("application", "extensions", self.manager)

    def test_failing_availability_does_not_hide_unrelated_actions(self):
        def broken():
            raise RuntimeError("broken extension availability")

        self.api.actions.register(
            "broken", Action("broken.run", "Broken", lambda: None, available=broken)
        )
        self.api.actions.register("healthy", Action("healthy.run", "Healthy", lambda: 42))
        self.assertEqual([action.id for action in self.api.actions.search()], ["healthy.run"])
        self.assertEqual(asyncio.run(self.api.actions.invoke("healthy.run")), 42)

    def test_activation_failure_rolls_back_contributions_and_resources(self):
        closed = Mock()

        def activate(context):
            context.on_dispose(closed)
            context.provide("temporary", object())
            context.action(Action("broken.run", "Broken", lambda: None))
            context.settings(SettingField("count", int, 1, "broken.count"))
            context.translations("en", {"count": "Count"})
            context.view(View("broken", "Broken", lambda: None))
            context.theme("broken-theme", {"accent": "#123456"})
            raise RuntimeError("secret diagnostic")

        self.manager.register(ExtensionSpec("broken", activate, api_version=2))
        self.assertFalse(self.manager.activate("broken"))
        closed.assert_called_once()
        self.assertEqual(self.manager.statuses()["broken"].reason, "activation_failed")
        with self.assertRaises(LookupError):
            self.api.get("temporary")
        self.assertFalse(self.api.actions.search())
        self.assertNotIn("broken", self.api.settings.fields())
        self.assertNotIn("broken.count", self.api.languages.merged("en"))
        self.assertNotIn("broken-theme", self.api.themes.available())

    def test_disable_dependency_releases_dependents_and_enable_recovers_them(self):
        def base(context):
            context.provide("archive", "archive-service")

        def transfer(context):
            context.provide("transfer", context.api.get("archive"))

        self.manager.register(ExtensionSpec("archive", base, api_version=2))
        self.manager.register(
            ExtensionSpec("transfer", transfer, api_version=2, requires=("archive",))
        )
        self.assertTrue(self.manager.activate("transfer"))
        self.manager.disable("archive")
        self.assertEqual(self.manager.statuses()["transfer"].state, "unavailable")
        self.assertNotEqual(self.api.settings.extension("transfer").get("enabled"), False)
        with self.assertRaises(LookupError):
            self.api.get("transfer")
        self.assertTrue(self.manager.enable("archive"))
        self.assertEqual(self.api.get("transfer"), "archive-service")

    def test_missing_or_cyclic_dependencies_do_not_execute_extensions(self):
        activate = Mock()
        self.manager.register(
            ExtensionSpec("missing", activate, api_version=2, requires=("unknown",))
        )
        self.assertFalse(self.manager.activate("missing"))
        activate.assert_not_called()
        self.manager.register(ExtensionSpec("a", activate, api_version=2, requires=("b",)))
        self.manager.register(ExtensionSpec("b", activate, api_version=2, requires=("a",)))
        with self.assertRaises(ValueError):
            self.manager.activate("a")
        activate.assert_not_called()

    def test_unrelated_extension_survives_failure_and_cleanup_errors(self):
        self.manager.register(
            ExtensionSpec("healthy", lambda ctx: ctx.provide("healthy", 42), api_version=2)
        )
        self.manager.register(
            ExtensionSpec(
                "bad", lambda ctx: lambda: (_ for _ in ()).throw(ValueError()), api_version=2
            )
        )
        self.manager.activate("healthy")
        self.manager.activate("bad")
        self.manager.disable("bad")
        self.assertEqual(self.api.get("healthy"), 42)
        self.assertEqual(self.manager.statuses()["bad"].reason, "deactivation_failed")

    def test_disabling_unknown_extension_does_not_persist_it(self):
        with self.assertRaises(KeyError):
            self.manager.disable("unknown")
        self.assertNotIn("unknown", self.api.settings.snapshot()["extensions"])

    def test_shared_settings_panel_routes_toggle_through_extension_lifecycle(self):
        self.manager.register(
            ExtensionSpec("sample", lambda ctx: ctx.provide("sample", 1), api_version=2)
        )
        self.manager.activate("sample")
        panel = SettingsPanel(self.api)
        asyncio.run(panel.change("extensions.sample", "enabled", False))
        with self.assertRaises(LookupError):
            self.api.get("sample")
        self.assertTrue(any(row["section"] == "extensions.sample" for row in panel.rows()))
        asyncio.run(panel.change("extensions.sample", "enabled", True))
        self.assertEqual(self.api.get("sample"), 1)

    def test_extension_settings_validate_without_losing_other_packages(self):
        self.api.settings.update_extension("other", {"custom": {"nested": 7}})
        self.api.settings.register("sample", (SettingField("limit", int, 10, "sample.limit"),))
        self.api.settings.update_extension("sample", {"limit": 20})
        previous = self.api.settings.path.read_bytes()
        with self.assertRaises(ValueError):
            self.api.settings.update_extension("sample", {"limit": True})
        self.assertEqual(self.api.settings.path.read_bytes(), previous)
        self.api.settings.unregister("sample")
        reopened = Settings(self.api.settings.path)
        self.assertEqual(reopened.extension("sample")["limit"], 20)
        self.assertEqual(reopened.extension("other")["custom"], {"nested": 7})

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
        self.api.languages.register("first", "en", {"title": "First", "fallback": "English"})
        self.api.languages.register("first", "tr", {"title": "Birinci"})
        self.api.languages.register("second", "tr", {"title": "İkinci"})
        self.assertEqual(self.api.languages.translate("first.title", "tr-TR"), "Birinci")
        self.assertEqual(self.api.languages.translate("first.fallback", "tr"), "English")
        self.assertEqual(self.api.languages.translate("second.title", "tr"), "İkinci")
        self.api.languages.unregister("first")
        self.assertEqual(self.api.languages.translate("second.title", "tr"), "İkinci")
        self.assertEqual(self.api.languages.translate("first.title", "tr"), "first.title")

    def test_actions_share_dispatch_context_and_shortcut_overrides(self):
        callback = Mock(return_value=42)
        enabled = False
        self.api.actions.register(
            "sample", Action("sample.run", "Run", callback, "f2", lambda: enabled)
        )
        self.assertFalse(self.api.actions.search("run"))
        with self.assertRaises(ValueError):
            asyncio.run(self.api.actions.invoke("sample.run"))
        callback.assert_not_called()
        enabled = True
        self.assertEqual(asyncio.run(self.api.actions.invoke("sample.run", value=1)), 42)
        callback.assert_called_once_with(value=1)
        self.assertEqual(self.api.actions.keymap({"sample.run": "f3"}), {"f3": "sample.run"})

    def test_views_are_lazy_and_closing_view_does_not_close_jobs(self):
        factory = Mock(return_value=object())
        self.api.views.register("sample", View("view", "View", factory))
        factory.assert_not_called()
        self.assertIs(self.api.views.open("view"), self.api.views.open("view"))
        factory.assert_called_once()
        self.api.views.close_tab("view")
        self.assertFalse(self.api.views.tabs())
        self.assertFalse(self.api.tasks._closed)

    def test_shipped_application_wires_layout_languages_settings_and_data(self):
        api = create_application(self.root)
        self.assertTrue((self.root / "config.json").is_file())
        self.assertTrue((self.root / "data").is_dir())
        self.assertEqual(api.get("extensions").statuses()["layout"].state, "active")
        api.settings.update("application", {"language": "tr"})
        rows = asyncio.run(api.actions.invoke("settings.show"))
        self.assertTrue(
            any(row.get("title") == "Çalışma alanı düzenlerini etkinleştir" for row in rows)
        )
        created = asyncio.run(api.actions.invoke("layout.create", name="Research"))
        loaded = asyncio.run(api.actions.invoke("layout.show", workspace_id=created["id"]))
        self.assertEqual(created, loaded)
