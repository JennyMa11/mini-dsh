"""Model selection contracts use explicit fake providers and temporary credential files."""

import os
from collections.abc import Mapping
from pathlib import Path

import pytest

from minidsh.adapters import ModelProfile, ScriptedModel
from minidsh.commands.arguments import build_parser
from minidsh.commands.configuration import resolve_arguments
from minidsh.commands.models import profile_for, select_model
from minidsh.config import resolve_config
from minidsh.domain import ModelRequest, ModelResponse
from minidsh.ports import Model

pytestmark = pytest.mark.integration


def _forbid_provider(
    profile: ModelProfile, *, environ: Mapping[str, str], temperature: float, max_attempts: int
) -> Model:
    del profile, environ, temperature, max_attempts
    pytest.fail("this selection must not construct a provider")


def _forbid_credentials() -> Path:
    pytest.fail("this selection must not consult the global credential path")


def test_explicit_offline_selection_ignores_cloud_defaults_without_credentials(
    tmp_path: Path,
) -> None:
    args = build_parser().parse_args(
        ["run", "task", "--provider", "scripted", "--script", str(tmp_path / "offline.json")]
    )
    resolved = resolve_config(cli={"model_profile": "deepseek"}, environ={})
    selection = select_model(
        args,
        resolved,
        environ={"MINIDSH_MODEL_PROFILE": "deepseek"},
        provider_factory=_forbid_provider,
        credentials_path=_forbid_credentials,
    )
    assert selection.model is None
    assert selection.profile is None
    assert selection.script == tmp_path / "offline.json"
    assert not (tmp_path / "offline.json").exists()
    assert args.model_profile is None


@pytest.mark.parametrize("provider", ["scripted", "mlx"])
def test_explicit_provider_rejects_conflicting_named_profile(provider: str) -> None:
    args = build_parser().parse_args(
        ["run", "task", "--provider", provider, "--model-profile", "deepseek"]
    )
    with pytest.raises(ValueError, match="cannot be combined"):
        profile_for(args, environ={})


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["environment", "workspace", "global"])
async def test_credential_precedence_and_redaction_are_preserved(
    tmp_path: Path, source: str
) -> None:
    workspace_file = tmp_path / ".minidsh/credentials.toml"
    global_file = tmp_path / "global/credentials.toml"
    global_file.parent.mkdir()
    global_file.write_text('[credentials]\nDEEPSEEK_API_KEY="synthetic-global"\n')
    if source != "global":
        workspace_file.parent.mkdir()
        workspace_file.write_text('[credentials]\nDEEPSEEK_API_KEY="synthetic-workspace"\n')
    environment = {"DEEPSEEK_API_KEY": "synthetic-environment"} if source == "environment" else {}
    environment_before = dict(environment)
    expected = "synthetic-" + source
    args = build_parser().parse_args(
        [
            "run",
            "task",
            "--workspace",
            str(tmp_path),
            "--model-profile",
            "deepseek",
            "--allow-unpriced",
            "--temperature",
            "0.25",
            "--max-attempts",
            "2",
        ]
    )
    namespace_before = vars(args).copy()
    used: list[str] = []

    def provider(
        profile: ModelProfile, *, environ: Mapping[str, str], temperature: float, max_attempts: int
    ) -> Model:
        used.append(profile.api_key(environ))
        assert temperature == 0.25
        assert max_attempts == 2
        return ScriptedModel([ModelResponse(content="answer " + expected)])

    selection = select_model(
        args,
        resolve_config(environ={}),
        environ=environment,
        provider_factory=provider,
        credentials_path=_forbid_credentials if source == "environment" else lambda: global_file,
    )
    assert used == [expected]
    assert environment == environment_before
    assert vars(args) == namespace_before
    assert selection.model is not None
    response = await selection.model.complete(ModelRequest((), ()))
    assert response.content == "answer [REDACTED]"
    assert expected not in str(selection.describe())


def test_resolved_cli_model_and_endpoint_win_over_environment(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "run",
            "task",
            "--workspace",
            str(tmp_path),
            "--model-profile",
            "deepseek",
            "--model",
            "cli-model",
            "--endpoint",
            "https://explicit.invalid/chat",
            "--no-thinking",
            "--allow-unpriced",
        ]
    )
    environment = {
        "MINIDSH_MODEL": "env-model",
        "MINIDSH_MODEL_ENDPOINT": "https://ambient.invalid/chat",
        "DEEPSEEK_API_KEY": "synthetic-key",
    }
    resolved = resolve_config(
        cli={"model": args.model, "endpoint": args.endpoint, "model_profile": "deepseek"},
        environ=environment,
    )

    def provider(
        profile: ModelProfile, *, environ: Mapping[str, str], temperature: float, max_attempts: int
    ) -> Model:
        del temperature, max_attempts
        assert profile.resolved_endpoint(environ) == "https://explicit.invalid/chat"
        assert profile.resolved_model(environ) == "cli-model"
        assert profile.thinking is False
        return ScriptedModel([])

    selection = select_model(
        args,
        resolved,
        environ=environment,
        provider_factory=provider,
        credentials_path=_forbid_credentials,
    )
    assert selection.label == "cli-model"
    assert environment["MINIDSH_MODEL"] == "env-model"


def test_unpriced_cloud_selection_stops_before_provider_construction(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        ["run", "task", "--workspace", str(tmp_path), "--model-profile", "deepseek"]
    )
    with pytest.raises(ValueError, match="no declared rate card"):
        select_model(
            args,
            resolve_config(environ={}),
            environ={"DEEPSEEK_API_KEY": "synthetic-key"},
            provider_factory=_forbid_provider,
            credentials_path=_forbid_credentials,
        )


def test_missing_mlx_path_does_not_consult_cloud_credentials(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        ["run", "task", "--workspace", str(tmp_path), "--provider", "mlx"]
    )
    with pytest.raises(ValueError, match="mlx requires"):
        select_model(
            args,
            resolve_config(environ={}),
            environ={},
            provider_factory=_forbid_provider,
            credentials_path=_forbid_credentials,
        )


def test_command_configuration_keeps_cli_environment_file_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in tuple(os.environ):
        if key.startswith("MINIDSH_"):
            monkeypatch.delenv(key)
    (tmp_path / "minidsh.toml").write_text("[minidsh]\nmax_steps=5\nallow_write=true\n")
    monkeypatch.setenv("MINIDSH_MAX_STEPS", "7")
    args = build_parser().parse_args(["config", "--max-steps", "9", "--no-allow-write"])
    result = resolve_arguments(args, tmp_path)
    assert result.budget.max_steps == 9
    assert result.options.allow_write is False
    assert result.sources["max_steps"] == "cli"
    inherited = resolve_arguments(build_parser().parse_args(["config"]), tmp_path)
    assert inherited.budget.max_steps == 7
    assert inherited.sources["max_steps"] == "env"
