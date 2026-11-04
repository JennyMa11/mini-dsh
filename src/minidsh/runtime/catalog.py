"""Explicit stable-ID plugin discovery. Installed code loads only after user trust."""

from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import cast

from minidsh.domain import JsonValue
from minidsh.runtime.plugins import Plugin, PluginManifest
from minidsh.runtime.services import ServiceRegistry

type PluginFactory = Callable[[dict[str, JsonValue]], Plugin]


@dataclass(frozen=True, slots=True)
class PluginSpec:
    plugin_id: str
    config: dict[str, JsonValue] = field(default_factory=dict)


class PluginCatalog:
    def __init__(self) -> None:
        self._factories: dict[str, tuple[PluginFactory, bool]] = {}

    def register(self, identifier: str, factory: PluginFactory, *, external: bool = False) -> None:
        if not identifier or identifier in self._factories:
            raise ValueError("duplicate or empty plugin ID")
        self._factories[identifier] = (factory, external)

    def instantiate(
        self, specs: tuple[PluginSpec, ...], *, trusted: frozenset[str]
    ) -> tuple[Plugin, ...]:
        installed = {entry.name: entry for entry in entry_points(group="minidsh.plugins")}
        plugins = []
        seen: set[str] = set()
        for spec in specs:
            identifier = spec.plugin_id
            if identifier in seen:
                raise ValueError("duplicate plugin selection")
            seen.add(identifier)
            registered = self._factories.get(identifier)
            external = registered is None or registered[1]
            if external and identifier not in trusted:
                raise ValueError(f"plugin must be explicitly trusted: {identifier}")
            if registered is not None:
                factory = registered[0]
            elif identifier in installed:
                factory = cast("PluginFactory", installed[identifier].load())
            else:
                raise ValueError(f"unknown plugin: {identifier}")
            plugin = factory(spec.config)
            if plugin.manifest.plugin_id != identifier:
                raise ValueError("plugin manifest ID does not match selected ID")
            plugins.append(plugin)
        return tuple(plugins)


class ValuePlugin:
    """Mount a built-in capability through the same lifecycle as external plugins."""

    def __init__(self, identifier: str, services: dict[str, object]) -> None:
        self.manifest = PluginManifest(identifier, "1.0", provides=tuple(services))
        self._services = services

    async def start(self, services: ServiceRegistry) -> None:
        for key, value in self._services.items():
            services.register(key, value)

    async def stop(self) -> None:
        pass
