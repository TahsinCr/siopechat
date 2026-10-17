"""Dependency registration, scopes, resolution, and public container lifecycle."""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import threading
from collections.abc import Callable, Iterable
from typing import Any, cast

from .annotations import (
    choose_factory,
    choose_token,
    ensure_hashable_token,
    infer_token_from_factory,
    is_direct_instance,
    normalize_dependencies,
    normalize_priority,
)
from .context import enter_container, exit_container
from .registration import (
    DEFAULT_PRIORITY,
    DependencyMap,
    Lifetime,
    InitializationGate,
    InitializationRetiredError,
    MISSING,
    Provider,
    ProviderCallable,
    Registration,
    T,
    Token,
)
from .errors import (
    AsyncDependencyError,
    DependencyCleanupPendingError,
    DependencyContainerClosedError,
    DependencyNotFoundError,
    DependencyResolutionError,
)
from .injection import injection, resolve_missing_kwargs, resolve_missing_kwargs_async
from .lifecycle import (
    ResourceTracker,
    cached_instances,
    dispose_many,
    dispose_many_async,
    requires_async_disposal,
)
from .resolution import (
    can_autowire,
    close_awaitable,
    enter_resolution,
    exit_resolution,
    format_token,
)


_cleanup_operations: contextvars.ContextVar[
    tuple[tuple[int, str, threading.Event], ...]
] = (
    contextvars.ContextVar("dependency_cleanup_operations", default=())
)


class _ShutdownAttempt:
    __slots__ = ("async_owner", "done", "error", "owner_task", "owner_thread_id")

    def __init__(self, *, async_owner: bool = False) -> None:
        self.async_owner = async_owner
        self.done = threading.Event()
        self.error: BaseException | None = None
        self.owner_task: asyncio.Task[Any] | None = None
        self.owner_thread_id = threading.get_ident()


class _TokenDisposalAttempt:
    __slots__ = ("async_owner", "done", "error", "owner_task", "owner_thread_id")

    def __init__(self, *, async_owner: bool = False) -> None:
        self.async_owner = async_owner
        self.owner_thread_id = threading.get_ident()
        self.owner_task: asyncio.Task[Any] | None = None
        self.done = threading.Event()
        self.error: BaseException | None = None


class DependencyContainer:
    """Synchronous and asynchronous dependency injection container."""

    __slots__ = (
        "auto_wire",
        "parent",
        "_context_tokens",
        "_async_operations",
        "_providers",
        "_disposal_attempts",
        "_pending_disposals",
        "_registration_lock",
        "_closed",
        "_cleanup_failed",
        "_shutdown_attempt",
        "_shutdown_lock",
        "_scope_cache",
        "_scope_initializers",
        "_scope_lock",
        "_tracker",
    )

    def __init__(
        self,
        *,
        parent: DependencyContainer | None = None,
        auto_wire: bool = True,
    ) -> None:
        self.parent = parent
        self.auto_wire = auto_wire
        self._providers: dict[Token, Provider] = {}
        self._registration_lock = threading.RLock()
        self._disposal_attempts: dict[Token, _TokenDisposalAttempt] = {}
        self._pending_disposals: dict[Token, tuple[Any, ...]] = {}
        self._scope_cache: dict[Token, Any] = {}
        self._scope_lock = threading.RLock()
        self._scope_initializers: dict[Token, InitializationGate] = {}
        self._tracker = ResourceTracker()
        self._async_operations: dict[Token, asyncio.Task[None]] = {}
        self._closed = False
        self._cleanup_failed = False
        self._shutdown_attempt: _ShutdownAttempt | None = None
        self._shutdown_lock = threading.Lock()
        self._context_tokens: list[
            contextvars.Token[DependencyContainer | None]
        ] = []

    def __contains__(self, token: Token) -> bool:
        return self.has(token)

    @property
    def closed(self) -> bool:
        """Whether successful shutdown made this container terminal."""

        with self._shutdown_lock:
            return self._closed

    @property
    def cleanup_pending(self) -> bool:
        """Whether a failed global shutdown must be retried."""

        with self._shutdown_lock:
            return self._cleanup_failed

    def _ensure_open(self) -> None:
        with self._shutdown_lock:
            if self._closed:
                raise DependencyContainerClosedError("Dependency container is closed")
            if self._cleanup_failed:
                raise DependencyCleanupPendingError(
                    "Dependency container shutdown cleanup is pending"
                )
            if self._shutdown_attempt is not None:
                raise DependencyContainerClosedError(
                    "Dependency container is shutting down"
                )
        if self.parent is not None:
            self.parent._ensure_open()

    def _claim_shutdown(self, *, async_owner: bool = False) -> tuple[_ShutdownAttempt | None, bool]:
        with self._registration_lock, self._shutdown_lock:
            if self._closed:
                return None, False
            attempt = self._shutdown_attempt
            if attempt is None:
                attempt = _ShutdownAttempt(async_owner=async_owner)
                self._shutdown_attempt = attempt
                return attempt, True
            current_task = asyncio.current_task() if async_owner else None
            if attempt.owner_task is not None and attempt.owner_task is current_task:
                raise DependencyResolutionError(
                    "Dependency container shutdown cannot re-enter its own cleanup"
                )
            if not attempt.async_owner and attempt.owner_thread_id == threading.get_ident():
                raise DependencyResolutionError(
                    "Dependency container shutdown cannot re-enter its own cleanup"
                )
            return attempt, False

    def _finish_shutdown(
        self,
        attempt: _ShutdownAttempt,
        error: BaseException | None,
        *,
        cleanup_started: bool,
    ) -> None:
        with self._shutdown_lock:
            attempt.error = error
            if error is None:
                self._closed = True
                self._cleanup_failed = False
            elif cleanup_started:
                self._cleanup_failed = True
            if self._shutdown_attempt is attempt:
                self._shutdown_attempt = None
            attempt.done.set()

    def _claim_token_disposal(
        self,
        token: Token,
        *,
        async_owner: bool = False,
    ) -> tuple[_TokenDisposalAttempt, bool]:
        with self._registration_lock:
            self._ensure_open()
            attempt = self._disposal_attempts.get(token)
            if attempt is not None:
                return attempt, False
            attempt = _TokenDisposalAttempt(async_owner=async_owner)
            self._disposal_attempts[token] = attempt
            return attempt, True

    @staticmethod
    def _wait_for_token_disposal(attempt: _TokenDisposalAttempt) -> None:
        if attempt.owner_thread_id == threading.get_ident():
            if not attempt.async_owner:
                raise DependencyResolutionError(
                    "Dependency cleanup cannot re-enter its own token disposal"
                )
            raise AsyncDependencyError(
                "Async dependency cleanup cannot be awaited through the synchronous API; "
                "use unregister_async() or shutdown_async()"
            )
        attempt.done.wait()
        if attempt.error is not None:
            raise attempt.error

    def _finish_token_disposal(
        self,
        token: Token,
        attempt: _TokenDisposalAttempt,
        error: BaseException | None,
    ) -> None:
        attempt.error = error
        with self._registration_lock:
            if self._disposal_attempts.get(token) is attempt:
                self._disposal_attempts.pop(token, None)
        attempt.done.set()

    def _token_disposals(self) -> tuple[_TokenDisposalAttempt, ...]:
        with self._registration_lock:
            return tuple(self._disposal_attempts.values())

    def _complete_async_operation(
        self,
        token: Token,
        task: asyncio.Task[None],
    ) -> None:
        with self._registration_lock:
            if self._async_operations.get(token) is task:
                self._async_operations.pop(token, None)
        if not task.cancelled():
            task.exception()

    def __enter__(self) -> DependencyContainer:
        self._ensure_open()
        self._context_tokens.append(enter_container(self))
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        try:
            self.shutdown()
        finally:
            self._reset_current_container()

    async def __aenter__(self) -> DependencyContainer:
        self._ensure_open()
        self._context_tokens.append(enter_container(self))
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        try:
            await self.shutdown_async()
        finally:
            self._reset_current_container()

    def register(
        self,
        token: Token | None = None,
        provider: Any = MISSING,
        *,
        abstract: Token | None = None,
        concrete: Any = MISSING,
        factory: Any = MISSING,
        instance: Any = MISSING,
        lifetime: Lifetime | str = Lifetime.TRANSIENT,
        dependencies: DependencyMap | None = None,
        priority: int = DEFAULT_PRIORITY,
    ) -> DependencyContainer:
        """Register a class, factory, or ready instance for a token."""

        self._ensure_open()
        registration = self._normalize_registration(
            token=token,
            provider=provider,
            abstract=abstract,
            concrete=concrete,
            factory=factory,
            instance=instance,
            lifetime=lifetime,
            dependencies=dependencies,
            priority=priority,
        )
        with self._registration_lock:
            self._ensure_open()
            if (
                registration.token in self._disposal_attempts
                or registration.token in self._pending_disposals
            ):
                raise DependencyCleanupPendingError(
                    f"{format_token(registration.token)} still has pending cleanup"
                )
            replacing = (
                registration.token in self._providers
                or registration.token in self._scope_cache
            )
        if replacing:
            # Disposal executes user code and must never run while the
            # registration lock is held.
            self.unregister(registration.token)
        with self._registration_lock:
            self._ensure_open()
            if (
                registration.token in self._disposal_attempts
                or registration.token in self._pending_disposals
            ):
                raise DependencyCleanupPendingError(
                    f"{format_token(registration.token)} still has pending cleanup"
                )
            if registration.token in self._providers:
                raise RuntimeError(
                    f"Concurrent registration for {format_token(registration.token)}"
                )
            self._providers[registration.token] = Provider(
                token=registration.token,
                factory=registration.factory,
                lifetime=registration.lifetime,
                dependencies=registration.dependencies,
                priority=registration.priority,
                instance=registration.instance,
            )
            if registration.instance is not MISSING:
                self._tracker.remember(registration.instance)
        return self

    def singleton(
        self,
        token: Token | None = None,
        provider: Any = MISSING,
        *,
        abstract: Token | None = None,
        concrete: Any = MISSING,
        factory: Any = MISSING,
        instance: Any = MISSING,
        dependencies: DependencyMap | None = None,
        priority: int = DEFAULT_PRIORITY,
    ) -> DependencyContainer:
        return self.register(
            token,
            provider,
            abstract=abstract,
            concrete=concrete,
            factory=factory,
            instance=instance,
            lifetime=Lifetime.SINGLETON,
            dependencies=dependencies,
            priority=priority,
        )

    def transient(
        self,
        token: Token | None = None,
        provider: Any = MISSING,
        *,
        abstract: Token | None = None,
        concrete: Any = MISSING,
        factory: Any = MISSING,
        dependencies: DependencyMap | None = None,
        priority: int = DEFAULT_PRIORITY,
    ) -> DependencyContainer:
        return self.register(
            token,
            provider,
            abstract=abstract,
            concrete=concrete,
            factory=factory,
            lifetime=Lifetime.TRANSIENT,
            dependencies=dependencies,
            priority=priority,
        )

    def scoped(
        self,
        token: Token | None = None,
        provider: Any = MISSING,
        *,
        abstract: Token | None = None,
        concrete: Any = MISSING,
        factory: Any = MISSING,
        dependencies: DependencyMap | None = None,
        priority: int = DEFAULT_PRIORITY,
    ) -> DependencyContainer:
        return self.register(
            token,
            provider,
            abstract=abstract,
            concrete=concrete,
            factory=factory,
            lifetime=Lifetime.SCOPED,
            dependencies=dependencies,
            priority=priority,
        )

    def instance(
        self,
        token: Token | None = None,
        instance: Any = MISSING,
        *,
        abstract: Token | None = None,
        priority: int = DEFAULT_PRIORITY,
    ) -> DependencyContainer:
        return self.register(
            token,
            abstract=abstract,
            instance=instance,
            lifetime=Lifetime.SINGLETON,
            priority=priority,
        )

    def provider(
        self,
        token: Token | None = None,
        *,
        abstract: Token | None = None,
        lifetime: Lifetime | str = Lifetime.TRANSIENT,
        dependencies: DependencyMap | None = None,
        priority: int = DEFAULT_PRIORITY,
    ) -> Callable[[ProviderCallable | type[Any]], ProviderCallable | type[Any]]:
        def decorator(factory: ProviderCallable | type[Any]) -> ProviderCallable | type[Any]:
            self.register(
                token,
                abstract=abstract,
                factory=factory,
                lifetime=lifetime,
                dependencies=dependencies,
                priority=priority,
            )
            return factory

        return decorator

    def inject(
        self,
        target: Callable[..., T] | type[T] | None = None,
        *,
        dependencies: DependencyMap | None = None,
        strict: bool = False,
        **named_dependencies: Token,
    ) -> Callable[..., T] | type[T] | Callable[[Callable[..., T] | type[T]], Any]:
        return injection(
            target,
            container=self,
            dependencies=dependencies,
            strict=strict,
            **named_dependencies,
        )

    def unregister(self, token: Token) -> None:
        """Unregister a token and synchronously dispose its cached resources."""

        self._ensure_open()
        attempt, owner = self._claim_token_disposal(token)
        if not owner:
            self._wait_for_token_disposal(attempt)
            return
        error: BaseException | None = None
        try:
            self._unregister_owned(token)
        except BaseException as failure:
            error = failure
            raise
        finally:
            self._finish_token_disposal(token, attempt, error)

    def _unregister_owned(self, token: Token) -> None:
        with self._registration_lock:
            pending = self._pending_disposals.get(token)
        candidates = pending or self._token_instances(token)
        async_only = next(
            (item for item in candidates if requires_async_disposal(item)),
            None,
        )
        if async_only is not None:
            raise AsyncDependencyError(
                f"{format_token(type(async_only))} async kapanıyor; "
                "unregister_async kullan."
            )
        gates = () if pending is not None else self._retire_token(token)
        candidates = pending or self._token_instances(token)
        async_only = next(
            (item for item in candidates if requires_async_disposal(item)),
            None,
        )
        if async_only is not None:
            for gate in gates:
                gate.reactivate()
            raise AsyncDependencyError(
                f"{format_token(type(async_only))} async kapanıyor; "
                "unregister_async kullan."
            )
        claimed = (
            self._tracker.claim(self._tracker.ordered(pending))
            if pending is not None
            else self._detach_token_instances(token)
        )
        try:
            dispose_many(
                claimed,
                on_success=self._forget_disposed,
                on_failure=self._tracker.release,
            )
        except BaseException:
            self._retain_pending_disposal(token, claimed)
            raise
        else:
            with self._registration_lock:
                self._pending_disposals.pop(token, None)

    async def unregister_async(self, token: Token) -> None:
        if any(
            container_id == id(self) and operation == "shutdown" and not done.is_set()
            for container_id, operation, done in _cleanup_operations.get()
        ):
            raise DependencyResolutionError(
                "Dependency cleanup cannot unregister during container shutdown"
            )
        self._ensure_open()
        attempt, owner = self._claim_token_disposal(token, async_owner=True)
        if not owner:
            if attempt.owner_task is asyncio.current_task():
                raise DependencyResolutionError(
                    "Dependency cleanup cannot re-enter its own token disposal"
                )
            await asyncio.to_thread(attempt.done.wait)
            if attempt.error is not None:
                raise attempt.error
            return
        task = asyncio.create_task(self._run_unregister_async(token, attempt))
        attempt.owner_task = task
        with self._registration_lock:
            self._async_operations[token] = task
        task.add_done_callback(
            lambda finished: self._complete_async_operation(token, finished)
        )
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            raise

    def has(self, token: Token) -> bool:
        return self._find_provider(token) is not None

    def can_resolve(self, token: Token) -> bool:
        try:
            self._ensure_open()
            provider = self._find_resolvable_provider(token)
        except (DependencyCleanupPendingError, DependencyContainerClosedError):
            return False
        return provider is not None or (self.auto_wire and can_autowire(token))

    def registered_tokens(self) -> tuple[Token, ...]:
        with self._registration_lock:
            tokens = tuple(self._providers)
        return tuple(sorted(tokens, key=format_token))

    def create_scope(self) -> DependencyContainer:
        self._ensure_open()
        return DependencyContainer(parent=self, auto_wire=self.auto_wire)

    def resolve(self, token: Token) -> Any:
        self._ensure_open()
        return self._resolve_provider(self._get_or_autowire_provider(token))

    async def resolve_async(self, token: Token) -> Any:
        self._ensure_open()
        return await self._resolve_provider_async(self._get_or_autowire_provider(token))

    def build(
        self,
        factory: ProviderCallable | type[T],
        *,
        dependencies: DependencyMap | None = None,
    ) -> T:
        self._ensure_open()
        return self._call_factory(factory, normalize_dependencies(dependencies))

    async def build_async(
        self,
        factory: ProviderCallable | type[T],
        *,
        dependencies: DependencyMap | None = None,
    ) -> T:
        self._ensure_open()
        return await self._call_factory_async(factory, normalize_dependencies(dependencies))

    def warmup(
        self,
        tokens: Iterable[Token] | None = None,
        *,
        lifetimes: Iterable[Lifetime | str] = (Lifetime.SINGLETON,),
    ) -> None:
        for provider in self._warmup_providers(tokens, lifetimes):
            self.resolve(provider.token)

    async def warmup_async(
        self,
        tokens: Iterable[Token] | None = None,
        *,
        lifetimes: Iterable[Lifetime | str] = (Lifetime.SINGLETON,),
    ) -> None:
        for provider in self._warmup_providers(tokens, lifetimes):
            await self.resolve_async(provider.token)

    def shutdown(self) -> None:
        attempt, owner = self._claim_shutdown()
        if attempt is None:
            return
        if not owner:
            if attempt.async_owner and attempt.owner_thread_id == threading.get_ident():
                raise AsyncDependencyError(
                    "Cannot synchronously wait for async shutdown on its event-loop thread"
                )
            attempt.done.wait()
            if attempt.error is not None:
                raise attempt.error
            return
        error: BaseException | None = None
        cleanup_started = False
        try:
            for disposal in self._token_disposals():
                self._wait_for_token_disposal(disposal)
            candidates = self._shutdown_instances()
            async_only = next(
                (item for item in candidates if requires_async_disposal(item)),
                None,
            )
            if async_only is not None:
                raise AsyncDependencyError(
                    f"{format_token(type(async_only))} async kapanıyor; "
                    "shutdown_async kullan."
                )
            cleanup_started = True
            self._retire_all()
            dispose_many(
                self._detach_shutdown_instances(),
                on_success=self._forget_disposed,
                on_failure=self._tracker.release,
            )
        except BaseException as failure:
            error = failure
            raise
        finally:
            self._finish_shutdown(
                attempt,
                error,
                cleanup_started=cleanup_started,
            )

    async def shutdown_async(self) -> None:
        if any(
            container_id == id(self) and operation == "token" and not done.is_set()
            for container_id, operation, done in _cleanup_operations.get()
        ):
            raise DependencyResolutionError(
                "Dependency cleanup cannot start container shutdown during token disposal"
            )
        attempt, owner = self._claim_shutdown(async_owner=True)
        if attempt is None:
            return
        if not owner:
            await asyncio.to_thread(attempt.done.wait)
            if attempt.error is not None:
                raise attempt.error
            return
        task = asyncio.create_task(self._shutdown_async(attempt))
        attempt.owner_task = task
        await asyncio.shield(task)

    async def _run_unregister_async(
        self,
        token: Token,
        attempt: _TokenDisposalAttempt,
    ) -> None:
        marker = _cleanup_operations.set(
            (*_cleanup_operations.get(), (id(self), "token", attempt.done))
        )
        error: BaseException | None = None
        try:
            await self._unregister_async_owned(token)
        except BaseException as failure:
            error = failure
            raise
        finally:
            _cleanup_operations.reset(marker)
            self._finish_token_disposal(token, attempt, error)

    async def _unregister_async_owned(self, token: Token) -> None:
        cancelled = False
        claimed: tuple[Any, ...] = ()
        with self._registration_lock:
            pending = self._pending_disposals.get(token)
        try:
            if pending is None:
                await self._retire_token_async(token)
        except asyncio.CancelledError:
            cancelled = True
            await self._retire_token_async(token)
        try:
            claimed = (
                self._tracker.claim(self._tracker.ordered(pending))
                if pending is not None
                else self._detach_token_instances(token)
            )
            await dispose_many_async(
                claimed,
                on_success=self._forget_disposed,
                on_failure=self._tracker.release,
            )
        except asyncio.CancelledError:
            cancelled = True
            self._retain_pending_disposal(token, claimed)
        except BaseException:
            self._retain_pending_disposal(token, claimed)
            raise
        else:
            with self._registration_lock:
                self._pending_disposals.pop(token, None)
        if cancelled:
            raise asyncio.CancelledError

    async def _shutdown_async(self, attempt: _ShutdownAttempt) -> None:
        marker = _cleanup_operations.set(
            (*_cleanup_operations.get(), (id(self), "shutdown", attempt.done))
        )
        cancelled = False
        failure: BaseException | None = None
        cleanup_started = False
        try:
            disposals = self._token_disposals()
            if disposals:
                await asyncio.gather(
                    *(asyncio.to_thread(item.done.wait) for item in disposals)
                )
            cleanup_started = True
            try:
                await self._retire_all_async()
            except asyncio.CancelledError:
                cancelled = True
                await self._retire_all_async()
            try:
                await dispose_many_async(
                    self._detach_shutdown_instances(),
                    on_success=self._forget_disposed,
                    on_failure=self._tracker.release,
                )
            except asyncio.CancelledError:
                cancelled = True
            if cancelled:
                raise asyncio.CancelledError
        except BaseException as error:
            failure = error
            raise
        finally:
            _cleanup_operations.reset(marker)
            self._finish_shutdown(
                attempt,
                failure,
                cleanup_started=cleanup_started,
            )

    def _normalize_registration(
        self,
        *,
        token: Token | None,
        provider: Any,
        abstract: Token | None,
        concrete: Any,
        factory: Any,
        instance: Any,
        lifetime: Lifetime | str,
        dependencies: DependencyMap | None,
        priority: int,
    ) -> Registration:
        selected_token = choose_token(token=token, abstract=abstract)
        selected_factory = choose_factory(
            provider=provider,
            concrete=concrete,
            factory=factory,
            instance=instance,
        )
        try:
            selected_lifetime = Lifetime(lifetime)
        except ValueError as exc:
            raise DependencyResolutionError(f"Geçersiz lifetime: {lifetime!r}.") from exc
        selected_instance = MISSING

        if selected_factory is MISSING:
            if selected_token is not None and inspect.isclass(selected_token):
                selected_factory = selected_token
            else:
                raise DependencyResolutionError(
                    "Dependency kaydı için token/abstract veya provider/concrete "
                    "belirtilmeli."
                )
        if selected_token is None:
            selected_token = infer_token_from_factory(selected_factory)
        if selected_token is None:
            raise DependencyResolutionError(
                "Token belirlenemedi. abstract=... veya token parametresi ver."
            )
        if instance is not MISSING:
            selected_instance = instance
            selected_lifetime = Lifetime.SINGLETON
        elif is_direct_instance(selected_factory):
            selected_instance = selected_factory
            selected_lifetime = Lifetime.SINGLETON
        ensure_hashable_token(selected_token)
        return Registration(
            token=selected_token,
            factory=selected_factory,
            lifetime=selected_lifetime,
            dependencies=normalize_dependencies(dependencies),
            priority=normalize_priority(priority),
            instance=selected_instance,
        )

    def _reset_current_container(self) -> None:
        if self._context_tokens:
            exit_container(self._context_tokens.pop())

    def _find_provider(self, token: Token) -> Provider | None:
        with self._registration_lock:
            provider = self._providers.get(token)
        if provider is not None:
            return provider
        return self.parent._find_provider(token) if self.parent is not None else None

    def _find_resolvable_provider(self, token: Token) -> Provider | None:
        with self._registration_lock:
            if token in self._disposal_attempts or token in self._pending_disposals:
                raise DependencyCleanupPendingError(
                    f"{format_token(token)} still has pending cleanup"
                )
            provider = self._providers.get(token)
        if provider is not None:
            return provider
        if self.parent is not None:
            return self.parent._find_resolvable_provider(token)
        return None

    def _get_or_autowire_provider(self, token: Token) -> Provider:
        provider = self._find_resolvable_provider(token)
        if provider is not None:
            return provider
        if self.auto_wire and can_autowire(token):
            return Provider(token=token, factory=token, lifetime=Lifetime.TRANSIENT)
        raise DependencyNotFoundError(
            f"{format_token(token)} için kayıtlı dependency bulunamadı."
        )

    def _warmup_providers(
        self,
        tokens: Iterable[Token] | None,
        lifetimes: Iterable[Lifetime | str],
    ) -> list[Provider]:
        selected_lifetimes = {Lifetime(item) for item in lifetimes}
        selected_tokens = set(tokens) if tokens is not None else None
        providers = [
            provider
            for provider in self._providers.values()
            if provider.lifetime in selected_lifetimes
            and (selected_tokens is None or provider.token in selected_tokens)
        ]
        return sorted(providers, key=lambda item: (item.priority, format_token(item.token)))

    def _resolve_provider(self, provider: Provider) -> Any:
        if provider.has_instance:
            return provider.instance
        stack_token = enter_resolution(provider.token)
        try:
            if provider.lifetime == Lifetime.TRANSIENT:
                return self._call_factory(provider.factory, provider.dependencies)
            if provider.lifetime == Lifetime.SCOPED:
                gate = self._get_scope_initializer(provider.token)
                return self._resolve_cached_sync(
                    gate,
                    lambda: self._scope_cache.get(provider.token, MISSING),
                    lambda value: self._store_scoped(provider.token, value),
                    lambda: self._call_factory(provider.factory, provider.dependencies),
                )
            return self._resolve_cached_sync(
                provider.initialization,
                lambda: provider.singleton,
                lambda value: self._store_singleton(provider, value),
                lambda: self._call_factory(provider.factory, provider.dependencies),
            )
        finally:
            exit_resolution(stack_token)

    async def _resolve_provider_async(self, provider: Provider) -> Any:
        if provider.has_instance:
            return provider.instance
        stack_token = enter_resolution(provider.token)
        try:
            if provider.lifetime == Lifetime.TRANSIENT:
                return await self._call_factory_async(
                    provider.factory, provider.dependencies
                )
            if provider.lifetime == Lifetime.SCOPED:
                gate = self._get_scope_initializer(provider.token)
                return await self._resolve_cached_async(
                    gate,
                    lambda: self._scope_cache.get(provider.token, MISSING),
                    lambda value: self._store_scoped(provider.token, value),
                    lambda: self._call_factory_async(
                        provider.factory, provider.dependencies
                    ),
                )
            return await self._resolve_cached_async(
                provider.initialization,
                lambda: provider.singleton,
                lambda value: self._store_singleton(provider, value),
                lambda: self._call_factory_async(
                    provider.factory, provider.dependencies
                ),
            )
        finally:
            exit_resolution(stack_token)

    def _call_factory(
        self,
        factory: ProviderCallable | type[T],
        dependencies: dict[str, Token],
    ) -> T:
        if inspect.iscoroutinefunction(factory):
            raise AsyncDependencyError(
                f"{format_token(factory)} async çalışıyor; "
                "resolve_async/build_async kullan."
            )
        kwargs = resolve_missing_kwargs(
            factory,
            args=(),
            kwargs={},
            dependencies=dependencies,
            container=self,
            strict=True,
        )
        result = factory(**kwargs)
        if inspect.isawaitable(result):
            close_awaitable(result)
            raise AsyncDependencyError(
                f"{format_token(factory)} awaitable döndürdü; "
                "resolve_async/build_async kullan."
            )
        return cast(T, result)

    async def _call_factory_async(
        self,
        factory: ProviderCallable | type[T],
        dependencies: dict[str, Token],
    ) -> T:
        kwargs = await resolve_missing_kwargs_async(
            factory,
            args=(),
            kwargs={},
            dependencies=dependencies,
            container=self,
            strict=True,
        )
        result = factory(**kwargs)
        return cast(T, await result if inspect.isawaitable(result) else result)

    def _provider_owner(self, provider: Provider) -> DependencyContainer:
        container: DependencyContainer | None = self
        while container is not None:
            if container._providers.get(provider.token) is provider:
                return container
            container = container.parent
        return self

    def _token_instances(self, token: Token) -> tuple[Any, ...]:
        values: list[Any] = []
        scoped = self._scope_cache.get(token, MISSING)
        if scoped is not MISSING:
            values.append(scoped)
        provider = self._providers.get(token)
        if provider is not None:
            values.extend(cached_instances(provider))
        return tuple(values)

    def _get_scope_initializer(self, token: Token) -> InitializationGate:
        with self._scope_lock:
            return self._scope_initializers.setdefault(token, InitializationGate())

    def _token_gates(self, token: Token) -> tuple[InitializationGate, ...]:
        gates: list[InitializationGate] = []
        with self._registration_lock, self._scope_lock:
            provider = self._providers.get(token)
            if provider is not None:
                gates.append(provider.initialization)
            scoped = self._scope_initializers.get(token)
            if scoped is not None and scoped not in gates:
                gates.append(scoped)
        return tuple(gates)

    def _retire_token(self, token: Token) -> tuple[InitializationGate, ...]:
        gates = self._token_gates(token)
        for gate in gates:
            gate.retire()
        return gates

    async def _retire_token_async(self, token: Token) -> None:
        gates = self._token_gates(token)
        for gate in gates:
            gate.begin_retire()
        await asyncio.gather(*(gate.retire_async() for gate in gates))

    def _all_local_gates(self) -> tuple[InitializationGate, ...]:
        with self._registration_lock, self._scope_lock:
            return tuple(
                dict.fromkeys(
                    [
                        *(provider.initialization for provider in self._providers.values()),
                        *self._scope_initializers.values(),
                    ]
                )
            )

    def _retire_all(self) -> None:
        for gate in self._all_local_gates():
            gate.retire()

    async def _retire_all_async(self) -> None:
        gates = self._all_local_gates()
        for gate in gates:
            gate.begin_retire()
        await asyncio.gather(*(gate.retire_async() for gate in gates))

    @staticmethod
    def _resolve_cached_sync(
        gate: InitializationGate,
        read: Callable[[], Any],
        store: Callable[[Any], None],
        create: Callable[[], Any],
    ) -> Any:
        while True:
            cached = read()
            if cached is not MISSING:
                return cached
            try:
                if gate.claim():
                    break
                gate.wait()
            except InitializationRetiredError as exc:
                if "Synchronous resolution" in str(exc):
                    raise AsyncDependencyError(str(exc)) from exc
                raise DependencyResolutionError(str(exc)) from exc
        try:
            value = create()
            store(value)
            return value
        finally:
            gate.release()

    @staticmethod
    async def _resolve_cached_async(
        gate: InitializationGate,
        read: Callable[[], Any],
        store: Callable[[Any], None],
        create: Callable[[], Any],
    ) -> Any:
        while True:
            cached = read()
            if cached is not MISSING:
                return cached
            try:
                if gate.claim(asynchronous=True):
                    break
                await gate.wait_async()
            except InitializationRetiredError as exc:
                raise DependencyResolutionError(str(exc)) from exc
        try:
            value = await create()
            store(value)
            return value
        finally:
            gate.release()

    def _store_scoped(self, token: Token, value: Any) -> None:
        self._scope_cache[token] = value
        self._tracker.remember(value)

    def _store_singleton(self, provider: Provider, value: Any) -> None:
        provider.singleton = value
        self._provider_owner(provider)._tracker.remember(value)

    def _is_referenced(self, instance: Any) -> bool:
        if any(value is instance for value in self._scope_cache.values()):
            return True
        return any(
            cached is instance
            for provider in self._providers.values()
            for cached in cached_instances(provider)
        )

    def _detach_token_instances(self, token: Token) -> tuple[Any, ...]:
        with self._registration_lock:
            candidates = self._token_instances(token)
            provider = self._providers.pop(token, None)
            self._scope_cache.pop(token, MISSING)
            with self._scope_lock:
                self._scope_initializers.pop(token, None)
            if provider is not None:
                provider.instance = MISSING
                provider.singleton = MISSING
            unreferenced = tuple(
                instance for instance in candidates if not self._is_referenced(instance)
            )
            ordered = self._tracker.ordered(unreferenced)
            return self._tracker.claim(ordered)

    def _forget_disposed(self, instance: Any) -> None:
        self._tracker.forget((instance,))

    def _retain_pending_disposal(
        self,
        token: Token,
        candidates: Iterable[Any],
    ) -> None:
        owned = {id(instance): instance for instance in self._tracker.resources()}
        remaining = tuple(
            owned[id(instance)]
            for instance in candidates
            if id(instance) in owned
        )
        with self._registration_lock:
            if remaining:
                self._pending_disposals[token] = remaining
            else:
                self._pending_disposals.pop(token, None)

    def _shutdown_instances(self) -> tuple[Any, ...]:
        candidates = list(self._scope_cache.values())
        for provider in self._providers.values():
            candidates.extend(cached_instances(provider))
        candidates.extend(self._tracker.resources())
        return self._tracker.ordered(candidates)

    def _detach_shutdown_instances(self) -> tuple[Any, ...]:
        candidates = list(self._shutdown_instances())
        self._scope_cache.clear()
        with self._scope_lock:
            self._scope_initializers.clear()
        for provider in self._providers.values():
            provider.instance = MISSING
            provider.singleton = MISSING
        ordered = self._tracker.ordered(candidates)
        return self._tracker.claim(ordered)
