"""Translate command arguments into layered configuration and render effective settings."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from minidsh.config import DEFAULT_CONFIG_NAME, ResolvedConfig, resolve_config
from minidsh.domain import FailureCategory


def _discover_config_path(explicit: Path | None, workspace: Path) -> Path | None:
    if explicit is not None:
        return explicit
    candidate = workspace / DEFAULT_CONFIG_NAME
    return candidate if candidate.is_file() else None


def _network_rules(hosts: Sequence[str] | None) -> list[dict[str, Any]] | None:
    if hosts is None:
        return None
    rules: list[dict[str, Any]] = []
    for entry in hosts:
        host, _, port = entry.partition(":")
        rule: dict[str, Any] = {"host": host, "protocol": "tcp"}
        if port:
            rule["ports"] = [int(port)]
        rules.append(rule)
    return rules


def _cli_overrides(args: argparse.Namespace) -> dict[str, Any]:
    overrides: dict[str, Any] = {
        "model_profile": getattr(args, "model_profile", None),
        "model": getattr(args, "model", None),
        "endpoint": getattr(args, "endpoint", None),
        "api_key_env": getattr(args, "api_key_env", None),
        "context_strategy": getattr(args, "context", None),
        "streaming_enabled": getattr(args, "stream", None),
        "approval_mode": getattr(args, "approval_mode", None),
        "plugins": (
            [{"id": identifier} for identifier in args.plugin]
            if getattr(args, "plugin", None) is not None
            else None
        ),
        "context_input_budget": getattr(args, "context_input_budget", None),
        "local_first_enabled": getattr(args, "local_first", None),
        "progressive_tools_enabled": getattr(args, "progressive_tools", None),
        "normalize_tool_results": getattr(args, "normalize_tool_results", None),
        "error_recovery_enabled": getattr(args, "error_recovery", None),
        "multi_agent_enabled": getattr(args, "multi_agent", None),
        "subagent_max_agents": getattr(args, "subagent_max_agents", None),
        "subagent_max_parallel": getattr(args, "subagent_max_parallel", None),
        "subagent_max_steps": getattr(args, "subagent_max_steps", None),
        "subagent_max_tokens": getattr(args, "subagent_max_tokens", None),
        "subagent_max_tool_calls": getattr(args, "subagent_max_tool_calls", None),
        "subagent_max_seconds": getattr(args, "subagent_max_seconds", None),
        "sliding_window": getattr(args, "sliding_window", None),
        "recent_messages": getattr(args, "recent_messages", None),
        "system_prompt": getattr(args, "system_prompt", None),
        "reflection_enabled": getattr(args, "reflection_enabled", None),
        "detailed_tool_feedback": getattr(args, "detailed_tool_feedback", None),
        "artifact_offload": getattr(args, "artifact_offload", None),
        "tool_max_retries": getattr(args, "tool_max_retries", None),
        "enabled_tools": getattr(args, "enabled_tools", None),
        "builtin_tools_enabled": getattr(args, "builtin_tools", None),
        "extra_permissions": getattr(args, "extra_permission", None),
        "allow_write": getattr(args, "allow_write", None),
        "allow_shell": getattr(args, "allow_shell", None),
        "sandbox_backend": getattr(args, "sandbox_backend", None),
        "docker_image": getattr(args, "docker_image", None),
        "max_steps": getattr(args, "max_steps", None),
        "max_tokens": getattr(args, "max_tokens", None),
        "max_tool_calls": getattr(args, "max_tool_calls", None),
        "max_duration_seconds": getattr(args, "max_seconds", None),
        "tool_output_limit": getattr(args, "tool_output_limit", None),
        "input_usd_per_million": getattr(args, "input_usd_per_million", None),
        "output_usd_per_million": getattr(args, "output_usd_per_million", None),
        "cached_input_usd_per_million": getattr(args, "cached_input_usd_per_million", None),
    }
    commands = getattr(args, "allow_command", None)
    if commands is not None:
        overrides["allowed_commands"] = list(dict.fromkeys(["git", *commands]))
    denied_tools = getattr(args, "deny_tool", None)
    if denied_tools is not None:
        overrides["denied_tools"] = list(dict.fromkeys(denied_tools))
    rules = _network_rules(getattr(args, "allow_host", None))
    if rules is not None:
        overrides["network_rules"] = rules
    test_command = getattr(args, "test_command", None)
    if test_command:
        overrides["test_command"] = shlex.split(str(test_command))
    build_command = getattr(args, "build_command", None)
    if build_command:
        overrides["build_command"] = shlex.split(str(build_command))
    return overrides


def resolve_arguments(args: argparse.Namespace, workspace: Path) -> ResolvedConfig:
    """Resolve CLI, environment and file settings, then apply provider-specific defaults."""
    config_path = _discover_config_path(getattr(args, "config", None), workspace)
    resolved = resolve_config(
        cli=_cli_overrides(args),
        environ=os.environ,
        config_path=config_path,
        profile=getattr(args, "profile", None),
    )
    if getattr(args, "provider", None) == "mlx":
        defaults: dict[str, Any] = {
            "context_strategy": "budget",
            "local_first_enabled": True,
            "progressive_tools_enabled": True,
            "normalize_tool_results": True,
        }
        selected = {key: value for key, value in defaults.items() if key not in resolved.sources}
        resolved = replace(resolved, options=replace(resolved.options, **selected))
    if resolved.options.multi_agent_enabled and args.command in {"benchmark", "ablation"}:
        raise ValueError(
            "Multi-Agent is supported by run/chat/gui; "
            "benchmark and ablation remain single-agent baselines"
        )
    return resolved


def config_command(args: argparse.Namespace) -> int:
    """Print effective runtime settings and their sources without reading credentials."""
    workspace = Path(args.workspace).resolve(strict=True)
    resolved = resolve_arguments(args, workspace)
    payload = {
        "model": resolved.model_options,
        "profile": resolved.profile,
        "available_profiles": list(resolved.available_profiles),
        "budget": {
            "max_steps": resolved.budget.max_steps,
            "max_tokens": resolved.budget.max_tokens,
            "max_tool_calls": resolved.budget.max_tool_calls,
            "max_duration_seconds": resolved.budget.max_duration_seconds,
        },
        "options": {
            "context_strategy": resolved.options.context_strategy,
            "multi_agent_enabled": resolved.options.multi_agent_enabled,
            "subagent_max_agents": resolved.options.subagent_max_agents,
            "subagent_max_parallel": resolved.options.subagent_max_parallel,
            "subagent_max_steps": resolved.options.subagent_max_steps,
            "subagent_max_tokens": resolved.options.subagent_max_tokens,
            "subagent_max_tool_calls": resolved.options.subagent_max_tool_calls,
            "subagent_max_seconds": resolved.options.subagent_max_seconds,
            "sliding_window": resolved.options.sliding_window,
            "recent_messages": resolved.options.recent_messages,
            "system_prompt": resolved.options.system_prompt,
            "reflection_enabled": resolved.options.reflection_enabled,
            "detailed_tool_feedback": resolved.options.detailed_tool_feedback,
            "builtin_tools_enabled": resolved.options.builtin_tools_enabled,
            "extra_permissions": sorted(resolved.options.extra_permissions),
            "enabled_tools": (
                sorted(resolved.options.enabled_tools)
                if resolved.options.enabled_tools is not None
                else None
            ),
            "test_command": resolved.options.test_command,
            "build_command": resolved.options.build_command,
            "allow_write": resolved.options.allow_write,
            "allow_shell": resolved.options.allow_shell,
            "allowed_commands": sorted(resolved.options.allowed_commands),
            "denied_tools": sorted(resolved.options.denied_tools),
            "sandbox_backend": resolved.options.sandbox_backend,
            "docker_image": resolved.options.docker_image,
            "tool_output_limit": resolved.options.tool_output_limit,
            "artifact_offload": resolved.options.artifact_offload,
            "tool_max_retries": resolved.options.tool_max_retries,
            "network_enabled": resolved.options.network.enabled,
            "network_rules": sorted(
                f"{rule.host}/{rule.protocol}/{sorted(rule.ports)}"
                for rule in resolved.options.network.allowed
            ),
            "input_usd_per_million": resolved.options.pricing.input_usd_per_million,
            "output_usd_per_million": resolved.options.pricing.output_usd_per_million,
            "cached_input_usd_per_million": resolved.options.pricing.cached_input_usd_per_million,
        },
        "failure_categories": [category.value for category in FailureCategory],
        "sources": resolved.sources,
    }
    sys.stdout.write(f"{json.dumps(payload, indent=2, sort_keys=True)}\n")
    return 0
