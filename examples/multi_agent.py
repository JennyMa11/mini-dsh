"""Offline team demo: uv run python examples/multi_agent.py."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from minidsh.adapters import JsonlTraceWriter, ScriptedModel
from minidsh.collaboration import build_multi_agent_runtime
from minidsh.composition import RuntimeOptions
from minidsh.domain import RunStatus
from minidsh.evaluation import TraceRun

EXPECTED_AGENTS = 2
EXPECTED_TOOL_CALLS = 3


async def _run(workspace: Path, examples: Path) -> None:
    """Run two independently scripted readers and verify their team summary."""
    runtime = build_multi_agent_runtime(
        workspace=workspace,
        model=ScriptedModel.from_file(examples / "multi_agent_responses.json"),
        subagent_model_factory=lambda: ScriptedModel.from_file(
            examples / "subagent_responses.json"
        ),
        options=RuntimeOptions(multi_agent_enabled=True, subagent_max_parallel=2),
    )
    trace_path = workspace / ".minidsh" / "traces" / "team.jsonl"
    runtime.events.subscribe("*", JsonlTraceWriter(trace_path))
    result = await runtime.agent.run("Delegate analysis and review of hello.py.")
    trace = TraceRun.from_file(trace_path)
    children = trace.by_name("sub_agent_finished")
    if (
        result.status is not RunStatus.FINISHED
        or len(children) != EXPECTED_AGENTS
        or result.tool_calls != EXPECTED_TOOL_CALLS
    ):
        raise RuntimeError("team demonstration did not meet its completion criteria")
    if any(event.data.get("status") != "finished" for event in children):
        raise RuntimeError("a Sub Agent did not complete")
    print(
        json.dumps(
            {
                "status": result.status.value,
                "answer": result.answer,
                "sub_agents": len(children),
                "team_tokens": result.token_usage.total_tokens,
                "team_tool_calls": result.tool_calls,
            },
            indent=2,
        )
    )


def main() -> None:
    """Prepare an isolated workspace and run the team demonstration."""
    examples = Path(__file__).resolve().parent
    with TemporaryDirectory(prefix="minidsh-team-") as directory:
        workspace = Path(directory)
        (workspace / "hello.py").write_text(
            'def greet(name: str) -> str:\n    return f"Hello, {name}!"\n', encoding="utf-8"
        )
        asyncio.run(_run(workspace, examples))


if __name__ == "__main__":
    main()
