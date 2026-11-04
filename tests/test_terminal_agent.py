"""Product contracts: direct startup, persisted models and external tools."""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

import pytest

from minidsh.adapters import ScriptedModel
from minidsh.cli import _model_arguments, _work_config
from minidsh.composition import RuntimeOptions
from minidsh.config import resolve_config
from minidsh.configured_runtime import build_configured_runtime
from minidsh.domain import AgentBudget, ModelResponse
from minidsh.infrastructure import WorkspaceSandbox
from minidsh.runtime.catalog import PluginCatalog, PluginSpec, ValuePlugin
from minidsh.tools.builtin import ReadFileTool
from minidsh.tools.runtime import ToolBundle


def test_model_settings_follow_cli_environment_file_order(tmp_path: Path) -> None:
    path = tmp_path / "minidsh.toml"
    path.write_text('[minidsh]\nmodel_profile="ollama"\nmodel="file-model"\n')
    resolved = resolve_config(
        config_path=path, environ={"MINIDSH_MODEL": "env-model"}, cli={"model_profile": "deepseek"}
    )
    args = _model_arguments(
        argparse.Namespace(
            provider="openai-compatible",
            script=None,
            model=None,
            model_profile=None,
            endpoint=None,
            api_key_env=None,
        ),
        resolved,
    )
    assert args.model_profile == "deepseek"
    assert args.model == "env-model"


def test_explicit_script_ignores_persisted_cloud_model(tmp_path: Path) -> None:
    path = tmp_path / "minidsh.toml"
    path.write_text('[minidsh]\nmodel_profile="deepseek"\n')
    resolved = resolve_config(config_path=path)
    args = _model_arguments(
        argparse.Namespace(
            provider="scripted",
            provider_explicit=True,
            script=Path("offline.json"),
            model_profile=None,
        ),
        resolved,
    )
    assert args.model_profile is None


@pytest.mark.asyncio
async def test_explicit_extension_tool_is_resolved_after_plugin_start(tmp_path: Path) -> None:
    catalog = PluginCatalog()
    tool = ReadFileTool(WorkspaceSandbox(tmp_path))
    catalog.register("files", lambda _: ValuePlugin("files", {"extra_tools": ToolBundle((tool,))}))
    options = RuntimeOptions(
        builtin_tools_enabled=False,
        enabled_tools=frozenset({"read_file"}),
        plugin_specs=(PluginSpec("files"),),
        service_selection={"tools": "extra_tools"},
    )
    runtime = build_configured_runtime(
        workspace=tmp_path,
        model=ScriptedModel([ModelResponse("ok")]),
        options=options,
        budget=AgentBudget(),
        catalog=catalog,
    )
    try:
        assert (await runtime.agent.run("hello")).answer == "ok"
        assert [tool.name for tool in runtime.tools.definitions()] == ["read_file"]
    finally:
        assert runtime.close is not None
        await runtime.close()


@pytest.mark.asyncio
async def test_work_auto_filter_includes_plugin_tools_and_default_shell(tmp_path: Path) -> None:
    catalog = PluginCatalog()
    tool = ReadFileTool(WorkspaceSandbox(tmp_path))
    tool.definition = replace(tool.definition, name="extension_read")
    catalog.register("files", lambda _: ValuePlugin("files", {"extra_tools": ToolBundle((tool,))}))
    resolved = resolve_config(
        cli={"plugins": [{"id": "files"}], "services": {"tools": "extra_tools"}}
    )
    args = argparse.Namespace(command="work", allow_shell=None)
    resolved = _work_config(args, resolved)
    runtime = build_configured_runtime(
        workspace=tmp_path,
        model=ScriptedModel([ModelResponse("ok")]),
        options=resolved.options,
        budget=resolved.budget,
        catalog=catalog,
        include_extension_tools=True,
    )
    try:
        await runtime.agent.run("hello")
        names = {tool.name for tool in runtime.tools.definitions()}
        assert "extension_read" in names
        assert "shell" in names
    finally:
        assert runtime.close is not None
        await runtime.close()
