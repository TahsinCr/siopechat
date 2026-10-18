"""Claude contracts, transport completion and account isolation."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from bootstrap import create_application
from providers import Capability, ConversationBound, ReplaceText, SendRequest
from providers.claude import ClaudeProvider
from providers.claude.records import conversation, project
from providers.errors import FeatureUnavailable, ProviderError
from providers.models import FeatureState

CHAT = "12345678-abcd-1234-abcd-123456789abc"
PROJECT = "87654321-abcd-1234-abcd-123456789abc"


class ClaudeRecordsTests(unittest.TestCase):
    def test_history_separates_public_thinking_and_retains_resources(self):
        data = {
            "uuid": CHAT,
            "name": "Chat",
            "project_uuid": PROJECT,
            "chat_messages": [
                {
                    "uuid": "u",
                    "sender": "human",
                    "content": [{"type": "text", "text": "Question"}],
                    "files": [{"uuid": "f", "file_name": "reference.pdf"}],
                },
                {
                    "uuid": "a",
                    "sender": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "Published summary"},
                        {"type": "text", "text": "Answer"},
                    ],
                },
            ],
        }
        value = conversation(data, "account")
        self.assertEqual(value.origin.provider_id, "claude")
        self.assertEqual(value.project_id, PROJECT)
        self.assertEqual(value.messages[-2].channel, "analysis")
        self.assertEqual(value.messages[-1].content[0].text, "Answer")
        self.assertEqual(value.messages[1].content[0].resource.name, "reference.pdf")

    def test_unknown_content_blocks_survive_for_other_extensions(self):
        block = {"type": "future_tool", "payload": {"source": "document"}}
        chat = conversation(
            {
                "uuid": CHAT,
                "chat_messages": [{"uuid": "a", "sender": "assistant", "content": [block]}],
            },
            "account",
        )
        self.assertEqual(chat.messages[0].content[0].metadata["native"], block)
        self.assertIsNone(chat.messages[0].content[0].text)

    def test_history_uses_the_selected_branch(self):
        value = conversation(
            {
                "uuid": CHAT,
                "current_leaf_message_uuid": "a2",
                "chat_messages": [
                    {
                        "uuid": "u",
                        "sender": "human",
                        "parent_message_uuid": None,
                        "text": "Question",
                    },
                    {
                        "uuid": "a1",
                        "sender": "assistant",
                        "parent_message_uuid": "u",
                        "text": "Old branch",
                    },
                    {
                        "uuid": "a2",
                        "sender": "assistant",
                        "parent_message_uuid": "u",
                        "text": "Selected branch",
                    },
                ],
            },
            "account",
        )
        self.assertEqual(
            [x.content[0].text for x in value.messages], ["Question", "Selected branch"]
        )

    def test_project_instructions_and_sources_are_provider_neutral(self):
        value = project(
            {
                "uuid": PROJECT,
                "name": "Research",
                "prompt_template": "Instructions",
                "docs": [{"uuid": "f", "file_name": "source.pdf"}],
            },
            "account",
        )
        self.assertEqual(value.instructions, "Instructions")
        self.assertEqual(value.resources[0].name, "source.pdf")
        self.assertEqual(value.metadata["kind"], "project")


class ClaudeProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.provider = ClaudeProvider(Path("unused"), "account")
        self.provider.browser.authenticated = True
        self.provider.browser.request = AsyncMock()

    async def test_dynamic_models_keep_only_discovered_efforts_and_default(self):
        self.provider.browser.request.return_value = {
            "models": [
                {
                    "id": "claude-future",
                    "title": "Future",
                    "efforts": [{"id": "high", "title": "High", "selected": True}],
                }
            ],
            "default_model": "claude-future",
        }
        result = await self.provider.models()
        self.assertEqual(result.state, FeatureState.AVAILABLE)
        self.assertEqual(result.models[0].default_effort, "high")
        self.assertEqual(result.models[0].id, "claude-future")

    async def test_one_model_feature_failure_does_not_remove_working_models(self):
        self.provider.browser.request.return_value = {
            "models": [
                {"id": "claude-main", "title": "Main", "efforts": [], "effort_error": "changed"},
                {
                    "id": "claude-broken",
                    "title": "Broken",
                    "efforts": [],
                    "selection_error": "unavailable",
                },
            ]
        }
        catalog = await self.provider.models()
        self.assertEqual([x.id for x in catalog.chat_models], ["claude-main"])
        self.assertEqual(catalog.chat_models[0].metadata["effort_error"], "changed")

    async def test_chat_and_project_queries_use_scoped_transport(self):
        self.provider.browser.request.return_value = [{"uuid": CHAT, "name": "Chat"}]
        result = await self.provider.list_project_chats(PROJECT)
        self.assertEqual(result.items[0].project_id, PROJECT)
        self.provider.browser.request.assert_awaited_once_with("project_chats", project_id=PROJECT)

    async def test_unsupported_creation_options_fail_before_remote_write(self):
        with self.assertRaises(FeatureUnavailable):
            await self.provider.create_project("Name", visibility="shared")
        self.provider.browser.request.assert_not_awaited()

    async def test_delete_and_move_do_not_retry_mutations(self):
        await self.provider.delete_chat(CHAT)
        self.provider.browser.request.assert_awaited_once_with("delete_chat", conversation_id=CHAT)
        self.provider.browser.request.reset_mock()
        self.provider.browser.request.side_effect = ProviderError("network")
        with self.assertRaises(ProviderError):
            await self.provider.move_chat(CHAT, PROJECT)
        self.provider.browser.request.assert_awaited_once()

    async def test_truncated_stream_is_not_success(self):
        async def events(*args, **kwargs):
            yield {"type": "bound", "id": CHAT}
            yield {"type": "replace", "text": "Partial", "channel": "final"}

        self.provider.browser.events = events
        with self.assertRaises(ProviderError):
            _ = [
                x
                async for x in self.provider.stream(
                    SendRequest("claude", "account", "local", "Question")
                )
            ]

    async def test_stream_preserves_thinking_channels_and_remote_identity(self):
        async def events(*args, **kwargs):
            yield {"type": "bound", "id": CHAT}
            yield {"type": "replace", "text": "Summary", "channel": "analysis"}
            yield {"type": "replace", "text": "Answer", "channel": "final"}
            yield {"type": "done"}

        self.provider.browser.events = events
        result = [
            x
            async for x in self.provider.stream(
                SendRequest("claude", "account", "local", "Question")
            )
        ]
        self.assertIsInstance(result[0], ConversationBound)
        self.assertIsInstance(result[1], ReplaceText)
        self.assertEqual(result[1].channel, "analysis")

    async def test_unsupported_attachments_are_explicit(self):
        self.assertNotIn(Capability.ATTACHMENTS, self.provider.capabilities)
        with self.assertRaises(FeatureUnavailable):
            _ = [
                x
                async for x in self.provider.stream(
                    SendRequest("claude", "account", "local", "Q", attachments=({"path": "file"},))
                )
            ]


class ClaudeAccountsTests(unittest.TestCase):
    def test_connect_opens_requested_login_page_in_existing_browser(self):
        from types import SimpleNamespace
        from unittest.mock import Mock

        with tempfile.TemporaryDirectory() as folder:
            api = create_application(Path(folder))
            try:
                accounts = api.get("accounts")
                key = accounts.add("claude", "Claude", browser_account="chatgpt/default")
                session = Mock(cdp_url="http://127.0.0.1:9222")
                session.launch.return_value = {"browser": "already_running"}
                with (
                    patch.object(accounts, "session", return_value=session),
                    patch(
                        "accounts.subprocess.run", return_value=SimpleNamespace(returncode=0)
                    ) as run,
                ):
                    self.assertTrue(accounts.session_action(key, "connect_browser")["opened"])
                    self.assertIn("https://claude.ai/", run.call_args.kwargs["input"])
            finally:
                api.runtime.close()

    def test_shared_browser_has_independent_account_records_and_verification(self):
        with tempfile.TemporaryDirectory() as folder:
            api = create_application(Path(folder))
            try:
                accounts = api.get("accounts")
                key = accounts.add("claude", "Claude", browser_account="chatgpt/default")
                self.assertEqual(
                    accounts.session(key).paths.profile,
                    accounts.session("chatgpt/default").paths.profile,
                )
                self.assertNotEqual(accounts.directory(key), accounts.directory("chatgpt/default"))
                self.assertIsInstance(accounts.provider(key), ClaudeProvider)
                accounts.credential_path(key).parent.mkdir(parents=True, exist_ok=True)
                accounts.credential_path(key).write_text("{}")
                accounts._remember_verification(key)
                with patch.object(accounts, "browser_connected", return_value=False):
                    self.assertTrue(accounts.status(key)["authenticated"])
                gpt_file = accounts.credential_path("chatgpt/default")
                gpt_file.parent.mkdir(parents=True, exist_ok=True)
                gpt_file.write_text("{}")
                with (
                    patch("providers.chatgpt.session.BrowserSession.launch") as launch,
                    patch("providers.chatgpt.session.BrowserSession.is_ready", return_value=True),
                ):
                    accounts.ensure_browser("chatgpt/default")
                    accounts.ensure_browser(key)
                    launch.assert_not_called()
                with self.assertRaises(ValueError):
                    accounts.add("claude", "Wrong", browser_account=key)
            finally:
                api.runtime.close()


class BrowserTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_result_requires_clean_process_exit_and_keeps_owned_tab(self):
        from types import SimpleNamespace
        from unittest.mock import Mock

        from providers.browser import BrowserTransport

        process = SimpleNamespace(
            stdin=SimpleNamespace(write=Mock(), drain=AsyncMock(), close=Mock()),
            stdout=SimpleNamespace(
                readline=AsyncMock(
                    side_effect=[
                        b'{"type":"tab","id":"owned"}\n',
                        b'{"type":"result","data":{"ok":true}}\n',
                        b"",
                    ]
                )
            ),
            wait=AsyncMock(return_value=0),
            returncode=0,
        )
        transport = BrowserTransport(Path("script.cjs"), "http://127.0.0.1:9222")
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=process):
            self.assertEqual(await transport.request("status"), {"ok": True})
        self.assertEqual(transport.tab_id, "owned")
        self.assertTrue(transport.authenticated)

    async def test_auth_permission_missing_and_rate_errors_remain_distinct(self):
        from types import SimpleNamespace
        from unittest.mock import Mock

        from providers.browser import BrowserTransport
        from providers.errors import AccountPaused, RateLimited, RemoteNotFound

        for status, expected in [
            (401, AccountPaused),
            (403, ProviderError),
            (404, RemoteNotFound),
            (429, RateLimited),
        ]:
            process = SimpleNamespace(
                stdin=SimpleNamespace(write=Mock(), drain=AsyncMock(), close=Mock()),
                stdout=SimpleNamespace(
                    readline=AsyncMock(
                        return_value=(
                            '{"type":"error","status":' + str(status) + ',"message":"failure"}\n'
                        ).encode()
                    )
                ),
                wait=AsyncMock(return_value=0),
                returncode=0,
            )
            transport = BrowserTransport(Path("script.cjs"), "http://127.0.0.1:9222")
            with patch(
                "asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=process
            ):
                with self.assertRaises(expected) as error:
                    await transport.request("history")
                if status == 403:
                    self.assertNotIsInstance(error.exception, AccountPaused)

    async def test_disconnected_close_never_recovers_a_browser(self):
        from unittest.mock import Mock

        from providers.browser import BrowserTransport

        ready = Mock()
        transport = BrowserTransport(Path("script.cjs"), "http://127.0.0.1:9222", ready)
        transport.tab_id = "owned"

        async def failed(*args, **kwargs):
            raise ProviderError("already disconnected")
            yield

        transport.events = failed
        await transport.close()
        self.assertIsNone(transport.tab_id)
        ready.assert_not_called()
