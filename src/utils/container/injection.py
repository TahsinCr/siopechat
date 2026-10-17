"""Constructor and callable injection decorators."""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast, TYPE_CHECKING

from .annotations import (
    dependency_items,
    ensure_hashable_token,
    marker_from_annotation,
    merge_dependencies,
    resolve_type_hints,
    token_from_annotation,
)
from .registration import DependencyMap, Inject, MISSING, T, Token
from .errors import DependencyNotFoundError, DependencyResolutionError
from .resolution import format_token

if TYPE_CHECKING:
    from .container import DependencyContainer


@dataclass(frozen=True, slots=True)
class InjectCandidate:
    name: str
    token: Token
    explicit: bool
    optional: bool = False


@dataclass(frozen=True, slots=True)
class InjectionPlan:
    signature: inspect.Signature
    candidates: tuple[InjectCandidate, ...]


def injection(
    target: Callable[..., T] | type[T] | None = None,
    *,
    container: DependencyContainer | None = None,
    dependencies: DependencyMap | None = None,
    strict: bool = False,
    **named_dependencies: Token,
) -> Callable[..., T] | type[T] | Callable[[Callable[..., T] | type[T]], Any]:
    """Inject missing callable or constructor parameters from a container."""

    dependency_map = merge_dependencies(dependencies, named_dependencies)

    def decorator(obj: Callable[..., T] | type[T]) -> Callable[..., T] | type[T]:
        if inspect.isclass(obj):
            return decorate_class(
                obj,
                container=container,
                dependencies=dependency_map,
                strict=strict,
            )
        if callable(obj):
            return decorate_callable(
                obj,
                container=container,
                dependencies=dependency_map,
                strict=strict,
            )
        raise TypeError("@injection yalnızca sınıf veya callable üzerine uygulanabilir.")

    return decorator if target is None else decorator(target)


def _resolver(container: DependencyContainer | None) -> DependencyContainer:
    if container is not None:
        return container
    from .context import get_current_container

    return get_current_container()


def decorate_class(
    cls: type[T],
    *,
    container: DependencyContainer | None,
    dependencies: dict[str, Token],
    strict: bool,
) -> type[T]:
    original_init = getattr(cls.__init__, "__dependency_original__", cls.__init__)
    if original_init is object.__init__:
        return cls

    @functools.wraps(original_init)
    def wrapped_init(self: Any, *args: Any, **kwargs: Any) -> None:
        injected = resolve_missing_kwargs(
            original_init,
            args=(self, *args),
            kwargs=kwargs,
            dependencies=dependencies,
            container=_resolver(container),
            strict=strict,
        )
        original_init(self, *args, **injected)

    wrapped_init.__dependency_original__ = original_init  # type: ignore[attr-defined]
    wrapped_init.__dependency_injected__ = True  # type: ignore[attr-defined]
    cls.__init__ = wrapped_init  # type: ignore[method-assign]
    return cls


def decorate_callable(
    func: Callable[..., T],
    *,
    container: DependencyContainer | None,
    dependencies: dict[str, Token],
    strict: bool,
) -> Callable[..., T]:
    original = getattr(func, "__dependency_original__", func)
    if inspect.iscoroutinefunction(original):

        @functools.wraps(original)
        async def async_wrapper(*args: Any, **kwargs: Any) -> T:
            injected = await resolve_missing_kwargs_async(
                original,
                args=args,
                kwargs=kwargs,
                dependencies=dependencies,
                container=_resolver(container),
                strict=strict,
            )
            return await original(*args, **injected)

        async_wrapper.__dependency_original__ = original  # type: ignore[attr-defined]
        async_wrapper.__dependency_injected__ = True  # type: ignore[attr-defined]
        return cast(Callable[..., T], async_wrapper)

    @functools.wraps(original)
    def sync_wrapper(*args: Any, **kwargs: Any) -> T:
        injected = resolve_missing_kwargs(
            original,
            args=args,
            kwargs=kwargs,
            dependencies=dependencies,
            container=_resolver(container),
            strict=strict,
        )
        return original(*args, **injected)

    sync_wrapper.__dependency_original__ = original  # type: ignore[attr-defined]
    sync_wrapper.__dependency_injected__ = True  # type: ignore[attr-defined]
    return sync_wrapper


def resolve_missing_kwargs(
    func: Callable[..., Any],
    *,
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
    dependencies: dict[str, Token],
    container: DependencyContainer,
    strict: bool,
) -> dict[str, Any]:
    plan = get_injection_plan(func, dependency_items(dependencies))
    bound = plan.signature.bind_partial(*args, **kwargs)
    final_kwargs = dict(kwargs)
    for candidate in plan.candidates:
        if candidate.name in bound.arguments:
            continue
        if not should_inject(candidate, container, strict):
            if candidate.optional:
                final_kwargs[candidate.name] = None
            continue
        try:
            final_kwargs[candidate.name] = container.resolve(candidate.token)
        except DependencyNotFoundError:
            if candidate.optional:
                final_kwargs[candidate.name] = None
            else:
                raise
    return final_kwargs


async def resolve_missing_kwargs_async(
    func: Callable[..., Any],
    *,
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
    dependencies: dict[str, Token],
    container: DependencyContainer,
    strict: bool,
) -> dict[str, Any]:
    plan = get_injection_plan(func, dependency_items(dependencies))
    bound = plan.signature.bind_partial(*args, **kwargs)
    final_kwargs = dict(kwargs)
    for candidate in plan.candidates:
        if candidate.name in bound.arguments:
            continue
        if not should_inject(candidate, container, strict):
            if candidate.optional:
                final_kwargs[candidate.name] = None
            continue
        try:
            final_kwargs[candidate.name] = await container.resolve_async(candidate.token)
        except DependencyNotFoundError:
            if candidate.optional:
                final_kwargs[candidate.name] = None
            else:
                raise
    return final_kwargs


def should_inject(
    candidate: InjectCandidate,
    container: DependencyContainer,
    strict: bool,
) -> bool:
    if candidate.explicit:
        return not (candidate.optional and not container.has(candidate.token))
    if container.has(candidate.token):
        return True
    if strict:
        raise DependencyNotFoundError(
            f"{candidate.name} parametresi için {format_token(candidate.token)} kayıtlı değil."
        )
    return False


@functools.lru_cache(maxsize=4096)
def get_injection_plan(
    func: Callable[..., Any],
    dependency_items_: tuple[tuple[str, Token], ...],
) -> InjectionPlan:
    signature = inspect.signature(func)
    # Local classes used only in postponed annotations are not recoverable via
    # get_type_hints(). Keep the raw annotation as a token so strict mode can
    # report its exact unresolved name instead of hiding the parameter.
    hints = resolve_type_hints(func)
    dependencies = dict(dependency_items_)
    candidates: list[InjectCandidate] = []
    used: set[str] = set()
    for name, parameter in signature.parameters.items():
        if parameter.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        candidate = make_inject_candidate(
            name=name,
            parameter=parameter,
            hints=hints,
            dependencies=dependencies,
        )
        if candidate is None:
            continue
        if parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
            raise DependencyResolutionError(
                f"Pozisyonel-only parametre inject edilemez: {name}."
            )
        if name in dependencies:
            used.add(name)
        candidates.append(candidate)
    unused = set(dependencies) - used
    if unused:
        raise DependencyResolutionError(
            "Dependency override parametresi callable imzasında yok: "
            + ", ".join(sorted(unused))
            + "."
        )
    return InjectionPlan(signature, tuple(candidates))


def make_inject_candidate(
    *,
    name: str,
    parameter: inspect.Parameter,
    hints: Mapping[str, Any],
    dependencies: Mapping[str, Token],
) -> InjectCandidate | None:
    if name in dependencies:
        return InjectCandidate(name, dependencies[name], True)
    annotation = hints.get(name, parameter.annotation)
    marker = marker_from_annotation(annotation)
    if isinstance(parameter.default, Inject):
        marker = parameter.default
    annotation_token = token_from_annotation(annotation)
    if marker is not None:
        token = marker.token
        optional = marker.optional
        if token is MISSING and annotation_token is not None:
            token = annotation_token.token
            optional = optional or annotation_token.optional
        if token is MISSING or token is inspect.Parameter.empty:
            raise DependencyResolutionError(
                f"{name} parametresi Inject(token) veya typehint gerektiriyor."
            )
        ensure_hashable_token(token)
        return InjectCandidate(name, token, True, optional)
    if annotation_token is None or parameter.default is not inspect.Parameter.empty:
        return None
    return InjectCandidate(
        name,
        annotation_token.token,
        False,
        annotation_token.optional,
    )
