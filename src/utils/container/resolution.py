"""Resolution-stack tracking and callable helpers."""

from __future__ import annotations

import contextvars
import inspect
from typing import Any

from .registration import (
    Token,
    begin_resolution_owner,
    end_resolution_owner,
)
from .errors import CircularDependencyError


_resolution_stack: contextvars.ContextVar[tuple[Token, ...]] = contextvars.ContextVar(
    "dependency_resolution_stack",
    default=(),
)


ResolutionTokens = tuple[
    contextvars.Token[tuple[Token, ...]],
    contextvars.Token[object | None] | None,
]


def enter_resolution(token: Token) -> ResolutionTokens:
    stack = _resolution_stack.get()
    if token in stack:
        cycle = " -> ".join(format_token(item) for item in (*stack, token))
        raise CircularDependencyError(f"Döngüsel dependency tespit edildi: {cycle}")
    owner_token = begin_resolution_owner()
    return _resolution_stack.set((*stack, token)), owner_token


def exit_resolution(tokens: ResolutionTokens) -> None:
    stack_token, owner_token = tokens
    _resolution_stack.reset(stack_token)
    end_resolution_owner(owner_token)


def can_autowire(token: Any) -> bool:
    return (
        inspect.isclass(token)
        and token.__module__ != "builtins"
        and not inspect.isabstract(token)
    )


async def maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def close_awaitable(value: Any) -> None:
    close = getattr(value, "close", None)
    if callable(close):
        close()


def format_token(token: Any) -> str:
    if inspect.isclass(token):
        module = getattr(token, "__module__", "")
        name = getattr(token, "__qualname__", token.__name__)
        if module and module != "builtins":
            return f"{module}.{name}"
        return name
    name = getattr(token, "__qualname__", None) or getattr(token, "__name__", None)
    return str(name) if name else repr(token)
