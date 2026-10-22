"""Multi-provider UI states and shared forms, with in-memory backends."""

import asyncio
import tempfile
import unittest
from unittest.mock import Mock

from providers import Capability
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

    def capabilities(self, key):
        return (
            frozenset({Capability.PROJECTS, Capability.PROJECT_CREATE})
            if key.startswith("chatgpt")
            else frozenset()
        )

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

    def list_chats(self, selected=None, cursor=None, account_keys=None):
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
            v.catalog = ModelCatalog(FeatureState.AVAILABLE, (Model("gpt-model", "GPT"),))
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
                attrs = v.app._merged_style.get_attrs_for_style_str("class:dialog.body")
                self.assertEqual(attrs.bgcolor, "252525")
                self.assertEqual(
                    set(v.forms.fields["accounts"].current_values),
                    {"chatgpt/default", "gemini/personal"},
                )
                v.forms.fields["accounts"].current_values = ["gemini/personal"]
                pipe.send_bytes(b"\x13")
                await self.settle(lambda: not v.forms.visible)
                self.assertEqual(v.provider_filter, {"gemini"})
                v.switch_tab(-1)
                self.assertEqual(v.account_key, "chatgpt/default")
                self.assertEqual(v.model, "gpt-model")
            finally:
                v.app.exit()
                await run

    async def test_new_tabs_reuse_their_provider_catalog_without_refetch(self):
        service = Accounts()
        with interaction.terminal(account_service=service) as (v, _):
            v._read_in_background = immediate
            v.refresh_provider_catalog()
            expected = v.catalog
            v.new_tab()
            self.assertIs(v.catalog, expected)
            self.assertEqual(v.tabs[-1]["account_key"], "chatgpt/default")
            v.select_account("gemini/personal")
            gemini = v.catalog
            self.assertNotEqual(gemini.models[0].id, expected.models[0].id)
            v.new_tab()
            self.assertIs(v.catalog, gemini)
            self.assertEqual(v.tabs[-1]["account_key"], "gemini/personal")
            v.select_tab(0)
            self.assertIs(v.catalog, expected)
            v.open_palette("models")
            self.assertEqual([i["id"] for i in v.palette_items], ["model.select:chatgpt/default"])

    async def test_slow_other_account_catalog_never_blocks_current_refresh(self):
        service = Accounts()
        with interaction.terminal(account_service=service) as (v, _):
            pending = []
            v._read_in_background = lambda *args, **kw: pending.append(args)
            v.refresh_provider_catalog()
            old_read, old_success, old_failure = pending.pop()
            v.bind_account("gemini/personal")
            v._save_active_tab()
            v.refresh_provider_catalog()
            current_read, current_success, _ = pending.pop()
            current_success(current_read())
            gemini = v.catalog
            self.assertFalse(v.catalog_loading)
            old_success(old_read())
            self.assertIs(v.catalog, gemini)
            self.assertFalse(v.catalog_loading)
            self.assertFalse(v._catalog_requests)
            # A discarded epoch must still clear the pending indicator.
            v.refresh_provider_catalog()
            _, _, fail = pending.pop()
            v.session_epoch += 1
            fail("old session")
            self.assertFalse(v.catalog_loading)

    async def test_metadata_refresh_does_not_block_enter_or_hide_send_result(self):
        service = Accounts()
        with interaction.terminal(account_service=service) as (v, pipe):
            v._read_in_background = immediate
            v.refresh_provider_catalog()
            sent = Mock(return_value={})
            v.send_prompt = sent
            run = asyncio.create_task(v.app.run_async())
            try:
                await self.settle(lambda: v.app.is_running)
                v.catalog_loading = v.sidebar_loading = True
                pipe.send_text("Question")
                pipe.send_bytes(b"\r")
                await self.settle(lambda: sent.call_count == 1 and not v.busy)
                self.assertEqual(v.messages[-2]["text"], "Question")
                v.status = "Request stopped: test"
                self.assertIn("Request stopped: test", "".join(f[1] for f in v.render_activity()))
            finally:
                v.app.exit()
                await run

    async def test_old_epoch_completion_cannot_clear_a_new_refresh(self):
        with interaction.terminal(account_service=Accounts()) as (v, _):
            pending = []
            v._read_in_background = lambda *args, **kw: pending.append(args)
            v.refresh_provider_catalog()
            _, _, old_failure = pending.pop()
            v.session_epoch += 1
            v._catalog_requests.clear()
            v.refresh_provider_catalog()
            read, success, _ = pending.pop()
            old_failure("old timeout")
            self.assertTrue(v.catalog_loading)
            success(read())
            self.assertFalse(v.catalog_loading)

    async def test_cached_catalog_selects_a_model_before_first_refresh_completes(self):
        service = Accounts()
        service.selected = "gemini/personal"
        cached = ModelCatalog(FeatureState.AVAILABLE, (Model("gemini-flash", "Flash"),))
        service.cached_models = lambda key: cached if key.startswith("gemini/") else None
        with interaction.terminal(account_service=service) as (v, _):
            self.assertEqual(v.model, "gemini-flash")
            for _ in range(3):
                v.new_tab()
                self.assertEqual(v.model, "gemini-flash")
                self.assertIs(v.catalog, cached)
            v.model = ""
            v.normalize_model_selection()
            self.assertEqual(v.model, "gemini-flash")

    async def test_file_form_focus_and_attach_action(self):
        with interaction.terminal(account_service=Accounts()) as (v, pipe):
            v._read_in_background = immediate
            v.attachment_policy_callback = lambda: {
                "max_files": 10,
                "max_bytes": 1024 * 1024,
                "allowed_extensions": (),
            }
            run = asyncio.create_task(v.app.run_async())
            try:
                await self.settle(lambda: v.app.is_running)
                with tempfile.NamedTemporaryFile(suffix=".pdf") as document:
                    document.write(b"pdf")
                    document.flush()
                    v.open_attachment()
                    self.assertIs(v.app.layout.current_control, v.forms.fields["files"].control)
                    path_form = v.forms.container
                    pipe.send_text(document.name)
                    pipe.send_bytes(b"\x13")
                    await self.settle(lambda: v.forms.container is not path_form)
                    pipe.send_bytes(b"\x13")
                    await self.settle(lambda: bool(v.pending_attachments) and not v.forms.visible)
                    self.assertEqual(v.pending_attachments[0]["name"], "demo.pdf")
                self.assertFalse(v.uploading_image)
            finally:
                v.app.exit()
                await run
