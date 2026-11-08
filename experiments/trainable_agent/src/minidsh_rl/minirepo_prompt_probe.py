"""Deterministic control probe for example actions; never an LLM efficacy result."""

import argparse
import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel

from minidsh.domain import ModelRequest
from minidsh.infrastructure.containers import DockerHost
from minidsh_rl.contracts import Turn
from minidsh_rl.minirepo_prompts import TOOL_EXAMPLE
from minidsh_rl.minirepo_rollout import run_rollout
from minidsh_rl.minirepo_tasks import MiniTask
from minidsh_rl.minirepo_tools import Empty, Patch, Read
from minidsh_rl.minirepo_verification import IMAGE
from minidsh_rl.model import Sample, parse_output

EXPECTED_PATCHES = 2


@dataclass
class ExampleBackend:
    """Synthetic IDs, prewritten actions, no SDK and no policy learning claims."""

    actions: list[str]
    index: int = 0
    requests: list[ModelRequest] = field(default_factory=list)

    def render(self, request: ModelRequest) -> tuple[int, ...]:
        self.requests.append(request)
        return (1, 2)

    async def sample(self, prompt: tuple[int, ...], limit: int, seed: int) -> Sample:
        del limit, seed
        text = self.actions[self.index]
        self.index += 1
        return Sample(Turn(prompt, (3,), (-0.1,)), text, "stop")


def example_task() -> MiniTask:
    """A public toy fixture separate from all research tasks and private oracles."""
    broken = "def format_tag(text):\n    return text.lower()\n"
    service = "from toy import format_tag\n\ndef solve(payload):\n    return format_tag(payload)\n"
    public = (
        "from toy import format_tag\nassert format_tag(' a ') == 'A'\n"
        "print('public tests passed')\n"
    )
    gold = broken.replace("text.lower()", "text.strip().upper()")
    return MiniTask(
        "control_tool_example",
        "control_only",
        "Return a stripped uppercase tag.",
        (
            ("toy.py", broken),
            ("service.py", service),
            ("public_tests.py", public),
            ("README.md", "format_tag(text) returns text stripped and uppercased."),
        ),
        ("toy.py", "service.py"),
        (("toy.py", gold), ("service.py", service)),
        "assert solve(' a ') == 'A'\nassert solve('b') == 'B'\n",
    )


async def probe(output: Path) -> None:
    """Check actual feedback and isolation through the production Loop composition."""
    if await asyncio.to_thread(output.exists):
        raise FileExistsError("new control probe output required")
    actions = re.findall(r"<tool_call>.*?</tool_call>", TOOL_EXAMPLE)
    schemas: dict[str, type[BaseModel]] = {
        "read_file": Read,
        "apply_patch": Patch,
        "run_tests": Empty,
    }
    for i, action in enumerate(actions):
        call = parse_output(action, f"example_{i}").tool_calls[0]
        schemas[call.name].model_validate(call.arguments)
    backend = ExampleBackend([*actions, "Fixed; submit for independent verification."])
    result = await run_rollout(
        example_task(),
        backend,
        policy_id="scripted_control_only",
        run_id="tool_example_control",
        seed_value=17,
        mode="interactive",
        output=output,
        host=DockerHost(),
        image=IMAGE,
        max_output_per_call=1024,
        prompt_variant="tool_example",
    )
    operations = json.loads((output / "operations.json").read_text())
    tests = [x for x in operations if x["operation"] == "test"]
    observed = "\n".join(m.content for request in backend.requests for m in request.messages)
    passed = (
        result["success"] is True
        and result["patches"] == EXPECTED_PATCHES
        and [(x["patches"], x["exit_code"]) for x in tests] == [(1, 1), (2, 0)]
        and "AssertionError" in observed
        and example_task().hidden_checks not in observed
    )
    (output / "control-summary.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "passed": passed,
                "backend": "scripted_control_only",
                "new_model_calls": 0,
                "synthetic_token_ids": True,
                "scientific_rollouts": 0,
                "example_sha256": hashlib.sha256(TOOL_EXAMPLE.encode()).hexdigest(),
                "public_test_exit_codes": [x["exit_code"] for x in tests],
                "failed_feedback_visible": "AssertionError" in observed,
                "private_checks_not_visible": example_task().hidden_checks not in observed,
                "independent_success": result["success"],
                "source_sha256": {
                    str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in Path(__file__).parent.glob("minirepo*.py")
                },
                "limitation": (
                    "Deterministic tool/feedback contract only; no learned behavior measured."
                ),
            },
            indent=2,
        )
    )
    if not passed:
        raise ValueError("tool-example control probe failed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(probe(args.output))


if __name__ == "__main__":
    main()
