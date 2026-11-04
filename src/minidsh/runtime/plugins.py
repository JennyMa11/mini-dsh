"""Dependency-checked plugin registration and lifecycle management."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from minidsh.runtime.services import ServiceRegistry


class PluginError(RuntimeError):
    """Raised when plugin dependency or lifecycle rules are violated."""


@dataclass(frozen=True, slots=True)
class PluginManifest:
    """Static plugin metadata used before plugin code is started."""

    plugin_id: str
    version: str
    provides: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.plugin_id or not self.version:
            msg = "plugin_id and version are required"
            raise ValueError(msg)
        if len(set(self.provides)) != len(self.provides):
            msg = f"plugin {self.plugin_id} declares duplicate provided services"
            raise ValueError(msg)


class Plugin(Protocol):
    """Runtime extension with explicit start and stop hooks."""

    @property
    def manifest(self) -> PluginManifest:
        """Return static dependency metadata."""

    async def start(self, services: ServiceRegistry) -> None:
        """Register declared services and allocate resources."""

    async def stop(self) -> None:
        """Release resources; it must be safe after partial startup."""


class PluginManager:
    """Start plugins in dependency order and stop them in reverse order."""

    def __init__(self, services: ServiceRegistry | None = None) -> None:
        self.services = services or ServiceRegistry()
        self._plugins: dict[str, Plugin] = {}
        self._started: list[Plugin] = []
        self._cleanup_offsets: list[int] = []
        self._service_offsets: list[frozenset[str]] = []

    def register(self, plugin: Plugin) -> None:
        """Register plugin metadata without running plugin code."""
        plugin_id = plugin.manifest.plugin_id
        if plugin_id in self._plugins:
            msg = f"duplicate plugin id: {plugin_id}"
            raise PluginError(msg)
        self._plugins[plugin_id] = plugin

    async def start(self) -> None:
        """Validate the full graph, then start each plugin transactionally."""
        if self._started:
            msg = "plugin manager already started"
            raise PluginError(msg)
        ordered = self._resolve_order()
        try:
            for plugin in ordered:
                self._started.append(plugin)
                self._cleanup_offsets.append(self.services.cleanup_count)
                self._service_offsets.append(self.services.keys)
                await plugin.start(self.services)
                unexpected = (self.services.keys - self._service_offsets[-1]) - set(
                    plugin.manifest.provides
                )
                if unexpected:
                    raise PluginError("plugin registered undeclared services")
                missing = [
                    key for key in plugin.manifest.provides if not self.services.contains(key)
                ]
                if missing:
                    msg = f"plugin {plugin.manifest.plugin_id} did not register: {missing}"
                    raise PluginError(msg)
        except Exception:
            try:
                await self.stop()
            except PluginError as rollback_error:
                msg = "plugin startup failed and rollback was incomplete"
                raise PluginError(msg) from rollback_error
            raise

    async def stop(self) -> None:
        """Stop started plugins and remove their declared services."""
        errors: list[Exception] = []
        while self._started:
            plugin = self._started.pop()
            cleanup_offset = self._cleanup_offsets.pop()
            service_offset = self._service_offsets.pop()
            try:
                await plugin.stop()
            except Exception as exc:
                errors.append(exc)
            finally:
                try:
                    self.services.cleanup_from(cleanup_offset)
                except Exception as exc:
                    errors.append(exc)
                for key in self.services.keys - service_offset:
                    self.services.unregister(key)
        if errors:
            msg = f"{len(errors)} plugin(s) failed during shutdown"
            raise PluginError(msg) from errors[0]

    async def replace_at_boundary(self, plugin_id: str, replacement: Plugin) -> None:
        """Replace a plugin between agent steps by restarting the dependency graph."""
        if plugin_id not in self._plugins:
            msg = f"plugin not found: {plugin_id}"
            raise PluginError(msg)
        if replacement.manifest.plugin_id != plugin_id:
            msg = "replacement must preserve plugin_id"
            raise PluginError(msg)
        previous = self._plugins[plugin_id]
        was_started = bool(self._started)
        if was_started:
            await self.stop()
        self._plugins[plugin_id] = replacement
        if was_started:
            try:
                await self.start()
            except Exception as replacement_error:
                self._plugins[plugin_id] = previous
                try:
                    await self.start()
                except Exception as rollback_error:
                    msg = "plugin replacement and rollback both failed"
                    raise PluginError(msg) from rollback_error
                msg = "plugin replacement failed; previous plugin was restored"
                raise PluginError(msg) from replacement_error

    def _resolve_order(self) -> list[Plugin]:
        providers: dict[str, str] = {}
        for plugin in self._plugins.values():
            for service in plugin.manifest.provides:
                if service in providers or self.services.contains(service):
                    msg = f"duplicate service provider: {service}"
                    raise PluginError(msg)
                providers[service] = plugin.manifest.plugin_id

        dependencies: dict[str, set[str]] = {}
        for plugin_id, plugin in self._plugins.items():
            dependency_ids: set[str] = set()
            for service in plugin.manifest.requires:
                if self.services.contains(service):
                    continue
                provider = providers.get(service)
                if provider is None:
                    msg = f"plugin {plugin_id} requires missing service: {service}"
                    raise PluginError(msg)
                dependency_ids.add(provider)
            dependencies[plugin_id] = dependency_ids

        ordered_ids: list[str] = []
        pending = dict(dependencies)
        while pending:
            ready = sorted(key for key, deps in pending.items() if not deps)
            if not ready:
                cycle = ", ".join(sorted(pending))
                msg = f"plugin dependency cycle: {cycle}"
                raise PluginError(msg)
            for plugin_id in ready:
                ordered_ids.append(plugin_id)
                pending.pop(plugin_id)
            for deps in pending.values():
                deps.difference_update(ready)
        return [self._plugins[plugin_id] for plugin_id in ordered_ids]
