"""Offline, file-only project Memory example; no user seed approval or model service.

Run: uv run python examples/project_memory_demo.py
The trusted caller creates private storage outside the actor workspace. This demo
reviews an unverified proposal and deliberately leaves it unapproved.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from minidsh.adapters import ScriptedModel
from minidsh.composition import RuntimeOptions, plugin_runtime
from minidsh.domain import AgentBudget, ModelResponse, RunStatus, ToolCall, ToolStatus
from minidsh.domain.memory import MemoryConfig


async def main() -> None:
    with TemporaryDirectory(prefix="minidsh-memory-demo-") as directory:
        root = Path(directory)
        workspace, private = root / "workspace", root / "private"
        workspace.mkdir()
        private.mkdir(mode=0o700)
        database = private / "memory.sqlite3"
        budget = AgentBudget(
            max_steps=4, max_tokens=8000, max_tool_calls=8, max_duration_seconds=20
        )
        async with plugin_runtime(
            workspace=workspace,
            model=ScriptedModel([ModelResponse("done")]),
            plugins=(),
            options=RuntimeOptions(enabled_tools=frozenset({"read_file"})),
            budget=budget,
        ) as disabled:
            await disabled.agent.run("Inspect the optional Memory configuration.")
            assert disabled.memory_maintenance is None
            assert not database.exists()
        model = ScriptedModel(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "proposal",
                            "propose_memory",
                            {
                                "kind": "diagnosis",
                                "topic": "assertion_inputs",
                                "content": "Compare assertion inputs before changing handlers.",
                            },
                        ),
                    )
                ),
                ModelResponse("Proposal ready for operator review."),
            ]
        )
        async with plugin_runtime(
            workspace=workspace,
            model=model,
            plugins=(),
            options=RuntimeOptions(
                enabled_tools=frozenset({"read_file", "propose_memory"}),
                memory=MemoryConfig("demo_project", "v1", str(database), str(private)),
            ),
            budget=budget,
        ) as runtime:
            result = await runtime.agent.run("Demonstrate an unverified diagnostic proposal.")
            assert result.status is RunStatus.FINISHED
            reply = next(m for m in result.history if m.tool_call_id == "proposal")
            entry_id = json.loads(reply.content)["result"]["entry_id"]
            assert runtime.memory_maintenance is not None
            reviewed = await runtime.memory_maintenance("demo_operator").execute(
                ToolCall("review", "review_memory", {"entry_id": entry_id}), "operator_review", 1
            )
            assert reviewed.status is ToolStatus.SUCCEEDED
            assert isinstance(reviewed.result, dict)
            assert reviewed.result["status"] == "proposed"
            assert reviewed.result["approved_by"] is None
            assert all(
                message.name != "project_memory"
                for request in model.requests
                for message in request.messages
            )
            print(
                json.dumps(
                    {
                        "run_id": result.run_id,
                        "default_off_created_database": False,
                        "proposal_status": reviewed.result["status"],
                        "approved_by": reviewed.result["approved_by"],
                        "injected_unverified_memory": False,
                        "real_model_calls": 0,
                    }
                )
            )


if __name__ == "__main__":
    asyncio.run(main())
