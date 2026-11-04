"""Offline Skills + Verification example; fixed visible check and independent final assertion.

Run: uv run python examples/coding_extension_demo.py
Fixture setup and final acceptance are trusted host code outside the agent workspace flow.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from minidsh.adapters import ScriptedModel
from minidsh.composition import RuntimeOptions, plugin_runtime
from minidsh.domain import AgentBudget, Event, ModelResponse, RunStatus, ToolCall
from minidsh.domain.verification import VerificationConfig


async def main() -> None:
    with TemporaryDirectory(prefix="minidsh-coding-demo-") as directory:
        workspace = Path(directory) / "workspace"
        workspace.mkdir()
        shutil.copytree(Path(__file__).parent / "skills", workspace / "skills")
        (workspace / "value.txt").write_text("broken")
        (workspace / "visible.py").write_text(
            "from pathlib import Path\nassert Path('value.txt').read_text() == 'fixed'\n"
        )
        model = ScriptedModel(
            [
                ModelResponse(
                    tool_calls=(ToolCall("guidance", "load_skill", {"name": "bug_fix"}),)
                ),
                ModelResponse("Candidate ready."),
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "repair",
                            "write_file",
                            {
                                "path": "value.txt",
                                "content": "fixed",
                                "overwrite": True,
                            },
                        ),
                    )
                ),
                ModelResponse("Repaired."),
            ],
            measure_input_tokens=True,
        )
        verdicts: list[str] = []

        async def record(event: Event) -> None:
            if event.name == "runtime_verification_completed":
                verdicts.append(str(event.data["runtime_verification"]))

        async with plugin_runtime(
            workspace=workspace,
            model=model,
            plugins=(),
            options=RuntimeOptions(
                skill_roots=("skills",),
                allow_write=True,
                allow_shell=True,
                allowed_commands=frozenset({Path(sys.executable).name}),
                verification=VerificationConfig((sys.executable, "visible.py"), "demo_visible_v1"),
            ),
            budget=AgentBudget(
                max_steps=8, max_tokens=8000, max_tool_calls=12, max_duration_seconds=20
            ),
        ) as runtime:
            runtime.events.subscribe("*", record)
            result = await runtime.agent.run(
                "Repair value.txt; its complete content must be fixed."
            )
        # This executable final criterion is outside the agent and is never fed back as guidance.
        accepted = (workspace / "value.txt").read_text() == "fixed"
        assert result.status is RunStatus.FINISHED
        assert accepted
        assert verdicts == ["failed", "passed"]
        print(
            json.dumps(
                {
                    "run_id": result.run_id,
                    "runtime_status": result.status.value,
                    "independent_criterion_version": "demo_independent_v1",
                    "independent_acceptance": accepted,
                    "visible_verdicts": verdicts,
                    "steps": result.steps,
                    "tool_calls": result.tool_calls,
                    "total_tokens": result.token_usage.total_tokens,
                }
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
