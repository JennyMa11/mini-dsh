"""Async file capability shared by tools and controlled context reads."""

from typing import Protocol

from minidsh.domain import JsonValue


class FileBackend(Protocol):
    async def invoke(self, operation: str, arguments: dict[str, JsonValue]) -> JsonValue: ...
    async def prepare(self, operation: str, arguments: dict[str, JsonValue]) -> tuple[str, str]: ...
