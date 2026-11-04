"""The frozen arms change only their declared capability configuration."""

from dataclasses import asdict
from pathlib import Path

import pytest

from minidsh.adapters import ScriptedModel
from minidsh.adapters.file_backend import LocalFileBackend
from minidsh.benchmark.extension_matrix import (
    ExtensionGroup,
    build_extension_runtime,
    extension_options,
)
from minidsh.composition import RuntimeEnvironment
from minidsh.domain import AgentBudget
from minidsh.domain.memory import MemoryConfig
from minidsh.infrastructure import CommandPolicy, LocalCommandRunner, WorkspaceSandbox


def test_paired_arms_change_only_the_predeclared_option(tmp_path: Path) -> None:
    memory = MemoryConfig(
        "extension_v1_project",
        "extension_v1",
        str(tmp_path / "memory.sqlite3"),
        str(tmp_path),
    )
    options = {
        group: extension_options(
            group,
            test_argv=("python", "visible.py"),
            memory=memory if group is ExtensionGroup.D else None,
        )
        for group in ExtensionGroup
    }
    assert options[ExtensionGroup.BASELINE] == options[ExtensionGroup.A]
    for left, right, field in (
        (ExtensionGroup.A, ExtensionGroup.B, "skill_roots"),
        (ExtensionGroup.B, ExtensionGroup.C, "verification"),
        (ExtensionGroup.C, ExtensionGroup.D, "memory"),
    ):
        before, after = asdict(options[left]), asdict(options[right])
        assert {key for key in before if before[key] != after[key]} == {field}
    for item in options.values():
        assert item.context_strategy == "full"
        assert item.allow_write is True
        assert item.allow_shell is True
        assert item.test_command is None  # The caller registers the same 30-second fixed tool.
        assert item.multi_agent_enabled is False
    assert options[ExtensionGroup.B].skill_roots == ("skills",)
    visible = options[ExtensionGroup.C].verification
    assert visible is not None
    assert visible.argv == ("python", "visible.py")
    assert visible.timeout_seconds == 30
    assert options[ExtensionGroup.D].memory == memory


def test_memory_seed_configuration_cannot_silently_change_the_wrong_arm(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="approved_memory_seed_required"):
        extension_options(ExtensionGroup.D, test_argv=("node", "smoke.js"))
    memory = MemoryConfig(
        "extension_v1_project",
        "extension_v1",
        str(tmp_path / "memory.sqlite3"),
        str(tmp_path),
    )
    with pytest.raises(ValueError, match="memory_only_in_group_d"):
        extension_options(ExtensionGroup.C, test_argv=("node", "smoke.js"), memory=memory)


def test_every_evaluation_arm_requires_an_explicitly_isolated_execution_world(
    tmp_path: Path,
) -> None:
    runner = LocalCommandRunner(WorkspaceSandbox(tmp_path), CommandPolicy())
    environment = RuntimeEnvironment(runner, LocalFileBackend((), workspace=tmp_path))
    model = ScriptedModel([])
    for group in ExtensionGroup:
        with pytest.raises(ValueError, match="extension_evaluation_requires_isolation"):
            build_extension_runtime(
                group=group,
                workspace=tmp_path,
                model=model,
                environment=environment,
                budget=AgentBudget(),
                test_argv=("python", "visible.py"),
            )
    assert model.requests == []
    assert not (tmp_path / ".minidsh").exists()
