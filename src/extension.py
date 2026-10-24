"""Public extension API backed by the application's shared runtime."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import inspect
import json
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from actions import Action, ActionService
from language import Catalog
from providers import ProviderRegistry
from runtime import Runtime
from settings import SettingField, Settings
from tasks import TaskService
from theme import ThemeService
from views import View, Views


class ExtensionAPI:
    version = 2

    def __init__(self, settings: Settings, runtime: Runtime | None = None):
        self.runtime = runtime if runtime is not None else Runtime()
        settings.events = self.events
        providers = ProviderRegistry()
        for name, service in (
            ("settings", settings),
            ("providers", providers),
            ("tasks", TaskService(providers, events=self.events)),
            ("actions", ActionService(self.events)),
            ("views", Views(self.events)),
            ("languages", Catalog()),
            ("themes", ThemeService()),
        ):
            self.provide("application", name, service)

    @property
    def events(self):
        return self.runtime.events

    @property
    def settings(self):
        return self.get("settings")

    @property
    def providers(self):
        return self.get("providers")

    @property
    def tasks(self):
        return self.get("tasks")

    @property
    def actions(self):
        return self.get("actions")

    @property
    def views(self):
        return self.get("views")

    @property
    def languages(self):
        return self.get("languages")

    @property
    def themes(self):
        return self.get("themes")

    def provide(self, owner, name, service):
        self.runtime.provide(owner, name, service)

    def get(self, name):
        return self.runtime.get(name)

    def _withdraw_contributions(self, owner):
        for registry in (self.actions, self.views, self.languages, self.themes):
            registry.unregister(owner)
        self.settings.unregister(owner)

    def withdraw(self, owner):
        try:
            self.runtime.unregister(owner)
        finally:
            self._withdraw_contributions(owner)

    async def withdraw_async(self, owner):
        try:
            await self.runtime.unregister_async(owner)
        finally:
            self._withdraw_contributions(owner)


@dataclass(frozen=True)
class ExtensionSpec:
    id: str
    activate: Callable
    api_version: int
    requires: tuple[str, ...] = ()
    version: str = "0.1.0"


@dataclass(frozen=True)
class ExtensionStatus:
    state: str
    reason: str | None = None


@dataclass(frozen=True)
class ExtensionChanged:
    id: str
    status: ExtensionStatus


class ExtensionContext:
    def __init__(self, owner: str, api: ExtensionAPI):
        self.owner = owner
        self.api = api
        self._cleanup: list[Callable] = []

    def on_dispose(self, callback: Callable):
        self._cleanup.append(callback)

    def dispose(self):
        failed = []
        while self._cleanup:
            callback = self._cleanup.pop()
            try:
                result = callback()
                if inspect.isawaitable(result):
                    if inspect.iscoroutine(result):
                        result.close()
                    raise RuntimeError("use asynchronous extension shutdown")
            except Exception:
                failed.append(callback)
        self._cleanup.extend(reversed(failed))
        if failed:
            raise RuntimeError("extension_cleanup_failed")

    async def adispose(self):
        failed = []
        while self._cleanup:
            callback = self._cleanup.pop()
            try:
                result = callback()
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                self._cleanup.append(callback)
                self._cleanup.extend(reversed(failed))
                raise
            except Exception:
                failed.append(callback)
        self._cleanup.extend(reversed(failed))
        if failed:
            raise RuntimeError("extension_cleanup_failed")

    def provide(self, name, service):
        self.api.provide(self.owner, name, service)

    def subscribe(self, callback, **options):
        subscription = self.api.events.subscribe(callback, **options)
        self.on_dispose(subscription.cancel)
        return subscription

    def action(self, action: Action):
        self.api.actions.register(self.owner, action)

    def view(self, view: View):
        self.api.views.register(self.owner, view)

    def settings(self, *fields: SettingField):
        self.api.settings.register(self.owner, tuple(fields))

    def translations(self, locale, messages):
        self.api.languages.register(self.owner, locale, messages)

    def theme(self, theme_id, tokens, **options):
        self.api.themes.register(self.owner, theme_id, tokens, **options)


class ExtensionManager:
    def __init__(self, api: ExtensionAPI):
        self.api = api
        self._specs: dict[str, ExtensionSpec] = {}
        self._statuses: dict[str, ExtensionStatus] = {}
        self._contexts: dict[str, ExtensionContext] = {}
        self.api.runtime.on_shutdown(self.shutdown)

    def _status(self, extension_id, state, reason=None):
        status = ExtensionStatus(state, reason)
        self._statuses[extension_id] = status
        self.api.events.publish(ExtensionChanged(extension_id, status))

    def register(self, spec: ExtensionSpec):
        if not spec.id or spec.id == "application" or spec.id in self._specs:
            raise ValueError("invalid or duplicate extension ID")
        if spec.api_version != self.api.version:
            raise ValueError("incompatible extension API version")
        self._specs[spec.id] = spec
        self._status(spec.id, "registered")

    def load_directory(self, directory):
        directory = Path(directory)
        for path in sorted(directory.glob("*/plugin.py")):
            name = path.parent.name
            if name.startswith("_") or name in self._specs:
                continue
            package = (
                "siope_extension_" + hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:16]
            )
            try:
                manifest_path = path.parent / "manifest.json"
                if not manifest_path.is_file():
                    raise ValueError("extension manifest is required")
                manifest = json.loads(manifest_path.read_text())
                if not isinstance(manifest, dict):
                    raise ValueError("invalid extension manifest")
                api_version = manifest.get("api_version")
                if (
                    manifest.get("id") != name
                    or type(api_version) is not int
                    or api_version != self.api.version
                ):
                    raise ValueError("invalid extension manifest")
                requires = manifest.get("requires")
                if not isinstance(requires, list) or not all(isinstance(x, str) for x in requires):
                    raise ValueError("invalid extension dependencies")

                def activate(
                    context,
                    path=path,
                    package=package,
                    name=name,
                    api_version=api_version,
                    requires=tuple(requires),
                ):
                    parent = types.ModuleType(package)
                    parent.__path__ = [str(path.parent)]
                    sys.modules[package] = parent
                    spec = importlib.util.spec_from_file_location(package + ".plugin", path)
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[spec.name] = module
                    spec.loader.exec_module(module)
                    if not isinstance(module.SPEC, ExtensionSpec) or module.SPEC.id != name:
                        raise ValueError("extension directory and SPEC.id must match")
                    if module.SPEC.api_version != api_version:
                        raise ValueError("extension API version differs from manifest")
                    if tuple(module.SPEC.requires) != requires:
                        raise ValueError("extension dependencies differ from manifest")
                    return module.SPEC.activate(context)

                self.register(
                    ExtensionSpec(
                        name,
                        activate,
                        api_version=api_version,
                        requires=tuple(requires),
                    )
                )
            except Exception:
                self._status(name, "failed", "discovery_failed")
        for name in tuple(self._specs):
            self.activate(name)

    def statuses(self):
        return self._statuses.copy()

    def activate(self, extension_id, _chain=()):
        if extension_id in _chain:
            raise ValueError("cyclic extension dependencies")
        if self._statuses[extension_id].state == "active":
            return True
        if extension_id in self._contexts:
            self._status(extension_id, "failed", "cleanup_pending")
            return False
        if not self.api.settings.extension(extension_id).get("enabled", True):
            self._status(extension_id, "disabled")
            return False
        spec = self._specs[extension_id]
        for dependency in spec.requires:
            if dependency not in self._specs or not self.activate(
                dependency, (*_chain, extension_id)
            ):
                self._status(extension_id, "unavailable", f"dependency:{dependency}")
                return False
        context = ExtensionContext(extension_id, self.api)
        self._contexts[extension_id] = context
        try:
            cleanup = spec.activate(context)
            if inspect.iscoroutine(cleanup):
                cleanup.close()
                raise TypeError("activation is synchronous; use runtime tasks for background work")
            if cleanup is not None:
                if not callable(cleanup):
                    raise TypeError("activation must return a cleanup callable or None")
                context.on_dispose(cleanup)
        except Exception:
            try:
                self._dispose(extension_id)
            except Exception:
                pass
            self._status(extension_id, "failed", "activation_failed")
            return False
        self._status(extension_id, "active")
        return True

    def _dispose(self, extension_id):
        context = self._contexts.get(extension_id)
        try:
            if context is not None:
                context.dispose()
        finally:
            self.api.withdraw(extension_id)
        self._contexts.pop(extension_id, None)

    async def _dispose_async(self, extension_id):
        context = self._contexts.get(extension_id)
        try:
            if context is not None:
                await context.adispose()
        finally:
            await self.api.withdraw_async(extension_id)
        self._contexts.pop(extension_id, None)

    def disable(self, extension_id, _dependency=None):
        if extension_id not in self._specs:
            raise KeyError(extension_id)
        for other, spec in self._specs.items():
            if extension_id in spec.requires and self._statuses[other].state == "active":
                self.disable(other, extension_id)
        if _dependency is None:
            self.api.settings.update_extension(extension_id, {"enabled": False})
        try:
            self._dispose(extension_id)
        except Exception:
            self._status(extension_id, "failed", "deactivation_failed")
        else:
            self._status(
                extension_id,
                "unavailable" if _dependency else "disabled",
                f"dependency:{_dependency}" if _dependency else None,
            )

    async def disable_async(self, extension_id, _dependency=None, *, persist=True):
        if extension_id not in self._specs:
            raise KeyError(extension_id)
        for other, spec in self._specs.items():
            if extension_id in spec.requires and self._statuses[other].state == "active":
                await self.disable_async(other, extension_id, persist=persist)
        if persist and _dependency is None:
            self.api.settings.update_extension(extension_id, {"enabled": False})
        try:
            await self._dispose_async(extension_id)
        except Exception:
            self._status(extension_id, "failed", "deactivation_failed")
        else:
            self._status(
                extension_id,
                "unavailable" if _dependency else "disabled",
                f"dependency:{_dependency}" if _dependency else None,
            )

    def enable(self, extension_id):
        if extension_id not in self._specs:
            raise KeyError(extension_id)
        self.api.settings.update_extension(extension_id, {"enabled": True})
        activated = self.activate(extension_id)
        if activated:
            for other, status in tuple(self._statuses.items()):
                if status.state == "unavailable":
                    self.activate(other)
        return activated

    async def shutdown(self):
        for extension_id in tuple(reversed(self._contexts)):
            await self.disable_async(extension_id, persist=False)
        if self._contexts:
            raise RuntimeError("extension_cleanup_pending")
