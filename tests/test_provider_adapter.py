import asyncio
import tempfile
import unittest
from pathlib import Path

from providers import ProviderRegistry, SendRequest
from providers.chatgpt import BrowserSession, ChatGPTProvider, SessionPaths
from providers.tests.fakes import FakeClient
from tasks import JobState, TaskService


class ProviderAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.session = BrowserSession(SessionPaths(root / "session.json", root / "profile"))
        self.client = FakeClient(self.session)
        self.provider = ChatGPTProvider(self.session, client_factory=lambda _: self.client)

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
