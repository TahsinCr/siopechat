"""Provider model, project and background-loading UI behavior."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from providers.chatgpt.discovery import model_catalog
from providers.chatgpt.errors import SentinelCompatibilityError
from providers.tests.fixtures import CATALOG
from tests.account_fixture import configured_application as create_application
from tests.tui_support import immediate, terminal


class ProviderUiTests(unittest.TestCase):
    def test_sentinel_failure_says_message_was_not_sent_and_finishes_the_turn(self):
        with terminal() as (view, _pipe):
            view.messages = [
                {"role": "user", "text": "hello"},
                {"role": "assistant", "text": ""},
            ]
            view.tabs[0]["messages"] = view.messages
            view.tabs[0]["busy"] = True

            def fail(*_args, **_kwargs):
                raise SentinelCompatibilityError("private signer detail")

            view.run_send(
                "hello",
                [],
                0,
                view.messages,
                None,
                "account-thinking",
                "standard",
                send_callback=fail,
            )

            self.assertEqual(view.status, view.tr("Message not sent"))
            self.assertIn(
                view.tr("ChatGPT verification is incompatible; the message was not sent"),
                view.messages[-1]["text"],
            )
            self.assertNotIn("private signer detail", view.messages[-1]["text"])
            self.assertFalse(view.tabs[0]["busy"])

    def test_work_model_is_preserved_as_provenance_but_not_offered_or_restored_as_chat_selection(
        self,
    ):
        raw = dict(
            CATALOG,
            models=CATALOG["models"] + [{"slug": "internal-sol-wm", "title": "Internal Sol"}],
        )
        with terminal(model="internal-sol-wm", load_catalog=lambda: model_catalog(raw)) as (
            view,
            _pipe,
        ):
            view._read_in_background = immediate
            view.input.text = "Keep the draft"
            view.submit_prompt()
            self.assertEqual(view.input.text, "Keep the draft")
            self.assertFalse(view.messages)
            view.refresh_provider_catalog()
            self.assertEqual(view.model, "account-thinking")
            self.assertEqual(view.last_response_model, "internal-sol-wm")
            view.open_palette("models")
            self.assertNotIn(
                "model.select:internal-sol-wm", {item["id"] for item in view.palette_items}
            )
            view.select_model("internal-sol-wm")
            self.assertEqual(view.model, "account-thinking")
            self.assertIn("kataloğunda yok", view.status)

    def test_model_and_effort_pickers_use_only_account_catalog_and_keep_selection_per_tab(self):
        with terminal(load_catalog=lambda: model_catalog(CATALOG)) as (view, _pipe):
            view._read_in_background = immediate
            view.refresh_provider_catalog()
            self.assertEqual(view.model, "account-thinking")
            self.assertEqual(view.effort, "standard")
            view.sidebar_items = [{"id": "old", "default_model_slug": "deleted-model"}]
            view.handle_command("/model")
            self.assertEqual(
                {i["id"] for i in view.palette_items},
                {"model.select:account-thinking", "model.select:account-instant"},
            )
            view.close_palette()
            view.handle_command("/effort extended")
            self.assertEqual(view.effort, "extended")
            view.input.text = "first draft"
            view.new_tab()
            view.handle_command("/model account-instant")
            self.assertEqual(view.model, "account-instant")
            self.assertIsNone(view.effort)
            view.input.text = "second draft"
            view.switch_tab(-1)
            self.assertEqual(
                (view.model, view.effort, view.input.text),
                ("account-thinking", "extended", "first draft"),
            )
            view.switch_tab(1)
            self.assertEqual(view.input.text, "second draft")

    def test_projects_load_lazily_page_chats_and_show_details_in_their_own_tab(self):
        detail = {
            "id": "g-p-work",
            "name": "Work",
            "instructions": "Be precise",
            "resources": [{"name": "Design.pdf", "type": "application/pdf"}],
        }
        list_chats = Mock(
            side_effect=[
                {"items": [{"id": "one", "title": "One"}], "cursor": "page2"},
                {"items": [{"id": "two", "title": "Two"}], "cursor": None},
            ]
        )
        with terminal(
            list_projects=lambda cursor: {"items": [detail], "cursor": None},
            get_project=lambda _: detail,
            list_project_chats=list_chats,
        ) as (view, _pipe):
            view._read_in_background = immediate
            view.refresh_projects()
            list_chats.assert_not_called()
            view._build_sidebar_rows()
            view.sidebar_index = next(
                i for i, row in enumerate(view.sidebar_rows) if row["kind"] == "project"
            )
            view.toggle_sidebar_group()
            self.assertEqual(
                list_chats.call_args_list,
                [unittest.mock.call("g-p-work", None), unittest.mock.call("g-p-work", "page2")],
            )
            self.assertIn("One", str(view.render_sidebar()))
            self.assertIn("Two", str(view.render_sidebar()))
            view.load_project_chats("g-p-work", more=True)
            self.assertIn("Two", str(view.render_sidebar()))
            view.messages = [{"role": "user", "text": "Keep this chat"}]
            view.show_project_details("g-p-work")
            self.assertEqual(view.tab_kind, "project")
            self.assertIn("Be precise", view.render_transcript())
            self.assertIn("Design.pdf", view.render_transcript())
            view.switch_tab(-1)
            self.assertIn("Keep this chat", view.render_transcript())
            self.assertEqual(view.tab_kind, "chat")

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
                        [row["title"] for row in view.sidebar_rows if row["kind"] == "project"],
                        ["Gemini notebook", "GPT project"],
                    )
                    self.assertLess(view.sidebar_index, len(view.sidebar_rows))

                    view.open_project_picker()
                    self.assertEqual(
                        [
                            row["title"]
                            for row in view.palette_items
                            if row["id"].startswith("project.select:")
                        ],
                        ["Projesiz sohbet", "GPT project"],
                    )

                    view.bind_account(gemini)
                    view._build_sidebar_rows()
                    self.assertEqual(
                        [row["title"] for row in view.sidebar_rows if row["kind"] == "project"],
                        ["Gemini notebook", "GPT project"],
                    )
                    view.open_project_picker()
                    self.assertEqual(
                        [
                            row["title"]
                            for row in view.palette_items
                            if row["id"].startswith("project.select:")
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

    def test_catalog_and_project_failures_keep_last_good_data_and_offer_retry(self):
        with terminal(
            load_catalog=Mock(side_effect=[model_catalog(CATALOG), RuntimeError("offline")]),
            list_projects=Mock(
                side_effect=[
                    {"items": [{"id": "g-p-work", "name": "Work"}], "cursor": None},
                    RuntimeError("HTTP 403"),
                ]
            ),
        ) as (view, _pipe):
            view._read_in_background = immediate
            view.refresh_provider_catalog()
            view.refresh_projects()
            view.refresh_provider_catalog()
            view.refresh_projects()
            self.assertEqual(len(view.catalog.models), 2)
            self.assertEqual(view.catalog_error, "offline")
            self.assertIn("g-p-work", view.project_items)
            self.assertIn("HTTP 403", str(view.render_sidebar()))
            self.assertFalse(view.projects_loading)

    def test_cached_model_and_project_metadata_are_restored_when_opening_chat(self):
        with terminal() as (view, _pipe):
            view.catalog = model_catalog(CATALOG)
            view.project_items["g-p-work"] = {"id": "g-p-work", "name": "Work"}
            view.load_conversation = lambda _: {
                "id": "c1",
                "model": "account-thinking",
                "project_id": "g-p-work",
                "messages": [],
            }
            with patch.object(view._reads, "submit", side_effect=lambda target: target() or True):
                view.resume_conversation("c1")
            self.assertEqual(view.model, "account-thinking")
            self.assertEqual(view.current_project["name"], "Work")

    def test_cached_project_survives_startup_and_catalog_fills_its_name(self):
        with terminal(
            conversation_id="c1",
            initial_messages=[{"role": "user", "text": "cached"}],
            initial_project={"id": "g-p-work"},
            list_projects=lambda cursor: {"items": [{"id": "g-p-work", "name": "Work"}]},
            refresh_on_start=True,
        ) as (view, _pipe):
            view._read_in_background = immediate
            self.assertTrue(view.load_on_start)
            view.refresh_projects()
            self.assertEqual(view.current_project, {"id": "g-p-work", "name": "Work"})
            self.assertIn("cached", view.render_transcript())

    def test_response_metadata_does_not_overwrite_the_next_selected_model(self):
        with terminal() as (view, _pipe):
            view.model = "account-thinking"
            view.messages = [{"role": "assistant", "text": ""}]
            view._save_active_tab()
            view.send_prompt = lambda *_: {
                "conversation_id": "chat",
                "conversation": {
                    "default_model_slug": "old-chat-default",
                    "messages": [
                        {
                            "id": "reply",
                            "author": {"role": "assistant"},
                            "metadata": {"model_slug": "actual-engine"},
                            "content": {"parts": ["answer"]},
                        }
                    ],
                },
            }
            view.run_send("question", [], 0, view.messages, None, "account-thinking", "standard")
            self.assertEqual(view.model, "account-thinking")
            self.assertEqual(view.last_response_model, "actual-engine")
            self.assertIn("answer", view.render_transcript())

    def test_background_content_loading_does_not_steal_search_focus(self):
        detail = {"id": "g-p-work", "name": "Work", "instructions": "Notes"}
        with terminal(get_project=lambda _: detail) as (view, _pipe):
            pending = []
            view._read_in_background = lambda read, success, failure: pending.append(
                (read, success)
            )
            view.show_project_details("g-p-work")
            view.open_palette()
            read, success = pending.pop()
            success(read())
            self.assertIs(view.app.layout.current_control, view.palette_search.control)
            self.assertTrue(view.palette_visible)
            view.close_palette()
            view.new_tab()
            view.load_conversation = lambda _: {"id": "chat", "messages": []}
            with patch.object(view._reads, "submit", return_value=True) as submit:
                view.resume_conversation("chat")
                load = submit.call_args.args[0]
            view.open_palette()
            load()
            self.assertIs(view.app.layout.current_control, view.palette_search.control)
