"""Public dependency injection API."""

# Python 3.10 unittest discovery probes source directories as top-level
# packages. The supported package is <parent>.core.dependency, so avoid eager
# imports only for that probe.
if __package__ == "dependency":
    __all__: list[str] = []
else:
    from .container import DependencyContainer
    from .facade import BaseDependencyContainer
    from .context import (
        get_current_container,
        get_default_container,
        set_default_container,
    )
    from .registration import (
        DEFAULT_PRIORITY,
        DependencyMap,
        Inject,
        Lifetime,
        Token,
    )
    from .errors import (
        AsyncDependencyError,
        CircularDependencyError,
        DependencyCleanupPendingError,
        DependencyContainerClosedError,
        DependencyError,
        DependencyNotFoundError,
        DependencyResolutionError,
    )
    from .injection import injection

    # Keep the long-standing public module identity stable for introspection and
    # pickle compatibility while implementation lives in focused submodules.
    for _public_object in (
        AsyncDependencyError,
        BaseDependencyContainer,
        CircularDependencyError,
        DependencyCleanupPendingError,
        DependencyContainerClosedError,
        DependencyContainer,
        DependencyError,
        DependencyNotFoundError,
        DependencyResolutionError,
        Inject,
        Lifetime,
        get_current_container,
        get_default_container,
        injection,
        set_default_container,
    ):
        _public_object.__module__ = __name__
    del _public_object

    __all__ = [
        "AsyncDependencyError",
        "BaseDependencyContainer",
        "CircularDependencyError",
        "DEFAULT_PRIORITY",
        "DependencyContainer",
        "DependencyCleanupPendingError",
        "DependencyContainerClosedError",
        "DependencyError",
        "DependencyMap",
        "DependencyNotFoundError",
        "DependencyResolutionError",
        "Inject",
        "Lifetime",
        "Token",
        "get_current_container",
        "get_default_container",
        "injection",
        "set_default_container",
    ]
