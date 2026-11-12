"""Offline manual consolidation in a temporary private store; no real model calls."""

import asyncio
import hashlib
import json
import tempfile
from pathlib import Path

from minidsh.adapters import ScriptedModel
from minidsh.adapters.memory import SQLiteMemoryStore
from minidsh.domain import ModelResponse, ToolCall
from minidsh.domain.memory import MemoryConfig, MemoryStatus
from minidsh.memory_cli import memory_command


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="minidsh-consolidation-") as temporary:
        root = await asyncio.to_thread(Path(temporary).resolve)
        workspace, private = root / "workspace", root / "private"
        workspace.mkdir()
        private.mkdir()
        original = workspace / "actor_trace.jsonl"
        original.write_text("original Actor facts remain unchanged\n")
        before = hashlib.sha256(original.read_bytes()).hexdigest()
        config = MemoryConfig(
            "demo_project",
            "v1",
            str(private / "memory.sqlite3"),
            str(private),
            retrieval_mode="progressive_v2",
        )
        assert (
            await memory_command(
                workspace=workspace,
                memory=config,
                operator_id="fixture_operator",
                action="migrate",
                arguments_text="{}",
            )
            == 0
        )
        fact = "The project command completed in the configured environment."
        arguments = {
            "materials_authorized": True,
            "redacted": True,
            "facts": [
                {
                    "fact_id": "command_check",
                    "source_run_id": "actor_source",
                    "content": fact,
                    "content_sha256": hashlib.sha256(fact.encode()).hexdigest(),
                }
            ],
        }
        model = ScriptedModel(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "draft",
                            "propose_consolidated_note",
                            {
                                "kind": "environment",
                                "topic": "command_policy",
                                "trigger": "For project commands.",
                                "rule": "Prefer the configured project runner.",
                                "verification": "Run the project checks.",
                            },
                        ),
                    )
                ),
                ModelResponse("A proposed note is ready for operator review."),
            ],
            measure_input_tokens=True,
        )
        assert (
            await memory_command(
                workspace=workspace,
                memory=config,
                operator_id="fixture_operator",
                action="consolidate",
                arguments_text=json.dumps(arguments),
                model_factory=lambda: model,
            )
            == 0
        )
        store = SQLiteMemoryStore(Path(config.database), allowed_root=private, workspace=workspace)
        try:
            entries = await store.snapshot(config.project_id)
            assert len(entries) == 1
            entry = entries[0]
            assert entry.status is MemoryStatus.PROPOSED
            assert entry.approval_sha256 is None
            assert entry.evidence_sha256 is None
            assert hashlib.sha256(original.read_bytes()).hexdigest() == before
            assert not (workspace / ".minidsh").exists()
            assert (
                await memory_command(
                    workspace=workspace,
                    memory=config,
                    operator_id="fixture_operator",
                    action="review-consolidation",
                    arguments_text=json.dumps({"source_run_id": entry.source_run_id}),
                )
                == 0
            )
            print(
                json.dumps(
                    {
                        "proposed_only": True,
                        "original_actor_trace_unchanged": True,
                        "private_audit_available": True,
                        "real_model_calls": 0,
                    }
                )
            )
        finally:
            await store.close()


if __name__ == "__main__":
    asyncio.run(main())
