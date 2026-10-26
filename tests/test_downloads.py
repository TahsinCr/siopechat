"""Safe, streaming and atomic file downloads."""

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

import httpx

from downloads import Downloads
from providers.files import DownloadSource, NativeDownloadClient
from settings import Settings
from tests.cli_support import terminal


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
            resolver=lambda host, port, **kw: [(2, 1, 6, "", ("93.184.216.34", port))],
        )

    def test_explicit_file_card_downloads_a_resource_without_content_type(self):
        service = self.service(lambda _: httpx.Response(200, content=b"artifact"))
        result = service.fetch("account", "https://example.com/result", force_download=True)
        self.assertEqual(result["kind"], "file")
        self.assertEqual(Path(result["path"]).read_bytes(), b"artifact")

    def test_private_targets_and_https_downgrade_are_rejected(self):
        for address in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1"):
            service = self.service(lambda _: self.fail("private target must not be requested"))
            service.resolver = lambda host, port, **kw: [(2, 1, 6, "", (address, port))]
            with self.assertRaises(ValueError):
                service.fetch("account", "https://example.com/file")
        service = self.service(
            lambda _: httpx.Response(302, headers={"location": "http://example.com/file"})
        )
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            service.fetch("account", "https://example.com/file")

    def test_download_limit_cleans_reserved_and_partial_files(self):
        self.settings.update("application", {"download_max_bytes": 3})

        class Body(httpx.SyncByteStream):
            def __iter__(self):
                yield b"12"
                yield b"34"

        for headers in ({"content-length": "4"}, {}):
            service = self.service(
                lambda _: httpx.Response(
                    200,
                    headers=dict(headers, **{"content-type": "application/pdf"}),
                    stream=Body(),
                )
            )
            with self.assertRaisesRegex(ValueError, "boyutu"):
                service.fetch("account", "https://example.com/file")
            self.assertEqual(list((self.root / "downloads").iterdir()), [])

    def test_validated_ip_is_pinned_while_host_and_tls_name_are_preserved(self):
        seen = []
        service = self.service(
            lambda request: (
                seen.append(request)
                or httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"ok")
            )
        )
        service.fetch("account", "https://example.com/file")
        self.assertEqual(seen[0].url.host, "93.184.216.34")
        self.assertEqual(seen[0].headers["host"], "example.com")
        self.assertEqual(seen[0].extensions["sni_hostname"], "example.com")

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
        self.assertEqual(
            Path(first["path"]).resolve(), (self.root / "downloads/hello.py").resolve()
        )
        self.assertEqual(Path(first["path"]).read_bytes(), data)
        second = service.fetch("account", "https://example.com/file")
        self.assertNotEqual(second["path"], first["path"])
        self.assertTrue(events)
        self.assertEqual(list((self.root / "downloads").glob("*.part")), [])
        self.settings.update("application", {"download_directory": "my-files"})
        third = service.fetch("account", "https://example.com/file")
        self.assertTrue(os.path.samefile(Path(third["path"]).parent, self.root / "my-files"))

    def test_named_binary_rejects_html_but_html_attachment_is_downloadable(self):
        self.accounts.resolve_download.side_effect = lambda key, url, *_: DownloadSource(
            url, filename="report.pdf"
        )
        page = self.service(
            lambda _: httpx.Response(
                200, headers={"content-type": "text/html"}, content=b"<html>sign in</html>"
            )
        )
        with self.assertRaisesRegex(ValueError, "HTML"):
            page.fetch("account", "https://example.com/report")
        self.assertEqual(list((self.root / "downloads").glob("*")), [])

        self.accounts.resolve_download.side_effect = lambda key, url, *_: DownloadSource(
            url, filename="report.html"
        )
        attachment = self.service(
            lambda _: httpx.Response(
                200,
                headers={
                    "content-type": "text/html",
                    "content-disposition": 'attachment; filename="report.html"',
                },
                content=b"<html>report</html>",
            )
        )
        result = attachment.fetch("account", "https://example.com/report")
        self.assertEqual(Path(result["path"]).name, "report.html")
        self.assertEqual(Path(result["path"]).read_bytes(), b"<html>report</html>")

    def test_pinned_https_connection_validates_the_original_hostname(self):
        import shutil
        import ssl
        import subprocess
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        if not shutil.which("openssl"):
            self.skipTest("openssl certificate fixture")
        certificate, key = self.root / "cert.pem", self.root / "key.pem"
        config = self.root / "openssl.cnf"
        config.write_text(
            "[req]\ndistinguished_name=dn\nx509_extensions=extensions\nprompt=no\n"
            "[dn]\nCN=localhost\n[extensions]\nsubjectAltName=DNS:localhost\n"
            "basicConstraints=critical,CA:TRUE\n"
            "keyUsage=critical,keyCertSign,digitalSignature,keyEncipherment\n"
            "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always\n"
            "extendedKeyUsage=serverAuth\n"
        )
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(key),
                "-out",
                str(certificate),
                "-days",
                "1",
                "-subj",
                "/CN=localhost",
                "-config",
                str(config),
            ],
            check=True,
            capture_output=True,
        )

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.end_headers()
                self.wfile.write(b"verified TLS")

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.settings.update("application", {"download_allow_private": True})
            trusted = ssl.create_default_context(cafile=str(certificate))
            service = Downloads(
                self.settings,
                self.root,
                self.accounts,
                lambda **options: httpx.Client(verify=trusted, **options),
                resolver=lambda host, port, **options: [(2, 1, 6, "", ("127.0.0.1", port))],
            )
            result = service.fetch("account", f"https://localhost:{server.server_port}/file")
            self.assertEqual(Path(result["path"]).read_bytes(), b"verified TLS")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

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
            self.settings.update("application", {"download_allow_private": True})
            service = Downloads(self.settings, self.root, self.accounts)
            result = service.fetch("account", f"http://127.0.0.1:{server.server_port}/file")
            self.assertEqual(Path(result["path"]).read_bytes(), payload)
            self.accounts.resolve_download.side_effect = lambda key, url, *args: DownloadSource(
                url, force_download=True, client_factory=NativeDownloadClient
            )
            native = service.fetch("account", f"http://127.0.0.1:{server.server_port}/file")
            self.assertEqual(Path(native["path"]).read_bytes(), payload)
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
            self.assertEqual(service.directory().resolve(), (self.root / "downloads").resolve())
            field = next(
                row for row in SettingsCatalog(api).rows() if row["key"] == "download_directory"
            )
            self.assertEqual(field["section"], "application")
            self.assertEqual(field["title"], "Download folder")
            for invalid in ("", "   ", "bad\0path"):
                with self.assertRaises(ValueError):
                    api.settings.update("application", {"download_directory": invalid})
            api.settings.update("application", {"download_directory": str(self.root / "other")})
            self.assertEqual(service.directory().resolve(), (self.root / "other").resolve())
        finally:
            api.runtime.close()

    def test_media_still_generating_is_not_saved_as_a_completed_file(self):
        service = self.service(lambda _: httpx.Response(206, content=b"not ready"))
        self.accounts.resolve_download.side_effect = lambda key, url, *args: DownloadSource(
            url, force_download=True
        )
        with self.assertRaisesRegex(ValueError, "hazır değil"):
            service.fetch("account", "https://example.com/media")
        self.assertFalse((self.root / "downloads").exists())

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
