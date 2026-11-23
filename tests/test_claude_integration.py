"""Claude contracts, transport completion and account isolation."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from webaiprovider import ClaudeWebAIProvider
from webaiprovider.providers.claude.records import conversation

from tests.account_fixture import configured_application as create_application

CHAT = "12345678-abcd-1234-abcd-123456789abc"
PROJECT = "87654321-abcd-1234-abcd-123456789abc"


class ClaudeRecordsTests(unittest.TestCase):
    def test_published_claude_citations_join_the_thinking_row_in_transcript(self):
        from tui.markdown import visible_chat_messages

        value = conversation(
            {
                "uuid": CHAT,
                "chat_messages": [
                    {
                        "uuid": "answer",
                        "sender": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "Searching the web"},
                            {
                                "type": "text",
                                "text": "Published answer",
                                "citations": [
                                    {
                                        "url": "https://example.com/reference",
                                        "title": "Reference",
                                    }
                                ],
                            },
                        ],
                    }
                ],
            },
        )
        messages = [
            {
                "id": item.id,
                "role": item.role,
                "channel": item.channel,
                "text": item.content[0].text,
                "metadata": item.metadata,
            }
            for item in value.messages
        ]

        visible = visible_chat_messages(messages)

        self.assertEqual([item["role"] for item in visible], ["reasoning", "assistant"])
        self.assertEqual(visible[0]["text"], "Searching the web")
        self.assertEqual(
            visible[1]["_thinking_sources"],
            ({"title": "Reference", "url": "https://example.com/reference"},),
        )


class ClaudeAccountsTests(unittest.TestCase):
    def test_connect_opens_requested_login_page_in_existing_browser(self):
        with tempfile.TemporaryDirectory() as folder:
            api = create_application(Path(folder))
            try:
                accounts = api.get("accounts")
                key = accounts.add("claude", "Claude", browser_account="chatgpt/default")
                with patch(
                    "accounts.sessions._launch_or_open_cdp_browser",
                    return_value={"opened": True},
                ) as launch:
                    self.assertTrue(accounts.session_action(key, "connect_browser")["opened"])
                    launch.assert_called_once_with(
                        accounts._cdp_url_for("chatgpt/default"),
                        accounts.directory(key) / "chrome-profile",
                        "https://claude.ai/",
                    )
            finally:
                api.runtime.close()

    def test_shared_browser_has_independent_account_records_and_verification(self):
        with tempfile.TemporaryDirectory() as folder:
            api = create_application(Path(folder))
            try:
                accounts = api.get("accounts")
                key = accounts.add("claude", "Claude", browser_account="chatgpt/default")
                self.assertEqual(
                    accounts._cdp_url_for(key),
                    accounts._cdp_url_for("chatgpt/default"),
                )
                self.assertNotEqual(accounts.directory(key), accounts.directory("chatgpt/default"))
                self.assertIsInstance(accounts.provider(key), ClaudeWebAIProvider)
                accounts.credentials(key).save({})
                accounts._remember_verification(key)
                with patch.object(accounts, "browser_connected", return_value=False):
                    self.assertTrue(accounts.status(key)["authenticated"])
                gpt_file = accounts.credentials("chatgpt/default")
                gpt_file.save({})
                self.assertNotEqual(accounts.credentials(key), gpt_file)
                with self.assertRaises(ValueError):
                    accounts.add("claude", "Wrong", browser_account=key)
            finally:
                api.runtime.close()
