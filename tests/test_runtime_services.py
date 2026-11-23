import asyncio
import signal
import tempfile
import unittest
from pathlib import Path

from eventflow import EventBus

from actions import Action, ActionChanged, ActionService
from bootstrap import Application, create_application
from runtime import Runtime, ServiceChanged
from settings import Settings, SettingsChanged


class RuntimeSyncCloseTests(unittest.TestCase):
    def test_close_completes_cleanup_when_sigint_arrives_during_shutdown(self):
        runtime = Runtime()
        calls = []
        previous_sigint = signal.getsignal(signal.SIGINT)

        def shutdown():
            calls.append("started")
            signal.raise_signal(signal.SIGINT)
            calls.append("finished")

        runtime.on_shutdown(lambda: asyncio.to_thread(shutdown))
        runtime.close()

        self.assertEqual(calls, ["started", "finished"])
        self.assertTrue(runtime._closed)
        self.assertIs(signal.getsignal(signal.SIGINT), previous_sigint)


class RuntimeServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.runtime = Runtime()
        self.settings = Settings(
            Path(self.directory.name) / "config.json", events=self.runtime.events
        )
        self.actions = ActionService(self.runtime.events)
        self.runtime.provide("application", "actions", self.actions)
        self.api = Application(self.settings, self.runtime, self.actions)
        self.addAsyncCleanup(self.api.runtime.aclose)

    async def test_runtime_registers_and_resolves_owned_services(self):
        self.assertIsInstance(self.api.events, EventBus)
        self.assertIs(self.api.get("actions"), self.api.actions)
        closed = []

        class Service:
            def close(self):
                closed.append(True)

        shared = Service()
        self.api.runtime.provide("sample", "shared", shared)
        self.assertIs(self.api.get("shared"), shared)
        with self.assertRaises(ValueError):
            self.api.runtime.provide("other", "shared", object())
        self.api.runtime.unregister("sample")
        self.assertEqual(closed, [True])
        with self.assertRaises(LookupError):
            self.api.get("shared")

    async def test_actions_and_settings_publish_through_the_same_bus(self):
        events = []
        self.api.events.subscribe(events.append)

        def fail(_):
            raise RuntimeError("observer failure")

        self.api.events.subscribe(fail)
        self.api.actions.register("sample", Action("sample.run", "Run", lambda: 42))
        self.assertEqual(await self.api.actions.invoke("sample.run"), 42)
        self.api.settings.update("application", {"language": "tr"})
        self.assertTrue(any(isinstance(event, ActionChanged) for event in events))
        self.assertTrue(any(isinstance(event, SettingsChanged) for event in events))
        self.assertGreater(self.api.events.stats.failed, 0)

    async def test_runtime_provided_resources_use_explicit_cleanup(self):
        closed = []

        class Resource:
            async def aclose(self):
                closed.append("async")

        seen = []
        sub = self.api.events.subscribe(seen.append, event_type=ServiceChanged)
        self.api.runtime.provide("sample", "resource", Resource())
        other_resource = object()
        self.api.runtime.provide("other", "other", other_resource)
        self.assertTrue(seen)
        await self.api.runtime.unregister_async("sample")
        self.assertEqual(closed, ["async"])
        sub.cancel()
        before = len(seen)
        self.api.runtime.provide("other", "another", object())
        self.assertEqual(len(seen), before)
        with self.assertRaises(LookupError):
            self.api.get("resource")
        self.assertIs(self.api.get("other"), other_resource)

    async def test_async_cleanup_failure_can_be_retried_without_stopping_other_services(self):
        class Resource:
            attempts = 0

            async def aclose(self):
                self.attempts += 1
                if self.attempts == 1:
                    raise OSError("temporary cleanup failure")

        resource = Resource()
        other_resource = object()
        self.api.runtime.provide("sample", "resource", resource)
        self.api.runtime.provide("other", "other", other_resource)
        with self.assertRaises(OSError):
            await self.api.runtime.unregister_async("sample")
        self.assertIs(self.api.get("other"), other_resource)
        await self.api.runtime.unregister_async("sample")
        self.assertEqual(resource.attempts, 2)
        with self.assertRaises(LookupError):
            self.api.get("resource")

    async def test_runtime_shutdown_preserves_settings_file_and_closes_accounts(self):
        root = Path(self.directory.name) / "application"
        api = create_application(root)
        settings = api.settings
        accounts = api.accounts
        before = settings.path.read_bytes()
        await api.runtime.aclose()
        self.assertEqual(settings.path.read_bytes(), before)
        self.assertTrue(accounts.closed)
        self.assertTrue(api.events.closed)
        self.assertTrue(api.runtime._closed)
        await api.runtime.aclose()

    async def test_cross_thread_bus_publication_reaches_subscriber(self):
        received = asyncio.Event()
        loop = asyncio.get_running_loop()
        events = []

        def on_event(event):
            events.append(event)
            loop.call_soon_threadsafe(received.set)

        sub = self.api.events.subscribe(on_event, event_type=ServiceChanged)
        try:
            event = ServiceChanged("external", "svc", "registered")
            await asyncio.to_thread(self.api.events.publish, event)
            await asyncio.wait_for(received.wait(), 1)
            self.assertEqual(events, [event])
        finally:
            sub.cancel()

    async def test_cancelling_shutdown_waiter_does_not_interrupt_cleanup(self):
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def shutdown():
            calls.append("started")
            started.set()
            await release.wait()
            calls.append("finished")

        self.api.runtime.on_shutdown(shutdown)
        first = asyncio.create_task(self.api.runtime.aclose())
        await started.wait()
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(self.api.runtime.aclose())
        release.set()
        await second
        self.assertEqual(calls, ["started", "finished"])
        self.assertTrue(self.api.runtime._closed)
