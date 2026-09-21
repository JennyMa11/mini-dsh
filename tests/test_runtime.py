"""Tests for event and plugin runtime guarantees."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from minidsh.domain import Event
from minidsh.runtime import (
    EventBus,
    PluginError,
    PluginManager,
    PluginManifest,
    ServiceError,
    ServiceRegistry,
)


@dataclass
class RecordingPlugin:
    manifest: PluginManifest
    order: list[str]
    service_value: object = field(default_factory=object)
    fail: bool = False
    fail_stop: bool = False
    silent: bool = False

    async def start(self, services: ServiceRegistry) -> None:
        self.order.append(f"start:{self.manifest.plugin_id}")
        if self.fail:
            msg = "boom"
            raise RuntimeError(msg)
        if not self.silent:
            for key in self.manifest.provides:
                services.register(key, self.service_value)

    async def stop(self) -> None:
        self.order.append(f"stop:{self.manifest.plugin_id}")
        if self.fail_stop:
            msg = "stop failed"
            raise RuntimeError(msg)


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
    assert bus.errors[0].event_name == "run_started"
    assert bus.errors[0].handler_name.endswith("broken")
    assert "subscriber failure" in bus.errors[0].error


@pytest.mark.asyncio
async def test_event_bus_unsubscribe_is_effective_and_idempotent() -> None:
    bus = EventBus()
    received: list[str] = []

    async def handler(event: Event) -> None:
        received.append(event.name)

    unsubscribe = bus.subscribe("run_started", handler)
    await bus.publish(Event("run_started", "run", 0))
    unsubscribe()
    await bus.publish(Event("run_started", "run", 0))
    unsubscribe()  # unsubscribing twice must stay safe

    assert received == ["run_started"]


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


# --- plugin manifest validation ------------------------------------------------


def test_manifest_requires_identity() -> None:
    with pytest.raises(ValueError, match="plugin_id and version are required"):
        PluginManifest("", "1")
    with pytest.raises(ValueError, match="plugin_id and version are required"):
        PluginManifest("model", "")


def test_manifest_rejects_duplicate_provided_services() -> None:
    with pytest.raises(ValueError, match="duplicate provided services"):
        PluginManifest("model", "1", provides=("model", "model"))


# --- service registry ----------------------------------------------------------


def test_registry_rejects_empty_and_duplicate_keys() -> None:
    registry = ServiceRegistry()
    with pytest.raises(ServiceError, match="cannot be empty"):
        registry.register("", object())

    registry.register("model", "value")
    with pytest.raises(ServiceError, match="already registered"):
        registry.register("model", "other")


def test_registry_validates_type_on_resolution() -> None:
    registry = ServiceRegistry()
    with pytest.raises(ServiceError, match="service not found"):
        registry.get("missing", str)

    registry.register("model", "value")
    assert registry.get("model", str) == "value"
    with pytest.raises(ServiceError, match="is not int"):
        registry.get("model", int)


def test_registry_snapshot_and_unregister() -> None:
    registry = ServiceRegistry()
    registry.register("model", "value")

    assert registry.keys == frozenset({"model"})
    registry.unregister("model")
    registry.unregister("model")  # unregistering twice stays safe
    assert registry.keys == frozenset()


# --- plugin graph validation ---------------------------------------------------


def test_manager_rejects_duplicate_plugin_ids() -> None:
    manager = PluginManager()
    manager.register(RecordingPlugin(PluginManifest("model", "1"), []))

    with pytest.raises(PluginError, match="duplicate plugin id"):
        manager.register(RecordingPlugin(PluginManifest("model", "2"), []))


@pytest.mark.asyncio
async def test_manager_rejects_a_second_start() -> None:
    manager = PluginManager()
    manager.register(RecordingPlugin(PluginManifest("model", "1"), []))
    await manager.start()

    with pytest.raises(PluginError, match="already started"):
        await manager.start()


@pytest.mark.asyncio
async def test_manager_rejects_duplicate_service_providers() -> None:
    manager = PluginManager()
    manager.register(RecordingPlugin(PluginManifest("a", "1", provides=("model",)), []))
    manager.register(RecordingPlugin(PluginManifest("b", "1", provides=("model",)), []))

    with pytest.raises(PluginError, match="duplicate service provider"):
        await manager.start()


@pytest.mark.asyncio
async def test_manager_rejects_a_service_provider_that_already_exists() -> None:
    manager = PluginManager()
    manager.services.register("model", object())
    manager.register(RecordingPlugin(PluginManifest("a", "1", provides=("model",)), []))

    with pytest.raises(PluginError, match="duplicate service provider"):
        await manager.start()


@pytest.mark.asyncio
async def test_manager_rejects_dependency_cycles() -> None:
    manager = PluginManager()
    manager.register(
        RecordingPlugin(PluginManifest("a", "1", provides=("a",), requires=("b",)), [])
    )
    manager.register(
        RecordingPlugin(PluginManifest("b", "1", provides=("b",), requires=("a",)), [])
    )

    with pytest.raises(PluginError, match="dependency cycle"):
        await manager.start()


@pytest.mark.asyncio
async def test_requirements_already_registered_need_no_plugin_provider() -> None:
    manager = PluginManager()
    manager.services.register("model", object())
    order: list[str] = []
    manager.register(RecordingPlugin(PluginManifest("consumer", "1", requires=("model",)), order))

    await manager.start()

    assert order == ["start:consumer"]


# --- plugin lifecycle failure handling -----------------------------------------


@pytest.mark.asyncio
async def test_plugin_that_does_not_register_declared_services_rolls_back() -> None:
    order: list[str] = []
    manager = PluginManager()
    manager.register(RecordingPlugin(PluginManifest("a", "1", provides=("a",)), order, silent=True))

    with pytest.raises(PluginError, match="did not register"):
        await manager.start()

    assert order == ["start:a", "stop:a"]
    assert manager.services.keys == frozenset()


@pytest.mark.asyncio
async def test_startup_failure_rolls_back_already_started_plugins() -> None:
    order: list[str] = []
    manager = PluginManager()
    manager.register(RecordingPlugin(PluginManifest("a", "1", provides=("a",)), order))
    manager.register(RecordingPlugin(PluginManifest("b", "1", provides=("b",)), order, fail=True))

    with pytest.raises(RuntimeError, match="boom"):
        await manager.start()

    assert order == ["start:a", "start:b", "stop:b", "stop:a"]
    assert manager.services.keys == frozenset()


@pytest.mark.asyncio
async def test_incomplete_rollback_is_reported_as_such() -> None:
    order: list[str] = []
    manager = PluginManager()
    manager.register(
        RecordingPlugin(PluginManifest("a", "1", provides=("a",)), order, fail_stop=True)
    )
    manager.register(RecordingPlugin(PluginManifest("b", "1", provides=("b",)), order, fail=True))

    with pytest.raises(PluginError, match="rollback was incomplete"):
        await manager.start()


@pytest.mark.asyncio
async def test_shutdown_failures_are_aggregated_and_still_unregister() -> None:
    order: list[str] = []
    manager = PluginManager()
    manager.register(
        RecordingPlugin(PluginManifest("a", "1", provides=("a",)), order, fail_stop=True)
    )
    manager.register(
        RecordingPlugin(PluginManifest("b", "1", provides=("b",)), order, fail_stop=True)
    )
    await manager.start()

    with pytest.raises(PluginError, match="2 plugin\\(s\\) failed during shutdown"):
        await manager.stop()

    # Every declared service is released even though the hooks raised.
    assert manager.services.keys == frozenset()


# --- hot replacement -----------------------------------------------------------


@pytest.mark.asyncio
async def test_replacement_rejects_unknown_plugin_and_id_change() -> None:
    manager = PluginManager()
    manager.register(RecordingPlugin(PluginManifest("model", "1"), []))

    with pytest.raises(PluginError, match="plugin not found"):
        await manager.replace_at_boundary(
            "absent", RecordingPlugin(PluginManifest("absent", "1"), [])
        )
    with pytest.raises(PluginError, match="must preserve plugin_id"):
        await manager.replace_at_boundary(
            "model", RecordingPlugin(PluginManifest("other", "1"), [])
        )


@pytest.mark.asyncio
async def test_replacement_and_rollback_both_failing_is_reported() -> None:
    order: list[str] = []
    manager = PluginManager()
    original = RecordingPlugin(PluginManifest("model", "1", provides=("model",)), order)
    manager.register(original)
    await manager.start()

    # Make the restore attempt fail as well, so neither the replacement nor the
    # rollback can bring the graph back up.
    original.fail = True
    replacement = RecordingPlugin(
        PluginManifest("model", "2", provides=("model",)), order, fail=True
    )

    with pytest.raises(PluginError, match="replacement and rollback both failed"):
        await manager.replace_at_boundary("model", replacement)


@pytest.mark.asyncio
async def test_replacement_is_applied_when_the_manager_is_not_started() -> None:
    order: list[str] = []
    manager = PluginManager()
    manager.register(RecordingPlugin(PluginManifest("model", "1"), order))
    replacement = RecordingPlugin(PluginManifest("model", "2"), order)

    await manager.replace_at_boundary("model", replacement)
    await manager.start()

    assert order == ["start:model"]
