"""Annotation parsing and registration normalization helpers."""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import UnionType
from typing import Annotated, Any, Union, get_args, get_origin, get_type_hints

from .registration import DependencyMap, Inject, MISSING, NONE_TYPE, Token
from .errors import DependencyResolutionError


@dataclass(frozen=True, slots=True)
class AnnotationToken:
    token: Token
    optional: bool = False


def choose_token(*, token: Token | None, abstract: Token | None) -> Token | None:
    if token is not None and abstract is not None:
        raise DependencyResolutionError("token ve abstract aynı anda verilmemeli.")
    selected = abstract if abstract is not None else token
    if selected is not None:
        ensure_hashable_token(selected)
    return selected


def choose_factory(*, provider: Any, concrete: Any, factory: Any, instance: Any) -> Any:
    supplied = sum(
        value is not MISSING for value in (provider, concrete, factory, instance)
    )
    if supplied > 1:
        raise DependencyResolutionError(
            "provider, concrete, factory ve instance seçeneklerinden yalnızca biri verilmeli."
        )
    if instance is not MISSING:
        return instance
    if factory is not MISSING:
        return factory
    if concrete is not MISSING:
        return concrete
    if provider is not MISSING:
        return provider
    return MISSING


def infer_token_from_factory(factory: Any) -> Token | None:
    if inspect.isclass(factory):
        ensure_hashable_token(factory)
        return factory
    if is_direct_instance(factory):
        token = type(factory)
        ensure_hashable_token(token)
        return token
    if callable(factory):
        annotation = resolve_type_hints(factory, strict=True).get(
            "return", inspect.Signature.empty
        )
        annotation_token = token_from_annotation(annotation)
        if annotation_token is not None:
            ensure_hashable_token(annotation_token.token)
            return annotation_token.token
    return None


def is_direct_instance(value: Any) -> bool:
    return not inspect.isclass(value) and not callable(value)


def normalize_dependencies(dependencies: DependencyMap | None) -> dict[str, Token]:
    if dependencies is None:
        return {}
    normalized: dict[str, Token] = {}
    for name, token in dependencies.items():
        if not isinstance(name, str) or not name:
            raise DependencyResolutionError(
                "dependencies anahtarları boş olmayan string olmalı."
            )
        ensure_hashable_token(token)
        normalized[name] = token
    return normalized


def merge_dependencies(
    dependencies: DependencyMap | None,
    named_dependencies: Mapping[str, Token],
) -> dict[str, Token]:
    merged = normalize_dependencies(dependencies)
    for name, token in named_dependencies.items():
        if name in merged and merged[name] != token:
            raise DependencyResolutionError(
                f"{name} dependency override değeri iki kez farklı verildi."
            )
        ensure_hashable_token(token)
        merged[name] = token
    return merged


def dependency_items(dependencies: Mapping[str, Token]) -> tuple[tuple[str, Token], ...]:
    return tuple(sorted(dependencies.items(), key=lambda item: item[0]))


def normalize_priority(priority: int) -> int:
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise DependencyResolutionError("priority int olmalı.")
    return priority


def ensure_hashable_token(token: Any) -> None:
    try:
        hash(token)
    except TypeError as exc:
        raise DependencyResolutionError(
            f"Dependency token hashable olmalı: {token!r}."
        ) from exc


def marker_from_annotation(annotation: Any) -> Inject | None:
    if get_origin(annotation) is Annotated:
        for metadata in get_args(annotation)[1:]:
            if isinstance(metadata, Inject):
                return metadata
    return None


def token_from_annotation(annotation: Any) -> AnnotationToken | None:
    if annotation is inspect.Parameter.empty:
        return None
    if get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    annotation, optional = unwrap_optional_annotation(annotation)
    if annotation is inspect.Parameter.empty:
        return None
    ensure_hashable_token(annotation)
    return AnnotationToken(annotation, optional)


def unwrap_optional_annotation(annotation: Any) -> tuple[Any, bool]:
    origin = get_origin(annotation)
    if origin not in (Union, UnionType):
        return annotation, False
    args = get_args(annotation)
    non_none = tuple(item for item in args if item is not NONE_TYPE)
    if len(non_none) == 1 and len(non_none) != len(args):
        return non_none[0], True
    return annotation, False


@functools.lru_cache(maxsize=4096)
def resolve_type_hints(
    func: Callable[..., Any],
    *,
    strict: bool = False,
) -> Mapping[str, Any]:
    """Resolve annotations, optionally surfacing invalid forward references."""

    target = func
    if inspect.isclass(func):
        target = func.__init__
    elif not inspect.isfunction(func) and not inspect.ismethod(func):
        target = getattr(func, "__call__", func)
    try:
        return get_type_hints(target, include_extras=True)
    except Exception as error:
        if strict:
            name = getattr(func, "__qualname__", repr(func))
            raise DependencyResolutionError(
                f"{name} type annotations could not be resolved: {error}"
            ) from error
        return {}
