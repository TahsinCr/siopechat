"""Dependency container exception hierarchy."""

from __future__ import annotations


class DependencyError(Exception):
    """Base dependency-container error."""


class DependencyNotFoundError(DependencyError):
    """Raised when no provider exists for a token."""


class DependencyResolutionError(DependencyError):
    """Raised when a dependency cannot be produced deterministically."""


class CircularDependencyError(DependencyResolutionError):
    """Raised for dependency cycles such as A -> B -> A."""


class AsyncDependencyError(DependencyResolutionError):
    """Raised when an async resource is used through the synchronous API."""


class DependencyCleanupPendingError(DependencyError):
    """Raised when a token still owns a resource that could not be closed."""


class DependencyContainerClosedError(DependencyError):
    """Raised when an operation requires a container that is closing or closed."""
