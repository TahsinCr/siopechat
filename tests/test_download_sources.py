"""Provider-owned download sources and authenticated file access."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock

import httpx

from providers.files import NativeDownloadClient, cookie_header


class ProviderFileTests(unittest.IsolatedAsyncioTestCase):
    async def test_chatgpt_sandbox_uses_real_message_and_scoped_endpoint(self):
        from providers.chatgpt import ChatGPTProvider

        factory = MagicMock()
        client = factory.return_value.__enter__.return_value
        client.get_conversation.return_value = {
            "messages": [
                {
                    "id": "real-message",
                    "content": {"parts": ["[download](sandbox:/mnt/data/game.py)"]},
                }
            ]
        }
        client.request.return_value.json.return_value = {
            "download_url": "https://files.example.com/signed"
        }
        provider = ChatGPTProvider(Mock(), client_factory=factory)
        source = await provider.resolve_download(
            "sandbox:/mnt/data/game.py", "chat", "local:assistant"
        )
        self.assertEqual(source.filename, "game.py")
        self.assertEqual(source.url, "https://files.example.com/signed")
        self.assertEqual(client.request.call_args.kwargs["params"]["message_id"], "real-message")
        self.assertEqual(
            client.request.call_args.args[1],
            "https://chatgpt.com/backend-api/conversation/chat/interpreter/download",
        )
        self.assertEqual(source.headers, {})
        client.get_conversation.return_value["messages"].append(
            {"id": "another", "content": {"parts": ["[download](sandbox:/mnt/data/game.py)"]}}
        )
        with self.assertRaises(ValueError):
            await provider.resolve_download("sandbox:/mnt/data/game.py", "chat", "unconfirmed")

    async def test_gemini_generated_placeholder_maps_only_to_provider_url(self):
        from providers.gemini.history import history
        from providers.models import Origin

        alias = "http://googleusercontent.com/image_generation_content/6_786"
        url = "https://lh3.googleusercontent.com/generated-file"
        generated = [[None, None, None, [None, 1, "cat.png", url]], [alias]]
        candidate = [None] * 38
        candidate[1] = [alias]
        candidate[12] = [{"8": [[generated]]}]
        turn = [[None, "r_id"], None, [["draw cat"]], [[candidate]]]
        chat = history([[turn]], lambda cid: Origin("gemini", "account", cid), "c_chat")
        message = chat.messages[-1]
        self.assertIn(url, message.content[0].text)
        self.assertNotIn(alias, message.content[0].text)
        self.assertEqual(message.content[1].resource.remote_url, url)

    async def test_gemini_alias_is_resolved_in_its_own_conversation(self):
        from unittest.mock import AsyncMock

        from providers.gemini import GeminiProvider
        from providers.models import Content, Conversation, Message, Origin, Resource

        alias = "http://googleusercontent.com/image_generation_content/6_786"
        provider = GeminiProvider(Path("unused"))
        provider._client = Mock(
            http=Mock(
                cookies=Mock(
                    get_dict=Mock(
                        return_value={"__Secure-1PSID": "owned-cookie", "unrelated": "never-send"}
                    )
                )
            )
        )
        provider.get_chat = AsyncMock(
            return_value=Conversation(
                Origin("gemini", "default", "c_owner"),
                "chat",
                (
                    Message(
                        "message",
                        "assistant",
                        (
                            Content(
                                "image",
                                resource=Resource(
                                    alias,
                                    "cat.png",
                                    "image/png",
                                    "https://lh3.googleusercontent.com/real-file",
                                ),
                            ),
                        ),
                    ),
                ),
            )
        )
        source = await provider.resolve_download(alias, "c_owner")
        provider.get_chat.assert_awaited_once_with("c_owner")
        self.assertIs(source.client_factory, NativeDownloadClient)
        self.assertEqual(source.url, "https://lh3.googleusercontent.com/real-file")
        self.assertEqual(source.headers["Cookie"], "__Secure-1PSID=owned-cookie")
        provider.get_chat.return_value = Conversation(
            Origin("gemini", "default", "c_other"), "other"
        )
        with self.assertRaisesRegex(ValueError, "sağlayıcıdan"):
            await provider.resolve_download(alias, "c_other")


class AuthenticatedFileTests(unittest.IsolatedAsyncioTestCase):
    async def test_chatgpt_estuary_keeps_own_origin_auth_only(self):
        from providers.chatgpt import ChatGPTProvider

        factory = MagicMock()
        client = factory.return_value.__enter__.return_value
        client.headers = {"authorization": "Bearer owned", "cookie": "owned=session"}
        client.get_conversation.return_value = {
            "messages": [{"id": "native", "content": {"parts": ["sandbox:/mnt/data/a.py"]}}]
        }
        provider = ChatGPTProvider(Mock(), client_factory=factory)
        for target, authorized in [
            ("https://chatgpt.com/backend-api/estuary/content?id=opaque", True),
            ("https://files.oaiusercontent.com/signed", False),
            ("https://chatgpt.com.attacker.example/file", False),
        ]:
            client.request.return_value.json.return_value = {"download_url": target}
            source = await provider.resolve_download("sandbox:/mnt/data/a.py", "chat")
            self.assertEqual(bool(source.headers.get("authorization")), authorized)
            self.assertEqual(bool(source.headers.get("cookie")), authorized)

    async def test_direct_chatgpt_estuary_link_uses_the_selected_account(self):
        from providers.chatgpt import ChatGPTProvider

        factory = MagicMock()
        client = factory.return_value.__enter__.return_value
        client.headers = {"authorization": "Bearer selected-account"}
        provider = ChatGPTProvider(Mock(), client_factory=factory)
        source = await provider.resolve_download(
            "https://chatgpt.com/backend-api/estuary/content?id=opaque", "chat"
        )
        self.assertTrue(source.force_download)
        self.assertEqual(source.headers["authorization"], "Bearer selected-account")
        client.get_conversation.assert_not_called()

    async def test_gemini_never_sends_cookies_to_arbitrary_cdn_subdomains(self):
        from providers.gemini import GeminiProvider

        provider = GeminiProvider(Path("unused"))
        provider._saved_cookies = {"__Secure-1PSID": "secret"}
        for url in [
            "https://untrusted.googleusercontent.com/file",
            "https://lh3.googleusercontent.com.evil.example/file",
        ]:
            source = await provider.resolve_download(url, "chat")
            self.assertTrue(source is None or not source.headers.get("Cookie"))

    async def test_claude_output_download_is_scoped_to_account_and_conversation(self):
        from providers.claude import ClaudeProvider
        from providers.session import save_session

        with tempfile.TemporaryDirectory() as folder:
            credentials = Path(folder) / "session.json"
            save_session(credentials, {"cookies": {"sessionKey": "owned", "lastActiveOrg": "org"}})
            provider = ClaudeProvider(credentials)
            cid = "12345678-1234-4234-8234-123456789abc"
            provider.transport = Mock()
            provider.transport.base = AsyncMock(return_value="/api/organizations/org")
            provider.transport.session.request = AsyncMock(
                return_value={"files_metadata": [{"path": "/mnt/user-data/outputs/game.py"}]}
            )
            source = await provider.resolve_download("sandbox:/mnt/user-data/outputs/game.py", cid)
            self.assertEqual(source.filename, "game.py")
            self.assertIn("/conversations/" + cid + "/wiggle/download-file?", source.url)
            self.assertIn("path=%2Fmnt%2Fuser-data%2Foutputs%2Fgame.py", source.url)
            self.assertIn("sessionKey=owned", source.headers["Cookie"])
            self.assertIs(source.client_factory, NativeDownloadClient)
            computer = await provider.resolve_download(
                "computer:///mnt/user-data/outputs/game.py", cid
            )
            self.assertEqual(computer.url, source.url)
            direct = await provider.resolve_download(source.url, cid)
            self.assertEqual(direct.url, source.url)
            with self.assertRaisesRegex(ValueError, "bulunamadı"):
                await provider.resolve_download("sandbox:/mnt/user-data/outputs/missing.py", cid)
            with self.assertRaises(ValueError):
                await provider.resolve_download("sandbox:/mnt/user-data/outputs/../secrets", cid)


class CredentialHeaderTests(unittest.TestCase):
    def test_only_named_cookies_are_sent_and_injection_is_rejected(self):
        self.assertEqual(
            cookie_header({"sessionKey": "owned", "extra": "secret"}, ("sessionKey",)),
            "sessionKey=owned",
        )
        for invalid in ("a\r\nCookie: stolen", "a; extra=stolen", 4):
            with self.assertRaisesRegex(ValueError, "geçersiz"):
                cookie_header({"sessionKey": invalid}, ("sessionKey",))

    def test_native_transport_failure_does_not_expose_signed_url(self):
        from unittest.mock import patch

        from curl_cffi.requests.exceptions import RequestException

        with patch("curl_cffi.requests.Session") as factory:
            factory.return_value.stream.side_effect = RequestException(
                "https://secret.example/?token=private"
            )
            with NativeDownloadClient(timeout=httpx.Timeout(5)) as client:
                with self.assertRaisesRegex(ValueError, "aktarım") as caught:
                    with client.stream("GET", "https://secret.example", headers={}):
                        pass
            self.assertNotIn("private", str(caught.exception))
            factory.return_value.close.assert_called_once()
