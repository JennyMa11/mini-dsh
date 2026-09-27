"""Layered runtime configuration: CLI > environment > minidsh.toml > defaults."""

from __future__ import annotations

import json
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from minidsh.composition import MAX_SUBAGENT_PARALLEL, MAX_SUBAGENTS, RuntimeOptions
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
    "CACHED_INPUT_USD_PER_MILLION": "cached_input_usd_per_million",
    "ALLOWED_COMMANDS": "allowed_commands",
    "ENABLED_TOOLS": "enabled_tools",
    "BUILTIN_TOOLS_ENABLED": "builtin_tools_enabled",
    "EXTRA_PERMISSIONS": "extra_permissions",
    "SLIDING_WINDOW": "sliding_window",
    "RECENT_MESSAGES": "recent_messages",
    "REFLECTION_ENABLED": "reflection_enabled",
    "DETAILED_TOOL_FEEDBACK": "detailed_tool_feedback",
    "SYSTEM_PROMPT": "system_prompt",
    "TEST_COMMAND": "test_command",
    "BUILD_COMMAND": "build_command",
    "NETWORK_RULES": "network_rules",
    "MULTI_AGENT_ENABLED": "multi_agent_enabled",
    "SUBAGENT_MAX_AGENTS": "subagent_max_agents",
    "SUBAGENT_MAX_PARALLEL": "subagent_max_parallel",
    "SUBAGENT_MAX_STEPS": "subagent_max_steps",
    "SUBAGENT_MAX_TOKENS": "subagent_max_tokens",
    "SUBAGENT_MAX_TOOL_CALLS": "subagent_max_tool_calls",
    "SUBAGENT_MAX_SECONDS": "subagent_max_seconds",
}

_LIST_FIELDS = frozenset(
    {
        "allowed_commands",
        "denied_tools",
        "enabled_tools",
        "extra_permissions",
        "test_command",
        "build_command",
        "network_rules",
    }
)
_BOOL_FIELDS = frozenset(
    {
        "allow_write",
        "allow_shell",
        "artifact_offload",
        "reflection_enabled",
        "detailed_tool_feedback",
        "builtin_tools_enabled",
        "multi_agent_enabled",
    }
)
_INT_FIELDS = frozenset(
    {
        "tool_output_limit",
        "tool_max_retries",
        "max_steps",
        "max_tokens",
        "max_tool_calls",
        "sliding_window",
        "recent_messages",
        "subagent_max_agents",
        "subagent_max_parallel",
        "subagent_max_steps",
        "subagent_max_tokens",
        "subagent_max_tool_calls",
    }
)
_FLOAT_FIELDS = frozenset(
    {
        "max_duration_seconds",
        "input_usd_per_million",
        "output_usd_per_million",
        "cached_input_usd_per_million",
        "subagent_max_seconds",
    }
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
    system_prompt: str = Field(
        default="You are a coding agent. Use tools to complete the task.", min_length=1
    )
    enabled_tools: list[str] | None = None
    builtin_tools_enabled: bool = True
    extra_permissions: list[str] = Field(default_factory=list)
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
    cached_input_usd_per_million: float | None = Field(default=None, ge=0)
    network_rules: list[_NetworkRuleConfig] = Field(default_factory=list)
    multi_agent_enabled: bool = False
    subagent_max_agents: int = Field(default=4, ge=1, le=MAX_SUBAGENTS)
    subagent_max_parallel: int = Field(default=2, ge=1, le=MAX_SUBAGENT_PARALLEL)
    subagent_max_steps: int = Field(default=8, ge=1)
    subagent_max_tokens: int = Field(default=10_000, ge=1)
    subagent_max_tool_calls: int = Field(default=16, ge=1)
    subagent_max_seconds: float = Field(default=60.0, gt=0, allow_inf_nan=False)

    @field_validator("extra_permissions")
    @classmethod
    def _validate_extra_permissions(cls, values: list[str]) -> list[str]:
        # Reuse the same capability validation for file, environment, and API callers.
        RuntimeOptions(extra_permissions=frozenset(values))
        return values


@dataclass(frozen=True, slots=True)
class ResolvedConfig:
    """Effective runtime options, budget, and per-field provenance."""

    options: RuntimeOptions
    budget: AgentBudget
    sources: dict[str, str] = field(default_factory=dict)
    profile: str | None = None
    available_profiles: tuple[str, ...] = ()


def load_config_file(path: Path | None) -> dict[str, Any]:
    """Read one TOML configuration file, or return an empty mapping when absent."""
    raw = _read_document(path)
    return _file_values(raw)


def _file_values(raw: dict[str, Any]) -> dict[str, Any]:
    section = raw.get("minidsh", {key: value for key, value in raw.items() if key != "profiles"})
    if not isinstance(section, dict):
        msg = "minidsh configuration section must be a table"
        raise ConfigError(msg)
    table = cast("dict[str, Any]", section)
    return {_FILE_ALIASES.get(key, key): value for key, value in table.items()}


def _read_document(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        msg = f"invalid configuration file: {path}"
        raise ConfigError(msg) from exc
    return raw


def _profiles(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    profiles = raw.get("profiles", {})
    if not isinstance(profiles, dict):
        raise ConfigError("profiles must be a table")
    validated: dict[str, dict[str, Any]] = {}
    for name, values in profiles.items():
        if not isinstance(values, dict):
            raise ConfigError(f"invalid configuration profile: {name}")
        table = cast("dict[str, Any]", values)
        normalized = {_FILE_ALIASES.get(key, key): value for key, value in table.items()}
        try:
            _ConfigModel.model_validate(normalized)
        except ValidationError as exc:
            raise ConfigError(f"invalid configuration profile: {name}") from exc
        validated[name] = normalized
    return validated


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
    if field_name in _LIST_FIELDS:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{ENV_PREFIX}{field_name.upper()} must be a JSON array") from exc
        if not isinstance(parsed, list):
            raise ConfigError(f"{ENV_PREFIX}{field_name.upper()} must be a JSON array")
        return parsed
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
    profile: str | None = None,
) -> ResolvedConfig:
    """Merge defaults, config file, environment, and CLI overrides in priority order."""
    raw = _read_document(config_path)
    file_values = _file_values(raw)
    profiles = _profiles(raw)
    selected = (
        profile
        if profile is not None
        else (environ or {}).get("MINIDSH_PROFILE", file_values.get("profile"))
    )
    file_values.pop("profile", None)
    if selected is not None and (not isinstance(selected, str) or selected not in profiles):
        raise ConfigError(f"unknown configuration profile: {selected}")
    layers: list[tuple[str, dict[str, Any]]] = [
        ("default", {}),
        ("config", file_values),
        (f"profile:{selected}", profiles[selected] if selected is not None else {}),
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
        multi_agent_enabled=config.multi_agent_enabled,
        subagent_max_agents=config.subagent_max_agents,
        subagent_max_parallel=config.subagent_max_parallel,
        subagent_max_steps=config.subagent_max_steps,
        subagent_max_tokens=config.subagent_max_tokens,
        subagent_max_tool_calls=config.subagent_max_tool_calls,
        subagent_max_seconds=config.subagent_max_seconds,
        system_prompt=config.system_prompt,
        enabled_tools=frozenset(config.enabled_tools) if config.enabled_tools is not None else None,
        builtin_tools_enabled=config.builtin_tools_enabled,
        extra_permissions=frozenset(config.extra_permissions),
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
        pricing=ModelPricing(
            config.input_usd_per_million,
            config.output_usd_per_million,
            config.cached_input_usd_per_million,
        ),
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
    return ResolvedConfig(options, budget, sources, selected, tuple(sorted(profiles)))


__all__ = [
    "DEFAULT_CONFIG_NAME",
    "ENV_PREFIX",
    "ConfigError",
    "ResolvedConfig",
    "environment_overrides",
    "load_config_file",
    "resolve_config",
]
