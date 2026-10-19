"""Streaming file delivery, provider resolution and partial transcript selection."""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock

import httpx

from downloads import Downloads
from providers.files import DownloadSource
from settings import Settings
from tests.test_cli_interaction import terminal


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.settings = Settings(self.root / "config.json")
        self.accounts = Mock()
        self.accounts.resolve_download.side_effect = lambda key, url, *args: DownloadSource(url)

    def service(self, respond):
        return Downloads(
            self.settings,
            self.root,
            self.accounts,
            lambda **kw: httpx.Client(transport=httpx.MockTransport(respond), **kw),
        )

    def test_streaming_atomic_filename_collision_and_custom_directory(self):
        data = b"abc" * 100000
        service = self.service(
            lambda _: httpx.Response(
                200,
                headers={
                    "content-type": "application/octet-stream",
                    "content-disposition": 'attachment; filename="hello.py"',
                },
                content=data,
            )
        )
        events = []
        first = service.fetch(
            "account", "https://example.com/file", progress=lambda *a: events.append(a)
        )
        self.assertEqual(Path(first["path"]), self.root / "downloads/hello.py")
        self.assertEqual(Path(first["path"]).read_bytes(), data)
        second = service.fetch("account", "https://example.com/file")
        self.assertNotEqual(second["path"], first["path"])
        self.assertTrue(events)
        self.assertEqual(list((self.root / "downloads").glob("*.part")), [])
        self.settings.update("application", {"download_directory": "my-files"})
        third = service.fetch("account", "https://example.com/file")
        self.assertEqual(Path(third["path"]).parent, self.root / "my-files")

    def test_real_http_stream_downloads_without_buffering_the_entire_response(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        payload = b"streamed-file" * 40000

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", 'attachment; filename="stream.bin"')
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                for start in range(0, len(payload), 16384):
                    self.wfile.write(payload[start : start + 16384])

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            service = Downloads(self.settings, self.root, self.accounts)
            result = service.fetch("account", f"http://127.0.0.1:{server.server_port}/file")
            self.assertEqual(Path(result["path"]).read_bytes(), payload)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_download_setting_is_exposed_and_invalid_paths_are_rejected(self):
        from bootstrap import create_application
        from settings import SettingsCatalog

        api = create_application(self.root)
        try:
            service = api.get("downloads")
            self.assertEqual(service.directory(), self.root / "downloads")
            field = next(
                row for row in SettingsCatalog(api).rows() if row["key"] == "download_directory"
            )
            self.assertEqual(field["section"], "application")
            self.assertEqual(field["title"], "Download folder")
            for invalid in ("", "   ", "bad\0path"):
                with self.assertRaises(ValueError):
                    api.settings.update("application", {"download_directory": invalid})
            api.settings.update("application", {"download_directory": str(self.root / "other")})
            self.assertEqual(service.directory(), self.root / "other")
        finally:
            api.runtime.close()

    def test_web_pages_and_failed_requests_do_not_create_files(self):
        service = self.service(
            lambda _: httpx.Response(
                200, headers={"content-type": "text/html"}, text="<html>Page</html>"
            )
        )
        self.assertEqual(service.fetch("account", "https://example.com")["kind"], "page")
        self.assertFalse((self.root / "downloads").exists())
        service = self.service(lambda _: httpx.Response(404))
        with self.assertRaisesRegex(ValueError, "HTTP 404"):
            service.fetch("account", "https://example.com/missing")
        self.assertFalse((self.root / "downloads").exists())

    def test_browser_challenge_is_not_misreported_as_a_file_failure(self):
        service = self.service(
            lambda _: httpx.Response(403, headers={"content-type": "text/html"}, text="challenge")
        )
        self.assertEqual(
            service.fetch("account", "https://example.com/page"),
            {"kind": "page", "url": "https://example.com/page"},
        )
        self.accounts.resolve_download.side_effect = None
        self.accounts.resolve_download.return_value = DownloadSource(
            "https://example.com/private-file", force_download=True
        )
        with self.assertRaisesRegex(ValueError, "HTTP 403"):
            service.fetch("account", "https://example.com/private-file")

    def test_redirect_does_not_forward_credentials_to_another_host(self):
        seen = []

        def respond(request):
            seen.append(request)
            if len(seen) == 1:
                return httpx.Response(302, headers={"location": "https://cdn.example.com/file"})
            return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"file")

        self.accounts.resolve_download.side_effect = None
        self.accounts.resolve_download.return_value = DownloadSource(
            "https://example.com/file", headers={"Authorization": "secret"}
        )
        self.service(respond).fetch("account", "https://example.com/file")
        self.assertEqual(seen[0].headers["authorization"], "secret")
        self.assertNotIn("authorization", seen[1].headers)

    def test_cancelled_transfer_removes_partial_and_reserved_files(self):
        class Broken(httpx.SyncByteStream):
            def __iter__(self):
                yield b"partial"
                raise httpx.ReadError("network interruption")

        service = self.service(
            lambda _: httpx.Response(
                200, headers={"content-type": "application/pdf"}, stream=Broken()
            )
        )
        with self.assertRaisesRegex(ValueError, "aktarımı kesildi"):
            service.fetch("account", "https://example.com/file")
        self.assertEqual(list((self.root / "downloads").iterdir()), [])

    def test_duplicates_and_concurrency_are_bounded(self):
        entered, release = threading.Event(), threading.Event()
        service = self.service(
            lambda _: httpx.Response(
                200, headers={"content-type": "application/pdf"}, content=b"file"
            )
        )

        def slow(*args):
            entered.set()
            release.wait(2)
            return DownloadSource("https://example.com/file")

        self.accounts.resolve_download.side_effect = slow
        errors = []

        def work():
            try:
                service.fetch("account", "https://example.com/file")
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=work)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            with self.assertRaisesRegex(ValueError, "zaten"):
                service.fetch("account", "https://example.com/file")
        finally:
            release.set()
            thread.join(2)
        self.assertEqual(errors, [])

    def test_link_work_keeps_its_original_account_and_chat_after_tab_switch(self):
        from tests.account_fixture import configured_application

        app = configured_application(self.root)
        try:
            accounts = app.get("accounts")
            accounts.download = Mock(
                return_value={"kind": "file", "path": str(self.root / "saved.py")}
            )
            with terminal(
                account_service=accounts,
                conversation_id="@chatgpt/default/owner",
                initial_messages=[
                    {
                        "role": "assistant",
                        "id": "server-message",
                        "text": "[File](sandbox:/mnt/data/game.py)",
                    }
                ],
            ) as (view, _):
                pending = []
                view._read_in_background = lambda read, success, failure, **kwargs: pending.append(
                    (read, success, failure)
                )
                view.refresh_sidebar = Mock()
                view.refresh_provider_catalog = Mock()
                view.open_message_link("sandbox:/mnt/data/game.py", 0)
                operation = pending[0]
                view.new_tab()
                operation[1](operation[0]())
                args = accounts.download.call_args.args
                self.assertEqual(
                    args[:4],
                    (
                        "chatgpt/default",
                        "sandbox:/mnt/data/game.py",
                        "@chatgpt/default/owner",
                        "server-message",
                    ),
                )
                self.assertIsNone(view.conversation_id)
        finally:
            app.runtime.close()


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
        self.assertEqual(source.url, "https://lh3.googleusercontent.com/real-file")
        self.assertNotIn("Cookie", source.headers)
        provider.get_chat.return_value = Conversation(
            Origin("gemini", "default", "c_other"), "other"
        )
        with self.assertRaisesRegex(ValueError, "sağlayıcıdan"):
            await provider.resolve_download(alias, "c_other")
