"""Saved web sessions: no browser processes, redirects or mutation retries."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from backend import BackendService
from providers.claude.transport import ClaudeTransport, organization_from_bootstrap
from providers.errors import AccountPaused, ProviderError, RateLimited, RemoteNotFound
from providers.gemini.rpc import NotebookEditor, NotebookRPC
from providers.session import WebSession, read_session, save_session


class SessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "session.json"
        save_session(self.path, {"sessionKey": "fixture"})
        self.client = SimpleNamespace(
            request=AsyncMock(return_value=httpx.Response(200, json={"ok": True})),
            cookies={"sessionKey": "rotated"},
            close=AsyncMock(),
        )
        self.factory = Mock(return_value=self.client)
        self.session = WebSession(
            "https://claude.ai", self.path, "sessionKey", client_factory=self.factory
        )
        self.addAsyncCleanup(self.session.close)

    async def test_http_request_reuses_connection_without_browser(self):
        with patch("asyncio.create_subprocess_exec", side_effect=AssertionError("browser")):
            await self.session.request("GET", "/api/bootstrap")
            await self.session.request("POST", "/api/projects", json={"name": "fixture"})
        self.assertEqual(self.factory.call_count, 1)
        self.assertFalse(self.client.request.call_args.kwargs["allow_redirects"])
        self.assertEqual(read_session(self.path)["sessionKey"], "rotated")
        if os.name != "nt":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    async def test_session_cannot_send_cookies_to_another_origin(self):
        for path in ["https://example.com/api", "//example.com/api", "/../outside"]:
            with self.assertRaises(ValueError):
                await self.session.request("POST", path)
        self.factory.assert_not_called()

    async def test_remote_mutation_failure_is_not_replayed(self):
        self.client.request.return_value = httpx.Response(403, json={"error": "denied"})
        with self.assertRaises(ProviderError) as error:
            await self.session.request("POST", "/api/project")
        self.assertNotIsInstance(error.exception, AccountPaused)
        self.client.request.assert_awaited_once()

    async def test_local_persistence_failure_does_not_reject_remote_success(self):
        with patch("providers.session.save_session", side_effect=OSError("disk full")):
            self.assertEqual(await self.session.request("POST", "/api/project"), {"ok": True})
        self.assertTrue(self.session.persistence_warning)

    def test_auth_access_missing_and_limit_are_different_failures(self):
        for status, kind in [
            (401, AccountPaused),
            (403, ProviderError),
            (404, RemoteNotFound),
            (429, RateLimited),
        ]:
            with self.subTest(status=status), self.assertRaises(kind):
                WebSession.check(httpx.Response(status, headers={"retry-after": "2"}))

    def test_invalid_credentials_are_rejected(self):
        self.path.write_text("[]")
        with self.assertRaises(AccountPaused):
            read_session(self.path)

    def test_custom_application_root_cannot_overwrite_real_session(self):
        backend = BackendService(Path(self.directory.name))
        self.assertTrue(backend.auth_store_path().is_relative_to(Path(self.directory.name)))
        self.assertTrue(
            backend.create_session().paths.profile.is_relative_to(Path(self.directory.name))
        )


class NotebookHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_rpc_keeps_native_identity_and_disables_automatic_retry(self):
        client = SimpleNamespace(
            batch=AsyncMock(
                return_value=SimpleNamespace(text=')]}\'\n[["wrb.fr","HcT8bb","[[]]"]]')
            )
        )
        rpc = NotebookRPC(AsyncMock(return_value=client))
        self.assertEqual(await rpc.read("notebooks", remote="notebooks/abcdef"), [[[]]])
        args = client.batch.call_args
        self.assertEqual(json.loads(args.args[0][0].payload), ["notebooks/abcdef", ["en"], 0])

    async def test_missing_rpc_ack_cannot_be_reported_as_success(self):
        client = SimpleNamespace(
            batch=AsyncMock(return_value=SimpleNamespace(text='[["wrb.fr","HcT8bb",null]]'))
        )
        with self.assertRaises(ProviderError):
            await NotebookRPC(AsyncMock(return_value=client)).call("HcT8bb", [])

    async def test_notebook_update_uses_write_settings_not_display_icon(self):
        detail = [None] * 13
        detail[6] = ["icon"]
        detail[12] = [2, None, None, None, 1]
        notebook = SimpleNamespace(
            name="Name", instructions="", metadata={"native": ["notebooks/abcdef", detail]}
        )
        rpc = SimpleNamespace(call=AsyncMock())
        editor = NotebookEditor(rpc, SimpleNamespace(get=AsyncMock(return_value=notebook)))
        await editor.write("update", "notebooks/abcdef", instructions="Instructions")
        payload = rpc.call.call_args.args[1][1]
        self.assertEqual(payload[12], [None, None, 0, 1, 0])
        self.assertIsNone(payload[18])
        self.assertEqual(payload[16], detail[12])

    async def test_cannot_delete_a_source_from_another_notebook(self):
        rpc = SimpleNamespace(call=AsyncMock())
        editor = NotebookEditor(rpc, None)
        with self.assertRaises(ValueError):
            await editor.write(
                "remove_source", "notebooks/abcdef", resource_id="notebooks/another/sources/a"
            )
        rpc.call.assert_not_awaited()


class ClaudeHTTPTests(unittest.IsolatedAsyncioTestCase):
    def test_catalog_uses_selected_workspace_and_filters_inactive_models(self):
        data = {
            "account": {
                "settings": {"default_model": "live"},
                "memberships": [
                    {
                        "organization": {
                            "uuid": "workspace",
                            "capabilities": ["chat"],
                            "claude_ai_bootstrap_models_config": [
                                {
                                    "model": "live",
                                    "name": "Live",
                                    "thinking_modes": [{"id": "auto", "title": "Thinking"}],
                                },
                                {"model": "old", "inactive": True},
                            ],
                        }
                    }
                ],
            }
        }
        org = organization_from_bootstrap(data, "workspace")
        self.assertEqual(org, "workspace")
        with self.assertRaises(ProviderError):
            organization_from_bootstrap(data, "other")

    async def test_http_stream_preserves_published_thinking_and_end_marker(self):
        cid = "11111111-1111-1111-1111-111111111111"
        events = [
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "Summary"},
            },
            {"type": "content_block_start", "index": 1, "content_block": {"type": "text"}},
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": "An"},
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": "swer"},
            },
            {"type": "message_stop"},
        ]

        class Response:
            status_code = 200

            async def aiter_lines(self):
                for e in events:
                    yield "data: " + json.dumps(e)
                    yield ""

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        session = SimpleNamespace(
            path=Path("unused"),
            request=AsyncMock(return_value={}),
            connect=Mock(return_value=SimpleNamespace(stream=Mock(return_value=Response()))),
            check=Mock(),
            url=lambda path: "https://claude.ai" + path,
        )
        transport = ClaudeTransport(Path("unused"), session=session)
        transport.organization = cid
        transport.catalog = {
            "default_model": "model",
            "models": [{"id": "model", "efforts": [], "native": {}}],
        }
        with patch("asyncio.create_subprocess_exec", side_effect=AssertionError("browser")):
            result = [e async for e in transport.events("send", prompt="Question", model="model")]
        self.assertTrue(any(e.get("channel") == "analysis" for e in result))
        final = [e for e in result if "text" in e and e.get("channel") != "analysis"]
        self.assertEqual("".join(e["text"] for e in final), "Answer")
        self.assertTrue(all(e["type"] == "delta" for e in final))
        self.assertEqual(session.request.await_count, 1)


class ClaudeFileDeletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_uploaded_files_use_native_file_uuid_and_documents_stay_separate(self):
        from providers.claude.transport import ClaudeProjectEditor

        pid = "11111111-1111-1111-1111-111111111111"
        transport = SimpleNamespace(
            base=AsyncMock(return_value="/api/organizations/org"),
            request=AsyncMock(
                return_value={
                    "docs": [],
                    "files": [{"uuid": "resource", "file_uuid": "native-file"}],
                }
            ),
            session=SimpleNamespace(request=AsyncMock()),
        )
        editor = ClaudeProjectEditor(transport)
        await editor.write("remove_source", pid, resource_id="resource")
        transport.session.request.assert_awaited_once_with(
            "POST",
            f"/api/organizations/org/projects/{pid}/files/delete_many",
            json={"file_uuids": ["native-file"]},
        )
        transport.session.request.reset_mock()
        transport.request.return_value = {"docs": [{"uuid": "document"}], "files": []}
        await editor.write("remove_source", pid, resource_id="document")
        transport.session.request.assert_awaited_once_with(
            "DELETE", f"/api/organizations/org/projects/{pid}/docs/document"
        )

    async def test_deleted_foreign_source_cannot_be_submitted(self):
        from providers.claude.transport import ClaudeProjectEditor

        transport = SimpleNamespace(
            base=AsyncMock(return_value="/api/organizations/org"),
            request=AsyncMock(return_value={"docs": [], "files": []}),
            session=SimpleNamespace(request=AsyncMock()),
        )
        with self.assertRaises(ValueError):
            await ClaudeProjectEditor(transport).write(
                "remove_source", "11111111-1111-1111-1111-111111111111", resource_id="foreign"
            )
        transport.session.request.assert_not_awaited()
