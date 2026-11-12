"""Offline P3 migration/merge/approval/history demo with synthetic temporary data."""

import asyncio
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from minidsh.adapters import ScriptedModel
from minidsh.composition import RuntimeOptions, build_local_runtime
from minidsh.domain import AgentBudget, JsonValue, ModelResponse, RunStatus, ToolCall, ToolStatus
from minidsh.domain.memory import MemoryConfig, MemoryEvidenceFile, MemoryEvidenceGrant

HISTORY_SCHEMA = 2


async def main() -> None:  # noqa: PLR0915 - one temporary, explicit end-to-end demonstration
    with TemporaryDirectory(prefix="minidsh-note-demo-") as folder:
        root = Path(folder)
        workspace, private = root / "workspace", root / "private"
        workspace.mkdir()
        private.mkdir(mode=0o700)
        rules = {
            "command_runner": "Use uv for project commands.",
            "check_result": "Inspect command completion before continuing.",
            "merged": "Use uv and inspect completion before continuing.",
        }
        grants = []
        for name, rule in rules.items():
            configuration = json.dumps({"confirmed_demo_rule": rule}).encode()
            (private / (name + ".json")).write_bytes(configuration)
            bundle = json.dumps(
                {
                    "schema_version": "1.0",
                    "kind": "operator_configuration",
                    "project_id": "demo",
                    "applicable_version": "v1",
                    "source_run_id": "manual_merge" if name == "merged" else "capture",
                    "configuration": {
                        "path": name + ".json",
                        "sha256": hashlib.sha256(configuration).hexdigest(),
                    },
                    "confirmed_by": "fixture_operator",
                    "confirmed_at": "2026-01-01T00:00:00+00:00",
                },
                sort_keys=True,
            ).encode()
            filename = name + "_bundle.json"
            (private / filename).write_bytes(bundle)
            grants.append(
                MemoryEvidenceGrant(
                    name, MemoryEvidenceFile(filename, hashlib.sha256(bundle).hexdigest())
                )
            )
        model = ScriptedModel(
            [
                ModelResponse(
                    tool_calls=tuple(
                        ToolCall(
                            name,
                            "propose_memory",
                            {"kind": "environment", "topic": name, "content": rules[name]},
                        )
                        for name in ("command_runner", "check_result")
                    )
                ),
                ModelResponse("Unverified proposals recorded."),
            ],
            measure_input_tokens=True,
        )
        runtime = build_local_runtime(
            workspace=workspace,
            model=model,
            options=RuntimeOptions(
                enabled_tools=frozenset({"propose_memory", "read_memory"}),
                memory=MemoryConfig(
                    "demo",
                    "v1",
                    str(private / "memory.sqlite3"),
                    str(private),
                    evidence_root=str(private),
                    evidence_grants=tuple(grants),
                    retrieval_mode="progressive_v2",
                ),
            ),
            budget=AgentBudget(
                max_steps=12, max_tool_calls=16, max_tokens=8000, max_duration_seconds=30
            ),
        )
        try:
            capture = await runtime.agent.run(
                "Record demo environment conventions.", run_id="capture"
            )
            assert capture.status is RunStatus.FINISHED
            assert runtime.memory_maintenance is not None
            operator = runtime.memory_maintenance("fixture_operator")
            step = 0

            async def operate(name: str, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
                nonlocal step
                step += 1
                observation = await operator.execute(
                    ToolCall(f"operator_{step}", name, arguments), "demo_operator", step
                )
                assert observation.status is ToolStatus.SUCCEEDED, observation.error
                assert isinstance(observation.result, dict)
                return observation.result

            async def approve(
                entry_id: str, content_hash: JsonValue, evidence_id: str
            ) -> dict[str, JsonValue]:
                base: dict[str, JsonValue] = {"entry_id": entry_id, "expected_revision": 1}
                await operate("validate_memory", {**base, "evidence_id": evidence_id})
                approved = await operate(
                    "approve_memory",
                    {
                        **base,
                        "content_sha256": content_hash,
                        "approved_at": "2026-01-01T00:00:00+00:00",
                    },
                )
                return await operate(
                    "commit_memory", {**base, "approval_sha256": approved["approval_sha256"]}
                )

            sources: list[JsonValue] = []
            old_ids = []
            for name in ("command_runner", "check_result"):
                reply = next(message for message in capture.history if message.tool_call_id == name)
                proposal = json.loads(reply.content)["result"]
                assert proposal["status"] == "proposed"
                committed = await approve(proposal["entry_id"], proposal["content_sha256"], name)
                old_ids.append(proposal["entry_id"])
                sources.append(
                    {
                        "entry_id": committed["entry_id"],
                        "revision": committed["revision"],
                        "proposal_sha256": committed["proposal_sha256"],
                        "evidence_sha256": committed["evidence_sha256"],
                        "source_run_id": committed["source_run_id"],
                    }
                )
            database_before = (private / "memory.sqlite3").read_bytes()
            migration = await operate("migrate_memory_history", {})
            assert migration["from_version"] == 1
            assert migration["to_version"] == HISTORY_SCHEMA
            backup_name = migration["backup_name"]
            assert isinstance(backup_name, str)
            assert (private / backup_name).read_bytes() == database_before
            merged = await operate(
                "merge_memory_notes",
                {
                    "entry_id": "merged_rules",
                    "kind": "environment",
                    "topic": "command_runner",
                    "source_run_id": "manual_merge",
                    "trigger": "For project commands.",
                    "rule": rules["merged"],
                    "verification": "Run the project command contract.",
                    "sources": sources,
                    "supersedes": sources,
                },
            )
            assert merged["status"] == "proposed"
            assert merged["approved_by"] is None
            for entry_id in old_ids:
                assert (await operate("review_memory", {"entry_id": entry_id}))[
                    "status"
                ] == "committed"
            committed = await approve("merged_rules", merged["content_sha256"], "merged")
            for entry_id in old_ids:
                assert (await operate("review_memory", {"entry_id": entry_id}))[
                    "status"
                ] == "superseded"
            model.responses.extend(
                [
                    ModelResponse(
                        tool_calls=(
                            ToolCall(
                                "read",
                                "read_memory",
                                {
                                    "references": [
                                        {
                                            "entry_id": "merged_rules",
                                            "expected_revision": 1,
                                            "expected_content_sha256": committed["content_sha256"],
                                        }
                                    ]
                                },
                            ),
                        )
                    ),
                    ModelResponse("Read the approved structured note."),
                ]
            )
            recalled = await runtime.agent.run("Read the approved project note.", run_id="recall")
            assert recalled.status is RunStatus.FINISHED
            assert rules["merged"] in str(model.requests[-1].messages)
            page = await operate("review_memory_history", {"entry_id": "merged_rules"})
            assert page["records"]
            assert page["next_after"] is not None
            print(
                json.dumps(
                    {
                        "synthetic_operator": "fixture_operator",
                        "migration_backup_verified": True,
                        "merged_proposal_required_new_approval": True,
                        "old_notes_superseded": len(old_ids),
                        "approved_structured_body_read": True,
                        "history_available": True,
                        "real_model_calls": 0,
                    }
                )
            )
        finally:
            if runtime.close is not None:
                await runtime.close()


if __name__ == "__main__":
    asyncio.run(main())
