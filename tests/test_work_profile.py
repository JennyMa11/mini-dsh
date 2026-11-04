"""Contracts for the explicit all-capabilities development launcher."""

from pathlib import Path

import pytest

from minidsh.adapters import ScriptedModel
from minidsh.composition import build_local_runtime
from minidsh.config import resolve_config
from minidsh.domain import ModelResponse
from minidsh.work_profile import TOOLS, full_work_config


def test_full_work_profile_enables_tools_and_extensions(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = tmp_path / "work.toml"
    config.write_text(full_work_config(workspace, tmp_path / "private"))
    options = resolve_config(config_path=config, environ={}).options
    assert options.allow_shell
    assert options.allow_write
    assert options.approval_mode == "ask"
    assert options.sandbox_backend == "docker"
    assert options.enabled_tools is not None
    assert {
        "read_file",
        "write_file",
        "patch_file",
        "search",
        "list_files",
        "search_code",
        "find_symbol",
        "git",
        "shell",
        "test",
        "build",
        "load_skill",
        "propose_memory",
    } == options.enabled_tools
    assert options.skill_roots == ("examples/skills",)
    assert options.verification is not None
    assert options.verification.argv == options.test_command
    assert options.memory is not None
    assert options.memory.project_id == "mini_dsh"
    assert Path(options.memory.database).parent == tmp_path / "private"
    assert not options.memory.evidence_grants
    assert not options.network.enabled
    assert not options.multi_agent_enabled
    assert not (tmp_path / "private").exists()


def test_full_work_profile_refuses_memory_inside_workspace(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="outside"):
        full_work_config(tmp_path, tmp_path / "private")


def test_full_work_profile_rejects_memory_symlink_into_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(workspace, target_is_directory=True)
    with pytest.raises(ValueError, match="outside"):
        full_work_config(workspace, alias / "private")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_full_work_runtime_exposes_only_actor_tools(tmp_path: Path) -> None:
    workspace, private = tmp_path / "workspace", tmp_path / "private"
    workspace.mkdir()
    private.mkdir(mode=0o700)
    config = tmp_path / "work.toml"
    config.write_text(full_work_config(workspace, private))
    runtime = build_local_runtime(
        workspace=workspace,
        model=ScriptedModel([ModelResponse("done")]),
        options=resolve_config(config_path=config, environ={}).options,
    )
    try:
        assert {tool.name for tool in runtime.tools.definitions()} == set(TOOLS)
        assert runtime.memory_maintenance is not None
    finally:
        assert runtime.close is not None
        await runtime.close()
