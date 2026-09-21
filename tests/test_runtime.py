"""Tests for event and plugin runtime guarantees."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from minidsh.domain import Event
from minidsh.runtime import EventBus, PluginError, PluginManager, PluginManifest, ServiceRegistry


@dataclass
class RecordingPlugin:
    manifest: PluginManifest
    order: list[str]
    service_value: object = field(default_factory=object)
    fail: bool = False

    async def start(self, services: ServiceRegistry) -> None:
        self.order.append(f"start:{self.manifest.plugin_id}")
        if self.fail:
            msg = "boom"
            raise RuntimeError(msg)
        for key in self.manifest.provides:
            services.register(key, self.service_value)

    async def stop(self) -> None:
        self.order.append(f"stop:{self.manifest.plugin_id}")


@pytest.mark.asyncio
async def test_plugins_start_in_dependency_order_and_stop_in_reverse() -> None:
    order: list[str] = []
    manager = PluginManager()
    manager.register(RecordingPlugin(PluginManifest("consumer", "1", requires=("model",)), order))
    manager.register(RecordingPlugin(PluginManifest("provider", "1", provides=("model",)), order))

    await manager.start()
    await manager.stop()

    assert order == ["start:provider", "start:consumer", "stop:consumer", "stop:provider"]


@pytest.mark.asyncio
async def test_plugin_graph_rejects_missing_dependencies() -> None:
    manager = PluginManager()
    manager.register(RecordingPlugin(PluginManifest("consumer", "1", requires=("model",)), []))

    with pytest.raises(PluginError, match="missing service"):
        await manager.start()


@pytest.mark.asyncio
async def test_event_bus_isolates_failed_subscriber() -> None:
    bus = EventBus()
    received: list[str] = []

    async def broken(event: Event) -> None:
        del event
        msg = "subscriber failure"
        raise RuntimeError(msg)

    async def healthy(event: Event) -> None:
        received.append(event.name)

    bus.subscribe("*", broken)
    bus.subscribe("run_started", healthy)
    await bus.publish(Event("run_started", "run", 0))

    assert received == ["run_started"]
    assert len(bus.errors) == 1


@pytest.mark.asyncio
async def test_failed_hot_replacement_restores_previous_plugin() -> None:
    order: list[str] = []
    manager = PluginManager()
    original = RecordingPlugin(PluginManifest("model", "1", provides=("model",)), order)
    manager.register(original)
    await manager.start()
    replacement = RecordingPlugin(
        PluginManifest("model", "2", provides=("model",)), order, fail=True
    )

    with pytest.raises(PluginError, match="previous plugin was restored"):
        await manager.replace_at_boundary("model", replacement)

    assert manager.services.contains("model")
    await manager.stop()
