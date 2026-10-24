import asyncio
import tempfile
import unittest
from pathlib import Path

from actions import Action, ActionChanged
from bootstrap import create_application
from extension import ExtensionAPI, ExtensionManager, ExtensionSpec
from runtime import ServiceChanged
from settings import Settings, SettingsChanged
from tasks import JobChanged, JobState
from tests.fakes import ControlledProvider, request
from utils.container import DependencyContainer
from utils.events import EventBus


class RuntimeServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.api = ExtensionAPI(Settings(Path(self.directory.name) / "config.json"))
        self.addAsyncCleanup(self.api.runtime.aclose)

    async def test_shared_services_resolve_from_the_upstream_container(self):
        self.assertIsInstance(self.api.container, DependencyContainer)
        self.assertIsInstance(self.api.events, EventBus)
        self.assertEqual(type(self.api.container).__module__, "utils.container")
        self.assertIs(self.api.get("actions"), self.api.container.resolve("actions"))
        shared = object()
        self.api.provide("sample", "shared", shared)
        self.api.container.singleton(
            "consumer", factory=lambda dependency: dependency, dependencies={"dependency": "shared"}
        )
        self.assertIs(self.api.container.resolve("consumer"), shared)
        with self.assertRaises(ValueError):
            self.api.provide("other", "shared", object())

    async def test_actions_settings_and_tasks_publish_through_the_same_bus(self):
        events = []
        self.api.events.subscribe(events.append)

        def fail(_):
            raise RuntimeError("observer failure")

        self.api.events.subscribe(fail)
        self.api.actions.register("sample", Action("sample.run", "Run", lambda: 42))
        self.assertEqual(await self.api.actions.invoke("sample.run"), 42)
        self.api.settings.update("application", {"language": "tr"})
        provider = ControlledProvider()
        self.api.providers.register(provider)
        job = self.api.tasks.submit(request())
        provider.release("one")
        result = await self.api.tasks.wait(job)
        self.assertEqual(result.state, JobState.COMPLETED)
        self.assertTrue(any(isinstance(event, ActionChanged) for event in events))
        self.assertTrue(any(isinstance(event, SettingsChanged) for event in events))
        self.assertTrue(any(isinstance(event, JobChanged) for event in events))
        self.assertGreater(self.api.events.stats.failed, 0)

    async def test_extension_uses_scopes_and_async_resource_cleanup(self):
        manager = ExtensionManager(self.api)
        closed = []

        class Resource:
            async def aclose(self):
                closed.append("async")

        class Scoped:
            def close(self):
                closed.append("scoped")

        seen = []

        def activate(context):
            context.container.scoped("scoped", factory=Scoped)
            context.container.resolve("scoped")
            context.provide("resource", Resource())
            context.subscribe(seen.append, event_type=ServiceChanged)

        manager.register(ExtensionSpec("sample", activate, api_version=1))
        self.assertTrue(manager.activate("sample"))
        self.api.provide("other", "other", object())
        self.assertTrue(seen)
        await manager.disable_async("sample")
        self.assertEqual(closed, ["scoped", "async"])
        before = len(seen)
        self.api.provide("other", "another", object())
        self.assertEqual(len(seen), before)
        with self.assertRaises(LookupError):
            self.api.get("resource")
        self.assertIsNotNone(self.api.get("other"))

    async def test_async_cleanup_failure_can_be_retried_without_stopping_other_services(self):
        manager = ExtensionManager(self.api)

        class Resource:
            attempts = 0

            async def aclose(self):
                self.attempts += 1
                if self.attempts == 1:
                    raise OSError("temporary cleanup failure")

        resource = Resource()
        manager.register(
            ExtensionSpec(
                "sample", lambda context: context.provide("resource", resource), api_version=1
            )
        )
        manager.activate("sample")
        await manager.disable_async("sample")
        self.assertEqual(manager.statuses()["sample"].state, "failed")
        self.assertIsNotNone(self.api.get("settings"))
        await manager.disable_async("sample")
        self.assertEqual(manager.statuses()["sample"].state, "disabled")
        self.assertEqual(resource.attempts, 2)

    async def test_runtime_shutdown_preserves_extension_enabled_preference(self):
        root = Path(self.directory.name) / "application"
        api = create_application(root)
        settings = api.settings
        tasks = api.tasks
        before = settings.path.read_bytes()
        await api.runtime.aclose()
        self.assertEqual(settings.path.read_bytes(), before)
        self.assertTrue(tasks._closed)
        self.assertTrue(api.events.closed)
        self.assertTrue(api.container.closed)
        await api.runtime.aclose()

    async def test_cross_thread_bus_publication_reaches_bounded_ui_queue(self):
        queue = self.api.tasks.subscribe(capacity=1)
        event = JobChanged("external", JobState.STREAMING, 1)
        await asyncio.to_thread(self.api.events.publish, event)
        self.assertEqual(await asyncio.wait_for(queue.get(), 1), event)
        queue.task_done()
        self.api.tasks.unsubscribe(queue)

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
        self.assertTrue(self.api.container.closed)


class ThemeTests(unittest.TestCase):
    def test_invalid_optional_theme_does_not_prevent_loading_healthy_theme(self):
        from theme import ThemeService

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "broken.json").write_text("invalid")
            (root / "healthy.json").write_text('{"id":"healthy","tokens":{"accent":"#123456"}}')
            themes = ThemeService()
            themes.load_directory(root)
        self.assertIn("healthy", themes.available())
        self.assertEqual(themes.resolve("healthy")["accent"], "#123456")
        self.assertEqual(themes.failures, {"broken.json": "invalid_theme"})
