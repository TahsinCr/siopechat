"""Application-facing dependency-container composition facade."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from .context import set_default_container
from .container import DependencyContainer
from .registration import (
    DEFAULT_PRIORITY,
    DependencyMap,
    Lifetime,
    MISSING,
    T,
    Token,
)


class BaseDependencyContainer:
    """Base class for application-specific dependency registrations."""

    __slots__ = ("container",)

    def __init__(
        self,
        *,
        container: DependencyContainer | None = None,
        parent: DependencyContainer | None = None,
        set_as_default: bool = True,
        auto_wire: bool = True,
    ) -> None:
        self.container = container or DependencyContainer(
            parent=parent,
            auto_wire=auto_wire,
        )
        self.configure()
        if set_as_default:
            set_default_container(self.container)

    def configure(self) -> None:
        """Override in a subclass to register application services."""

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
        return self.container.singleton(
            token,
            provider,
            abstract=abstract,
            concrete=concrete,
            factory=factory,
            instance=instance,
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
        return self.container.transient(
            token,
            provider,
            abstract=abstract,
            concrete=concrete,
            factory=factory,
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
        return self.container.scoped(
            token,
            provider,
            abstract=abstract,
            concrete=concrete,
            factory=factory,
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
        return self.container.instance(
            token,
            instance,
            abstract=abstract,
            priority=priority,
        )

    def inject(
        self,
        target: Callable[..., T] | type[T] | None = None,
        *,
        dependencies: DependencyMap | None = None,
        strict: bool = False,
        **named_dependencies: Token,
    ) -> Callable[..., T] | type[T] | Callable[[Callable[..., T] | type[T]], Any]:
        return self.container.inject(
            target,
            dependencies=dependencies,
            strict=strict,
            **named_dependencies,
        )

    def resolve(self, token: Token) -> Any:
        return self.container.resolve(token)

    async def resolve_async(self, token: Token) -> Any:
        return await self.container.resolve_async(token)

    def warmup(
        self,
        tokens: Iterable[Token] | None = None,
        *,
        lifetimes: Iterable[Lifetime | str] = (Lifetime.SINGLETON,),
    ) -> None:
        self.container.warmup(tokens=tokens, lifetimes=lifetimes)

    async def warmup_async(
        self,
        tokens: Iterable[Token] | None = None,
        *,
        lifetimes: Iterable[Lifetime | str] = (Lifetime.SINGLETON,),
    ) -> None:
        await self.container.warmup_async(tokens=tokens, lifetimes=lifetimes)

    def create_scope(self) -> DependencyContainer:
        return self.container.create_scope()

    def unregister(self, token: Token) -> None:
        self.container.unregister(token)

    async def unregister_async(self, token: Token) -> None:
        await self.container.unregister_async(token)

    def shutdown(self) -> None:
        self.container.shutdown()

    async def shutdown_async(self) -> None:
        await self.container.shutdown_async()
