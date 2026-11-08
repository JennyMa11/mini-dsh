"""Full Loop/ToolRuntime contract with a scripted model and real Docker isolation."""

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from minidsh_rl.contracts import Turn
from minidsh_rl.model import Sample
from minidsh_rl.rollout import run_rollout
from minidsh_rl.tasks import smoke_tasks

from minidsh.domain import ModelRequest
from minidsh.infrastructure.process import CommandPolicy, LocalCommandRunner
from minidsh.infrastructure.sandbox import WorkspaceSandbox

IMAGE = (
    "python:3.12-slim-bookworm@sha256:"
    "e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337"
)


@dataclass
class Script:
    actions: list[str]
    index: int = 0

    def render(self, request: ModelRequest) -> tuple[int, ...]:
        assert request.messages
        return (2,)

    async def sample(self, prompt: tuple[int, ...], limit: int, seed: int) -> Sample:
        assert limit > 0
        assert seed >= 17
        text = self.actions[self.index]
        self.index += 1
        return Sample(Turn(prompt, (1,), (-0.2,)), text, "stop")


def action(argv: list[str]) -> str:
    return (
        "<tool_call>" + json.dumps({"name": "shell", "arguments": {"argv": argv}}) + "</tool_call>"
    )


@pytest.mark.integration
async def test_loop_edits_then_independently_verifies_and_records_tokens(tmp_path: Path) -> None:
    task = smoke_tasks()[0]
    backend = Script(
        [
            action(["cat", "implementation.py"]),
            action(
                [
                    "python3",
                    "-c",
                    (
                        "from pathlib import Path; "
                        f'Path("implementation.py").write_text({task.reference!r})'
                    ),
                ]
            ),
            action(["python3", "smoke_test.py"]),
            "Fixed and checked.",
        ]
    )
    host = LocalCommandRunner(WorkspaceSandbox(tmp_path), CommandPolicy(frozenset({"docker"})))
    result = await run_rollout(
        task,
        backend,
        policy_id="scripted",
        run_id="contract",
        seed=17,
        output=tmp_path / "run",
        host=host,
        image=IMAGE,
    )
    assert result.success
    assert result.status == "finished"
    assert result.tool_calls == 3
    assert len(list((tmp_path / "run" / "tokens").glob("turn_*.json"))) == 4
    assert (tmp_path / "run" / "verification" / "result.json").is_file()


@pytest.mark.integration
async def test_modifying_visible_tests_cannot_earn_success(tmp_path: Path) -> None:
    backend = Script(
        [
            action(
                ["python3", "-c", 'from pathlib import Path; Path("smoke_test.py").write_text("")']
            ),
            "Tests pass.",
        ]
    )
    host = LocalCommandRunner(WorkspaceSandbox(tmp_path), CommandPolicy(frozenset({"docker"})))
    result = await run_rollout(
        smoke_tasks()[0],
        backend,
        policy_id="scripted",
        run_id="tamper",
        seed=17,
        output=tmp_path / "run",
        host=host,
        image=IMAGE,
    )
    assert not result.success
    assert result.failure_type == "forbidden_workspace_change"
