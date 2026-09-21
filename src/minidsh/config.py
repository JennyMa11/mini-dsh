"""Layered runtime configuration: CLI > environment > minidsh.toml > defaults."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from minidsh.composition import RuntimeOptions
from minidsh.domain import AgentBudget, ModelPricing
from minidsh.infrastructure import NetworkPolicy, NetworkRule

DEFAULT_CONFIG_NAME = "minidsh.toml"
ENV_PREFIX = "MINIDSH_"

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})

# TOML keys that are accepted as spellings of a differently named field.
_FILE_ALIASES: dict[str, str] = {"max_seconds": "max_duration_seconds"}

_ENV_FIELDS: dict[str, str] = {
    "CONTEXT_STRATEGY": "context_strategy",
    "ALLOW_WRITE": "allow_write",
    "ALLOW_SHELL": "allow_shell",
    "DENIED_TOOLS": "denied_tools",
    "SANDBOX_BACKEND": "sandbox_backend",
    "DOCKER_IMAGE": "docker_image",
    "TOOL_OUTPUT_LIMIT": "tool_output_limit",
    "ARTIFACT_OFFLOAD": "artifact_offload",
    "TOOL_MAX_RETRIES": "tool_max_retries",
    "MAX_STEPS": "max_steps",
    "MAX_TOKENS": "max_tokens",
    "MAX_TOOL_CALLS": "max_tool_calls",
    "MAX_SECONDS": "max_duration_seconds",
    "INPUT_USD_PER_MILLION": "input_usd_per_million",
    "OUTPUT_USD_PER_MILLION": "output_usd_per_million",
}

_BOOL_FIELDS = frozenset({"allow_write", "allow_shell", "artifact_offload", "reflection_enabled"})
_INT_FIELDS = frozenset(
    {"tool_output_limit", "tool_max_retries", "max_steps", "max_tokens", "max_tool_calls"}
)
_FLOAT_FIELDS = frozenset(
    {"max_duration_seconds", "input_usd_per_million", "output_usd_per_million"}
)


class ConfigError(RuntimeError):
    """Invalid configuration file or override value."""


class _NetworkRuleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = Field(min_length=1)
    protocol: str = Field(default="tcp", pattern=r"^(tcp|udp)$")
    ports: list[int] = Field(default_factory=list)


class _ConfigModel(BaseModel):
    """Validated configuration values from any single layer."""

    model_config = ConfigDict(extra="forbid")

    context_strategy: str = "full"
    allow_write: bool = False
    allow_shell: bool = False
    allowed_commands: list[str] = Field(default_factory=lambda: ["git"])
    denied_tools: list[str] = Field(default_factory=list)
    sliding_window: int = Field(default=20, ge=1)
    recent_messages: int = Field(default=12, ge=1)
    reflection_enabled: bool = False
    detailed_tool_feedback: bool = True
    sandbox_backend: str = Field(default="local", pattern=r"^(local|docker)$")
    docker_image: str = "python:3.12-slim"
    test_command: list[str] | None = None
    build_command: list[str] | None = None
    tool_output_limit: int = Field(default=20_000, ge=1)
    artifact_offload: bool = True
    tool_max_retries: int = Field(default=1, ge=0, le=5)
    max_steps: int = Field(default=50, ge=1)
    max_tokens: int = Field(default=100_000, ge=1)
    max_tool_calls: int = Field(default=100, ge=1)
    max_duration_seconds: float = Field(default=900.0, gt=0)
    input_usd_per_million: float = Field(default=0.0, ge=0)
    output_usd_per_million: float = Field(default=0.0, ge=0)
    network_rules: list[_NetworkRuleConfig] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ResolvedConfig:
    """Effective runtime options, budget, and per-field provenance."""

    options: RuntimeOptions
    budget: AgentBudget
    sources: dict[str, str] = field(default_factory=dict)


def load_config_file(path: Path | None) -> dict[str, Any]:
    """Read one TOML configuration file, or return an empty mapping when absent."""
    if path is None or not path.is_file():
        return {}
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        msg = f"invalid configuration file: {path}"
        raise ConfigError(msg) from exc
    section = raw.get("minidsh", raw)
    if not isinstance(section, dict):
        msg = "minidsh configuration section must be a table"
        raise ConfigError(msg)
    table = cast("dict[str, Any]", section)
    return {_FILE_ALIASES.get(key, key): value for key, value in table.items()}


def environment_overrides(environ: Mapping[str, str]) -> dict[str, Any]:
    """Translate recognized ``MINIDSH_*`` variables into typed overrides."""
    overrides: dict[str, Any] = {}
    for env_key, field_name in _ENV_FIELDS.items():
        raw = environ.get(f"{ENV_PREFIX}{env_key}")
        if raw is None:
            continue
        overrides[field_name] = _coerce(field_name, raw)
    return overrides


def _coerce(field_name: str, raw: str) -> Any:
    value = raw.strip()
    if field_name in _BOOL_FIELDS:
        lowered = value.lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
        msg = f"{ENV_PREFIX}{field_name.upper()} must be a boolean"
        raise ConfigError(msg)
    if field_name in _INT_FIELDS:
        try:
            return int(value)
        except ValueError as exc:
            msg = f"{ENV_PREFIX}{field_name.upper()} must be an integer"
            raise ConfigError(msg) from exc
    if field_name in _FLOAT_FIELDS:
        try:
            return float(value)
        except ValueError as exc:
            msg = f"{ENV_PREFIX}{field_name.upper()} must be a number"
            raise ConfigError(msg) from exc
    return value


def resolve_config(
    *,
    cli: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
    config_path: Path | None = None,
) -> ResolvedConfig:
    """Merge defaults, config file, environment, and CLI overrides in priority order."""
    layers: list[tuple[str, dict[str, Any]]] = [
        ("default", {}),
        ("config", load_config_file(config_path)),
        ("env", environment_overrides(environ or {})),
        ("cli", {key: value for key, value in (cli or {}).items() if value is not None}),
    ]
    merged: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for source, values in layers:
        for key, value in values.items():
            merged[key] = value
            sources[key] = source
    try:
        config = _ConfigModel.model_validate(merged)
    except ValidationError as exc:
        msg = f"invalid effective configuration: {exc}"
        raise ConfigError(msg) from exc

    network = NetworkPolicy(
        frozenset(
            NetworkRule(rule.host, rule.protocol, frozenset(rule.ports))
            for rule in config.network_rules
        )
    )
    options = RuntimeOptions(
        context_strategy=config.context_strategy,
        allow_write=config.allow_write,
        allow_shell=config.allow_shell,
        allowed_commands=frozenset(config.allowed_commands),
        denied_tools=frozenset(config.denied_tools),
        sliding_window=config.sliding_window,
        recent_messages=config.recent_messages,
        reflection_enabled=config.reflection_enabled,
        detailed_tool_feedback=config.detailed_tool_feedback,
        sandbox_backend=config.sandbox_backend,
        docker_image=config.docker_image,
        test_command=tuple(config.test_command) if config.test_command else None,
        build_command=tuple(config.build_command) if config.build_command else None,
        network=network,
        pricing=ModelPricing(config.input_usd_per_million, config.output_usd_per_million),
        tool_output_limit=config.tool_output_limit,
        artifact_offload=config.artifact_offload,
        tool_max_retries=config.tool_max_retries,
    )
    budget = AgentBudget(
        max_steps=config.max_steps,
        max_tokens=config.max_tokens,
        max_tool_calls=config.max_tool_calls,
        max_duration_seconds=config.max_duration_seconds,
    )
    return ResolvedConfig(options, budget, sources)


__all__ = [
    "DEFAULT_CONFIG_NAME",
    "ENV_PREFIX",
    "ConfigError",
    "ResolvedConfig",
    "environment_overrides",
    "load_config_file",
    "resolve_config",
]
