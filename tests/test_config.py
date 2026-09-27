"""Layered configuration precedence, coercion, and validation tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from minidsh.config import (
    ConfigError,
    environment_overrides,
    load_config_file,
    resolve_config,
)
from minidsh.domain import TokenUsage


def _config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "minidsh.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_defaults_apply_when_no_layer_is_present() -> None:
    resolved = resolve_config()

    assert resolved.options.context_strategy == "full"
    assert resolved.options.allow_write is False
    assert resolved.options.allow_shell is False
    assert resolved.options.allowed_commands == frozenset({"git"})
    assert resolved.budget.max_steps == 50
    assert resolved.options.network.enabled is False
    assert resolved.sources == {}


def test_config_file_layer_is_read_from_the_minidsh_table(tmp_path: Path) -> None:
    path = _config(
        tmp_path,
        '[minidsh]\ncontext_strategy = "sliding"\nallow_write = true\nmax_steps = 7\n',
    )

    resolved = resolve_config(config_path=path)

    assert resolved.options.context_strategy == "sliding"
    assert resolved.options.allow_write is True
    assert resolved.budget.max_steps == 7
    assert resolved.sources["context_strategy"] == "config"
    assert resolved.sources["max_steps"] == "config"


def test_config_file_without_a_section_is_accepted(tmp_path: Path) -> None:
    path = _config(tmp_path, 'context_strategy = "summary"\n')

    assert load_config_file(path) == {"context_strategy": "summary"}


def test_absent_config_paths_yield_an_empty_mapping(tmp_path: Path) -> None:
    assert load_config_file(None) == {}
    assert load_config_file(tmp_path / "missing.toml") == {}


def test_invalid_toml_raises_config_error(tmp_path: Path) -> None:
    path = _config(tmp_path, "not = = valid toml\n")

    with pytest.raises(ConfigError, match="invalid configuration file"):
        load_config_file(path)


def test_non_table_section_is_rejected(tmp_path: Path) -> None:
    path = _config(tmp_path, 'minidsh = "not a table"\n')

    with pytest.raises(ConfigError, match="must be a table"):
        load_config_file(path)


def test_environment_overrides_are_typed() -> None:
    overrides = environment_overrides(
        {
            "MINIDSH_CONTEXT_STRATEGY": "retrieval",
            "MINIDSH_ALLOW_WRITE": "YES",
            "MINIDSH_ALLOW_SHELL": "off",
            "MINIDSH_MAX_STEPS": "3",
            "MINIDSH_MAX_TOOL_CALLS": "9",
            "MINIDSH_MAX_SECONDS": "12.5",
            "MINIDSH_TOOL_OUTPUT_LIMIT": "4096",
        }
    )

    assert overrides == {
        "context_strategy": "retrieval",
        "allow_write": True,
        "allow_shell": False,
        "max_steps": 3,
        "max_tool_calls": 9,
        "max_duration_seconds": 12.5,
        "tool_output_limit": 4096,
    }


def test_unrecognised_environment_variables_are_ignored() -> None:
    assert environment_overrides({"MINIDSH_UNKNOWN": "1", "PATH": "/bin"}) == {}


@pytest.mark.parametrize("raw", ["maybe", "", "2"])
def test_invalid_boolean_environment_value_raises(raw: str) -> None:
    with pytest.raises(ConfigError, match="must be a boolean"):
        environment_overrides({"MINIDSH_ALLOW_WRITE": raw})


@pytest.mark.parametrize(
    ("variable", "raw"),
    [("MINIDSH_MAX_STEPS", "many"), ("MINIDSH_MAX_SECONDS", "soon")],
)
def test_invalid_numeric_environment_value_raises(variable: str, raw: str) -> None:
    with pytest.raises(ConfigError, match="must be a"):
        environment_overrides({variable: raw})


def test_cli_beats_environment_beats_config_beats_default(tmp_path: Path) -> None:
    path = _config(
        tmp_path,
        '[minidsh]\ncontext_strategy = "summary"\nmax_steps = 11\ntool_output_limit = 111\n',
    )

    resolved = resolve_config(
        cli={"context_strategy": "retrieval", "tool_output_limit": 999},
        environ={
            "MINIDSH_CONTEXT_STRATEGY": "sliding",
            "MINIDSH_MAX_STEPS": "22",
            "MINIDSH_TOOL_OUTPUT_LIMIT": "222",
        },
        config_path=path,
    )

    assert resolved.options.context_strategy == "retrieval"
    assert resolved.sources["context_strategy"] == "cli"
    assert resolved.budget.max_steps == 22
    assert resolved.sources["max_steps"] == "env"
    assert resolved.options.tool_output_limit == 999
    assert resolved.sources["tool_output_limit"] == "cli"


def test_none_cli_values_do_not_shadow_lower_layers(tmp_path: Path) -> None:
    path = _config(tmp_path, '[minidsh]\ncontext_strategy = "summary"\n')

    resolved = resolve_config(cli={"context_strategy": None}, config_path=path)

    assert resolved.options.context_strategy == "summary"
    assert resolved.sources["context_strategy"] == "config"


def test_unknown_config_key_is_rejected(tmp_path: Path) -> None:
    path = _config(tmp_path, "[minidsh]\nnot_a_field = 1\n")

    with pytest.raises(ConfigError, match="invalid effective configuration"):
        resolve_config(config_path=path)


def test_out_of_range_config_value_is_rejected(tmp_path: Path) -> None:
    path = _config(tmp_path, "[minidsh]\ntool_max_retries = 99\n")

    with pytest.raises(ConfigError, match="invalid effective configuration"):
        resolve_config(config_path=path)


def test_network_rules_become_a_host_protocol_port_allowlist(tmp_path: Path) -> None:
    path = _config(
        tmp_path,
        "[minidsh]\n\n"
        "[[minidsh.network_rules]]\n"
        'host = "pypi.org"\n'
        'protocol = "tcp"\n'
        "ports = [443]\n",
    )

    resolved = resolve_config(config_path=path)

    assert resolved.options.network.enabled is True
    assert resolved.options.network.authorizes("pypi.org", protocol="tcp", port=443) is True
    assert resolved.options.network.authorizes("pypi.org", protocol="tcp", port=80) is False
    assert resolved.options.network.authorizes("pypi.org", protocol="udp", port=443) is False
    assert resolved.options.network.authorizes("evil.example", protocol="tcp", port=443) is False


def test_pricing_is_resolved_into_a_rate_card(tmp_path: Path) -> None:
    path = _config(
        tmp_path,
        "[minidsh]\ninput_usd_per_million = 3.0\noutput_usd_per_million = 15.0\n",
    )

    resolved = resolve_config(config_path=path)

    assert resolved.options.pricing.cost_usd(TokenUsage(1_000_000, 0)) == pytest.approx(3.0)
    assert resolved.options.pricing.cost_usd(TokenUsage(0, 1_000_000)) == pytest.approx(15.0)


def test_budget_layer_is_independent_of_runtime_options(tmp_path: Path) -> None:
    path = _config(
        tmp_path,
        "[minidsh]\nmax_steps = 4\nmax_tokens = 500\nmax_tool_calls = 6\nmax_seconds = 30\n",
    )

    budget = resolve_config(config_path=path).budget

    assert (budget.max_steps, budget.max_tokens) == (4, 500)
    assert (budget.max_tool_calls, budget.max_duration_seconds) == (6, 30.0)


def test_named_profile_merges_below_environment_and_cli(tmp_path: Path) -> None:
    path = _config(
        tmp_path,
        "[minidsh]\nmax_steps = 20\n"
        '[profiles.review]\ncontext_strategy = "summary"\nmax_steps = 4\n'
        'enabled_tools = ["read_file", "search"]\n',
    )
    resolved = resolve_config(
        config_path=path,
        profile="review",
        environ={"MINIDSH_MAX_STEPS": "8"},
        cli={"recent_messages": 3},
    )
    assert resolved.profile == "review"
    assert resolved.options.context_strategy == "summary"
    assert resolved.sources["context_strategy"] == "profile:review"
    assert resolved.options.enabled_tools == frozenset({"read_file", "search"})
    assert resolved.budget.max_steps == 8
    assert resolved.options.recent_messages == 3


def test_profile_selection_cli_over_environment_over_file(tmp_path: Path) -> None:
    path = _config(
        tmp_path,
        '[minidsh]\nprofile = "a"\n[profiles.a]\nmax_steps = 1\n[profiles.b]\nmax_steps = 2\n',
    )
    assert resolve_config(config_path=path).budget.max_steps == 1
    assert resolve_config(config_path=path, environ={"MINIDSH_PROFILE": "b"}).budget.max_steps == 2
    assert (
        resolve_config(
            config_path=path, profile="a", environ={"MINIDSH_PROFILE": "b"}
        ).budget.max_steps
        == 1
    )


def test_unknown_profile_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unknown configuration profile"):
        resolve_config(config_path=_config(tmp_path, "[minidsh]\n"), profile="missing")


def test_profiles_are_validated_even_if_not_selected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="invalid configuration profile"):
        resolve_config(config_path=_config(tmp_path, "[profiles.broken]\nallow_wirte = true\n"))


def test_extended_environment_options_use_json_arrays() -> None:
    resolved = resolve_config(
        environ={
            "MINIDSH_ENABLED_TOOLS": '["read_file"]',
            "MINIDSH_ALLOWED_COMMANDS": '["git", "python"]',
            "MINIDSH_DENIED_TOOLS": '["git"]',
            "MINIDSH_SLIDING_WINDOW": "7",
            "MINIDSH_RECENT_MESSAGES": "4",
            "MINIDSH_REFLECTION_ENABLED": "true",
            "MINIDSH_DETAILED_TOOL_FEEDBACK": "false",
            "MINIDSH_SYSTEM_PROMPT": "Custom instructions.",
        }
    )
    assert resolved.options.enabled_tools == frozenset({"read_file"})
    assert resolved.options.denied_tools == frozenset({"git"})
    assert resolved.options.allowed_commands == frozenset({"git", "python"})
    assert resolved.options.sliding_window == 7
    assert resolved.options.recent_messages == 4
    assert resolved.options.reflection_enabled is True
    assert resolved.options.detailed_tool_feedback is False
    assert resolved.options.system_prompt == "Custom instructions."


@pytest.mark.parametrize("raw", ["git,python", '"git"', "{}"])
def test_invalid_environment_list_is_rejected(raw: str) -> None:
    with pytest.raises(ConfigError, match="JSON array"):
        environment_overrides({"MINIDSH_ALLOWED_COMMANDS": raw})


@pytest.mark.parametrize("permission", ["fs.write", "process.exec"])
def test_extra_permissions_cannot_bypass_dedicated_write_or_shell_flags(permission: str) -> None:
    with pytest.raises(ConfigError, match="reserved permissions"):
        resolve_config(cli={"extra_permissions": [permission]})


def test_collaboration_options_are_layered_and_can_be_disabled(tmp_path: Path) -> None:
    path = _config(
        tmp_path, "[profiles.team]\nmulti_agent_enabled = true\nsubagent_max_agents = 6\n"
    )
    resolved = resolve_config(
        config_path=path,
        profile="team",
        environ={"MINIDSH_SUBAGENT_MAX_PARALLEL": "3", "MINIDSH_SUBAGENT_MAX_SECONDS": "15"},
        cli={"multi_agent_enabled": False},
    )
    assert resolved.options.multi_agent_enabled is False
    assert resolved.options.subagent_max_agents == 6
    assert resolved.options.subagent_max_parallel == 3
    assert resolved.options.subagent_max_seconds == 15


@pytest.mark.parametrize(
    "field", ["subagent_max_agents", "subagent_max_parallel", "subagent_max_tokens"]
)
def test_invalid_collaboration_limit_is_rejected(field: str) -> None:
    with pytest.raises(ConfigError, match="invalid effective configuration"):
        resolve_config(cli={field: 0})
