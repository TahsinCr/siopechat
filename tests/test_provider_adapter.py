import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from providers import Capability, ProviderRegistry, ReplaceText, SendRequest, TextDelta
from providers.chatgpt import BrowserSession, ChatGPTProvider, SessionPaths
from providers.chatgpt.client import ChatGPTClient
from providers.chatgpt.errors import SentinelCompatibilityError
from providers.errors import RateLimited, RemoteNotFound
from providers.models import Choice, FeatureState, Model, ModelCatalog, SessionState
from tasks import JobState, TaskService
from tests.fakes import FakeClient


class ProviderAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_structured_text_parts_are_readable_alongside_thinking(self):
        self.client.raw["messages"] = [
            {
                "id": "thought",
                "author": {"role": "assistant"},
                "content": {"content_type": "thoughts", "thoughts": [{"summary": "Thinking"}]},
            },
            {
                "id": "answer",
                "author": {"role": "assistant"},
                "content": {
                    "content_type": "multimodal_text",
                    "parts": [{"content_type": "audio_transcription", "text": "Spoken answer"}],
                },
            },
        ]
        chat = await self.provider.get_chat("remote")
        self.assertEqual([m.content[0].text for m in chat.messages], ["Thinking", "Spoken answer"])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.session = BrowserSession(
            SessionPaths(root / "session.json", root / "cache", root / "profile")
        )
        self.client = FakeClient(self.session)
        self.provider = ChatGPTProvider(self.session, client_factory=lambda _: self.client)

    async def test_session_snapshot_is_not_mistaken_for_verified_authentication(self):
        self.assertEqual((await self.provider.session_status()).state, SessionState.LOGIN_REQUIRED)
        self.session.paths.session_file.write_text("{}")
        self.assertEqual((await self.provider.session_status()).state, SessionState.UNKNOWN)
        self.assertEqual((await self.provider.authenticate()).state, SessionState.AUTHENTICATED)

    async def test_forget_session_retains_browser_profile_and_caches(self):
        self.session.paths.session_file.write_text("{}")
        self.session.paths.profile.mkdir()
        self.session.paths.cache.mkdir()
        result = await self.provider.forget_session()
        self.assertEqual(result.state, SessionState.LOGIN_REQUIRED)
        self.assertFalse(self.session.paths.session_file.exists())
        self.assertTrue(self.session.paths.profile.exists())
        self.assertTrue(self.session.paths.cache.exists())

    async def test_history_keeps_provenance_thinking_channel_sources_and_native_fields(self):
        page = await self.provider.list_chats(limit=1)
        self.assertEqual(page.next_cursor, "1")
        chat = page.items[0]
        self.assertEqual(chat.origin.provider_id, "chatgpt")
        self.assertEqual(chat.origin.remote_id, "remote")
        self.assertEqual(chat.messages[0].channel, "analysis")
        self.assertEqual(chat.messages[0].content[0].text, "Visible summary")
        self.assertEqual(
            chat.messages[0].metadata["citations"], self.client.raw["messages"][0]["citations"]
        )
        self.assertEqual(chat.metadata, self.client.raw)

    async def test_discovery_creation_and_deletion_are_available(self):
        description = await self.provider.describe()
        self.assertEqual(description.feature(Capability.PROJECTS).state, FeatureState.AVAILABLE)
        self.client.model_catalog = lambda: ModelCatalog(FeatureState.AVAILABLE)
        self.client.list_projects = lambda cursor: {"items": [], "cursor": None}
        self.assertEqual((await self.provider.models()).state, FeatureState.AVAILABLE)
        self.assertEqual((await self.provider.list_projects()).items, ())
        self.client.create_project = lambda name, instructions, **options: {
            "id": "g-p-created",
            "name": name,
            "instructions": instructions,
            "metadata": options,
        }
        created = await self.provider.create_project("New project", memory="project_only")
        self.assertEqual(created.name, "New project")
        project_reads = iter([{"id": "g-p-created", "name": "Fixture"}, RemoteNotFound("deleted")])

        def get_project(value):
            result = next(project_reads)
            if isinstance(result, Exception):
                raise result
            return result

        self.client.get_project = get_project
        with (
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as browser,
            patch.object(self.provider.session, "launch"),
        ):
            await self.provider.delete_project("g-p-created")
            browser.assert_not_awaited()
            self.assertEqual(
                self.client.deleted,
                ("DELETE", "https://chatgpt.com/backend-api/gizmos/g-p-created"),
            )

    async def test_request_waits_for_background_preparation(self):
        ready, release = threading.Event(), threading.Event()

        def prepare_catalog():
            ready.set()
            if not release.wait(2):
                raise TimeoutError("test warmup was not released")
            return ModelCatalog(FeatureState.AVAILABLE)

        self.client.model_catalog = prepare_catalog
        warmup = asyncio.create_task(self.provider.prepare())
        self.assertTrue(await asyncio.to_thread(ready.wait, 2))
        history = asyncio.create_task(self.provider.get_chat("remote"))
        await asyncio.sleep(0.02)
        self.assertFalse(history.done())
        release.set()
        await warmup
        self.assertEqual((await history).origin.remote_id, "remote")

    async def test_default_http_client_is_reused_after_startup_warmup(self):
        import httpx

        raw = {"id": "remote", "title": "Example", "messages": []}
        catalog = ModelCatalog(FeatureState.AVAILABLE)
        self.session.load = lambda: ("https://chatgpt.com/", {"authorization": "Bearer test"})
        real_http_client = httpx.Client
        with (
            patch("providers.chatgpt.client.httpx.Client", wraps=real_http_client) as clients,
            patch.object(ChatGPTClient, "model_catalog", return_value=catalog) as model_catalog,
            patch.object(ChatGPTClient, "list_page", return_value={"items": [raw]}),
            patch.object(ChatGPTClient, "get_conversation", return_value=raw),
            patch.object(ChatGPTClient, "get_project", return_value={"metadata": {}}),
        ):
            provider = ChatGPTProvider(self.session)
            await provider.prepare()
            await provider.models()
            await provider.models(force=True)
            await provider.list_chats(limit=1)
            await provider.get_chat("remote")
            await provider.project_options("project")
            self.assertEqual(clients.call_count, 1)
            self.assertIsNotNone(provider._http_client)
            self.assertIs(
                provider._http_client.temporary_conversations, provider._temporary_conversations
            )
            model_catalog.assert_called_with(refresh=True)
            await provider.close()
            self.assertIsNone(provider._http_client)

    async def test_project_file_source_is_streamed_using_a_dedicated_client(self):
        from providers.chatgpt.projects import ChatGPTProjectEditor

        path = Path(self.directory.name) / "source.txt"
        path.write_text("fixture")
        self.client.upload_file = lambda name, mime, data, **kwargs: (
            {"id": "file", "size_bytes": data.stat().st_size}
            if isinstance(data, Path) and data.resolve() == path.resolve()
            else self.fail("expected a disk path")
        )
        editor = ChatGPTProjectEditor(self.session, lambda _: self.client)
        with patch("pathlib.Path.read_bytes", side_effect=AssertionError("must stream")):
            await editor.write("add_source", "project", kind="file", path=path)
        self.assertTrue(self.client.closed.is_set())

    async def test_completed_snapshot_publishes_thinking_summaries(self):
        self.client.closed.set()
        self.client.raw["messages"][0]["content"] = {
            "content_type": "thoughts",
            "thoughts": [{"summary": "Published summary"}],
        }
        events = [
            event
            async for event in self.provider.stream(
                SendRequest("chatgpt", "default", "local", "hello")
            )
        ]
        self.assertIn(ReplaceText("Published summary", "analysis"), events)

    async def test_completion_does_not_publish_hidden_or_tool_directed_analysis(self):
        self.client.closed.set()
        self.client.raw["messages"] = [
            {
                "id": "hidden",
                "author": {"role": "assistant"},
                "channel": "analysis",
                "metadata": {"is_visually_hidden_from_conversation": True},
                "content": {"parts": ["Hidden"]},
            },
            {
                "id": "tool",
                "author": {"role": "assistant"},
                "recipient": "browser",
                "channel": "analysis",
                "content": {"parts": ["Tool protocol"]},
            },
        ]
        events = [
            event
            async for event in self.provider.stream(
                SendRequest("chatgpt", "default", "local", "hello")
            )
        ]
        self.assertFalse(
            any(isinstance(event, ReplaceText) and event.channel == "analysis" for event in events)
        )

    async def test_stream_produces_common_events_and_releases_transport(self):
        self.client.closed.set()
        events = [
            event
            async for event in self.provider.stream(
                SendRequest("chatgpt", "default", "local", "hello")
            )
        ]
        self.assertEqual(events[0], TextDelta("partial"))
        self.assertEqual(events[-1].conversation_id, "remote")
        self.assertTrue(self.client.closed.is_set())

    async def test_job_cancellation_closes_sync_transport_and_waits_for_worker(self):
        registry = ProviderRegistry()
        registry.register(self.provider)
        runtime = TaskService(registry)
        job = runtime.submit(SendRequest("chatgpt", "default", "local", "hello"))
        self.assertTrue(await asyncio.to_thread(self.client.started.wait, 2))
        result = await asyncio.wait_for(runtime.cancel(job), 3)
        self.assertEqual(result.state, JobState.CANCELLED)
        self.assertEqual(result.text, "partial")
        self.assertTrue(self.client.closed.is_set())
        await runtime.close()

    async def test_upload_cancellation_signals_transport_and_waits_for_worker(self):
        import threading
        import time

        started, stopped = threading.Event(), threading.Event()
        closed = threading.Event()

        class UploadClient:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                self.close()

            def close(self):
                closed.set()

            def upload_file(self, _name, _mime, _path, *, cancel_event=None):
                started.set()
                while not (cancel_event and cancel_event.is_set()) and not closed.is_set():
                    time.sleep(0.002)
                stopped.set()
                raise RuntimeError("upload stopped")

        client = UploadClient()
        provider = ChatGPTProvider(self.session, client_factory=lambda _session: client)
        cancel_event = threading.Event()
        task = asyncio.create_task(
            provider.upload_file(
                Path(self.directory.name) / "cancel.txt", cancel_event=cancel_event
            )
        )
        self.assertTrue(await asyncio.to_thread(started.wait, 2))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        self.assertTrue(cancel_event.is_set())
        self.assertTrue(stopped.is_set())
        self.assertTrue(closed.is_set())

    async def test_rate_limit_survives_adapter_and_is_not_retried(self):
        self.client.closed.set()
        self.client.failure = RateLimited(30)
        with self.assertRaises(RateLimited) as raised:
            async for _ in self.provider.stream(
                SendRequest("chatgpt", "default", "local", "hello")
            ):
                pass
        self.assertEqual(raised.exception.retry_after, 30)

    async def test_sentinel_compatibility_error_keeps_its_type_and_submission_state(self):
        self.client.closed.set()
        failure = SentinelCompatibilityError("private signer detail")
        self.client.failure = failure
        with self.assertRaises(SentinelCompatibilityError) as raised:
            async for _ in self.provider.stream(
                SendRequest("chatgpt", "default", "local", "hello")
            ):
                pass
        self.assertIs(raised.exception, failure)
        self.assertEqual(raised.exception.code, "chatgpt_sentinel_incompatible")
        self.assertFalse(raised.exception.message_submitted)

    async def test_runtime_health_overlay_leaves_unrelated_capabilities_usable(self):
        registry = ProviderRegistry()
        registry.register(self.provider)
        registry.mark_unavailable("chatgpt", Capability.CHAT)
        description = await registry.describe("chatgpt")
        self.assertEqual(description.feature(Capability.CHAT).state, FeatureState.UNAVAILABLE)
        self.assertEqual(description.feature(Capability.HISTORY).state, FeatureState.AVAILABLE)
        self.assertIs(registry.require("chatgpt", Capability.HISTORY), self.provider)

    def test_medium_effort_only_selected_when_catalog_supports_it(self):
        catalog = ModelCatalog(
            FeatureState.AVAILABLE,
            (
                Model("a", "A", (Choice("low", "Low"), Choice("medium", "Medium")), "low"),
                Model("b", "B", (Choice("auto", "Automatic"),), "auto"),
            ),
        )
        self.assertEqual(catalog.effort("a"), "medium")
        self.assertEqual(catalog.effort("b"), "auto")
        self.assertIsNone(catalog.effort("unknown"))
