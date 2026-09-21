"""Minimal end-to-end Mini-dsh demo.

It runs the complete ``Observe -> Plan -> Act -> Execute -> Observe -> Finish``
loop against a throwaway workspace with no network access and no API key:

    uv run python examples/minimal_demo.py

The model is deterministic, so the printed metrics are reproducible.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from minidsh.adapters import JsonlTraceWriter, ScriptedModel
from minidsh.composition import RuntimeOptions, build_local_runtime
from minidsh.domain import ModelPricing, ModelResponse, RunStatus, TokenUsage, ToolCall

BROKEN_SOURCE = """def add(left: int, right: int) -> int:
    return left - right
"""

UNIT_TEST = """import unittest

from calc import add


class TestAdd(unittest.TestCase):
    def test_add(self) -> None:
        self.assertEqual(add(2, 3), 5)


if __name__ == "__main__":
    unittest.main()
"""


def scripted_model() -> ScriptedModel:
    """A deterministic model: read, patch, run the test, then answer."""
    return ScriptedModel(
        [
            ModelResponse(
                tool_calls=(ToolCall("c1", "read_file", {"path": "calc.py"}),),
                usage=TokenUsage(120, 18),
            ),
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        "c2",
                        "patch_file",
                        {
                            "path": "calc.py",
                            "expected": "return left - right",
                            "replacement": "return left + right",
                        },
                    ),
                ),
                usage=TokenUsage(210, 42),
            ),
            ModelResponse(
                tool_calls=(ToolCall("c3", "test", {}),),
                usage=TokenUsage(260, 12),
            ),
            ModelResponse(
                content="add() now returns the sum; the unit test passes.",
                usage=TokenUsage(300, 24),
            ),
        ]
    )


async def main() -> int:
    """Run the demo and return a process exit code."""
    with tempfile.TemporaryDirectory(prefix="minidsh-demo-") as temporary:
        workspace = Path(temporary)
        (workspace / "calc.py").write_text(BROKEN_SOURCE, encoding="utf-8")
        (workspace / "test_calc.py").write_text(UNIT_TEST, encoding="utf-8")

        runtime = build_local_runtime(
            workspace=workspace,
            model=scripted_model(),
            options=RuntimeOptions(
                context_strategy="retrieval",
                allow_write=True,
                allow_shell=True,
                allowed_commands=frozenset({"git", "python"}),
                test_command=("python", "-m", "unittest", "discover", "-s", "."),
                pricing=ModelPricing(input_usd_per_million=3.0, output_usd_per_million=15.0),
            ),
        )
        trace = workspace / "trace.jsonl"
        runtime.events.subscribe("*", JsonlTraceWriter(trace))

        result = await runtime.agent.run("Fix add() so the failing unit test passes.")

        print(f"workspace     : {workspace}")
        print(f"status        : {result.status.value}")
        print(f"answer        : {result.answer}")
        print(f"steps         : {result.steps}")
        print(f"tool calls    : {result.tool_calls}")
        print(f"tokens        : {result.token_usage.total_tokens}")
        print(f"cost (USD)    : {result.cost_usd:.6f}")
        print(f"trace lines   : {len(trace.read_text(encoding='utf-8').splitlines())}")
        print(f"patched source: {(workspace / 'calc.py').read_text(encoding='utf-8').strip()}")
        return 0 if result.status is RunStatus.FINISHED else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
