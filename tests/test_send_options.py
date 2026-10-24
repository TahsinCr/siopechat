"""Per-tab temporary and project send request contracts."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

from providers.chatgpt import BrowserSession, ChatGPTClient, SessionPaths
from providers.chatgpt.discovery import model_catalog
from providers.chatgpt.errors import ChatGPTWebError, SentinelCompatibilityError
from tests.provider_fixtures import CATALOG


class SendOptionsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.client = ChatGPTClient(
            BrowserSession(SessionPaths(root / "session", root / "cache", root / "profile")),
            authenticate=False,
        )
        self.client.http.close()
        self.requests = []

        def handle(request):
            body = json.loads(request.content)
            self.requests.append(body)
            n = len(self.requests)
            self.messages = [
                {
                    "id": f"r-{n}",
                    "author": {"role": "assistant"},
                    "channel": "analysis",
                    "content": {"parts": ["Summary"]},
                },
                {
                    "id": f"a-{n}",
                    "author": {"role": "assistant"},
                    "channel": "final",
                    "content": {"parts": ["Answer"]},
                },
            ]
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text="\n".join(
                    [
                        "data: " + json.dumps({"conversation_id": "remote", "message": m})
                        for m in self.messages
                    ]
                    + ["data: [DONE]"]
                ),
            )

        self.client.http = httpx.Client(transport=httpx.MockTransport(handle))
        self.addCleanup(self.client.close)
        self.client.create_sentinel_headers = Mock(return_value={})
        self.client.model_catalog = Mock(return_value=model_catalog(CATALOG))

    def test_temporary_multiturn_has_no_history_get(self):
        self.client.get_conversation = Mock(side_effect=AssertionError("no temporary GET"))
        events = []
        with patch.dict("os.environ", {"CHATGPT_WEB_SEND_MODE": "node"}):
            remote, conv, _ = self.client.send_prompt(
                None, "First", model="account-thinking", temporary=True, on_event=events.append
            )
            self.client.send_prompt(remote, "Second", model="account-thinking", temporary=True)
        self.assertTrue(conv["temporary"])
        self.assertTrue(all(b["history_and_training_disabled"] for b in self.requests))
        self.assertEqual(self.requests[1]["parent_message_id"], "a-1")
        self.assertEqual([e["channel"] for e in events], ["analysis", "final"])
        self.assertEqual(len(self.client.temporary_conversations["remote"]["messages"]), 6)
        self.client.get_conversation.assert_not_called()

    def test_project_reaches_request_body(self):
        self.client.get_conversation = Mock(
            side_effect=lambda _: {
                "id": "remote",
                "messages": self.requests[-1]["messages"] + self.messages,
            }
        )
        with patch.dict("os.environ", {"CHATGPT_WEB_SEND_MODE": "node"}):
            self.client.send_prompt(
                None, "Project", project_id="g-p-work", model="account-thinking"
            )
        self.assertEqual(self.requests[0]["gizmo_id"], "g-p-work")

    def test_missing_temporary_context_never_falls_back_to_saved_chat(self):
        self.client.get_conversation = Mock(side_effect=AssertionError("no fallback GET"))
        with self.assertRaises(ChatGPTWebError):
            self.client.send_prompt("expired", "Q", temporary=True)
        self.client.get_conversation.assert_not_called()
        self.assertEqual(self.requests, [])

    def test_temporary_never_sends_prompt_to_browser_recipe_capture(self):
        self.client.create_sentinel_headers = Mock(
            side_effect=SentinelCompatibilityError("changed")
        )
        self.client.capture_send_recipe = Mock(
            side_effect=AssertionError("no private browser draft")
        )
        with patch.dict("os.environ", {"CHATGPT_WEB_SEND_MODE": "node"}):
            with self.assertRaises(ChatGPTWebError):
                self.client.send_prompt(None, "Private", model="account-thinking", temporary=True)
        self.client.capture_send_recipe.assert_not_called()
        self.assertEqual(self.requests, [])

    def test_temporary_project_fails_before_network(self):
        with self.assertRaises(ChatGPTWebError):
            self.client.send_prompt(None, "Q", temporary=True, project_id="g-p-work")
        self.assertEqual(self.requests, [])
