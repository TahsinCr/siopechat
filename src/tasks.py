"""One asyncio-loop runtime; UI subscriptions never own the response stream."""

import asyncio
import logging
import math
import time
import uuid
from contextlib import aclosing
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from providers.api import (
    AccountPaused,
    Capability,
    CompatibilityError,
    ConversationBound,
    MessageAction,
    MessageMetadata,
    ProviderError,
    RateLimited,
    ReplaceText,
    SendRequest,
    TextDelta,
)
from providers.registry import ProviderRegistry
from utils.events import EventBus

_LOG = logging.getLogger(__name__)


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    STREAMING = "streaming"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL = frozenset({JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED})


class _AccountBlocked(AccountPaused):
    """Queued work observed an existing pause; do not change its recovery mode."""


@dataclass(frozen=True)
class JobSnapshot:
    id: str
    provider_id: str
    account_id: str
    chat_id: str
    state: JobState
    text: str
    conversation_id: str | None
    error_code: str | None
    version: int
    reasoning: str = ""
    metadata: MessageMetadata | None = None


@dataclass(frozen=True)
class JobChanged:
    id: str
    state: JobState
    version: int


@dataclass
class _Job:
    request: SendRequest
    state: JobState = JobState.QUEUED
    chunks: list[str] = field(default_factory=list)
    length: int = 0
    reasoning_chunks: list[str] = field(default_factory=list)
    reasoning_length: int = 0
    conversation_id: str | None = None
    error_code: str | None = None
    version: int = 0
    task: asyncio.Task | None = None
    cancel_requested: bool = False
    event_sink: Callable | None = None
    complete: Callable | None = None
    guard: Callable | None = None
    result: object = None
    error: BaseException | None = None
    metadata: MessageMetadata | None = None


class TaskService:
    def __init__(
        self,
        providers: ProviderRegistry,
        *,
        events: EventBus | None = None,
        per_account: int = 1,
        max_jobs: int = 256,
        max_response_chars: int = 2_000_000,
        clock: Callable[[], float] = time.monotonic,
        provider_resolver: Callable | None = None,
    ):
        if per_account < 1 or max_jobs < 1 or max_response_chars < 1:
            raise ValueError("limits must be positive")
        self.events = events if events is not None else EventBus()
        self._owns_events = events is None
        self.providers = providers
        self.provider_resolver = provider_resolver
        self._per_account = per_account
        self._max_jobs = max_jobs
        self._max_chars = max_response_chars
        self._clock = clock
        self._jobs: dict[str, _Job] = {}
        self._active_chats: set[tuple[str, str, str]] = set()
        self._chat_jobs: dict[tuple[str, str, str], int] = {}
        self._gates: dict[tuple[str, str], asyncio.Semaphore] = {}
        self._blocked: dict[tuple[str, str], float | None] = {}
        self._subscribers: dict[asyncio.Queue[JobChanged], object] = {}
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None

    def _check_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("runtime belongs to a different event loop")

    def subscribe(self, capacity: int = 64) -> asyncio.Queue[JobChanged]:
        self._check_loop()
        if self._closed:
            raise RuntimeError("runtime is closed")
        if capacity < 1:
            raise ValueError("capacity must be positive")
        queue: asyncio.Queue[JobChanged] = asyncio.Queue(maxsize=capacity)
        loop = asyncio.get_running_loop()

        def deliver(event):
            if queue not in self._subscribers:
                return
            if queue.full():
                queue.get_nowait()
                queue.task_done()
            queue.put_nowait(event)

        def receive(event):
            try:
                current = asyncio.get_running_loop()
            except RuntimeError:
                current = None
            if current is loop:
                deliver(event)
            else:
                loop.call_soon_threadsafe(deliver, event)

        self._subscribers[queue] = self.events.subscribe(receive, event_type=JobChanged)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[JobChanged]) -> None:
        self._check_loop()
        subscription = self._subscribers.pop(queue, None)
        if subscription is not None:
            subscription.cancel()

    def _changed(self, job_id: str, state: JobState | None = None) -> None:
        job = self._jobs[job_id]
        if state is not None:
            job.state = state
        job.version += 1
        event = JobChanged(job_id, job.state, job.version)
        # The shared bus carries invalidations; bounded UI queues retain drop/resync semantics.
        try:
            self.events.publish(event)
        except Exception:
            # State notifications are invalidations; a broken observer must not
            # turn a successful provider response into a failed job.
            _LOG.warning("Task state observer failed for job %s", job_id)

    @staticmethod
    def _deliver_event(job: _Job, event) -> None:
        sink = job.event_sink
        if sink is None:
            return
        try:
            sink(event)
        except Exception:
            # Detach a faulty view once. Provider streaming and persistence own
            # the job lifecycle, not a callback attached to one presentation.
            job.event_sink = None
            _LOG.warning("Detached failing task observer for job %s", job.request.chat_id)

    def _check_account(self, account: tuple[str, str]) -> None:
        if account not in self._blocked:
            return
        until = self._blocked[account]
        if until is None or self._clock() < until:
            raise _AccountBlocked("account requires waiting or user action")
        del self._blocked[account]

    def _block_account(self, account: tuple[str, str], until: float | None) -> None:
        if account not in self._blocked:
            self._blocked[account] = until
        elif self._blocked[account] is not None:
            previous = self._blocked[account]
            self._blocked[account] = None if until is None else max(previous, until)

    def resume_account(self, provider_id: str, account_id: str) -> None:
        self._check_loop()
        account = (provider_id, account_id)
        until = self._blocked.get(account)
        if until is not None and self._clock() < until:
            raise AccountPaused("server cooldown has not expired")
        self._blocked.pop(account, None)

    def submit(
        self, request: SendRequest, *, on_event=None, on_complete=None, guard=None, queue_chat=False
    ) -> str:
        self._check_loop()
        if self._closed:
            raise RuntimeError("runtime is closed")
        if not all((request.provider_id, request.account_id, request.chat_id)):
            raise ValueError("provider, account and local chat IDs are required")
        if not request.prompt.strip() and request.action != MessageAction.RETRY:
            raise ValueError("prompt must not be empty")
        if len(self._jobs) >= self._max_jobs:
            raise ValueError("job capacity reached; persist and forget completed jobs")
        self.providers.require(request.provider_id, Capability.CHAT)
        if request.action:
            self.providers.require(request.provider_id, request.action.capability)
        if request.branch:
            self.providers.require(request.provider_id, Capability.CHAT_BRANCH)
        if request.model is not None:
            self.providers.require(request.provider_id, Capability.MODELS)
        if request.effort is not None:
            self.providers.require(request.provider_id, Capability.EFFORT)
        account = (request.provider_id, request.account_id)
        self._check_account(account)
        chat = (*account, request.chat_id)
        if queue_chat and self._per_account != 1:
            raise ValueError("queued chat submissions require account serialization")
        if chat in self._active_chats and not queue_chat:
            raise ValueError("this chat already has an active job")
        job_id = uuid.uuid4().hex
        self._jobs[job_id] = _Job(
            request,
            conversation_id=request.conversation_id,
            event_sink=on_event,
            complete=on_complete,
            guard=guard,
        )
        self._active_chats.add(chat)
        self._chat_jobs[chat] = self._chat_jobs.get(chat, 0) + 1
        self._changed(job_id)
        self._jobs[job_id].task = asyncio.create_task(self._run(job_id))
        return job_id

    def _release_chat(self, request: SendRequest) -> None:
        account = (request.provider_id, request.account_id)
        chat = (*account, request.chat_id)
        count = self._chat_jobs.get(chat, 0)
        if count > 1:
            self._chat_jobs[chat] = count - 1
        else:
            self._chat_jobs.pop(chat, None)
            self._active_chats.discard(chat)

    def account_gate(self, provider_id, account_id):
        self._check_loop()
        return self._gates.setdefault(
            (provider_id, account_id), asyncio.Semaphore(self._per_account)
        )

    async def _run(self, job_id: str) -> None:
        job = self._jobs[job_id]
        request = job.request
        account = (request.provider_id, request.account_id)
        gate = self.account_gate(*account)
        try:
            async with gate:
                if self._closed:
                    raise asyncio.CancelledError
                self._check_account(account)
                # Recheck queued work: an extension may have been disabled.
                provider = self.providers.require(request.provider_id, Capability.CHAT)
                if request.temporary:
                    self.providers.require(request.provider_id, Capability.TEMPORARY)
                if request.project_id:
                    self.providers.require(request.provider_id, Capability.PROJECTS)
                if request.model is not None:
                    self.providers.require(request.provider_id, Capability.MODELS)
                if request.effort is not None:
                    self.providers.require(request.provider_id, Capability.EFFORT)
                if self.provider_resolver is not None:
                    provider = self.provider_resolver(request.provider_id, request.account_id)
                if request.action:
                    self.providers.require(request.provider_id, request.action.capability)
                if request.branch:
                    self.providers.require(request.provider_id, Capability.CHAT_BRANCH)
                if job.guard is not None:
                    job.guard()
                self._changed(job_id, JobState.RUNNING)
                async with aclosing(provider.stream(request)) as stream:
                    async for event in stream:
                        if job.cancel_requested:
                            raise asyncio.CancelledError
                        if isinstance(event, ConversationBound):
                            if not event.conversation_id:
                                raise ValueError("empty remote conversation ID")
                            job.conversation_id = event.conversation_id
                        elif isinstance(event, MessageMetadata):
                            job.metadata = event
                            self._deliver_event(job, event)
                        elif isinstance(event, (TextDelta, ReplaceText)):
                            if isinstance(event, TextDelta) and not event.text:
                                continue
                            if event.channel == "analysis":
                                length = len(event.text) + (
                                    job.reasoning_length if isinstance(event, TextDelta) else 0
                                )
                                if length + job.length > self._max_chars:
                                    job.error_code = "response_too_large"
                                    raise ValueError("response capacity reached")
                                if isinstance(event, ReplaceText):
                                    job.reasoning_chunks.clear()
                                job.reasoning_chunks.append(event.text)
                                job.reasoning_length = length
                                self._deliver_event(job, event)
                                self._changed(job_id)
                                continue
                            length = len(event.text)
                            if isinstance(event, TextDelta):
                                length += job.length
                            if length + job.reasoning_length > self._max_chars:
                                job.error_code = "response_too_large"
                                raise ValueError("response capacity reached")
                            if isinstance(event, ReplaceText):
                                job.chunks.clear()
                            job.chunks.append(event.text)
                            job.length = length
                            job.state = JobState.STREAMING
                        else:
                            raise TypeError("unsupported provider event")
                        self._deliver_event(job, event)
                        self._changed(job_id)
                if job.cancel_requested:
                    raise asyncio.CancelledError
                if job.complete is not None:
                    snapshot = self._snapshot(job_id, job)
                    writing = asyncio.create_task(
                        job.complete(snapshot), name="siope-persist:" + job_id
                    )
                    try:
                        job.result = await asyncio.shield(writing)
                    except asyncio.CancelledError:
                        # The provider has already completed and persistence has
                        # started. Finish that accepted write, then report success;
                        # cancelling here would mislabel durable data as discarded.
                        job.result = await asyncio.shield(writing)
                self._changed(job_id, JobState.COMPLETED)
        except asyncio.CancelledError:
            self._changed(job_id, JobState.CANCELLED)
            raise
        except RateLimited as error:
            job.error = error
            try:
                delay = float(error.retry_after)
            except (TypeError, ValueError, OverflowError):
                delay = float("nan")
            until = self._clock() + delay if math.isfinite(delay) and delay >= 0 else None
            self._block_account(account, until)
            job.error_code = "rate_limited"
            self._changed(job_id, JobState.FAILED)
        except _AccountBlocked as error:
            job.error = error
            job.error_code = "account_paused"
            self._changed(job_id, JobState.FAILED)
        except AccountPaused as error:
            job.error = error
            self._block_account(account, None)
            job.error_code = "account_paused"
            self._changed(job_id, JobState.FAILED)
        except CompatibilityError as error:
            job.error = error
            if self.provider_resolver is None:
                self.providers.mark_unavailable(request.provider_id, error.capability)
            job.error_code = "capability_changed"
            self._changed(job_id, JobState.FAILED)
        except ProviderError as error:
            job.error = error
            job.error_code = error.code
            self._changed(job_id, JobState.FAILED)
        except Exception as error:
            job.error = error
            # Raw adapter errors can contain request headers or conversation data.
            job.error_code = job.error_code or "provider_error"
            self._changed(job_id, JobState.FAILED)
        finally:
            self._release_chat(request)

    def snapshot(self, job_id: str) -> JobSnapshot:
        self._check_loop()
        return self._snapshot(job_id, self._jobs[job_id])

    @staticmethod
    def _snapshot(job_id: str, job: _Job) -> JobSnapshot:
        return JobSnapshot(
            id=job_id,
            provider_id=job.request.provider_id,
            account_id=job.request.account_id,
            chat_id=job.request.chat_id,
            state=job.state,
            text="".join(job.chunks),
            reasoning="".join(job.reasoning_chunks),
            conversation_id=job.conversation_id,
            error_code=job.error_code,
            version=job.version,
            metadata=job.metadata,
        )

    def snapshots(self) -> tuple[JobSnapshot, ...]:
        self._check_loop()
        return tuple(self.snapshot(job_id) for job_id in self._jobs)

    async def wait(self, job_id: str) -> JobSnapshot:
        self._check_loop()
        job = self._jobs[job_id]
        task = job.task
        # Waiting does not own/cancel the job. asyncio.wait also distinguishes
        # observer cancellation from cancellation of the underlying task.
        await asyncio.wait((task,))
        if not task.cancelled():
            task.result()
        return self._snapshot(job_id, job)

    async def result(self, job_id):
        snapshot = await self.wait(job_id)
        job = self._jobs[job_id]
        if snapshot.state == JobState.CANCELLED:
            raise asyncio.CancelledError
        if job.error is not None:
            raise job.error
        return job.result

    def _request_cancel(self, job_id: str) -> None:
        job = self._jobs[job_id]
        if not job.cancel_requested:
            job.cancel_requested = True
            self._changed(job_id, JobState.CANCELLING)
            job.task.cancel()

    async def cancel(self, job_id: str) -> JobSnapshot:
        self._check_loop()
        job = self._jobs[job_id]
        if job.state not in TERMINAL:
            self._request_cancel(job_id)
            await asyncio.wait((job.task,))
            if not job.task.cancelled():
                job.task.result()
            # Tasks cancelled before their first instruction have no finally.
            if job.state not in TERMINAL:
                self._release_chat(job.request)
                self._changed(job_id, JobState.CANCELLED)
        return self._snapshot(job_id, job)

    def forget(self, job_id: str) -> None:
        self._check_loop()
        if self._jobs[job_id].state not in TERMINAL:
            raise ValueError("cannot forget an active job")
        del self._jobs[job_id]

    async def close(self) -> None:
        if self._closed:
            return
        self._check_loop()
        self._closed = True
        # Cancel all queued tasks before yielding: releasing one account gate
        # must not start another queued send during shutdown.
        pending = [(key, job) for key, job in self._jobs.items() if job.state not in TERMINAL]
        for job_id, job in pending:
            self._request_cancel(job_id)
        await asyncio.gather(*(job.task for _, job in pending), return_exceptions=True)
        for job_id, job in pending:
            if job.state not in TERMINAL:
                self._release_chat(job.request)
                self._changed(job_id, JobState.CANCELLED)
        for subscription in self._subscribers.values():
            subscription.cancel()
        self._subscribers.clear()
        if self._owns_events:
            self.events.close()
