"""Provider-owned download sources and authenticated file access."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
from webaiprovider import Account, ChatGPTProvider, ClaudeProvider, GeminiProvider
from webaiprovider.accounts.credentials import save_session
from webaiprovider.providers.gemini.history import parse_chat_history
from webaiprovider.resources.chat.types import Conversation
from webaiprovider.resources.message.types import Attachment, Content, MessageData
from webaiprovider.transport.downloader import NativeDownloadClient, cookie_header


class ProviderFileTests(unittest.TestCase):
    def test_chatgpt_sandbox_uses_real_message_and_scoped_endpoint(self):
        client = Mock()
        factory = Mock(return_value=client)
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
        provider = ChatGPTProvider(Account("default", "chatgpt", {}), client_factory=factory)
        source = provider._resolve_download_data(
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
            provider._resolve_download_data("sandbox:/mnt/data/game.py", "chat", "unconfirmed")

    def test_gemini_generated_placeholder_maps_only_to_provider_url(self):
        alias = "http://googleusercontent.com/image_generation_content/6_786"
        url = "https://lh3.googleusercontent.com/generated-file"
        generated = [[None, None, None, [None, 1, "cat.png", url]], [alias]]
        candidate = [None] * 38
        candidate[1] = [alias]
        candidate[12] = [{"8": [[generated]]}]
        turn = [[None, "r_id"], None, [["draw cat"]], [[candidate]]]
        chat = parse_chat_history([[turn]], "c_chat")
        message = chat.messages[-1]
        self.assertIn(url, message.content[0].text)
        self.assertNotIn(alias, message.content[0].text)
        self.assertEqual(message.content[1].resource.remote_url, url)

    def test_gemini_alias_is_resolved_in_its_own_conversation(self):
        alias = "http://googleusercontent.com/image_generation_content/6_786"
        provider = GeminiProvider(Account("default", "gemini", Path("unused")))
        provider._client = Mock(
            http=Mock(
                cookies=Mock(
                    get_dict=Mock(
                        return_value={"__Secure-1PSID": "owned-cookie", "unrelated": "never-send"}
                    )
                )
            )
        )
        provider._get_chat_data = Mock(
            return_value=Conversation(
                "c_owner",
                "chat",
                (
                    MessageData(
                        "message",
                        "assistant",
                        (
                            Content(
                                "image",
                                resource=Attachment(
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
        source = provider._resolve_download_data(alias, "c_owner")
        provider._get_chat_data.assert_called_once_with("c_owner")
        self.assertIs(source.client_factory, NativeDownloadClient)
        self.assertEqual(source.url, "https://lh3.googleusercontent.com/real-file")
        self.assertEqual(source.headers["Cookie"], "__Secure-1PSID=owned-cookie")
        provider._get_chat_data.return_value = Conversation("c_other", "other")
        with self.assertRaisesRegex(ValueError, "file URL"):
            provider._resolve_download_data(alias, "c_other")


class AuthenticatedFileTests(unittest.TestCase):
    def test_chatgpt_estuary_keeps_own_origin_auth_only(self):
        client = Mock()
        factory = Mock(return_value=client)
        client.headers = {"authorization": "Bearer owned", "cookie": "owned=session"}
        client.get_conversation.return_value = {
            "messages": [{"id": "native", "content": {"parts": ["sandbox:/mnt/data/a.py"]}}]
        }
        provider = ChatGPTProvider(Account("default", "chatgpt", {}), client_factory=factory)
        for target, authorized in [
            ("https://chatgpt.com/backend-api/estuary/content?id=opaque", True),
            ("https://files.oaiusercontent.com/signed", False),
            ("https://chatgpt.com.attacker.example/file", False),
        ]:
            client.request.return_value.json.return_value = {"download_url": target}
            source = provider._resolve_download_data("sandbox:/mnt/data/a.py", "chat")
            self.assertEqual(bool(source.headers.get("authorization")), authorized)
            self.assertEqual(bool(source.headers.get("cookie")), authorized)

    def test_direct_chatgpt_estuary_link_uses_the_selected_account(self):
        client = Mock()
        factory = Mock(return_value=client)
        client.headers = {"authorization": "Bearer selected-account"}
        provider = ChatGPTProvider(Account("default", "chatgpt", {}), client_factory=factory)
        source = provider._resolve_download_data(
            "https://chatgpt.com/backend-api/estuary/content?id=opaque", "chat"
        )
        self.assertTrue(source.force_download)
        self.assertEqual(source.headers["authorization"], "Bearer selected-account")
        client.get_conversation.assert_not_called()

    def test_gemini_never_sends_cookies_to_arbitrary_cdn_subdomains(self):
        provider = GeminiProvider(Account("default", "gemini", {"__Secure-1PSID": "secret"}))
        for url in [
            "https://untrusted.googleusercontent.com/file",
            "https://lh3.googleusercontent.com.evil.example/file",
        ]:
            source = provider._resolve_download_data(url, "chat")
            self.assertTrue(source is None or not source.headers.get("Cookie"))

    def test_claude_output_download_is_scoped_to_account_and_conversation(self):
        with tempfile.TemporaryDirectory() as folder:
            credentials = Path(folder) / "session.json"
            save_session(credentials, {"cookies": {"sessionKey": "owned", "lastActiveOrg": "org"}})
            provider = ClaudeProvider(Account("default", "claude", credentials))
            cid = "12345678-1234-4234-8234-123456789abc"
            provider.client.base = Mock(return_value="/api/organizations/org")
            provider.client.http = Mock(
                request=Mock(
                    return_value={"files_metadata": [{"path": "/mnt/user-data/outputs/game.py"}]}
                )
            )
            source = provider._resolve_download_data("sandbox:/mnt/user-data/outputs/game.py", cid)
            self.assertEqual(source.filename, "game.py")
            self.assertIn("/conversations/" + cid + "/wiggle/download-file?", source.url)
            self.assertIn("path=%2Fmnt%2Fuser-data%2Foutputs%2Fgame.py", source.url)
            self.assertIn("sessionKey=owned", source.headers["Cookie"])
            self.assertIs(source.client_factory, NativeDownloadClient)
            computer = provider._resolve_download_data(
                "computer:///mnt/user-data/outputs/game.py", cid
            )
            self.assertEqual(computer.url, source.url)
            direct = provider._resolve_download_data(source.url, cid)
            self.assertEqual(direct.url, source.url)
            with self.assertRaisesRegex(ValueError, "File was not found"):
                provider._resolve_download_data("sandbox:/mnt/user-data/outputs/missing.py", cid)
            with self.assertRaises(ValueError):
                provider._resolve_download_data("sandbox:/mnt/user-data/outputs/../secrets", cid)


class CredentialHeaderTests(unittest.TestCase):
    def test_only_named_cookies_are_sent_and_injection_is_rejected(self):
        self.assertEqual(
            cookie_header({"sessionKey": "owned", "extra": "secret"}, ("sessionKey",)),
            "sessionKey=owned",
        )
        for invalid in ("a\r\nCookie: stolen", "a; extra=stolen", 4):
            with self.assertRaisesRegex(ValueError, "Session record is invalid"):
                cookie_header({"sessionKey": invalid}, ("sessionKey",))

    def test_cookie_header_accepts_wrapped_or_flat_cookie_records(self):
        self.assertEqual(
            cookie_header({"cookies": {"sessionKey": "owned"}}, ("sessionKey",)), "sessionKey=owned"
        )
        self.assertEqual(
            cookie_header({"cookies": "unrelated", "sessionKey": "owned"}, ("sessionKey",)),
            "sessionKey=owned",
        )

    def test_native_transport_failure_does_not_expose_signed_url(self):
        from curl_cffi.requests.exceptions import RequestException

        with patch("curl_cffi.requests.Session") as factory:
            factory.return_value.stream.side_effect = RequestException(
                "https://93.184.216.34/?token=private"
            )
            with NativeDownloadClient(timeout=httpx.Timeout(5)) as client:
                with self.assertRaisesRegex(ValueError, "File transfer was interrupted") as caught:
                    with client.stream("GET", "https://93.184.216.34/?token=private", headers={}):
                        pass
            self.assertNotIn("private", str(caught.exception))
            factory.return_value.close.assert_called_once()
