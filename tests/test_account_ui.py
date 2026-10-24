"""Multi-provider UI states and shared forms, with in-memory backends."""

import asyncio
import tempfile
import unittest
from unittest.mock import Mock, patch

from providers.models import FeatureState, Model, ModelCatalog
from tests import cli_support as interaction
from tests.cli_support import immediate
from tests.fakes import Accounts


class AccountUiTests(unittest.IsolatedAsyncioTestCase):
    settle = staticmethod(interaction.wait_until)

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

    async def test_new_chat_status_counts_all_enabled_provider_catalogs(self):
        service = Accounts()
        service.entries.append(
            {
                "key": "claude/work",
                "provider": "claude",
                "id": "work",
                "label": "Claude",
                "icon": "✳",
            }
        )
        service.active_accounts = lambda: service.entries
        catalogs = {
            "chatgpt/default": ModelCatalog(
                FeatureState.AVAILABLE, (Model("g1", "GPT 1"), Model("g2", "GPT 2"))
            ),
            "gemini/personal": ModelCatalog(
                FeatureState.AVAILABLE,
                (Model("gm1", "Gemini 1"), Model("gm2", "Gemini 2"), Model("gm3", "Gemini 3")),
            ),
            "claude/work": ModelCatalog(
                FeatureState.AVAILABLE,
                (Model("c1", "Claude 1"), Model("c2", "Claude 2"), Model("c3", "Claude 3")),
            ),
        }
        service.models = lambda key: catalogs[key]
        with interaction.terminal(account_service=service) as (view, _):
            view._read_in_background = immediate
            view._account_catalogs["chatgpt/default"] = catalogs["chatgpt/default"]
            view.catalog = catalogs["chatgpt/default"]
            view.refresh_other_provider_catalogs()

            self.assertIn("8", view.status)
            self.assertIn("3/3", view.status)

    async def test_cached_conversation_can_send_before_provider_refresh_finishes(self):
        service = Accounts()
        service.active_accounts = lambda: service.entries
        service.archive = lambda: object()
        service.cached_models = lambda _key: ModelCatalog(
            FeatureState.AVAILABLE, (Model("gpt-test", "GPT test"),)
        )
        cached = {"title": "Cached history", "messages": [{"role": "user", "text": "Earlier"}]}
        service.cached_window = lambda _key, _remote: cached
        provider_load = Mock(side_effect=AssertionError("provider refresh must stay in background"))
        callbacks = service.callbacks("chatgpt/default")
        callbacks["load_conversation"] = provider_load
        service.callbacks = lambda _key: callbacks

        with interaction.terminal(account_service=service) as (view, _):
            conversation_id = "@chatgpt/default/remote-chat"
            view.sidebar_items = [{"id": conversation_id, "title": "Cached history"}]
            pending_reads = []
            view._reads.submit = lambda read: pending_reads.append(read) or True
            provider_refreshes = []
            view._read_in_background = lambda *args, **kwargs: provider_refreshes.append(args)

            view.resume_conversation(conversation_id)
            self.assertFalse(view.loading_conversation)
            view.input.text = "Continue while syncing"
            with patch("cli.chat.threading.Thread") as thread:
                view.submit_prompt()
            thread.return_value.start.assert_called_once_with()
            self.assertTrue(view.tabs[view.active_tab]["busy"])

            pending_reads.pop()()

            self.assertFalse(view.loading_conversation)
            self.assertIn("Earlier", [message["text"] for message in view.messages])
            self.assertIn("Continue while syncing", [message["text"] for message in view.messages])
            self.assertTrue(view.tabs[view.active_tab]["busy"])
            self.assertTrue(view.tabs[view.active_tab]["syncing"])
            self.assertEqual(len(provider_refreshes), 1)
            provider_load.assert_not_called()
            provider_refreshes[0][2]("offline")
            self.assertFalse(view.tabs[view.active_tab]["syncing"])
            self.assertIn("offline", view.status)

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
