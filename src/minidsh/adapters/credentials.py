"""Private credential configuration, separate from model-visible runtime options."""

from __future__ import annotations

import json
import os
import re
import tomllib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from minidsh.adapters.trace import JsonlTraceWriter
from minidsh.domain import JsonValue, ToolCall, ToolDefinition, ToolStatus
from minidsh.infrastructure import WorkspaceSandbox
from minidsh.runtime import EventBus
from minidsh.tools.runtime import PermissionPolicy, ToolRegistry, ToolRuntime, schema_for

_MAX_CONFIG_BYTES = 65_536
_KEY_NAME = re.compile(r"[A-Z][A-Z0-9_]*\Z")
_IGNORE_ENTRY = "/.minidsh/credentials.toml"


class _CredentialConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    credentials: dict[str, str]


def default_credentials_path() -> Path:
    """Return the global credential file; an explicit config home must be absolute."""
    configured = os.environ.get("MINIDSH_CONFIG_HOME")
    directory = Path(configured) if configured else Path.home() / ".config" / "minidsh"
    if not directory.is_absolute():
        raise ValueError("MINIDSH_CONFIG_HOME must be an absolute directory")
    return directory / "credentials.toml"


@dataclass(frozen=True, slots=True)
class CredentialSelection:
    """Credential and safe provenance; repr never exposes its value."""

    value: str | None = field(repr=False)
    source: str


class CredentialStore:
    """Resolve environment > workspace > global keys and persist explicit user entry."""

    def __init__(self, workspace: Path, global_path: Path) -> None:
        self._sandbox = WorkspaceSandbox(workspace)
        self.global_path = global_path
        self.workspace_path = self._sandbox.root / ".minidsh" / "credentials.toml"

    def resolve(self, name: str, environ: Mapping[str, str]) -> CredentialSelection:
        """Read only the selected provider key, without exporting it to os.environ."""
        self._validate_name(name)
        if value := environ.get(name, "").strip():
            return CredentialSelection(value, "environment")
        self._check_workspace_paths()
        for path, source in (
            (self.workspace_path, "workspace config"),
            (self.global_path, "global config"),
        ):
            if value := self._read(path).get(name, "").strip():
                return CredentialSelection(value, source)
        return CredentialSelection(None, "not configured")

    async def save_workspace(self, name: str, key: str) -> Path:
        """Save a privately entered key via an isolated authorized Tool Runtime."""
        self._validate_name(name)
        if not key.strip():
            raise ValueError("credential must not be empty")
        self._check_workspace_paths(include_ignore=True)
        trace_dir = self.workspace_path.parent / "traces"
        if trace_dir.is_symlink():
            raise ValueError("credential trace directory must not be a symlink")
        registry = ToolRegistry()
        registry.register(_SaveCredentialTool(self, name, key.strip()))
        bus = EventBus()
        run_id = uuid.uuid4().hex
        unsubscribe = bus.subscribe("*", JsonlTraceWriter(trace_dir / f"{run_id}.jsonl"))
        try:
            runtime = ToolRuntime(
                registry, PermissionPolicy(frozenset({"credentials.write"})), bus, max_retries=0
            )
            result = await runtime.execute(ToolCall("save-key", "save_credentials", {}), run_id, 0)
            if result.status is not ToolStatus.SUCCEEDED:
                raise ValueError("cannot save workspace credential configuration")
        finally:
            unsubscribe()
        return self.workspace_path

    @staticmethod
    def _validate_name(name: str) -> None:
        if not _KEY_NAME.fullmatch(name):
            raise ValueError("invalid credential environment variable name")

    @staticmethod
    def _read(path: Path) -> dict[str, str]:
        try:
            if not path.exists():
                return {}
            if not path.is_file() or path.stat().st_size > _MAX_CONFIG_BYTES:
                raise ValueError("invalid credential configuration file")
            document = tomllib.loads(path.read_text(encoding="utf-8"))
            values = _CredentialConfig.model_validate(document).credentials
            for name in values:
                CredentialStore._validate_name(name)
            return values
        except (OSError, UnicodeError, ValueError):
            raise ValueError("cannot read credential configuration") from None

    def _check_workspace_paths(self, *, include_ignore: bool = False) -> None:
        paths = [self.workspace_path.parent, self.workspace_path]
        if include_ignore:
            paths.append(self._sandbox.root / ".gitignore")
        if any(path.is_symlink() for path in paths):
            raise ValueError("credential configuration paths must not be symlinks")
        self._sandbox.resolve(".minidsh/credentials.toml")

    def _save(self, name: str, key: str) -> None:
        self._check_workspace_paths(include_ignore=True)
        self.workspace_path.parent.mkdir(mode=0o700, exist_ok=True)
        values = self._read(self.workspace_path)
        values[name] = key
        content = "[credentials]\n" + "".join(
            f"{variable} = {json.dumps(value, ensure_ascii=False)}\n"
            for variable, value in sorted(values.items())
        )
        if len(content.encode("utf-8")) > _MAX_CONFIG_BYTES:
            raise ValueError("credential configuration exceeds size limit")
        ignore = self._sandbox.root / ".gitignore"
        text = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
        if _IGNORE_ENTRY not in text.splitlines():
            if text and not text.endswith("\n"):
                text += "\n"
            text += "\n# Mini-dsh private credentials\n" + _IGNORE_ENTRY + "\n"
            mode = ignore.stat().st_mode & 0o777 if ignore.exists() else 0o644
            destination = self._sandbox.atomic_write(".gitignore", text)
            destination.chmod(mode)
        # mkstemp in atomic_write creates a private 0600 file before replacement.
        self._sandbox.atomic_write(".minidsh/credentials.toml", content)


class _SaveCredentialInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _SaveCredentialTool:
    """UI-only bound operation: neither arguments nor observations carry the key."""

    input_model = _SaveCredentialInput

    def __init__(self, store: CredentialStore, name: str, key: str) -> None:
        self._store = store
        self._name = name
        self._key = key
        self.definition = ToolDefinition(
            "save_credentials",
            "Save a user-entered workspace credential.",
            schema_for(self.input_model),
            side_effects=True,
            permissions=("credentials.write",),
        )

    async def execute(self, arguments: BaseModel) -> JsonValue:
        """Persist only the credential bound by the trusted interactive controller."""
        del arguments
        self._store._save(self._name, self._key)
        return {"saved": True, "path": ".minidsh/credentials.toml"}
