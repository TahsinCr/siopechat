"""Project details share the composer without becoming a conversation themselves."""

import asyncio
import unittest
from unittest.mock import Mock, patch

from providers.models import Choice, FeatureState, Model, ModelCatalog
from tests import tui_support as interaction
from tests.fakes import Accounts
from tests.tui_support import ResizableOutput, screen_text


def project_view(view):
    view.tab_kind = "project"
    view.current_project = {"id": "@chatgpt/default/project", "name": "Example"}
    view.title = "Example"
    view.messages = [{"role": "system", "text": "Project instructions"}]
    view._save_active_tab()


class ProjectComposerTests(unittest.IsolatedAsyncioTestCase):
    settle = staticmethod(interaction.wait_until)

    async def test_details_allow_typing_and_editing_hides_composer_and_metadata(self):
        output = ResizableOutput()
        with interaction.terminal(output=output) as (v, pipe):
            run = asyncio.create_task(v.app.run_async())
            try:
                await self.settle(lambda: v.app.is_running)
                project_view(v)
                v.app.layout.focus(v.input)
                pipe.send_text("project prompt")
                await self.settle(lambda: v.input.text == "project prompt")
                await asyncio.sleep(0.08)
                self.assertIn("Gönder", screen_text(v))
                self.assertIn("Seçilmedi", screen_text(v))
                self.assertIn("Sağlayıcı varsayılanı", screen_text(v))
                v.forms.open(
                    "Projeyi düzenle",
                    [("name", "Proje adı", "Example", None)],
                    lambda _: None,
                    inline_project=v.current_project["id"],
                )
                await asyncio.sleep(0.08)
                self.assertNotIn("Gönder", screen_text(v))
                self.assertNotIn("Seçilmedi", screen_text(v))
                self.assertEqual(list(v.render_chat_header()), [])
                v.forms.close()
                await asyncio.sleep(0.08)
                self.assertIn("Gönder", screen_text(v))
                self.assertEqual(v.input.text, "project prompt")
            finally:
                v.app.exit()
                await run

    def test_first_prompt_opens_separate_chat_with_project_model_and_effort(self):
        with interaction.terminal() as (v, _):
            project_view(v)
            project = dict(v.current_project)
            v.input.text = "Tell me about this project"
            model, effort = v.model, v.effort
            with patch("tui.chat.composer.threading.Thread") as thread:
                v.submit_prompt()
            self.assertEqual(len(v.tabs), 2)
            self.assertEqual(v.tabs[0]["kind"], "project")
            self.assertEqual(v.tabs[0]["messages"][0]["text"], "Project instructions")
            self.assertEqual(v.tabs[0]["draft"], "")
            self.assertEqual(v.tab_kind, "chat")
            self.assertEqual(v.current_project, project)
            args = thread.call_args.kwargs["args"]
            self.assertEqual(args[0], "Tell me about this project")
            self.assertEqual(args[4:8], (None, model, effort, project["id"]))
            self.assertFalse(args[8])
            self.assertEqual(v.messages[0]["role"], "user")
            self.assertEqual(v.input.text, "")

    def test_empty_or_blocked_submission_keeps_project_and_draft(self):
        with interaction.terminal() as (v, _):
            project_view(v)
            v.submit_prompt()
            self.assertEqual(len(v.tabs), 1)
            v.input.text = "Draft"
            v.uploading_image = True
            v.submit_prompt()
            self.assertEqual(v.input.text, "Draft")
            self.assertEqual(v.tab_kind, "project")
            self.assertEqual(len(v.tabs), 1)

    def test_project_model_picker_does_not_fetch_other_accounts_even_without_details(self):
        service = Accounts()
        service.active_accounts = lambda: service.entries
        service.cached_models = Mock()
        with interaction.terminal(account_service=service) as (v, _):
            project_view(v)
            v.messages = []
            v.catalog = ModelCatalog(
                FeatureState.AVAILABLE, (Model("gpt", "GPT", (Choice("medium", "Medium"),)),)
            )
            service.cached_models.reset_mock()
            v.open_palette("models")
            self.assertTrue(v.palette_items)
            self.assertTrue(all(item["id"].startswith("model.select:") for item in v.palette_items))
            service.cached_models.assert_not_called()

    def test_async_project_details_keep_a_draft_typed_during_loading(self):
        with interaction.terminal() as (v, _):
            v.get_project_callback = lambda _: {}
            v._read_in_background = Mock()
            v.show_project_details("@chatgpt/default/p")
            v.input.text = "Do not lose this"
            loaded = v._read_in_background.call_args.args[1]
            loaded({"id": "@chatgpt/default/p", "name": "Loaded", "instructions": "Rules"})
            self.assertEqual(v.input.text, "Do not lose this")
