"""Full Loop and permission pipeline, with deterministic model and real isolation."""

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from minidsh_rl.contracts import Turn
from minidsh_rl.minirepo_rollout import run_rollout
from minidsh_rl.minirepo_tasks import development_tasks
from minidsh_rl.minirepo_verification import IMAGE
from minidsh_rl.model import Sample

from minidsh.domain import ModelRequest
from minidsh.infrastructure.containers import DockerHost


@dataclass
class Script:
    actions: list[str]
    index: int = 0
    limits: list[int] = field(default_factory=list)
    requests: list[ModelRequest] = field(default_factory=list)

    def render(self, request: ModelRequest) -> tuple[int, ...]:
        assert request.messages
        self.requests.append(request)
        return (1, 2)

    async def sample(self, prompt: tuple[int, ...], limit: int, seed: int) -> Sample:
        assert limit > 0
        assert seed >= 17
        self.limits.append(limit)
        text = self.actions[self.index]
        self.index += 1
        return Sample(Turn(prompt, (3,), (-0.1,)), text, "stop")


def patch(edits: list[dict[str, str]]) -> str:
    return (
        "<tool_call>"
        + json.dumps({"name": "apply_patch", "arguments": {"edits": edits}})
        + "</tool_call>"
    )


@pytest.mark.integration
@pytest.mark.parametrize("mode", ["single", "interactive"])
@pytest.mark.parametrize("cap", [512, 1024])
async def test_mode_preserves_real_calls_and_independent_verdict(
    tmp_path: Path, mode: str, cap: int
) -> None:
    from typing import Literal, cast  # noqa: PLC0415

    backend = Script(
        [
            patch(
                [
                    {
                        "path": "orders.py",
                        "old": "totals[sku] = quantity",
                        "new": "totals[sku] = totals.get(sku, 0) + quantity",
                    },
                    {
                        "path": "reservation.py",
                        "old": "remaining = stock",
                        "new": "remaining = dict(stock)",
                    },
                ]
            ),
            "Fixed.",
        ]
    )
    output = tmp_path / mode
    result = await run_rollout(
        development_tasks()[0],
        backend,
        policy_id="scripted",
        run_id=mode,
        seed_value=17,
        mode=cast(Literal["single", "interactive"], mode),
        output=output,
        host=DockerHost(),
        image=IMAGE,
        max_output_per_call=cap,
    )
    assert result["success"] is True
    assert result["patches"] == 1
    assert backend.limits[0] == cap
    assert result["sampled_calls"] == (1 if mode == "single" else 2)
    assert result["synthetic_stop"] is (mode == "single")
    assert len(list((output / "tokens").glob("turn_*.json"))) == result["sampled_calls"]
    assert (output / "verification/result.json").exists()


@pytest.mark.integration
@pytest.mark.parametrize("variant", ["original", "tool_example"])
async def test_failed_feedback_revision_is_visible_and_recorded(
    tmp_path: Path, variant: str
) -> None:
    from typing import cast  # noqa: PLC0415

    from minidsh_rl.minirepo_prompts import (  # noqa: PLC0415
        TOOL_EXAMPLE,
        PromptVariant,
    )

    backend = Script(
        [
            patch(
                [
                    {
                        "path": "orders.py",
                        "old": "totals[sku] = quantity",
                        "new": "totals[sku] = totals.get(sku, 0) + quantity",
                    }
                ]
            ),
            '<tool_call>{"name":"run_tests","arguments":{}}</tool_call>',
            patch(
                [
                    {
                        "path": "reservation.py",
                        "old": "remaining = stock",
                        "new": "remaining = dict(stock)",
                    }
                ]
            ),
            '<tool_call>{"name":"run_tests","arguments":{}}</tool_call>',
            "Fixed.",
        ]
    )
    result = await run_rollout(
        development_tasks()[0],
        backend,
        policy_id="scripted",
        run_id=variant,
        seed_value=17,
        mode="interactive",
        output=tmp_path / variant,
        host=DockerHost(),
        image=IMAGE,
        max_output_per_call=1024,
        prompt_variant=cast(PromptVariant, variant),
    )
    assert result["success"] is True
    assert result["patches"] == 2
    assert result["sampled_calls"] == 5
    assert result["synthetic_stop"] is False
    assert result["prompt_variant"] == variant
    assert (TOOL_EXAMPLE in backend.requests[0].messages[0].content) is (variant == "tool_example")
    operations = json.loads((tmp_path / variant / "operations.json").read_text())
    tests = [op for op in operations if op["operation"] == "test"]
    assert [(op["patches"], op["exit_code"]) for op in tests] == [(1, 1), (2, 0)]
    # Real feedback is in later model requests; hidden checks are never provided.
    observed = "\n".join(m.content for request in backend.requests for m in request.messages)
    assert "AssertionError" in observed
    assert development_tasks()[0].hidden_checks not in observed
