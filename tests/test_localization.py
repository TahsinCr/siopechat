"""Locale coverage, real terminal labels and release document consistency."""

import ast
import asyncio
import copy
import json
import re
import string
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bootstrap import create_application
from cli.actions import _ACTION_NAMES, action_title
from cli.library import SORT_OPTIONS
from language import Catalog, literal
from settings import DEFAULTS
from tests import cli_support as interaction
from tests.cli_support import ResizableOutput, screen_text

ROOT = Path(__file__).resolve().parents[1]


def bundles():
    return {
        locale: json.loads((ROOT / "src/lang" / (locale + ".json")).read_text(encoding="utf-8"))
        for locale in ("en", "tr", "ru")
    }


def catalog():
    result = Catalog()
    for locale, messages in bundles().items():
        result.register("application", locale, messages)
    return result


class LocalizationTests(unittest.TestCase):
    def test_bundled_locales_have_identical_keys_and_template_arguments(self):
        values = bundles()
        formatter = string.Formatter()

        def fields(text):
            return {field for _, field, _, _ in formatter.parse(text) if field is not None}

        for locale in ("tr", "ru"):
            self.assertEqual(set(values["en"]), set(values[locale]))
            for key, source in values["en"].items():
                self.assertTrue(values[locale][key].strip(), (locale, key))
                self.assertEqual(fields(source), fields(values[locale][key]), (locale, key))
        self.assertFalse(any(re.search("[çğıöşüÇĞİÖŞÜ]", value) for value in values["ru"].values()))

    def test_source_calls_actions_and_sort_labels_are_covered(self):
        translated = bundles()["ru"]
        for source in (
            *_ACTION_NAMES.values(),
            *(action_title(key) for key in _ACTION_NAMES),
            *(title for _, title in SORT_OPTIONS),
        ):
            self.assertIn("ui." + source, translated)
        for filename in [*ROOT.glob("src/cli/*.py"), ROOT / "extensions/layout/panes.py"]:
            for node in ast.walk(ast.parse(filename.read_text(encoding="utf-8"))):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "tr"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                ):
                    self.assertIn(
                        "ui." + node.args[0].value.strip(), translated, (filename.name, node.lineno)
                    )

    def test_literal_lookup_retains_layout_and_unknown_content(self):
        messages = catalog().merged("ru")
        self.assertEqual(literal(messages, "  Kaydet \n"), "  Сохранить \n")
        self.assertEqual(literal(messages, "my private project"), "my private project")
        self.assertEqual(literal(messages, " \n "), " \n ")
        self.assertEqual(catalog().merged("ru-RU")["application.save"], "Сохранить")

    def test_russian_is_available_with_both_bundled_extensions(self):
        with tempfile.TemporaryDirectory() as directory:
            api = create_application(Path(directory))
            try:
                self.assertIn("ru", api.languages.locales())
                api.settings.update("application", {"language": "ru"})
                merged = api.languages.merged("ru")
                self.assertIn("компоновки", merged["layout.enabled"])
                self.assertEqual(merged["archive.enabled"], "Локальный архив и синхронизация")
                from settings import SettingsCatalog

                rows = SettingsCatalog(api).rows()
                self.assertIn(
                    "ru", next(row["choices"] for row in rows if row["key"] == "language")
                )
            finally:
                api.runtime.close()

    def test_bundled_extensions_do_not_depend_on_checkout_location(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("bootstrap.__file__", str(root / "installed" / "bootstrap.py")):
                api = create_application(root / "home")
            try:
                statuses = api.get("extensions").statuses()
                self.assertEqual(statuses["layout"].state, "active")
                self.assertEqual(statuses["archive"].state, "active")
            finally:
                api.runtime.close()


class RussianTerminalTests(unittest.IsolatedAsyncioTestCase):
    settle = staticmethod(interaction.wait_until)

    async def test_russian_composer_settings_confirmation_and_help(self):
        output = ResizableOutput()
        with interaction.terminal(output=output) as (view, pipe):
            config = copy.deepcopy(DEFAULTS)
            config["application"]["language"] = "ru"
            view.apply_preferences({"settings": config, "translations": catalog().merged("ru")})
            run = asyncio.create_task(view.app.run_async())
            try:
                await self.settle(lambda: view.app.is_running)
                await asyncio.sleep(0.08)
                text = screen_text(view)
                for word in ("Модель:", "Усилие:", "Аккаунты", "Напишите сообщение"):
                    self.assertIn(word, text)
                view.forms.confirm(view.tr("Sohbeti sil"), Mock())
                await asyncio.sleep(0.06)
                text = screen_text(view)
                for word in ("Удалить навсегда?", "Да", "Нет"):
                    self.assertIn(word, text)
                pipe.send_bytes(b"\r")
                await self.settle(lambda: not view.forms.visible)
                view.handle_command("/help")
                self.assertIn("**Команды**", view.messages[-1]["text"])
                view.handle_command("/help")
                view.settings_dialog.open()
                await asyncio.sleep(0.06)
                self.assertIn("Общие", screen_text(view))
                self.assertIn("Сохранить", screen_text(view))
                self.assertIn("Закрыть", screen_text(view))
            finally:
                view.app.exit()
                await run

    def test_thinking_and_pending_reply_labels_follow_renderer_locale(self):
        with interaction.terminal() as (view, _):
            config = copy.deepcopy(DEFAULTS)
            config["application"]["language"] = "ru"
            view.apply_preferences({"settings": config, "translations": catalog().merged("ru")})
            thinking = view.renderer.render("reasoning", "Native published text", 80, False, False)
            waiting = view.renderer.render("assistant", "", 80, False, True)
            self.assertIn("Сводка размышлений", thinking)
            self.assertIn("Native published text", thinking)
            self.assertIn("Подготовка ответа", waiting)

    def test_project_labels_translate_without_changing_user_content(self):
        with interaction.terminal() as (view, _):
            view.translations = catalog().merged("ru")
            project = {"name": "Kaydet", "instructions": "Talimat yok.", "resources": []}
            value = view.project_messages(project)[0]["text"]
            self.assertIn("# Kaydet", value)
            self.assertIn("Talimat yok.", value)
            self.assertIn("## Инструкции", value)
            self.assertIn("## Источники", value)
