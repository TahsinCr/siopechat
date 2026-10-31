"""Transport contracts for account-owned web catalogs and projects."""

import asyncio
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

from providers.chatgpt import BrowserSession, ChatGPTClient, ChatGPTProvider, SessionPaths
from providers.chatgpt.discovery import model_catalog
from providers.chatgpt.errors import ChatGPTWebError
from providers.chatgpt.formats import normalize_conversation
from providers.models import ModelCatalog

from .fixtures import CATALOG

PROJECT = {
    "gizmo": {
        "gizmo": {
            "id": "g-p-work",
            "gizmo_type": "snorlax",
            "display": {"name": "Work"},
            "instructions": "Use concise answers.",
        },
        "files": [{"file_id": "file-ref", "name": "Reference.pdf", "type": "application/pdf"}],
    }
}


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.session = BrowserSession(SessionPaths(root / "session", root / "profile"))
        self.client = ChatGPTClient(self.session, authenticate=False)
        self.client.http.close()
        self.addCleanup(self.client.close)
        self.requests = []

        def respond(request):
            self.requests.append(request)
            if request.url.path == "/backend-api/models":
                value = CATALOG
            elif request.url.path.endswith("/sidebar"):
                value = {
                    "items": [PROJECT, {"gizmo": {"id": "g-custom", "display": {"name": "GPT"}}}],
                    "cursor": "next",
                }
            elif request.url.path.endswith("/conversations"):
                value = {
                    "items": [{"id": "remote-chat", "title": "Project conversation"}],
                    "cursor": "next-chat",
                }
            else:
                value = PROJECT["gizmo"]
            return httpx.Response(200, json=value)

        self.client.http = httpx.Client(transport=httpx.MockTransport(respond))

    def test_http_project_create_reads_back_and_never_launches_a_process(self):
        from unittest.mock import Mock

        self.client.request = Mock(
            return_value=httpx.Response(
                200, json={"resource": {"id": "g-p-created", "display": {"name": "Demo"}}}
            )
        )
        self.client.get_project = Mock(
            return_value={
                "id": "g-p-created",
                "name": "Demo",
                "metadata": {"gizmo": {"memory_scope": "project_v2"}},
            }
        )
        with patch("subprocess.run", side_effect=AssertionError("browser")):
            value = self.client.create_project("Demo", memory="project_only")
        self.assertEqual(value["id"], "g-p-created")
        self.assertEqual(self.client.request.call_args.kwargs["json"]["memory_scope"], "project_v2")
        self.client.get_project.assert_called_once_with("g-p-created")
        self.client.request.reset_mock()
        with self.assertRaises(ValueError):
            self.client.create_project("Demo", visibility="public")
        self.client.request.assert_not_called()

    def test_chat_picker_uses_categories_without_exposing_work_variants_or_pinning_versions(self):
        raw = {
            "models": [
                {"slug": "gpt-future-thinking", "title": "Provider engine name"},
                {"slug": "gpt-future-sol-wm", "title": "Sol work variant"},
                {"slug": "research", "title": "Research"},
            ],
            "categories": [
                {
                    "default_model": "gpt-future-thinking",
                    "supported_models": ["gpt-future-thinking"],
                    "model_version": "42",
                    "model_lane": "thinking",
                }
            ],
            "default_model_slug": "gpt-future-thinking",
        }
        catalog = model_catalog(raw)
        self.assertEqual(len(catalog.models), 3)
        self.assertEqual([m.id for m in catalog.chat_models], ["gpt-future-thinking"])
        self.assertEqual(catalog.chat_models[0].chat_title, "GPT-42 Thinking")
        restored = ModelCatalog.from_dict(asdict(catalog))
        self.assertEqual(restored.chat_models, catalog.chat_models)
        self.assertEqual(restored.models[1].metadata["title"], "Sol work variant")

    def test_work_variants_stay_hidden_if_an_older_response_has_no_categories(self):
        catalog = model_catalog({"models": [{"slug": "normal"}, {"slug": "private-wm"}]})
        self.assertEqual([m.id for m in catalog.chat_models], ["normal"])

    def test_project_sidebar_and_details_preserve_instructions_and_files(self):
        listing = self.client.list_projects()
        self.assertEqual(len(listing["items"]), 1)
        self.assertEqual(listing["cursor"], "next")
        self.assertEqual(listing["items"][0]["name"], "Work")
        detail = self.client.get_project("g-p-work")
        self.assertEqual(detail["instructions"], "Use concise answers.")
        self.assertEqual(detail["resources"][0]["name"], "Reference.pdf")
        self.assertEqual(self.requests[0].url.params["conversations_per_gizmo"], "0")
        self.assertEqual(self.requests[0].url.params["owned_only"], "false")
        self.client.list_projects("next")
        self.assertEqual(self.requests[-1].url.params["cursor"], "next")

    def test_project_chat_pagination_and_cache_preserve_membership(self):
        page = self.client.list_project_chats("g-p-work", "next-chat")
        self.assertEqual(page["items"][0]["project_id"], "g-p-work")
        self.assertEqual(self.requests[-1].url.params["cursor"], "next-chat")
        normalized = normalize_conversation({"id": "remote-chat", "gizmo_id": "g-p-work"})
        self.assertEqual(normalized["project_id"], "g-p-work")

    def test_common_provider_exposes_the_same_real_catalog_and_project_records(self):
        # The common provider owns client lifetime; give each call its own transport.
        def factory(_session):
            client = ChatGPTClient(self.session, authenticate=False)
            client.http.close()
            client.http = httpx.Client(transport=self.client.http._transport)
            return client

        provider = ChatGPTProvider(self.session, client_factory=factory)
        catalog = asyncio.run(provider.models())
        projects = asyncio.run(provider.list_projects())
        detail = asyncio.run(provider.get_project("g-p-work"))
        chats = asyncio.run(provider.list_project_chats("g-p-work"))
        self.assertEqual(catalog.default_model, "account-thinking")
        self.assertEqual(projects.next_cursor, "next")
        self.assertEqual(detail.resources[0].id, "file-ref")
        self.assertEqual(detail.instructions, "Use concise answers.")
        self.assertEqual(chats.items[0].project_id, "g-p-work")
        self.assertEqual(chats.next_cursor, "next-chat")

    def test_changed_responses_fail_instead_of_claiming_an_empty_account(self):
        for value in ({}, {"models": {}}, {"models": [None]}, {"models": [{"title": "No slug"}]}):
            with self.subTest(value=value), self.assertRaises(ChatGPTWebError):
                model_catalog(value)
        self.client.http.close()
        self.client.http = httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"unexpected": []}))
        )
        with self.assertRaisesRegex(ChatGPTWebError, "items"):
            self.client.list_projects()

    def test_direct_send_applies_selected_model_effort_and_keeps_project(self):
        previous = {"id": "chat", "current_node": "parent", "gizmo_id": "g-p-work", "messages": []}
        body = self.client.create_conversation_body(previous, "hello")
        self.assertNotIn("thinking_effort", body)
        self.assertNotIn("model", body)
        self.assertEqual(body["gizmo_id"], "g-p-work")
        with (
            patch.object(
                self.client,
                "send_prompt_direct",
                return_value=("chat", "user-msg", {"id": "user-msg"}),
            ) as send,
            patch.object(
                self.client,
                "get_conversation",
                side_effect=[previous, {"messages": [{"id": "user-msg"}]}],
            ),
        ):
            self.client.send_prompt("chat", "hello", model="account-thinking", effort="standard")
        self.assertEqual(send.call_args.args[-2:], ("account-thinking", "standard"))
        with self.assertRaisesRegex(ChatGPTWebError, "not offered"):
            self.client.send_prompt(None, "hello", model="account-instant", effort="extended")

    def test_truncated_or_malformed_chatgpt_stream_cannot_succeed(self):
        for payload in (
            'data: {"conversation_id":"chat"}\n\n',
            "data: {bad}\n\ndata: [DONE]\n\n",
            'data: {"error":{"message":"secret"}}\n\ndata: [DONE]\n\n',
        ):
            self.client.http.close()
            self.client.http = httpx.Client(
                transport=httpx.MockTransport(
                    lambda _: httpx.Response(
                        200, headers={"content-type": "text/event-stream"}, text=payload
                    )
                )
            )
            with patch.object(self.client, "create_sentinel_headers", return_value={}):
                with self.assertRaises(ChatGPTWebError):
                    self.client.send_prompt_direct(None, "hello", [])

    def test_timezone_uses_a_real_iana_name_or_omits_the_optional_field(self):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        from providers.chatgpt.client import system_timezone

        try:
            ZoneInfo("Asia/Tokyo")
        except ZoneInfoNotFoundError:
            with patch.dict("os.environ", {"TZ": "Asia/Tokyo"}):
                self.assertIsNone(system_timezone())
        else:
            with patch.dict("os.environ", {"TZ": "Asia/Tokyo"}):
                self.assertEqual(system_timezone(), "Asia/Tokyo")
        with patch("providers.chatgpt.client.system_timezone", return_value=None):
            body = self.client.create_conversation_body(None, "Question")
        self.assertNotIn("timezone", body)
        self.assertIsInstance(body["timezone_offset_min"], int)

    def test_empty_mutation_success_is_explicit_and_json_reads_remain_strict(self):
        self.client.http.close()
        self.client.http = httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(204))
        )
        self.client.delete_conversation("chat")
        with self.assertRaises(ChatGPTWebError):
            self.client.request("GET", "https://chatgpt.com/backend-api/test")

    def test_model_and_effort_reach_the_actual_stream_request(self):
        sent = []

        def respond(request):
            sent.append(json.loads(request.content))
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text='data: {"conversation_id":"chat","message":{"id":"a","author":{"role":"assistant"},"content":{"parts":["Answer"]}}}\n\ndata: [DONE]\n\n',
            )

        self.client.http.close()
        self.client.http = httpx.Client(transport=httpx.MockTransport(respond))
        with patch.object(self.client, "create_sentinel_headers", return_value={}):
            self.client.send_prompt_direct(
                None, "hello", [], model="account-thinking", effort="standard"
            )
        self.assertEqual(
            (sent[0]["model"], sent[0]["thinking_effort"]), ("account-thinking", "standard")
        )

    def test_chatgpt_stream_accepts_sse_data_without_space_and_multiline_json(self):
        event = json.dumps(
            {
                "conversation_id": "chat",
                "message": {
                    "id": "assistant",
                    "author": {"role": "assistant"},
                    "content": {"parts": ["Answer"]},
                },
            }
        )
        midpoint = event.index('"message"')
        payload = (
            f": keepalive\n\ndata:{event[:midpoint]}\ndata: {event[midpoint:]}\n\ndata:[DONE]\n\n"
        )
        self.client.http.close()
        self.client.http = httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, headers={"content-type": "text/event-stream"}, text=payload
                )
            )
        )
        with patch.object(self.client, "create_sentinel_headers", return_value={}):
            result = self.client.send_prompt_direct(None, "hello", [])

        self.assertEqual(result[0], "chat")
        self.assertEqual(self.client._last_stream_messages[0]["content"]["parts"], ["Answer"])

    def test_chatgpt_reports_generated_text_before_sse_done(self):
        event = {
            "conversation_id": "chat",
            "message": {
                "id": "assistant",
                "author": {"role": "assistant"},
                "status": "finished_successfully",
                "content": {"parts": ["Answer"]},
            },
        }
        payload = "data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n"
        self.client.http.close()
        self.client.http = httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, headers={"content-type": "text/event-stream"}, text=payload
                )
            )
        )
        received = []
        with patch.object(self.client, "create_sentinel_headers", return_value={}):
            self.client.send_prompt_direct(None, "hello", [], on_event=received.append)

        self.assertEqual([item["type"] for item in received], ["delta", "response_complete"])

    def test_model_catalog_reuses_valid_cache_and_refreshes_expired_cache(self):
        import time

        cached = model_catalog(CATALOG)
        self.client._model_catalog = (time.monotonic(), cached)
        with patch.object(self.client, "request") as request:
            self.assertIs(self.client.model_catalog(), cached)
            request.assert_not_called()

            response = Mock()
            response.json.return_value = CATALOG
            request.return_value = response
            self.client._model_catalog = (time.monotonic() - 301, cached)
            refreshed = self.client.model_catalog()

        self.assertIsNot(refreshed, cached)
        request.assert_called_once()
