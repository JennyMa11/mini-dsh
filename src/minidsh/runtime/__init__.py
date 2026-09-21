"""Plugin lifecycle, service injection, and event dispatch."""

from minidsh.runtime.events import EventBus
from minidsh.runtime.plugins import Plugin, PluginError, PluginManager, PluginManifest
from minidsh.runtime.services import ServiceError, ServiceRegistry

__all__ = [
    "EventBus",
    "Plugin",
    "PluginError",
    "PluginManager",
    "PluginManifest",
    "ServiceError",
    "ServiceRegistry",
]
