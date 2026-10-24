"""Public configuration, headless boundaries, themes and keyboard UX."""

import asyncio
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from providers.models import Choice, FeatureState, Model, ModelCatalog
from settings import SettingsCatalog
from tests import cli_support as interaction
from tests.account_fixture import configured_application as create_application
from tests.cli_support import immediate
from theme import ThemeService


class ProductServicesTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.api = create_application(self.root)
        self.addCleanup(self.api.runtime.close)
        self.accounts = self.api.get("accounts")
        self.gemini = self.accounts.add("gemini", "Gemini")

    def test_provider_flags_replace_default_and_keep_one_active(self):
        self.accounts.select(self.gemini)
        self.accounts.set_enabled("gemini", False)
        self.assertEqual(self.accounts.active(), "chatgpt/default")
        with self.assertRaises(ValueError):
            self.accounts.set_enabled("chatgpt", False)
        with self.assertRaises(ValueError):
            self.accounts.select(self.gemini)
        self.accounts.set_enabled("gemini", True)
        self.accounts.select(self.gemini)
        rows = [
            r for r in SettingsCatalog(self.api).rows() if r["section"].startswith("providers.")
        ]
        self.assertEqual(
            {r["section"] for r in rows},
            {"providers.chatgpt", "providers.gemini", "providers.claude"},
        )
        self.assertNotIn("default", self.api.settings.snapshot()["providers"])

    def test_account_preferences_survive_restart_and_theme_changes(self):
        self.accounts.select(self.gemini)
        self.accounts.remember(self.gemini, "flash", "extended")
        catalog = ModelCatalog(
            FeatureState.AVAILABLE, (Model("flash", "Flash", (Choice("extended", "Extended"),)),)
        )
        self.accounts._catalogs[self.gemini] = (time.monotonic(), catalog)
        with interaction.terminal(account_service=self.accounts) as (v, _):
            self.assertEqual((v.model, v.effort), ("flash", "extended"))
            v.apply_preferences(
                {
                    "settings": self.api.settings.snapshot(),
                    "tokens": self.api.themes.resolve("default"),
                }
            )
            self.assertEqual((v.model, v.effort), ("flash", "extended"))
            v.new_tab()
            self.assertEqual((v.model, v.effort), ("flash", "extended"))
        fresh = create_application(self.root)
        try:
            self.assertEqual(
                fresh.get("accounts").preference(self.gemini),
                {"model": "flash", "effort": "extended"},
            )
        finally:
            fresh.runtime.close()

    def test_explicit_default_effort_remains_an_absent_override(self):
        self.accounts.select(self.gemini)
        self.accounts.remember(self.gemini, "flash", None)
        catalog = ModelCatalog(
            FeatureState.AVAILABLE,
            (Model("flash", "Flash", (Choice("extended", "Extended"),), "extended"),),
        )
        self.accounts._catalogs[self.gemini] = (time.monotonic(), catalog)
        with interaction.terminal(account_service=self.accounts) as (v, _):
            v._read_in_background = immediate
            self.assertIsNone(v.effort)
            v.refresh_provider_catalog()
            self.assertIsNone(v.effort)
            v.new_tab()
            self.assertIsNone(v.effort)

    def test_disabling_a_provider_does_not_relabel_its_open_conversation(self):
        self.accounts.select(self.gemini)
        self.accounts._catalogs[self.gemini] = (
            time.monotonic(),
            ModelCatalog(FeatureState.AVAILABLE, (Model("flash", "Flash"),)),
        )
        with interaction.terminal(
            account_service=self.accounts,
            conversation_id="@" + self.gemini + "/c_demo",
            initial_messages=[{"role": "user", "text": "Keep"}],
        ) as (v, _):
            v._read_in_background = lambda *args, **kwargs: None
            self.accounts.set_enabled("gemini", False)
            v.apply_preferences(
                {
                    "settings": self.api.settings.snapshot(),
                    "tokens": self.api.themes.resolve("default"),
                }
            )
            self.assertEqual(v.tabs[0]["account_key"], self.gemini)
            self.assertEqual(v.account_key, "chatgpt/default")
            self.assertIsNone(v.conversation_id)
            self.assertEqual(v.tabs[0]["messages"][0]["text"], "Keep")

    def test_python_theme_and_extension_discovery_are_isolated(self):
        from extension import ExtensionSpec

        directory = self.root / "themes"
        directory.mkdir()
        (directory / "custom.py").write_text(
            'THEME = {"id": "custom", "tokens": {"accent": "#abcdef"}, "options": {"tab_width": 20}}'
        )
        (directory / "broken.py").write_text('raise RuntimeError("bad theme")')
        service = ThemeService()
        service.load_directory(directory)
        self.assertEqual(service.resolve("custom")["options"]["tab_width"], 20)
        self.assertIn("broken.py", service.failures)
        with self.assertRaises(ValueError):
            service.register("test", "bad", {}, options={"tab_width": 1000})
        directory = self.root / "extensions" / "sample"
        directory.mkdir(parents=True)
        (directory / "manifest.json").write_text('{"id":"sample","api_version":2,"requires":[]}')
        (directory / "helper.py").write_text('VALUE = "ok"')
        (directory / "plugin.py").write_text(
            'from extension import ExtensionSpec, Action\nfrom .helper import VALUE\ndef activate(ctx):\n    ctx.action(Action("sample.hello", "Hello", lambda: VALUE))\nSPEC = ExtensionSpec("sample", activate, api_version=2)\n'
        )
        manager = self.api.get("extensions")
        with self.assertRaises(ValueError):
            manager.register(ExtensionSpec("old_api", lambda _: None, api_version=1))
        manager.load_directory(directory.parent)
        self.assertEqual(asyncio.run(self.api.actions.invoke("sample.hello")), "ok")
        self.api.get("extensions").disable("sample")
        self.assertFalse(self.api.actions.search("sample.hello"))

    def test_disabled_extension_is_not_imported_until_explicit_enable(self):
        directory = self.root / "extensions" / "lazy"
        directory.mkdir(parents=True)
        marker = directory / "imported"
        (directory / "manifest.json").write_text('{"id":"lazy","api_version":2,"requires":[]}')
        (directory / "plugin.py").write_text(
            'from pathlib import Path\nfrom extension import ExtensionSpec\nPath(__file__).with_name("imported").touch()\nSPEC = ExtensionSpec("lazy", lambda ctx: None, api_version=2)\n'
        )
        self.api.settings.update_extension("lazy", {"enabled": False})
        manager = self.api.get("extensions")
        manager.load_directory(directory.parent)
        self.assertFalse(marker.exists())
        self.assertEqual(manager.statuses()["lazy"].state, "disabled")
        self.assertTrue(manager.enable("lazy"))
        self.assertTrue(marker.exists())

    def test_extension_without_manifest_is_rejected_before_import(self):
        directory = self.root / "extensions" / "manifest_required"
        directory.mkdir(parents=True)
        marker = directory / "imported"
        (directory / "plugin.py").write_text(
            "from pathlib import Path\n"
            'Path(__file__).with_name("imported").touch()\n'
            'raise RuntimeError("must not import")\n'
        )

        manager = self.api.get("extensions")
        manager.load_directory(directory.parent)

        self.assertFalse(marker.exists())
        self.assertEqual(manager.statuses()["manifest_required"].state, "failed")

    def test_application_composition_does_not_import_cli(self):
        script = """import builtins,tempfile
from pathlib import Path
original = builtins.__import__
def guarded(name,*args,**kwargs):
    if not (args[3] if len(args) > 3 else kwargs.get('level', 0)) and (name == 'cli' or name.startswith('cli.')):
        raise AssertionError(name)
    return original(name,*args,**kwargs)
builtins.__import__ = guarded
from tests.account_fixture import configured_application as create_application
with tempfile.TemporaryDirectory() as folder:
    api = create_application(Path(folder))
    api.runtime.close()
"""
        result = subprocess.run([os.sys.executable, "-c", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class ProductKeyboardTests(unittest.IsolatedAsyncioTestCase):
    settle = staticmethod(interaction.wait_until)

    async def test_extension_action_palette_collects_required_arguments(self):
        from unittest.mock import Mock

        invoke = Mock(return_value={"ok": True})

        def actions():
            return [
                {
                    "id": "example.run",
                    "title": "Example",
                    "parameters": ({"key": "name", "label": "Name", "required": True},),
                }
            ]

        with interaction.terminal(list_actions=actions, invoke_action=invoke) as (v, pipe):
            run = asyncio.create_task(v.app.run_async())
            try:
                await self.settle(lambda: v.app.is_running)
                v.run_action("example.run")
                self.assertTrue(v.forms.visible)
                v.forms.fields["name"].text = "Demo"
                pipe.send_bytes(b"\x13")
                await self.settle(lambda: invoke.called and not v.background_jobs)
                invoke.assert_called_once_with("example.run", {"name": "Demo"})
            finally:
                v.app.exit()
                await run

    async def test_invalid_frontend_theme_falls_back_without_losing_context(self):
        with interaction.terminal() as (v, _):
            service = ThemeService()
            service.register("test", "broken", {}, markdown={"markdown.text": "not-a-real-style"})
            v.apply_preferences({"tokens": service.resolve("broken")})
            self.assertIn("varsayılan", v.status)
            self.assertEqual(v.theme_options, {})

    async def test_ctrl_d_responds_with_draft_and_ctrl_p_works_from_settings(self):
        with interaction.terminal(settings_rows=lambda: []) as (v, pipe):
            v._read_in_background = immediate
            run = asyncio.create_task(v.app.run_async())
            try:
                await self.settle(lambda: v.app.is_running)
                pipe.send_text("draft")
                pipe.send_bytes(b"\x04")
                await self.settle(lambda: "tekrar" in v.status)
                self.assertTrue(v.app.is_running)
                pipe.send_bytes(b"\x04")
                await asyncio.wait_for(run, 2)
                self.assertFalse(v.app.is_running)
            finally:
                if v.app.is_running:
                    v.app.exit()
                    await run
        with interaction.terminal(settings_rows=lambda: []) as (v, pipe):
            v._read_in_background = immediate
            run = asyncio.create_task(v.app.run_async())
            try:
                await self.settle(lambda: v.app.is_running)
                v.settings_dialog.open()
                pipe.send_bytes(b"\x10")
                await self.settle(lambda: v.palette_visible)
                v.close_palette()
                self.assertEqual(v.app.layout.current_control, v.settings_dialog.category_control)
                pipe.send_bytes(b"\x10")
                await self.settle(lambda: v.palette_visible)
                v.palette_index = next(
                    i for i, item in enumerate(v.palette_filtered) if item["id"] == "chat.new"
                )
                pipe.send_bytes(b"\r")
                await self.settle(lambda: v.tab_kind == "chat" and not v.palette_visible)
            finally:
                v.app.exit()
                await run
