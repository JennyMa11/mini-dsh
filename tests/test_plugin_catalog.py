"""Explicit plugin trust and lifecycle contracts."""

import pytest

from minidsh.domain import JsonValue
from minidsh.runtime.catalog import PluginCatalog, PluginSpec
from minidsh.runtime.plugins import PluginManifest
from minidsh.runtime.services import ServiceRegistry


class _Plugin:
    manifest = PluginManifest("custom", "1", provides=("value",))

    async def start(self, services: ServiceRegistry) -> None:
        services.register("value", 42)

    async def stop(self) -> None:
        pass


def test_untrusted_factory_is_never_loaded() -> None:
    catalog = PluginCatalog()
    called = []

    def factory(config: dict[str, JsonValue]) -> _Plugin:
        called.append(config)
        return _Plugin()

    catalog.register("custom", factory, external=True)
    with pytest.raises(ValueError, match="trusted"):
        catalog.instantiate((PluginSpec("custom"),), trusted=frozenset())
    assert not called


def test_explicitly_trusted_factory_and_duplicate_ids() -> None:
    catalog = PluginCatalog()
    catalog.register("custom", lambda _: _Plugin(), external=True)
    plugin = catalog.instantiate((PluginSpec("custom"),), trusted=frozenset({"custom"}))[0]
    assert plugin.manifest.plugin_id == "custom"
    with pytest.raises(ValueError, match="duplicate"):
        catalog.instantiate(
            (PluginSpec("custom"), PluginSpec("custom")), trusted=frozenset({"custom"})
        )
