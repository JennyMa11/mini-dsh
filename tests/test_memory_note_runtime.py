"""Structured notes use real private evidence, operator tools and atomic history."""

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from minidsh.adapters import ScriptedModel
from minidsh.adapters.memory import SQLiteMemoryStore
from minidsh.adapters.memory_evidence import JsonMemoryEvidenceSource
from minidsh.adapters.trace import JsonlTraceWriter
from minidsh.composition import RuntimeOptions, build_local_runtime
from minidsh.configured_runtime import build_configured_runtime
from minidsh.domain import (
    AgentBudget,
    JsonValue,
    Message,
    ModelResponse,
    Role,
    RunStatus,
    ToolCall,
    ToolStatus,
)
from minidsh.domain.memory import (
    MemoryConfig,
    MemoryEntry,
    MemoryEvidence,
    MemoryEvidenceFile,
    MemoryEvidenceGrant,
    MemoryEvidenceKind,
    MemoryKind,
    MemoryStatus,
)
from minidsh.domain.memory_notes import MemoryNote, MemoryNoteSource, parse_memory_note
from minidsh.runtime import EventBus
from minidsh.runtime.memory import MemoryService, ProposeMemoryTool
from minidsh.runtime.memory_maintenance import memory_maintenance_tools
from minidsh.runtime.memory_note_tools import ProposeMemoryNoteTool, memory_note_maintenance_tools
from minidsh.tools import PermissionPolicy, ToolRegistry, ToolRuntime

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_n10_mutually_referencing_uncommitted_drafts_cannot_authorize_each_other(
    tmp_path: Path,
) -> None:
    service, store, tools, _ = await _setup(tmp_path)
    pending = []
    try:
        for name in ("cycle_a", "cycle_b"):
            entry = MemoryEntry.propose(
                name,
                "alpha",
                MemoryKind.ENVIRONMENT,
                topic=name,
                content="Use uv.",
                source_run_id="new_source",
                applicable_version="v1",
            )
            await store.create(entry)
            await service.validate(name, expected_revision=1, evidence_id="new_source")
            pending.append(
                await service.approve(
                    name,
                    expected_revision=1,
                    content_sha256=entry.content_sha256,
                    approver="fixture_operator",
                    approved_at="2026-01-01T00:00:00+00:00",
                )
            )
        before = (tmp_path / "private/memory.sqlite3").read_bytes()
        for index in range(2):
            target, source = pending[index], pending[1 - index]
            reference: dict[str, JsonValue] = {
                "entry_id": source.entry_id,
                "revision": 1,
                "proposal_sha256": source.proposal_sha256,
                "evidence_sha256": source.evidence_sha256,
                "source_run_id": source.source_run_id,
            }
            refused = await tools.execute(
                ToolCall(
                    "cycle",
                    "revise_memory_note",
                    {
                        "entry_id": target.entry_id,
                        "expected_revision": 1,
                        "source_run_id": "new_revision",
                        "trigger": "For commands.",
                        "rule": "Use the other draft.",
                        "verification": "Run the check.",
                        "sources": [reference],
                    },
                ),
                "operator",
                1,
            )
            assert refused.status is ToolStatus.FAILED
            assert refused.error is not None
            assert refused.error.code == "memory_note_source_unavailable"
            assert (tmp_path / "private/memory.sqlite3").read_bytes() == before
            assert await store.get("alpha", target.entry_id) == target
    finally:
        await store.close()


def _bus(root: Path) -> EventBus:
    bus = EventBus()
    bus.subscribe("*", JsonlTraceWriter(root / "memory_operator_trace.jsonl"))
    return bus


async def _setup(
    root: Path,
) -> tuple[MemoryService, SQLiteMemoryStore, ToolRuntime, list[MemoryEntry]]:
    workspace, private = root / "workspace", root / "private"
    workspace.mkdir()
    private.mkdir()
    config = b"use_uv=true\n"
    (private / "project.toml").write_bytes(config)
    grants = []
    for name in ("old_a", "old_b", "new_source"):
        raw = json.dumps(
            {
                "schema_version": "1.0",
                "kind": "operator_configuration",
                "project_id": "alpha",
                "applicable_version": "v1",
                "source_run_id": name,
                "configuration": {
                    "path": "project.toml",
                    "sha256": hashlib.sha256(config).hexdigest(),
                },
                "confirmed_by": "fixture_operator",
                "confirmed_at": "2026-01-01T00:00:00+00:00",
            },
            sort_keys=True,
        ).encode()
        (private / (name + ".json")).write_bytes(raw)
        grants.append(
            MemoryEvidenceGrant(
                name, MemoryEvidenceFile(name + ".json", hashlib.sha256(raw).hexdigest())
            )
        )
    store = SQLiteMemoryStore(private / "memory.sqlite3", allowed_root=private, workspace=workspace)
    service = MemoryService(
        store,
        project_id="alpha",
        applicable_version="v1",
        evidence=JsonMemoryEvidenceSource(private, workspace=workspace, grants=tuple(grants)),
        config=MemoryConfig(
            "alpha",
            "v1",
            str(private / "memory.sqlite3"),
            str(private),
            retrieval_mode="progressive_v2",
        ),
    )
    registry = ToolRegistry()
    for tool in (
        *memory_maintenance_tools(service, operator_id="fixture_operator"),
        *memory_note_maintenance_tools(service, operator_id="fixture_operator"),
    ):
        registry.register(tool)
    tools = ToolRuntime(
        registry,
        PermissionPolicy(frozenset({"memory.maintain", "memory.migrate", "memory.evidence.read"})),
        _bus(root),
    )
    migrated = await tools.execute(ToolCall("migrate", "migrate_memory_history", {}), "operator", 1)
    assert migrated.status is ToolStatus.SUCCEEDED
    entries = []
    for name in ("old_a", "old_b"):
        proposal = MemoryEntry.propose(
            name,
            "alpha",
            MemoryKind.ENVIRONMENT,
            topic=name,
            content="Use uv.",
            source_run_id=name,
            applicable_version="v1",
        )
        await store.create(proposal)
        await service.validate(name, expected_revision=1, evidence_id=name)
        approved = await service.approve(
            name,
            expected_revision=1,
            content_sha256=proposal.content_sha256,
            approver="fixture_operator",
            approved_at="2026-01-01T00:00:00+00:00",
        )
        entries.append(
            await service.commit(
                name, expected_revision=1, approval_sha256=approved.approval_sha256 or ""
            )
        )
    return service, store, tools, entries


def _arguments(entries: list[MemoryEntry]) -> dict[str, JsonValue]:
    references: list[JsonValue] = [asdict(MemoryNoteSource.from_entry(entry)) for entry in entries]
    return {
        "entry_id": "merged",
        "kind": "environment",
        "topic": "old_a",
        "source_run_id": "new_source",
        "trigger": "For project commands.",
        "rule": "Use uv and check completion.",
        "verification": "Execute the command contract.",
        "sources": references,
        "supersedes": references,
    }


@pytest.mark.asyncio
async def test_n04_08_09_merge_requires_new_evidence_then_atomic_supersession(
    tmp_path: Path,
) -> None:
    _, store, tools, old = await _setup(tmp_path)
    try:
        merged = await tools.execute(
            ToolCall("merge", "merge_memory_notes", _arguments(old)), "operator", 2
        )
        assert merged.status is ToolStatus.SUCCEEDED
        proposal = await store.get("alpha", "merged")
        assert proposal is not None
        assert proposal.status is MemoryStatus.PROPOSED
        assert [await store.get("alpha", e.entry_id) for e in old] == old
        before = (tmp_path / "private/memory.sqlite3").read_bytes()
        refused = await tools.execute(
            ToolCall(
                "old",
                "validate_memory",
                {"entry_id": "merged", "expected_revision": 1, "evidence_id": "old_a"},
            ),
            "operator",
            3,
        )
        assert refused.status is ToolStatus.FAILED
        assert refused.error is not None
        assert refused.error.code == "memory_note_evidence_reused"
        assert (tmp_path / "private/memory.sqlite3").read_bytes() == before
        verified = await tools.execute(
            ToolCall(
                "new",
                "validate_memory",
                {"entry_id": "merged", "expected_revision": 1, "evidence_id": "new_source"},
            ),
            "operator",
            4,
        )
        assert verified.status is ToolStatus.SUCCEEDED
        approved = await tools.execute(
            ToolCall(
                "approve",
                "approve_memory",
                {
                    "entry_id": "merged",
                    "expected_revision": 1,
                    "content_sha256": proposal.content_sha256,
                    "approved_at": "2026-01-01T00:00:00+00:00",
                },
            ),
            "operator",
            5,
        )
        assert approved.status is ToolStatus.SUCCEEDED
        current = await store.get("alpha", "merged")
        assert current is not None
        committed = await tools.execute(
            ToolCall(
                "commit",
                "commit_memory",
                {
                    "entry_id": "merged",
                    "expected_revision": 1,
                    "approval_sha256": current.approval_sha256,
                },
            ),
            "operator",
            6,
        )
        assert committed.status is ToolStatus.SUCCEEDED
        active = await store.get("alpha", "merged")
        assert active is not None
        assert active.status is MemoryStatus.COMMITTED
        for entry in old:
            replaced = await store.get("alpha", entry.entry_id)
            assert replaced is not None
            assert replaced.status is MemoryStatus.SUPERSEDED
        page = await tools.execute(
            ToolCall("history", "review_memory_history", {"entry_id": "old_a"}), "operator", 7
        )
        assert page.status is ToolStatus.SUCCEEDED
        assert isinstance(page.result, dict)
        assert page.result["records"]
    finally:
        await store.close()


@pytest.mark.parametrize("permission", [False, True])
@pytest.mark.asyncio
async def test_n08_new_maintenance_is_denied_before_io_or_during_a_run(
    tmp_path: Path, *, permission: bool
) -> None:
    service, store, tools, entries = await _setup(tmp_path)
    if permission:
        service.begin_run("actor")
    else:
        registry = ToolRegistry()
        for tool in memory_note_maintenance_tools(service, operator_id="fixture_operator"):
            registry.register(tool)
        tools = ToolRuntime(registry, PermissionPolicy(), _bus(tmp_path))
        await store.close()
    before = (tmp_path / "private/memory.sqlite3").read_bytes()
    try:
        calls = [
            ToolCall("migration", "migrate_memory_history", {}),
            ToolCall("history", "review_memory_history", {"entry_id": "old_a"}),
            ToolCall("merge", "merge_memory_notes", _arguments(entries)),
        ]
        for call in calls:
            result = await tools.execute(call, "operator", 1)
            assert result.status is (ToolStatus.FAILED if permission else ToolStatus.DENIED)
            if permission:
                assert result.error is not None
                assert result.error.code == "memory_run_active"
        assert (tmp_path / "private/memory.sqlite3").read_bytes() == before
    finally:
        if permission:
            service.end_run("actor")
        await store.close()


@pytest.mark.asyncio
async def test_n08_structured_proposal_cannot_bypass_typed_permissions_with_the_plain_tool(
    tmp_path: Path,
) -> None:
    service, store, _, _ = await _setup(tmp_path)
    registry = ToolRegistry()
    registry.register(ProposeMemoryTool(service))
    registry.register(ProposeMemoryNoteTool(service))
    tools = ToolRuntime(registry, PermissionPolicy(frozenset({"memory.propose"})), _bus(tmp_path))
    service.begin_run("actor")
    before = (tmp_path / "private/memory.sqlite3").read_bytes()
    try:
        note = MemoryNote("For commands.", "Use uv.", "Run the check.")
        plain = await tools.execute(
            ToolCall(
                "plain",
                "propose_memory",
                {"kind": "environment", "topic": "raw_note", "content": note.to_content()},
            ),
            "actor",
            1,
        )
        assert plain.status is ToolStatus.FAILED
        assert plain.error is not None
        assert plain.error.code == "memory_note_tool_required"
        typed = await tools.execute(
            ToolCall(
                "typed",
                "propose_memory_note",
                {
                    "kind": "environment",
                    "topic": "typed_note",
                    "trigger": note.trigger,
                    "rule": note.rule,
                    "verification": note.verification,
                },
            ),
            "actor",
            2,
        )
        assert typed.status is ToolStatus.DENIED
        assert (tmp_path / "private/memory.sqlite3").read_bytes() == before
    finally:
        service.end_run("actor")
        await store.close()


@pytest.mark.asyncio
async def test_n03_structured_revision_binds_new_run_and_clears_old_approval(
    tmp_path: Path,
) -> None:
    _, store, tools, old = await _setup(tmp_path)
    try:
        args = _arguments(old)
        args["supersedes"] = []
        result = await tools.execute(ToolCall("merge", "merge_memory_notes", args), "operator", 1)
        assert result.status is ToolStatus.SUCCEEDED
        revision: dict[str, JsonValue] = {
            "entry_id": "merged",
            "expected_revision": 1,
            "source_run_id": "revision_run",
            "trigger": "On project commands.",
            "rule": "Use uv, then inspect the result.",
            "verification": "Run the check.",
        }
        changed = await tools.execute(
            ToolCall("revise", "revise_memory_note", revision), "operator", 2
        )
        assert changed.status is ToolStatus.SUCCEEDED
        current = await store.get("alpha", "merged")
        assert current is not None
        assert current.revision == 2
        assert current.source_run_id == "revision_run"
        assert current.approval_sha256 is None
        note = parse_memory_note(current.content)
        assert note is not None
        assert note.rule == revision["rule"]
        assert len((await store.history("alpha", "merged")).records) == 2
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_n08_legacy_store_cannot_silently_drop_note_history(tmp_path: Path) -> None:
    _, store, _, old = await _setup(tmp_path)

    class LegacyStore:
        async def create(self, entry: MemoryEntry) -> MemoryEntry:
            return await store.create(entry)

        async def get(self, project_id: str, entry_id: str) -> MemoryEntry | None:
            return await store.get(project_id, entry_id)

        async def snapshot(self, project_id: str) -> tuple[MemoryEntry, ...]:
            return await store.snapshot(project_id)

        async def replace(self, expected: MemoryEntry, updated: MemoryEntry) -> MemoryEntry:
            return await store.replace(expected, updated)

        async def close(self) -> None:
            await store.close()

    await store.close()
    service = MemoryService(LegacyStore(), project_id="alpha", applicable_version="v1")
    registry = ToolRegistry()
    for tool in memory_note_maintenance_tools(service, operator_id="fixture_operator"):
        registry.register(tool)
    tools = ToolRuntime(
        registry,
        PermissionPolicy(frozenset({"memory.maintain", "memory.evidence.read"})),
        _bus(tmp_path),
    )
    before = (tmp_path / "private/memory.sqlite3").read_bytes()
    refused = await tools.execute(
        ToolCall("legacy", "merge_memory_notes", _arguments(old)), "operator", 1
    )
    assert refused.status is ToolStatus.FAILED
    assert refused.error is not None
    assert refused.error.code == "memory_history_unsupported"
    assert (tmp_path / "private/memory.sqlite3").read_bytes() == before


@pytest.mark.parametrize("scope", ["project", "version"])
@pytest.mark.asyncio
async def test_n10_real_foreign_committed_source_cannot_be_used_in_an_alpha_v1_note(
    tmp_path: Path, scope: str
) -> None:
    _, store, tools, old = await _setup(tmp_path)
    project, version = ("beta", "v1") if scope == "project" else ("alpha", "v0")
    entry = MemoryEntry.propose(
        "foreign",
        project,
        MemoryKind.ENVIRONMENT,
        topic="foreign",
        content="Foreign private knowledge.",
        source_run_id="foreign",
        applicable_version=version,
    )
    validated = entry.validate(
        MemoryEvidence(
            "d" * 64,
            project,
            version,
            MemoryEvidenceKind.OPERATOR_CONFIGURATION,
            "foreign",
            accepted=True,
            source_exists=True,
            confirmed_by="fixture_operator",
            confirmed_at="2026-01-01T00:00:00+00:00",
        )
    )
    approved = validated.approve(
        expected_revision=1,
        content_sha256=validated.content_sha256,
        approver="fixture_operator",
        approved_at="2026-01-01T00:00:00+00:00",
    )
    committed = approved.commit(expected_revision=1, approval_sha256=approved.approval_sha256)
    await store.create(entry)
    for previous, following in ((entry, validated), (validated, approved), (approved, committed)):
        await store.replace(previous, following)
    args = _arguments(old)
    args["sources"] = [asdict(MemoryNoteSource.from_entry(committed))]
    args["supersedes"] = []
    before = (tmp_path / "private/memory.sqlite3").read_bytes()
    try:
        refused = await tools.execute(ToolCall("scope", "merge_memory_notes", args), "operator", 1)
        assert refused.status is ToolStatus.FAILED
        assert refused.error is not None
        assert refused.error.code == "memory_note_source_unavailable"
        assert "Foreign private knowledge." not in str(refused)
        assert (tmp_path / "private/memory.sqlite3").read_bytes() == before
        assert await store.get(project, "foreign") == committed
    finally:
        await store.close()


@pytest.mark.parametrize("invalid", ["wrong_fingerprint", "pending_source", "self_supersession"])
@pytest.mark.asyncio
async def test_n10_unverified_or_self_references_cannot_create_a_replacement(
    tmp_path: Path, invalid: str
) -> None:
    _, store, tools, old = await _setup(tmp_path)
    args = _arguments(old)
    action = "merge_memory_notes"
    if invalid == "self_supersession":
        args.pop("kind")
        args.pop("topic")
        args["entry_id"] = "old_a"
        args["expected_revision"] = 1
        action = "revise_memory_note"
    else:
        refs = args["sources"]
        assert isinstance(refs, list)
        assert isinstance(refs[0], dict)
        if invalid == "wrong_fingerprint":
            refs[0]["proposal_sha256"] = "f" * 64
        else:
            pending = MemoryEntry.propose(
                "pending",
                "alpha",
                MemoryKind.ENVIRONMENT,
                topic="pending",
                content="Unverified.",
                source_run_id="pending",
                applicable_version="v1",
            )
            await store.create(pending)
            refs[0]["entry_id"] = pending.entry_id
            refs[0]["proposal_sha256"] = pending.proposal_sha256
        args["supersedes"] = []
    before = (tmp_path / "private/memory.sqlite3").read_bytes()
    try:
        result = await tools.execute(ToolCall("bad", action, args), "operator", 1)
        assert result.status is ToolStatus.FAILED
        assert (tmp_path / "private/memory.sqlite3").read_bytes() == before
    finally:
        await store.close()


@pytest.mark.parametrize("field", ["trigger", "rule", "verification", "exceptions"])
@pytest.mark.asyncio
async def test_n08_structured_fields_cannot_wrap_observed_raw_material(
    tmp_path: Path, field: str
) -> None:
    service, store, _, _ = await _setup(tmp_path)
    registry = ToolRegistry()
    registry.register(ProposeMemoryNoteTool(service))
    tools = ToolRuntime(
        registry,
        PermissionPolicy(frozenset({"memory.propose", "memory.read", "memory.evidence.read"})),
        _bus(tmp_path),
    )
    service.begin_run("actor")
    raw = "Raw command output must stay in the factual record."
    service.observe_context(
        "actor", (Message(Role.TOOL, raw, name="read_file", tool_call_id="file"),)
    )
    before = (tmp_path / "private/memory.sqlite3").read_bytes()
    arguments: dict[str, JsonValue] = {
        "kind": "environment",
        "topic": "wrapped",
        "trigger": "For commands.",
        "rule": "Use uv.",
        "verification": "Run a check.",
    }
    arguments[field] = [raw] if field == "exceptions" else raw
    try:
        result = await tools.execute(ToolCall("raw", "propose_memory_note", arguments), "actor", 1)
        assert result.status is ToolStatus.FAILED
        assert result.error is not None
        assert result.error.code == "memory_raw_material"
        assert (tmp_path / "private/memory.sqlite3").read_bytes() == before
    finally:
        service.end_run("actor")
        await store.close()


@pytest.mark.parametrize("route", ["local", "configured"])
@pytest.mark.parametrize(
    "strategy", ["full", "sliding", "summary", "retrieval", "retrieval_summary", "symbol", "budget"]
)
@pytest.mark.asyncio
async def test_n12_actor_proposes_then_operator_commits_and_next_run_reads_note(
    tmp_path: Path, route: str, strategy: str
) -> None:
    _, store, _, _ = await _setup(tmp_path)
    await store.close()
    private = tmp_path / "private"
    grants = tuple(
        MemoryEvidenceGrant(
            path.stem, MemoryEvidenceFile(path.name, hashlib.sha256(path.read_bytes()).hexdigest())
        )
        for path in sorted(private.glob("*.json"))
    )
    config = MemoryConfig(
        "alpha",
        "v1",
        str(private / "memory.sqlite3"),
        str(private),
        evidence_root=str(private),
        evidence_grants=grants,
        retrieval_mode="progressive_v2",
    )
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        "note",
                        "propose_memory_note",
                        {
                            "kind": "environment",
                            "topic": "actor_rule",
                            "trigger": "For commands.",
                            "rule": "Use uv and inspect completion.",
                            "verification": "Run the contract.",
                        },
                    ),
                )
            ),
            ModelResponse("Proposed only."),
        ]
    )
    build = build_local_runtime if route == "local" else build_configured_runtime
    runtime = build(
        workspace=tmp_path / "workspace",
        model=model,
        options=RuntimeOptions(
            memory=config,
            enabled_tools=frozenset({"propose_memory_note", "read_memory"}),
            context_strategy=strategy,
        ),
        budget=AgentBudget(
            max_steps=12, max_tokens=8000, max_tool_calls=16, max_duration_seconds=30
        ),
    )
    runtime.events.subscribe("*", JsonlTraceWriter(tmp_path / "memory_operator_trace.jsonl"))
    try:
        first = await runtime.agent.run("Propose one project rule.", run_id="new_source")
        assert first.status is RunStatus.FINISHED
        reply = next(m for m in first.history if m.tool_call_id == "note")
        payload = json.loads(reply.content)
        assert payload["status"] == "succeeded"
        assert payload["result"]["status"] == "proposed"
        assert payload["result"]["approved_by"] is None
        entry_id = payload["result"]["entry_id"]
        assert runtime.memory_maintenance is not None
        operator = runtime.memory_maintenance("fixture_operator")
        assert "migrate_memory_history" in {tool.name for tool in operator.definitions()}
        verified = await operator.execute(
            ToolCall(
                "v",
                "validate_memory",
                {"entry_id": entry_id, "expected_revision": 1, "evidence_id": "new_source"},
            ),
            "operator",
            1,
        )
        assert verified.status is ToolStatus.SUCCEEDED
        approved = await operator.execute(
            ToolCall(
                "a",
                "approve_memory",
                {
                    "entry_id": entry_id,
                    "expected_revision": 1,
                    "content_sha256": payload["result"]["content_sha256"],
                    "approved_at": "2026-01-01T00:00:00+00:00",
                },
            ),
            "operator",
            2,
        )
        assert approved.status is ToolStatus.SUCCEEDED
        assert isinstance(approved.result, dict)
        commit = await operator.execute(
            ToolCall(
                "c",
                "commit_memory",
                {
                    "entry_id": entry_id,
                    "expected_revision": 1,
                    "approval_sha256": approved.result["approval_sha256"],
                },
            ),
            "operator",
            3,
        )
        assert commit.status is ToolStatus.SUCCEEDED
        database_before_recall = (private / "memory.sqlite3").read_bytes()
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
                                        "entry_id": entry_id,
                                        "expected_revision": 1,
                                        "expected_content_sha256": payload["result"][
                                            "content_sha256"
                                        ],
                                    }
                                ]
                            },
                        ),
                    )
                ),
                ModelResponse("Read the committed note."),
            ]
        )
        second = await runtime.agent.run("Read the project note.", run_id="recall")
        assert second.status is RunStatus.FINISHED
        assert "Use uv and inspect completion." in str(model.requests[-1].messages)
        assert not {
            "merge_memory_notes",
            "migrate_memory_history",
            "review_memory_history",
            "revise_memory_note",
        } & {tool.name for r in model.requests for tool in r.tools}
        assert all(
            "Use uv and inspect completion." not in message.content
            for message in model.requests[1].messages
            if message.name in {"project_memory", "project_memory_index"}
        )
        assert runtime.conversations is not None
        model.responses.extend(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "conversation_read",
                            "read_memory",
                            {
                                "references": [
                                    {
                                        "entry_id": entry_id,
                                        "expected_revision": 1,
                                        "expected_content_sha256": payload["result"][
                                            "content_sha256"
                                        ],
                                    }
                                ]
                            },
                        ),
                    )
                ),
                ModelResponse("Conversation recalled."),
            ]
        )
        recalled = await runtime.conversations.run_turn(
            "Recall the same approved note.", run_id="conversation_recall", seed=first.history
        )
        assert recalled.status is RunStatus.FINISHED
        assert "Use uv and inspect completion." in str(model.requests[-1].messages)
        assert (private / "memory.sqlite3").read_bytes() == database_before_recall
        (tmp_path / "memory_requests.json").write_text(
            json.dumps([asdict(request) for request in model.requests])
        )
        (tmp_path / "memory_effects.json").write_text(
            json.dumps(
                {
                    "scope": "read-only recall and Conversation after explicit operator commit",
                    "before_sha256": hashlib.sha256(database_before_recall).hexdigest(),
                    "after_sha256": hashlib.sha256(
                        (private / "memory.sqlite3").read_bytes()
                    ).hexdigest(),
                    "unchanged": True,
                }
            )
        )
    finally:
        if runtime.close:
            await runtime.close()
