import asyncio
import unittest

from providers import (
    AccountPaused,
    Capability,
    RateLimited,
    TextDelta,
)
from providers.registry import ProviderRegistry
from tasks import JobState, TaskService
from tests.fakes import ControlledProvider, request


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.registry = ProviderRegistry()
        self.provider = ControlledProvider()
        self.registry.register(self.provider)
        self.now = 100.0
        self.runtime = TaskService(self.registry, per_account=2, clock=lambda: self.now)

    async def asyncTearDown(self):
        await self.runtime.close()

    async def start_pair(self):
        first = self.runtime.submit(request("one"))
        second = self.runtime.submit(request("two"))
        await asyncio.wait_for(self.provider.started.get(), 1)
        await asyncio.wait_for(self.provider.started.get(), 1)
        return first, second

    async def test_concurrent_rate_limit_cannot_shorten_existing_cooldown(self):
        self.provider.failures = {"one": RateLimited(60), "two": RateLimited(5)}
        first, second = await self.start_pair()
        self.provider.release("one")
        await self.runtime.wait(first)
        self.provider.release("two")
        await self.runtime.wait(second)
        self.now += 10
        with self.assertRaises(AccountPaused):
            self.runtime.submit(request("three"))

    async def test_rate_limit_cannot_clear_required_user_verification(self):
        self.provider.failures = {"one": AccountPaused(), "two": RateLimited(5)}
        first, second = await self.start_pair()
        self.provider.release("one")
        await self.runtime.wait(first)
        self.provider.release("two")
        await self.runtime.wait(second)
        self.now += 10
        with self.assertRaises(AccountPaused):
            self.runtime.submit(request("three"))

    async def test_user_verification_escalates_an_existing_finite_cooldown(self):
        self.provider.failures = {"one": RateLimited(5), "two": AccountPaused()}
        first, second = await self.start_pair()
        self.provider.release("one")
        await self.runtime.wait(first)
        self.provider.release("two")
        await self.runtime.wait(second)
        self.now += 10
        with self.assertRaises(AccountPaused):
            self.runtime.submit(request("three"))

    async def test_waiter_retains_result_if_completed_job_is_forgotten(self):
        self.provider.release("one")
        job = self.runtime.submit(request())
        await self.runtime.wait(job)
        waiter = asyncio.create_task(self.runtime.wait(job))
        await asyncio.sleep(0)
        self.runtime.forget(job)
        self.assertEqual((await waiter).state, JobState.COMPLETED)

    async def test_cancellation_suppressed_by_adapter_is_not_success(self):
        class SuppressingProvider:
            id = "suppress"
            capabilities = frozenset({Capability.CHAT})

            async def stream(self, request):
                yield TextDelta("partial")
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    return

        self.registry.register(SuppressingProvider())
        job = self.runtime.submit(request(provider="suppress"))
        await asyncio.sleep(0)
        self.assertEqual((await self.runtime.cancel(job)).state, JobState.CANCELLED)

    async def test_repeated_cancel_does_not_interrupt_adapter_cleanup(self):
        cleanup_started = asyncio.Event()
        cleanup_release = asyncio.Event()
        cleanup_done = asyncio.Event()

        class CleaningProvider:
            id = "cleanup"
            capabilities = frozenset({Capability.CHAT})

            async def stream(self, request):
                try:
                    yield TextDelta("partial")
                    await asyncio.Event().wait()
                finally:
                    cleanup_started.set()
                    await cleanup_release.wait()
                    cleanup_done.set()

        self.registry.register(CleaningProvider())
        job = self.runtime.submit(request(provider="cleanup"))
        await asyncio.sleep(0)
        first = asyncio.create_task(self.runtime.cancel(job))
        await asyncio.wait_for(cleanup_started.wait(), 1)
        second = asyncio.create_task(self.runtime.cancel(job))
        await asyncio.sleep(0)
        cleanup_release.set()
        results = await asyncio.wait_for(asyncio.gather(first, second), 1)
        self.assertTrue(cleanup_done.is_set())
        self.assertTrue(all(result.state == JobState.CANCELLED for result in results))

    async def test_dropped_notifications_do_not_break_queue_join(self):
        queue = self.runtime.subscribe(1)
        self.provider.release("one")
        job = self.runtime.submit(request())
        await self.runtime.wait(job)
        queue.get_nowait()
        queue.task_done()
        await asyncio.wait_for(queue.join(), 0.1)

    async def test_cannot_add_subscribers_after_shutdown(self):
        await self.runtime.close()
        with self.assertRaises(RuntimeError):
            self.runtime.subscribe()


class LoopOwnershipTests(unittest.TestCase):
    def test_second_event_loop_is_rejected_explicitly(self):
        runtime = TaskService(ProviderRegistry())

        async def first_loop():
            queue = runtime.subscribe()
            runtime.unsubscribe(queue)

        asyncio.run(first_loop())

        async def second_loop():
            with self.assertRaises(RuntimeError):
                runtime.subscribe()

        asyncio.run(second_loop())
