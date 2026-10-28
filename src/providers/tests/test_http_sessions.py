"""Saved web sessions: no browser processes, redirects or mutation retries."""

import asyncio
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from providers.claude.transport import ClaudeTransport, organization_from_bootstrap
from providers.errors import AccountPaused, ProviderError, RateLimited, RemoteNotFound
from providers.gemini.rpc import NotebookEditor, NotebookRPC
from providers.session import WebSession, read_session, save_session, save_session_async


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

    async def test_large_json_is_parsed_off_the_provider_loop(self):
        loop_thread = threading.get_ident()
        self.client.request.return_value = SimpleNamespace(
            status_code=200,
            content=b" " * (256 * 1024),
            json=lambda: {"thread": threading.get_ident()},
        )
        value = await self.session.request("GET", "/api/history")
        self.assertNotEqual(value["thread"], loop_thread)

    async def test_cancelled_persistence_finishes_before_returning(self):
        started, release = threading.Event(), threading.Event()

        def write(*_args):
            started.set()
            if not release.wait(2):
                raise TimeoutError("write not released")

        with patch("providers.session.save_session", side_effect=write):
            task = asyncio.create_task(save_session_async(self.path, {}))
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            task.cancel()
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

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

    async def test_first_discovery_is_shared_with_requests_during_warmup(self):
        organization = "11111111-1111-1111-1111-111111111111"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            save_session(path, {"cookies": {"lastActiveOrg": organization}})
            calls = []

            async def request(method, endpoint, **_kwargs):
                calls.append(endpoint)
                if endpoint == "/api/bootstrap":
                    await asyncio.sleep(0.02)
                    return {
                        "account": {
                            "memberships": [
                                {
                                    "organization": {
                                        "uuid": organization,
                                        "capabilities": ["chat"],
                                    }
                                }
                            ]
                        }
                    }
                if endpoint.endswith("/model_selector/chat"):
                    await asyncio.sleep(0.02)
                    return {
                        "model_selector_config": [
                            {"id": "chat", "models": [{"id": "model", "name": "Model"}]}
                        ],
                        "model_selector_state": [],
                    }
                if endpoint.endswith("/chat_conversations?limit=50&offset=0"):
                    return []
                raise AssertionError(endpoint)

            session = SimpleNamespace(
                path=path,
                request=AsyncMock(side_effect=request),
                close=AsyncMock(),
            )
            transport = ClaudeTransport(path, session=session)
            self.addAsyncCleanup(transport.close)
            _, chats = await asyncio.gather(
                transport.prepare(), transport.request("chats", cursor=0, limit=50)
            )

        self.assertEqual(chats, [])
        self.assertEqual(calls.count("/api/bootstrap"), 1)
        selector = f"/api/organizations/{organization}/model_selector/chat"
        chats_endpoint = f"/api/organizations/{organization}/chat_conversations?limit=50&offset=0"
        self.assertEqual(calls.count(selector), 1)
        self.assertGreater(calls.index(chats_endpoint), calls.index(selector))

    async def test_model_catalog_expires_and_explicit_refresh_bypasses_cache(self):
        transport = ClaudeTransport("unused")
        self.addAsyncCleanup(transport.close)

        async def discover():
            transport.organization = "org"
            transport.catalog = {"models": []}
            transport._discovered_at = asyncio.get_running_loop().time()
            return transport.catalog

        with patch.object(transport, "_discover", side_effect=discover) as fetch:
            await asyncio.gather(transport.discover(), transport.discover())
            await transport.discover()
            self.assertEqual(fetch.call_count, 1)
            await transport.discover(force=True)
            self.assertEqual(fetch.call_count, 2)
            transport._discovered_at -= 301
            await transport.discover()
            self.assertEqual(fetch.call_count, 3)

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
