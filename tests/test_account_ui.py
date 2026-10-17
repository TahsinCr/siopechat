"""Multi-provider UI states and shared forms, with in-memory backends."""

import asyncio
import unittest
from unittest.mock import Mock

from providers.models import Choice, FeatureState, Model, ModelCatalog
from tests import test_cli_interaction as interaction
from tests.test_ui_controls import immediate


class Accounts:
    def __init__(self):
        self.selected = "chatgpt/default"
        self.entries = [
            {
                "key": "chatgpt/default",
                "provider": "chatgpt",
                "id": "default",
                "label": "GPT",
                "icon": "◈",
            },
            {
                "key": "gemini/personal",
                "provider": "gemini",
                "id": "personal",
                "label": "Gemini",
                "icon": "✦",
            },
        ]
        self.backend = Mock()

    def active(self):
        return self.selected

    def items(self):
        return self.entries

    def status(self, key):
        return {"saved_snapshot": False, "authenticated": False, "account": key}

    def cache(self, key):
        return self.backend

    def select(self, key):
        self.selected = key

    def list_chats(self, selected=None, cursor=None):
        return {"items": [], "cursor": None}

    def callbacks(self, key):
        model = Model(key, key, (Choice("standard", "Standart"),))
        return {
            "load_catalog": lambda: ModelCatalog(FeatureState.AVAILABLE, (model,)),
            "list_projects_callback": lambda cursor=None: {"items": [], "cursor": None},
            "session_action": lambda _: self.status(key),
            "attach_file_callback": lambda path: {"id": "file-test", "name": "demo.pdf"},
            "load_conversation": lambda _: {"messages": []},
        }


class AccountUiTests(unittest.IsolatedAsyncioTestCase):
    settle = interaction.InputTests.settle

    async def test_accounts_do_not_rewrite_other_tab_models_and_filter_is_multiselect(self):
        service = Accounts()
        with interaction.terminal(
            account_service=service, initial_messages=[{"role": "user", "text": "Keep GPT"}]
        ) as (v, pipe):
            v._read_in_background = immediate
            v.model = "gpt-model"
            v._save_active_tab()
            run = asyncio.create_task(v.app.run_async())
            try:
                await self.settle(lambda: v.app.is_running)
                v.select_account("gemini/personal")
                self.assertEqual(v.tabs[0]["account_key"], "chatgpt/default")
                self.assertEqual(v.tabs[0]["model"], "gpt-model")
                self.assertEqual(v.provider_id, "gemini")
                self.assertFalse(
                    any(f[0] == "class:metadata.project" for f in v.render_chat_header())
                )
                v.open_filters()
                self.assertTrue(v.forms.visible)
                self.assertEqual(
                    set(v.forms.fields["providers"].current_values), {"chatgpt", "gemini"}
                )
                v.forms.fields["providers"].current_values = ["gemini"]
                pipe.send_bytes(b"\x13")
                await self.settle(lambda: not v.forms.visible)
                self.assertEqual(v.provider_filter, {"gemini"})
                v.switch_tab(-1)
                self.assertEqual(v.account_key, "chatgpt/default")
                self.assertEqual(v.model, "gpt-model")
            finally:
                v.app.exit()
                await run

    async def test_file_form_focus_and_attach_action(self):
        with interaction.terminal(account_service=Accounts()) as (v, pipe):
            v._read_in_background = immediate
            run = asyncio.create_task(v.app.run_async())
            try:
                await self.settle(lambda: v.app.is_running)
                v.open_attachment()
                self.assertIs(v.app.layout.current_control, v.forms.fields["path"].control)
                pipe.send_text("/tmp/demo.pdf")
                pipe.send_bytes(b"\x13")
                await self.settle(lambda: not v.forms.visible)
                self.assertEqual(v.pending_attachments[0]["name"], "demo.pdf")
                self.assertFalse(v.uploading_image)
            finally:
                v.app.exit()
                await run
