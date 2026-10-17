"""Native Gemini routing and browser subprocess lifecycle without account traffic."""

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from providers import ConversationBound, ReplaceText, SendRequest
from providers.errors import ProviderError
from providers.gemini import GeminiProvider
from providers.gemini.browser import NativeBrowser


class NativeBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_readiness_failure_happens_before_subprocess_and_is_not_retried(self):
        ready = Mock(side_effect=RuntimeError("browser unavailable"))
        browser = NativeBrowser("http://127.0.0.1:9231", ensure_ready=ready)
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
            with self.assertRaisesRegex(RuntimeError, "browser unavailable"):
                await anext(browser.events("send", prompt="test"))
            spawn.assert_not_awaited()
        ready.assert_called_once_with()

    async def test_configured_browser_bypasses_legacy_sdk_generation_and_models(self):
        p = GeminiProvider(
            Path("unused"), "test", notebooks=SimpleNamespace(cdp_url="http://127.0.0.1:9231")
        )
        p.browser.authenticated = True
        p._connect = AsyncMock(side_effect=AssertionError("legacy SDK must not send"))

        async def stream(request):
            self.assertEqual(request.project_id, "notebooks/abcdef")
            yield ConversationBound("c_abcdef")
            yield ReplaceText("Answer", "final")

        p.browser.stream = stream
        p.browser.models = AsyncMock(return_value="native catalog")
        self.assertEqual(await p.models(), "native catalog")
        request = SendRequest(
            "gemini",
            "test",
            "local",
            "Question",
            model="gemini-flash",
            effort="native:4",
            project_id="notebooks/abcdef",
        )
        events = [e async for e in p.stream(request)]
        self.assertEqual(events[-1].text, "Answer")
        p._connect.assert_not_awaited()

    async def test_native_catalog_keeps_discovered_model_and_effort_identities(self):
        b = NativeBrowser("local")

        async def events(mode):
            yield {
                "type": "models",
                "items": [
                    {
                        "id": "gemini-next",
                        "title": "9 Next",
                        "remote": "opaque",
                        "efforts": [{"id": "native:4", "title": "Orta"}],
                    }
                ],
            }

        b.events = events
        model = (await b.models()).models[0]
        self.assertEqual(
            (model.id, model.title, model.default_effort), ("gemini-next", "9 Next", "native:4")
        )
        self.assertEqual(model.efforts[0].title, "Orta")

    async def test_eof_without_completion_is_not_a_successful_send(self):
        b = NativeBrowser("local")

        async def events(*args, **kwargs):
            yield {"type": "bound", "id": "c_abcdef"}

        b.events = events
        with self.assertRaisesRegex(ProviderError, "tamamlanmadan"):
            _ = [e async for e in b.stream(SendRequest("gemini", "test", "local", "Question"))]

    async def test_temporary_resume_uses_its_owned_tab_without_navigation_identity(self):
        b = NativeBrowser("local")
        calls = []

        async def events(mode, **options):
            calls.append(options)
            yield {"type": "tab", "id": "owned-temp"}
            yield {"type": "bound", "id": options["temporary_id"]}
            yield {"type": "replace", "text": "Answer", "channel": "final"}
            yield {"type": "done"}

        b.events = events
        request = SendRequest("gemini", "test", "local", "Question", temporary=True)
        first = [e async for e in b.stream(request)]
        remote = first[0].conversation_id
        request = SendRequest(
            "gemini", "test", "local", "Follow up", conversation_id=remote, temporary=True
        )
        _ = [e async for e in b.stream(request)]
        self.assertTrue(calls[1]["resume_temporary"])
        self.assertEqual(calls[1]["tab_id"], "owned-temp")
        self.assertIsNone(calls[1]["conversation_id"])
        b.close_tab = AsyncMock()
        await b.close_temporary(remote)
        b.close_tab.assert_awaited_once_with("owned-temp")
        self.assertNotIn(remote, b.temporary_tabs)

    async def test_stream_subprocess_tracks_only_owned_target_and_authentication(self):
        b = NativeBrowser("http://127.0.0.1:9231")
        records = [
            {"type": "tab", "id": "owned"},
            {"type": "status", "stage": "sent"},
            {"type": "done"},
        ]
        process = SimpleNamespace(
            stdin=Mock(drain=AsyncMock()),
            stdout=SimpleNamespace(
                readline=AsyncMock(
                    side_effect=[*(json.dumps(r).encode() + b"\n" for r in records), b""]
                )
            ),
            wait=AsyncMock(return_value=0),
            returncode=0,
        )
        with patch("providers.gemini.browser.asyncio.create_subprocess_exec", return_value=process):
            _ = [e async for e in b.events("send", prompt="Fixture")]
        self.assertEqual(b.tab_id, "owned")
        self.assertEqual(b.owned_tabs, {"owned"})
        self.assertTrue(b.authenticated)

    async def test_cancellation_terminates_browser_operation(self):
        import asyncio

        b = NativeBrowser("local")

        async def blocked():
            await asyncio.sleep(10)

        process = SimpleNamespace(
            stdin=Mock(drain=AsyncMock()),
            stdout=SimpleNamespace(readline=blocked),
            wait=AsyncMock(return_value=0),
            returncode=None,
            terminate=Mock(),
            kill=Mock(),
        )
        with patch("providers.gemini.browser.asyncio.create_subprocess_exec", return_value=process):
            task = asyncio.create_task(anext(b.events("send")))
            await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        process.terminate.assert_called_once()
        process.wait.assert_awaited_once()

    async def test_local_logout_cannot_be_bypassed_by_an_open_browser(self):
        import tempfile

        from providers.errors import AccountPaused

        with tempfile.TemporaryDirectory() as folder:
            p = GeminiProvider(
                Path(folder) / "cookies.json",
                notebooks=SimpleNamespace(cdp_url="http://127.0.0.1:9231"),
            )
            p.browser.models = AsyncMock()
            with self.assertRaises(AccountPaused):
                await p.models()
            p.browser.models.assert_not_awaited()
            await p.authenticate()
            p.browser.models.assert_awaited_once()


class AccountNativeTests(unittest.TestCase):
    def test_native_result_passes_through_account_send_and_archive_and_temporary_cleanup(self):
        import tempfile

        from accounts import identity
        from bootstrap import create_application

        with tempfile.TemporaryDirectory() as folder:
            api = create_application(Path(folder))
            try:
                accounts = api.get("accounts")
                key = accounts.add("gemini", "Native fixture")
                provider = GeminiProvider(
                    Path("unused"),
                    key.split("/")[1],
                    notebooks=SimpleNamespace(cdp_url="http://127.0.0.1:9231"),
                )
                provider.browser.authenticated = True
                provider._connect = AsyncMock(side_effect=AssertionError("SDK must not send"))

                async def stream(request):
                    self.assertEqual(request.project_id, "notebooks/abcdef")
                    yield ConversationBound("c_abcdef")
                    yield ReplaceText("Reasoning summary", "analysis")
                    yield ReplaceText("Answer", "final")

                provider.browser.stream = stream
                provider.browser.close_temporary = AsyncMock()
                accounts._providers[key] = provider
                delivered = []
                result = accounts.send(
                    key,
                    None,
                    "Question",
                    [],
                    delivered.append,
                    lambda _: None,
                    model="gemini-flash",
                    effort="native:4",
                    project_id=identity(key, "notebooks/abcdef"),
                )
                self.assertIsNone(result["cache_warning"])
                record = accounts.cached(key, "c_abcdef")
                self.assertEqual(
                    [m["text"] for m in record["messages"]],
                    ["Question", "Reasoning summary", "Answer"],
                )
                self.assertEqual(record["project_id"], identity(key, "notebooks/abcdef"))
                self.assertEqual(delivered[-1]["text"], "Answer")
                provider._connect.assert_not_awaited()
                accounts.callbacks(key)["forget_temporary"](identity(key, "temporary_fixture"))
                provider.browser.close_temporary.assert_awaited_once_with("temporary_fixture")
            finally:
                api.runtime.close()
