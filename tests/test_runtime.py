import asyncio
import unittest

from providers import (
    AccountPaused,
    Capability,
    CompatibilityError,
    RateLimited,
    ReplaceText,
    TextDelta,
)
from providers.chatgpt.errors import SentinelCompatibilityError
from providers.registry import ProviderRegistry
from tasks import JobState, TaskService
from tests.fakes import ControlledProvider, request


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.registry = ProviderRegistry()
        self.gpt = ControlledProvider()
        self.gemini = ControlledProvider("gemini")
        self.registry.register(self.gpt)
        self.registry.register(self.gemini)
        self.now = 100.0
        self.runtime = TaskService(self.registry, clock=lambda: self.now)

    async def asyncTearDown(self):
        await self.runtime.close()

    async def started(self, provider):
        return await asyncio.wait_for(provider.started.get(), 1)

    async def finish(self, job_id):
        return await asyncio.wait_for(self.runtime.wait(job_id), 1)

    def test_registry_account_factory_preserves_identity_and_isolates_instances(self):
        from types import SimpleNamespace

        registry = ProviderRegistry()
        registry.register(
            ControlledProvider(),
            account_factory=lambda account, **options: SimpleNamespace(
                id="gpt", account_id=account, options=options
            ),
        )
        first = registry.create_account("gpt", "personal", credential_file="personal")
        second = registry.create_account("gpt", "work", credential_file="work")
        self.assertIsNot(first, second)
        self.assertEqual(first.options["credential_file"], "personal")
        self.assertEqual(second.account_id, "work")

    async def test_account_resolver_never_uses_the_registry_default_session(self):
        personal, work = ControlledProvider(), ControlledProvider()
        self.runtime.provider_resolver = lambda provider, account: {
            "personal": personal,
            "work": work,
        }[account]
        first = self.runtime.submit(request("one", account="personal"))
        second = self.runtime.submit(request("two", account="work"))
        await self.started(personal)
        await self.started(work)
        self.assertEqual(self.gpt.calls, [])
        personal.release("one")
        work.release("two")
        self.assertEqual((await self.finish(first)).state, JobState.COMPLETED)
        self.assertEqual((await self.finish(second)).state, JobState.COMPLETED)

    async def test_other_provider_finishes_while_first_is_waiting(self):
        first = self.runtime.submit(request())
        second = self.runtime.submit(request("two", "gemini"))
        await self.started(self.gpt)
        await self.started(self.gemini)
        self.gemini.release("two")
        result = await self.finish(second)
        self.assertEqual(result.state, JobState.COMPLETED)
        self.assertEqual(result.text, "partial response")
        self.assertEqual(result.conversation_id, "remote-two")
        self.assertEqual(self.runtime.snapshot(first).state, JobState.STREAMING)

    async def test_same_account_serializes_but_different_account_runs(self):
        first = self.runtime.submit(request("one"))
        queued = self.runtime.submit(request("two"))
        other = self.runtime.submit(request("three", account="other"))
        self.assertEqual(
            {await self.started(self.gpt), await self.started(self.gpt)}, {"one", "three"}
        )
        self.assertEqual(self.runtime.snapshot(queued).state, JobState.QUEUED)
        self.gpt.release("one")
        await self.finish(first)
        self.assertEqual(await self.started(self.gpt), "two")
        await self.runtime.cancel(queued)
        await self.runtime.cancel(other)

    async def test_view_closing_and_waiter_cancellation_do_not_stop_job(self):
        notifications = self.runtime.subscribe()
        job = self.runtime.submit(request())
        await self.started(self.gpt)
        waiter = asyncio.create_task(self.runtime.wait(job))
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.runtime.unsubscribe(notifications)
        self.gpt.release("one")
        self.assertEqual((await self.finish(job)).state, JobState.COMPLETED)

    async def test_duplicate_active_chat_rejected_and_reusable_after_completion(self):
        first = self.runtime.submit(request())
        with self.assertRaises(ValueError):
            self.runtime.submit(request())
        self.gpt.release("one")
        await self.finish(first)
        second = self.runtime.submit(request())
        self.assertEqual((await self.finish(second)).state, JobState.COMPLETED)

    async def test_cancel_running_closes_generator_and_preserves_partial(self):
        job = self.runtime.submit(request())
        await self.started(self.gpt)
        result = await self.runtime.cancel(job)
        self.assertEqual(result.state, JobState.CANCELLED)
        self.assertEqual(result.text, "partial")
        self.assertIn("one", self.gpt.closed)
        self.assertEqual(len(self.gpt.calls), 1)

    async def test_cancel_before_task_starts_releases_chat_identity(self):
        job = self.runtime.submit(request())
        self.assertEqual((await self.runtime.cancel(job)).state, JobState.CANCELLED)
        self.assertFalse(self.gpt.calls)
        replacement = self.runtime.submit(request())
        self.gpt.release("one")
        await self.finish(replacement)

    async def test_failing_view_callback_does_not_fail_provider_or_persistence(self):
        persisted = []

        def broken_view(_event):
            raise RuntimeError("view closed during stream")

        async def persist(snapshot):
            persisted.append(snapshot)

        job = self.runtime.submit(request(), on_event=broken_view, on_complete=persist)
        await self.started(self.gpt)
        self.gpt.release("one")

        result = await self.finish(job)

        self.assertEqual(result.state, JobState.COMPLETED)
        self.assertEqual(result.text, "partial response")
        self.assertEqual(len(persisted), 1)
        self.assertEqual(persisted[0].text, "partial response")
        self.assertEqual(persisted[0].conversation_id, "remote-one")

    async def test_cancel_during_persistence_finishes_accepted_response(self):
        persist_started = asyncio.Event()
        persist_release = asyncio.Event()
        persisted = []

        async def persist(snapshot):
            persist_started.set()
            await persist_release.wait()
            persisted.append(snapshot.text)
            return "saved"

        job = self.runtime.submit(request(), on_complete=persist)
        await self.started(self.gpt)
        self.gpt.release("one")
        await asyncio.wait_for(persist_started.wait(), 1)

        stopping = asyncio.create_task(self.runtime.cancel(job))
        await asyncio.sleep(0)
        self.assertEqual(self.runtime.snapshot(job).state, JobState.CANCELLING)
        persist_release.set()

        result = await asyncio.wait_for(stopping, 1)
        self.assertEqual(result.state, JobState.COMPLETED)
        self.assertEqual(await self.runtime.result(job), "saved")
        self.assertEqual(persisted, ["partial response"])

    async def test_cancel_queued_does_not_affect_running_job(self):
        running = self.runtime.submit(request())
        queued = self.runtime.submit(request("two"))
        await self.started(self.gpt)
        self.assertEqual((await self.runtime.cancel(queued)).state, JobState.CANCELLED)
        self.gpt.release("one")
        self.assertEqual((await self.finish(running)).state, JobState.COMPLETED)
        self.assertEqual([value.chat_id for value in self.gpt.calls], ["one"])

    async def test_queued_job_rechecks_extension_toggle(self):
        first = self.runtime.submit(request())
        queued = self.runtime.submit(request("two"))
        await self.started(self.gpt)
        self.registry.set_enabled("gpt", False)
        self.gpt.release("one")
        self.assertEqual((await self.finish(first)).state, JobState.COMPLETED)
        self.assertEqual((await self.finish(queued)).state, JobState.FAILED)
        self.assertEqual(len(self.gpt.calls), 1)

    async def test_rate_limit_blocks_queued_and_new_work_without_retry(self):
        self.gpt.failure = RateLimited(30)
        first = self.runtime.submit(request())
        queued = self.runtime.submit(request("two"))
        self.gpt.release("one")
        self.assertEqual((await self.finish(first)).error_code, "rate_limited")
        self.assertEqual((await self.finish(queued)).error_code, "account_paused")
        with self.assertRaises(AccountPaused):
            self.runtime.submit(request("three"))
        with self.assertRaises(AccountPaused):
            self.runtime.resume_account("gpt", "default")
        self.assertEqual(len(self.gpt.calls), 1)
        self.now += 31
        self.gpt.failure = None
        self.gpt.release("three")
        later = self.runtime.submit(request("three"))
        self.assertEqual((await self.finish(later)).state, JobState.COMPLETED)

    async def test_unknown_retry_delay_requires_explicit_resume(self):
        self.gpt.failure = RateLimited()
        self.gpt.release("one")
        await self.finish(self.runtime.submit(request()))
        self.now += 1_000_000
        with self.assertRaises(AccountPaused):
            self.runtime.submit(request("two"))
        self.runtime.resume_account("gpt", "default")
        self.gpt.failure = None
        self.gpt.release("two")
        await self.finish(self.runtime.submit(request("two")))

    async def test_auth_pause_is_scoped_to_account(self):
        self.gpt.failure = AccountPaused()
        self.gpt.release("one")
        self.assertEqual(
            (await self.finish(self.runtime.submit(request()))).error_code, "account_paused"
        )
        with self.assertRaises(AccountPaused):
            self.runtime.submit(request("two"))
        self.gemini.release("two")
        await self.finish(self.runtime.submit(request("two", "gemini")))

    async def test_compatibility_failure_disables_only_reported_capability(self):
        self.gpt.failure = CompatibilityError(Capability.PROJECTS)
        self.gpt.release("one")
        result = await self.finish(self.runtime.submit(request()))
        self.assertEqual(result.error_code, "capability_changed")
        with self.assertRaises(ValueError):
            self.registry.require("gpt", Capability.PROJECTS)
        self.assertIs(self.registry.require("gpt", Capability.CHAT), self.gpt)
        self.registry.restore("gpt", Capability.PROJECTS)
        self.assertIs(self.registry.require("gpt", Capability.PROJECTS), self.gpt)

    async def test_partial_failure_is_not_retried_or_reported_as_success(self):
        self.gpt.failure = RuntimeError("secret=token conversation=user-data")
        self.gpt.release("one")
        result = await self.finish(self.runtime.submit(request()))
        self.assertEqual(result.state, JobState.FAILED)
        self.assertEqual(result.text, "partial")
        self.assertEqual(result.error_code, "provider_error")
        self.assertNotIn("secret", repr(result))
        self.assertEqual(len(self.gpt.calls), 1)
        self.gemini.release("other")
        other = self.runtime.submit(request("other", "gemini"))
        self.assertEqual((await self.finish(other)).state, JobState.COMPLETED)

    async def test_pre_submission_failure_is_terminal_and_releases_chat_for_next_send(self):
        self.gpt.failure = SentinelCompatibilityError("private signer detail")
        first = self.runtime.submit(request())
        self.gpt.release("one")
        failed = await self.finish(first)
        self.assertEqual(failed.state, JobState.FAILED)
        self.assertEqual(failed.error_code, "chatgpt_sentinel_incompatible")
        self.assertFalse(self.gpt.failure.message_submitted)

        self.gpt.failure = None
        second = self.runtime.submit(request())
        succeeded = await self.finish(second)
        self.assertEqual(succeeded.state, JobState.COMPLETED)

    async def test_malformed_retry_delay_still_fails_cleanly_and_pauses(self):
        self.gpt.failure = RateLimited("invalid header")
        self.gpt.release("one")
        result = await self.finish(self.runtime.submit(request()))
        self.assertEqual(result.state, JobState.FAILED)
        self.assertEqual(result.error_code, "rate_limited")
        with self.assertRaises(AccountPaused):
            self.runtime.submit(request("two"))

    async def test_notifications_are_bounded_and_state_can_be_resynchronized(self):
        queue = self.runtime.subscribe(capacity=1)
        self.gpt.release("one")
        job = self.runtime.submit(request())
        result = await self.finish(job)
        self.assertEqual(queue.qsize(), 1)
        self.assertEqual(queue.get_nowait().state, JobState.COMPLETED)
        self.assertEqual(self.runtime.snapshots(), (result,))
        self.assertEqual(result.text, "partial response")

    async def test_replace_events_and_size_limit(self):
        class ReplacingProvider:
            id = "replace"
            capabilities = frozenset({Capability.CHAT})

            async def stream(self, request):
                yield TextDelta("old")
                yield ReplaceText("new")
                yield TextDelta("!")

        self.registry.register(ReplacingProvider())
        job = self.runtime.submit(request(provider="replace"))
        self.assertEqual((await self.finish(job)).text, "new!")
        small = TaskService(self.registry, max_response_chars=3)
        self.addAsyncCleanup(small.close)
        result = await small.wait(small.submit(request(provider="replace")))
        self.assertEqual(result.state, JobState.FAILED)
        self.assertEqual(result.error_code, "response_too_large")
        self.assertEqual(result.text, "new")

    async def test_unsupported_options_rejected_before_provider_call(self):
        self.gpt.capabilities = frozenset({Capability.CHAT})
        for options in ({"model": "x"}, {"effort": "high"}):
            with self.assertRaises(ValueError):
                self.runtime.submit(request(**options))
        self.assertFalse(self.gpt.calls)

    async def test_capacity_requires_explicit_release_of_finished_jobs(self):
        small = TaskService(self.registry, max_jobs=1)
        self.addAsyncCleanup(small.close)
        first = small.submit(request())
        with self.assertRaises(ValueError):
            small.forget(first)
        self.gpt.release("one")
        await small.wait(first)
        with self.assertRaises(ValueError):
            small.submit(request("two"))
        small.forget(first)
        second = small.submit(request("two"))
        await small.cancel(second)

    async def test_close_cancels_all_jobs_and_rejects_new_work(self):
        jobs = [self.runtime.submit(request(str(index))) for index in range(3)]
        await self.started(self.gpt)
        await self.runtime.close()
        self.assertTrue(all(self.runtime.snapshot(job).state == JobState.CANCELLED for job in jobs))
        self.assertEqual([value.chat_id for value in self.gpt.calls], ["0"])
        with self.assertRaises(RuntimeError):
            self.runtime.submit(request("new"))

    async def test_parallelism_is_explicitly_configurable_per_account(self):
        parallel = TaskService(self.registry, per_account=2)
        self.addAsyncCleanup(parallel.close)
        jobs = [parallel.submit(request(str(index))) for index in range(3)]
        self.assertEqual({await self.started(self.gpt), await self.started(self.gpt)}, {"0", "1"})
        self.assertEqual(parallel.snapshot(jobs[2]).state, JobState.QUEUED)
        await parallel.close()

    async def test_queued_job_rechecks_changed_effort_capability(self):
        first = self.runtime.submit(request())
        queued = self.runtime.submit(request("two", effort="provider-owned-effort"))
        await self.started(self.gpt)
        self.registry.mark_unavailable("gpt", Capability.EFFORT)
        self.gpt.release("one")
        await self.finish(first)
        self.assertEqual((await self.finish(queued)).state, JobState.FAILED)
        self.assertEqual(len(self.gpt.calls), 1)

    async def test_empty_deltas_do_not_grow_response_or_emit_invalidations(self):
        class EmptyProvider:
            id = "empty"
            capabilities = frozenset({Capability.CHAT})

            async def stream(self, request):
                for _ in range(1000):
                    yield TextDelta("")
                yield TextDelta("ok")

        self.registry.register(EmptyProvider())
        job = self.runtime.submit(request(provider="empty"))
        result = await self.finish(job)
        self.assertEqual(result.text, "ok")
        self.assertEqual(result.version, 4)  # queued, running, text, completed


class RegistryTests(unittest.TestCase):
    def test_duplicate_registration_and_disabled_extension(self):
        registry = ProviderRegistry()
        provider = ControlledProvider()
        registry.register(provider)
        with self.assertRaises(ValueError):
            registry.register(provider)
        registry.set_enabled("gpt", False)
        with self.assertRaises(ValueError):
            registry.require("gpt", Capability.CHAT)
        registry.set_enabled("gpt", True)
        self.assertIs(registry.require("gpt", Capability.CHAT), provider)
