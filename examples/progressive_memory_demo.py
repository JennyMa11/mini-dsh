"""Offline v2 discovery/read demo, using temporary synthetic operator-confirmed data.

Run: uv run python examples/progressive_memory_demo.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from minidsh.adapters import ScriptedModel
from minidsh.composition import RuntimeOptions, build_local_runtime
from minidsh.domain import AgentBudget, JsonValue, ModelResponse, RunStatus, ToolCall, ToolStatus
from minidsh.domain.memory import MemoryConfig, MemoryEvidenceFile, MemoryEvidenceGrant


async def main() -> None:
    with TemporaryDirectory(prefix="minidsh-progressive-demo-") as directory:
        root = Path(directory)
        workspace, private = root / "workspace", root / "private"
        workspace.mkdir()
        private.mkdir(mode=0o700)
        body = "Use uv for project checks."
        configuration = json.dumps({"demo_policy": body}).encode()
        (private / "configuration.json").write_bytes(configuration)
        document = json.dumps(
            {
                "schema_version": "1.0",
                "kind": "operator_configuration",
                "project_id": "demo_project",
                "applicable_version": "v1",
                "source_run_id": "demo_capture",
                "configuration": {
                    "path": "configuration.json",
                    "sha256": hashlib.sha256(configuration).hexdigest(),
                },
                "confirmed_by": "fixture_operator",
                "confirmed_at": "2026-01-01T00:00:00+00:00",
            },
            sort_keys=True,
        ).encode()
        (private / "bundle.json").write_bytes(document)
        grant = MemoryEvidenceGrant(
            "demo_configuration",
            MemoryEvidenceFile("bundle.json", hashlib.sha256(document).hexdigest()),
        )
        model = ScriptedModel(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "proposal",
                            "propose_memory",
                            {
                                "kind": "environment",
                                "topic": "project_checks",
                                "content": body,
                            },
                        ),
                    )
                ),
                ModelResponse("Demo proposal recorded."),
            ],
            measure_input_tokens=True,
        )
        runtime = build_local_runtime(
            workspace=workspace,
            model=model,
            options=RuntimeOptions(
                enabled_tools=frozenset({"propose_memory", "search_memory", "read_memory"}),
                memory=MemoryConfig(
                    "demo_project",
                    "v1",
                    str(private / "memory.sqlite3"),
                    str(private),
                    evidence_root=str(private),
                    evidence_grants=(grant,),
                    retrieval_mode="progressive_v2",
                ),
            ),
            budget=AgentBudget(
                max_steps=12, max_tool_calls=16, max_tokens=8000, max_duration_seconds=30
            ),
        )
        try:
            capture = await runtime.agent.run("Record the demo environment.", run_id="demo_capture")
            assert capture.status is RunStatus.FINISHED
            proposal = next(m for m in capture.history if m.tool_call_id == "proposal")
            receipt = json.loads(proposal.content)["result"]
            entry_id, content_hash = receipt["entry_id"], receipt["content_sha256"]
            assert runtime.memory_maintenance is not None
            operator = runtime.memory_maintenance("fixture_operator")

            async def maintain(name: str, extra: dict[str, JsonValue]) -> dict[str, JsonValue]:
                observed = await operator.execute(
                    ToolCall(
                        name,
                        name,
                        {
                            "entry_id": entry_id,
                            "expected_revision": 1,
                            **extra,
                        },
                    ),
                    "demo_operator",
                    1,
                )
                assert observed.status is ToolStatus.SUCCEEDED
                assert isinstance(observed.result, dict)
                return observed.result

            await maintain("validate_memory", {"evidence_id": "demo_configuration"})
            approved = await maintain(
                "approve_memory",
                {
                    "content_sha256": content_hash,
                    "approved_at": "2026-01-01T00:00:00+00:00",
                },
            )
            await maintain("commit_memory", {"approval_sha256": approved["approval_sha256"]})
            model.responses.extend(
                [
                    ModelResponse(
                        tool_calls=(ToolCall("search", "search_memory", {"query": "uv"}),)
                    ),
                    ModelResponse(
                        tool_calls=(
                            ToolCall(
                                "read",
                                "read_memory",
                                {
                                    "references": [
                                        {
                                            "entry_id": entry_id,
                                            "expected_revision": 1,
                                            "expected_content_sha256": content_hash,
                                        }
                                    ]
                                },
                            ),
                        )
                    ),
                    ModelResponse("Demo knowledge retrieved."),
                ]
            )
            recalled = await runtime.agent.run(
                "Find the project check convention.", run_id="demo_recall"
            )
            assert recalled.status is RunStatus.FINISHED
            search = next(m for m in recalled.history if m.tool_call_id == "search")
            read = next(m for m in recalled.history if m.tool_call_id == "read")
            assert body not in search.content
            assert body in read.content
            assert any(m.name == "project_memory_index" for m in model.requests[-1].messages)
            print(
                json.dumps(
                    {
                        "mode": "progressive_v2",
                        "synthetic_operator": "fixture_operator",
                        "search_contains_body": False,
                        "approved_body_read": True,
                        "capture_status": capture.status.value,
                        "recall_status": recalled.status.value,
                        "real_model_calls": 0,
                    }
                )
            )
        finally:
            if runtime.close is not None:
                await runtime.close()


if __name__ == "__main__":
    asyncio.run(main())
