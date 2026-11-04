"""User-owned plugin trust, deliberately absent from workspace runtime configuration."""

import json
import tomllib
from collections.abc import Mapping
from pathlib import Path


def trusted_plugin_ids(
    environ: Mapping[str, str], cli: tuple[str, ...], *, read_global: bool
) -> frozenset[str]:
    trusted = set(cli)
    raw = environ.get("MINIDSH_TRUSTED_PLUGINS", "[]")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("MINIDSH_TRUSTED_PLUGINS must be a JSON string array") from exc
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ValueError("MINIDSH_TRUSTED_PLUGINS must be a JSON string array")
    trusted.update(parsed)
    if read_global:
        configured = environ.get("MINIDSH_CONFIG_HOME")
        home = Path(configured) if configured else Path.home() / ".config" / "minidsh"
        if not home.is_absolute():
            raise ValueError("MINIDSH_CONFIG_HOME must be absolute")
        path = home / "plugins.toml"
        if path.exists():
            document = tomllib.loads(path.read_text())
            identifiers = document.get("trusted_plugins", [])
            if not isinstance(identifiers, list) or not all(
                isinstance(item, str) for item in identifiers
            ):
                raise ValueError("global trusted_plugins must be a string array")
            trusted.update(identifiers)
    return frozenset(trusted)
