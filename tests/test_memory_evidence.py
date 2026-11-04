"""Private, hash-pinned evidence bundles through the evidence-source port."""

import asyncio
import hashlib
import json
import sqlite3
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from minidsh.adapters.memory import SQLiteMemoryStore
from minidsh.adapters.memory_evidence import JsonMemoryEvidenceSource
from minidsh.application.context_assembly import ContextAssembly
from minidsh.context import FullHistoryContext
from minidsh.domain import JsonValue, Message, Role, ToolCall, ToolStatus
from minidsh.domain.memory import (
    MemoryEntry,
    MemoryEvidence,
    MemoryEvidenceFile,
    MemoryEvidenceGrant,
    MemoryEvidenceKind,
    MemoryKind,
    MemoryStatus,
)
from minidsh.ports.memory import MemoryEvidenceError
from minidsh.runtime import EventBus
from minidsh.runtime.memory import MemoryService, RetrieveMemoryTool
from minidsh.runtime.memory_maintenance import memory_maintenance_tools
from minidsh.tools import PermissionPolicy, ToolRegistry, ToolRuntime
from minidsh.tools.runtime import ToolError

pytestmark = pytest.mark.integration


def write_acceptance_bundle(root: Path) -> MemoryEvidenceGrant:
    """Synthetic verifier fixtures, never a claim of real task acceptance."""
    files: dict[str, object] = {}
    for name, data in {
        "criterion.py": b"assert True  # synthetic component fixture\n",
        "result.txt": b"fixture result\n",
        "trace.jsonl": (
            b'{"schema_version":"1.0","name":"run_started","run_id":"run_one",'
            b'"step_id":0,"data":{}}\n'
            b'{"schema_version":"1.0","name":"run_finished","run_id":"run_one",'
            b'"step_id":1,"data":{"status":"finished"}}\n'
        ),
        "acceptance.json": json.dumps(
            {
                "schema_version": "1.0",
                "run_id": "run_one",
                "case_id": "synthetic_fixture",
                "source": "criterion.py",
                "task_success": True,
            }
        ).encode(),
    }.items():
        (root / name).write_bytes(data)
        files[name] = {"path": name, "sha256": hashlib.sha256(data).hexdigest()}
    document = json.dumps(
        {
            "schema_version": "1.0",
            "kind": "independent_acceptance",
            "project_id": "project_one",
            "applicable_version": "v1",
            "source_run_id": "run_one",
            "acceptance": files["acceptance.json"],
            "trace": files["trace.jsonl"],
            "criterion": files["criterion.py"],
            "artifacts": [files["result.txt"]],
        },
        sort_keys=True,
    ).encode()
    (root / "bundle.json").write_bytes(document)
    return MemoryEvidenceGrant(
        "verified_fixture", MemoryEvidenceFile("bundle.json", hashlib.sha256(document).hexdigest())
    )


@pytest.mark.asyncio
async def test_registered_evidence_checks_real_backing_files_without_exposing_their_contents(
    tmp_path: Path,
) -> None:
    workspace, private = tmp_path / "workspace", tmp_path / "private"
    workspace.mkdir()
    private.mkdir()
    grant = write_acceptance_bundle(private)
    source = JsonMemoryEvidenceSource(private, workspace=workspace, grants=(grant,))
    evidence = await source.resolve("verified_fixture")
    assert evidence.kind is MemoryEvidenceKind.INDEPENDENT_ACCEPTANCE
    assert evidence.sha256 == grant.document.sha256
    assert evidence.project_id == "project_one"
    assert evidence.applicable_version == "v1"
    assert evidence.source_run_id == "run_one"
    assert evidence.source_exists is True
    assert evidence.accepted is True
    assert "fixture result" not in repr(evidence)
    assert str(private) not in repr(evidence)


@pytest.mark.parametrize(
    "invalid",
    [
        "failed",
        "unknown",
        "coerced_boolean",
        "wrong_run",
        "wrong_criterion",
        "unfinished",
        "late_call",
    ],
)
@pytest.mark.asyncio
async def test_pinned_bytes_do_not_turn_invalid_or_incomplete_verifier_facts_into_acceptance(
    tmp_path: Path, invalid: str
) -> None:
    workspace, private = tmp_path / "workspace", tmp_path / "private"
    workspace.mkdir()
    private.mkdir()
    write_acceptance_bundle(private)
    bundle: dict[str, JsonValue] = json.loads((private / "bundle.json").read_bytes())
    field = "trace" if invalid in {"unfinished", "late_call"} else "acceptance"
    reference = bundle[field]
    assert isinstance(reference, dict)
    name = reference["path"]
    assert isinstance(name, str)
    if field == "trace":
        lines = (private / name).read_bytes().splitlines(keepends=True)
        if invalid == "unfinished":
            lines.pop()
        else:
            lines.append(
                b'{"schema_version":"1.0","name":"tool_call_started",'
                b'"run_id":"run_one","step_id":1,"data":{}}\n'
            )
        data = b"".join(lines)
    else:
        facts: dict[str, JsonValue] = json.loads((private / name).read_bytes())
        if invalid in {"failed", "unknown", "coerced_boolean"}:
            facts["task_success"] = {"failed": False, "unknown": None, "coerced_boolean": "true"}[
                invalid
            ]
        elif invalid == "wrong_run":
            facts["run_id"] = "another_run"
        else:
            facts["source"] = "different_criterion.py"
        data = json.dumps(facts).encode()
    (private / name).write_bytes(data)
    reference["sha256"] = hashlib.sha256(data).hexdigest()
    encoded = json.dumps(bundle, sort_keys=True).encode()
    (private / "bundle.json").write_bytes(encoded)
    grant = MemoryEvidenceGrant(
        "invalid_fixture", MemoryEvidenceFile("bundle.json", hashlib.sha256(encoded).hexdigest())
    )
    source = JsonMemoryEvidenceSource(private, workspace=workspace, grants=(grant,))
    with pytest.raises(MemoryEvidenceError):
        await source.resolve("invalid_fixture")


@pytest.mark.parametrize("target", ["bundle.json", "acceptance.json", "trace.jsonl", "result.txt"])
@pytest.mark.asyncio
async def test_changed_registered_files_are_refused_without_echoing_private_bytes(
    tmp_path: Path, target: str
) -> None:
    workspace, private = tmp_path / "workspace", tmp_path / "private"
    workspace.mkdir()
    private.mkdir()
    grant = write_acceptance_bundle(private)
    (private / target).write_text("fixture_private_diagnostic_93413")
    source = JsonMemoryEvidenceSource(private, workspace=workspace, grants=(grant,))
    with pytest.raises(MemoryEvidenceError) as caught:
        await source.resolve("verified_fixture")
    assert str(caught.value) == "memory_evidence_changed"
    assert "fixture_private_diagnostic_93413" not in str(caught.value)
    assert str(private) not in str(caught.value)


@pytest.mark.asyncio
async def test_an_evidence_id_cannot_select_an_unregistered_path_or_follow_a_symlink(
    tmp_path: Path,
) -> None:
    workspace, private = tmp_path / "workspace", tmp_path / "private"
    workspace.mkdir()
    private.mkdir()
    grant = write_acceptance_bundle(private)
    source = JsonMemoryEvidenceSource(private, workspace=workspace, grants=(grant,))
    with pytest.raises(MemoryEvidenceError, match="memory_evidence_not_granted"):
        await source.resolve("../bundle.json")
    original = tmp_path / "original.json"
    (private / "bundle.json").rename(original)
    (private / "bundle.json").symlink_to(original)
    with pytest.raises(MemoryEvidenceError, match="memory_evidence_denied"):
        await source.resolve("verified_fixture")


@pytest.mark.asyncio
async def test_environment_source_requires_a_pinned_operator_confirmation(tmp_path: Path) -> None:
    workspace, private = tmp_path / "workspace", tmp_path / "private"
    workspace.mkdir()
    private.mkdir()
    configuration = b'{"test_runner":"python3","network":"none"}\n'
    (private / "project.json").write_bytes(configuration)
    document = json.dumps(
        {
            "schema_version": "1.0",
            "kind": "operator_configuration",
            "project_id": "project_one",
            "applicable_version": "v1",
            "source_run_id": "confirmed_configuration",
            "configuration": {
                "path": "project.json",
                "sha256": hashlib.sha256(configuration).hexdigest(),
            },
            "confirmed_by": "fixture_operator",
            "confirmed_at": "2026-10-29T00:00:00+00:00",
        }
    ).encode()
    (private / "confirmation.json").write_bytes(document)
    grant = MemoryEvidenceGrant(
        "confirmed_environment",
        MemoryEvidenceFile("confirmation.json", hashlib.sha256(document).hexdigest()),
    )
    source = JsonMemoryEvidenceSource(private, workspace=workspace, grants=(grant,))
    evidence = await source.resolve("confirmed_environment")
    assert evidence.kind is MemoryEvidenceKind.OPERATOR_CONFIGURATION
    assert evidence.confirmed_by == "fixture_operator"
    assert evidence.confirmed_at == "2026-10-29T00:00:00+00:00"
    assert evidence.source_exists is True
    assert evidence.accepted is True
    assert "test_runner" not in repr(evidence)


@pytest.mark.asyncio
async def test_outside_run_tools_validate_review_approve_and_commit_with_real_evidence(
    tmp_path: Path,
) -> None:
    workspace, private = tmp_path / "workspace", tmp_path / "private"
    workspace.mkdir()
    private.mkdir()
    grant = write_acceptance_bundle(private)
    source = JsonMemoryEvidenceSource(private, workspace=workspace, grants=(grant,))
    database = private / "memory.sqlite3"
    store = SQLiteMemoryStore(database, allowed_root=private, workspace=workspace)
    proposal = MemoryEntry.propose(
        "memory_one",
        "project_one",
        MemoryKind.REPAIR,
        topic="assertion_diagnosis",
        content="Inspect assertion inputs before editing the source.",
        source_run_id="run_one",
        applicable_version="v1",
    )
    await store.create(proposal)
    service = MemoryService(
        store, project_id="project_one", applicable_version="v1", evidence=source
    )
    registry = ToolRegistry()
    for tool in memory_maintenance_tools(service, operator_id="fixture_operator"):
        registry.register(tool)
    denied = ToolRuntime(registry, PermissionPolicy(), EventBus())
    validate = ToolCall(
        "validate",
        "validate_memory",
        {"entry_id": "memory_one", "expected_revision": 1, "evidence_id": "verified_fixture"},
    )
    assert (await denied.execute(validate, "maintenance", 0)).status is ToolStatus.DENIED
    runtime = ToolRuntime(
        registry,
        PermissionPolicy(frozenset({"memory.maintain", "memory.evidence.read"})),
        EventBus(),
    )
    service.begin_run("run_one")
    refused = await runtime.execute(validate, "maintenance", 0)
    assert refused.error is not None
    assert refused.error.code == "memory_run_active"
    assert await store.get("project_one", "memory_one") == proposal
    service.end_run("run_one")
    assert (await runtime.execute(validate, "maintenance", 1)).status is ToolStatus.SUCCEEDED
    reviewed = await runtime.execute(
        ToolCall("review", "review_memory", {"entry_id": "memory_one"}), "maintenance", 2
    )
    assert reviewed.status is ToolStatus.SUCCEEDED
    assert isinstance(reviewed.result, dict)
    assert reviewed.result["content"] == proposal.content
    assert reviewed.result["evidence_sha256"] == grant.document.sha256
    approved = await runtime.execute(
        ToolCall(
            "approve",
            "approve_memory",
            {
                "entry_id": "memory_one",
                "expected_revision": 1,
                "content_sha256": reviewed.result["content_sha256"],
                "approved_at": "2026-10-29T00:00:00+00:00",
            },
        ),
        "maintenance",
        3,
    )
    assert approved.status is ToolStatus.SUCCEEDED
    assert isinstance(approved.result, dict)
    commit = ToolCall(
        "commit",
        "commit_memory",
        {
            "entry_id": "memory_one",
            "expected_revision": 1,
            "approval_sha256": approved.result["approval_sha256"],
        },
    )
    assert (await runtime.execute(commit, "maintenance", 4)).status is ToolStatus.SUCCEEDED
    assert (await runtime.execute(commit, "maintenance", 5)).status is ToolStatus.SUCCEEDED
    await store.close()
    reopened = SQLiteMemoryStore(database, allowed_root=private, workspace=workspace)
    committed = await reopened.get("project_one", "memory_one")
    assert committed is not None
    assert committed.status is MemoryStatus.COMMITTED
    assert committed.approved_by == "fixture_operator"
    assert committed.approved_revision == 1
    await reopened.close()


async def _maintenance_fixture(
    tmp_path: Path,
    *,
    paths: tuple[str, ...] = (),
    error_type: str | None = None,
) -> tuple[SQLiteMemoryStore, MemoryService, ToolRuntime, MemoryEntry]:
    workspace, private = tmp_path / "workspace", tmp_path / "private"
    workspace.mkdir()
    private.mkdir()
    grant = write_acceptance_bundle(private)
    source = JsonMemoryEvidenceSource(private, workspace=workspace, grants=(grant,))
    store = SQLiteMemoryStore(private / "memory.sqlite3", allowed_root=private, workspace=workspace)
    proposal = MemoryEntry.propose(
        "memory_one",
        "project_one",
        MemoryKind.REPAIR,
        topic="assertion_diagnosis",
        content="Inspect assertion inputs before editing the source.",
        source_run_id="run_one",
        applicable_version="v1",
        paths=paths,
        error_type=error_type,
    )
    await store.create(proposal)
    service = MemoryService(
        store, project_id="project_one", applicable_version="v1", evidence=source
    )
    registry = ToolRegistry()
    for tool in memory_maintenance_tools(service, operator_id="fixture_operator"):
        registry.register(tool)
    runtime = ToolRuntime(
        registry,
        PermissionPolicy(frozenset({"memory.maintain", "memory.evidence.read"})),
        EventBus(),
    )
    return store, service, runtime, proposal


async def _commit_fixture_with_tools(runtime: ToolRuntime, proposal: MemoryEntry) -> None:
    validated = await runtime.execute(
        ToolCall(
            "validate",
            "validate_memory",
            {"entry_id": "memory_one", "expected_revision": 1, "evidence_id": "verified_fixture"},
        ),
        "maintenance",
        1,
    )
    assert validated.status is ToolStatus.SUCCEEDED
    approved = await runtime.execute(
        ToolCall(
            "approve",
            "approve_memory",
            {
                "entry_id": "memory_one",
                "expected_revision": 1,
                "content_sha256": proposal.content_sha256,
                "approved_at": "2026-10-29T00:00:00+00:00",
            },
        ),
        "maintenance",
        2,
    )
    assert approved.status is ToolStatus.SUCCEEDED
    assert isinstance(approved.result, dict)
    committed = await runtime.execute(
        ToolCall(
            "commit",
            "commit_memory",
            {
                "entry_id": "memory_one",
                "expected_revision": 1,
                "approval_sha256": approved.result["approval_sha256"],
            },
        ),
        "maintenance",
        3,
    )
    assert committed.status is ToolStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_permissioned_retrieval_populates_only_a_run_owned_readonly_view(
    tmp_path: Path,
) -> None:
    store, service, runtime, proposal = await _maintenance_fixture(tmp_path)
    await _commit_fixture_with_tools(runtime, proposal)
    service.begin_run("run_two")
    registry = ToolRegistry()
    registry.register(RetrieveMemoryTool(service))
    call = ToolCall("retrieve", "retrieve_memory", {})
    denied = ToolRuntime(registry, PermissionPolicy(), EventBus())
    assert (await denied.execute(call, "run_two", 1)).status is ToolStatus.DENIED
    assert service.selection_for("run_two") is None
    reader = ToolRuntime(
        registry,
        PermissionPolicy(frozenset({"memory.read", "memory.evidence.read"})),
        EventBus(),
    )
    retrieved = await reader.execute(call, "run_two", 2)
    assert retrieved.status is ToolStatus.SUCCEEDED
    assert proposal.content not in str(retrieved.result)
    assert "criterion.py" not in str(retrieved.result)
    selection = service.selection_for("run_two")
    assert selection is not None
    assert len(selection.entries) == 1
    assert selection.entries[0].content == proposal.content
    assert selection.entries[0].status is MemoryStatus.COMMITTED
    assert selection.entries[0].evidence_sha256 is not None
    assert service.selection_for("another_run") is None
    service.end_run("run_two")
    assert service.selection_for("run_two") is None
    await store.close()


@pytest.mark.parametrize("source_state", ["changed", "ungranted"])
@pytest.mark.asyncio
async def test_retrieval_omits_unverifiable_committed_records_without_mutating_them(
    tmp_path: Path, source_state: str
) -> None:
    store, service, runtime, proposal = await _maintenance_fixture(tmp_path)
    await _commit_fixture_with_tools(runtime, proposal)
    reason = "memory_evidence_changed"
    if source_state == "changed":
        (tmp_path / "private" / "result.txt").write_text("Changed after commit.")
    else:
        service = MemoryService(store, project_id="project_one", applicable_version="v1")
        reason = "memory_evidence_unavailable"
    service.begin_run("run_two")
    registry = ToolRegistry()
    registry.register(RetrieveMemoryTool(service))
    reader = ToolRuntime(
        registry,
        PermissionPolicy(frozenset({"memory.read", "memory.evidence.read"})),
        EventBus(),
    )
    result = await reader.execute(ToolCall("read", "retrieve_memory", {}), "run_two", 1)
    assert result.status is ToolStatus.SUCCEEDED
    selected = service.selection_for("run_two")
    assert selected is not None
    assert selected.entries == ()
    assert [(item.entry_id, item.reason) for item in selected.omissions] == [("memory_one", reason)]
    assert proposal.content not in str(result.result)
    stored = await store.get("project_one", "memory_one")
    assert stored is not None
    assert stored.status is MemoryStatus.COMMITTED
    service.end_run("run_two")
    await store.close()


@pytest.mark.asyncio
async def test_context_prepares_memory_through_tools_then_reuses_a_readonly_run_view(
    tmp_path: Path,
) -> None:
    store, service, maintenance, proposal = await _maintenance_fixture(tmp_path)
    await _commit_fixture_with_tools(maintenance, proposal)
    service.begin_run("run_two")
    registry = ToolRegistry()
    registry.register(RetrieveMemoryTool(service))
    tools = ToolRuntime(
        registry, PermissionPolicy(frozenset({"memory.read", "memory.evidence.read"})), EventBus()
    )
    assembly = ContextAssembly(FullHistoryContext(), memory=service, tools=tools)
    history = (Message(Role.SYSTEM, "root constraint"), Message(Role.USER, "current task"))
    accounting: list[tuple[int, float]] = []

    def account(calls: int, milliseconds: float) -> None:
        accounting.append((calls, milliseconds))

    prepared = await assembly.prepare(history, "current task", "run_two", 1, 10, account)
    assert prepared.tool_calls == 1
    assert sum(calls for calls, _duration in accounting) == 1
    assert prepared.view.messages[:2] == history
    memory = [message for message in prepared.view.messages if message.name == "project_memory"]
    assert len(memory) == 1
    assert memory[0].role is Role.USER
    assert proposal.content in memory[0].content
    assert "criterion.py" not in memory[0].content
    sources = prepared.view.metadata["sources"]
    assert isinstance(sources, list)
    source = next(
        item
        for item in sources
        if isinstance(item, dict) and item["source_id"] == "memory:memory_one"
    )
    assert isinstance(source, dict)
    assert source["revision"] == 1
    assert source["trust"] == "project_data"
    assert source["evidence_sha256"] is not None
    cached = await assembly.prepare(history, "current task", "run_two", 2, 10)
    assert cached.tool_calls == 0
    assert cached.view.messages == prepared.view.messages
    assert history == (Message(Role.SYSTEM, "root constraint"), Message(Role.USER, "current task"))
    service.end_run("run_two")
    await store.close()


@pytest.mark.asyncio
async def test_context_matches_memory_to_observed_file_paths_and_structured_error_feedback(
    tmp_path: Path,
) -> None:
    store, service, maintenance, proposal = await _maintenance_fixture(
        tmp_path, paths=("src/value.py",), error_type="AssertionError"
    )
    await _commit_fixture_with_tools(maintenance, proposal)
    service.begin_run("run_two")
    registry = ToolRegistry()
    registry.register(RetrieveMemoryTool(service))
    tools = ToolRuntime(
        registry, PermissionPolicy(frozenset({"memory.read", "memory.evidence.read"})), EventBus()
    )
    assembly = ContextAssembly(FullHistoryContext(), memory=service, tools=tools)
    task = "repair src/value.py"
    history = (Message(Role.SYSTEM, "root constraint"), Message(Role.USER, task))
    first = await assembly.prepare(history, task, "run_two", 1, 10)
    assert not any(message.name == "project_memory" for message in first.view.messages)
    observed = (
        *history,
        Message(
            Role.ASSISTANT,
            "",
            tool_calls=(ToolCall("read", "read_file", {"path": "src/value.py"}),),
        ),
        Message(
            Role.TOOL, '{"status":"succeeded","result":{}}', name="read_file", tool_call_id="read"
        ),
        Message(Role.ASSISTANT, "", tool_calls=(ToolCall("test", "test", {}),)),
        Message(
            Role.TOOL,
            '{"status":"succeeded","result":{"exit_code":1,"stderr":"AssertionError: fixture"}}',
            name="test",
            tool_call_id="test",
        ),
    )
    second = await assembly.prepare(observed, task, "run_two", 2, 10)
    assert second.tool_calls == 1
    assert any(proposal.content in message.content for message in second.view.messages)
    selection = service.selection_for(
        "run_two", paths=("src/value.py",), error_type="AssertionError"
    )
    assert selection is not None
    assert [entry.entry_id for entry in selection.entries] == ["memory_one"]
    service.end_run("run_two")
    await store.close()


@pytest.mark.parametrize("stage", ["approve", "commit"])
@pytest.mark.asyncio
async def test_changed_evidence_invalidates_authority_before_approval_or_commit(
    tmp_path: Path, stage: str
) -> None:
    store, _service, runtime, proposal = await _maintenance_fixture(tmp_path)
    validated = await runtime.execute(
        ToolCall(
            "validate",
            "validate_memory",
            {"entry_id": "memory_one", "expected_revision": 1, "evidence_id": "verified_fixture"},
        ),
        "maintenance",
        1,
    )
    assert validated.status is ToolStatus.SUCCEEDED
    approve = ToolCall(
        "approve",
        "approve_memory",
        {
            "entry_id": "memory_one",
            "expected_revision": 1,
            "content_sha256": proposal.content_sha256,
            "approved_at": "2026-10-29T00:00:00+00:00",
        },
    )
    action = approve
    expected_status = MemoryStatus.VALIDATED
    if stage == "commit":
        approved = await runtime.execute(approve, "maintenance", 2)
        assert approved.status is ToolStatus.SUCCEEDED
        assert isinstance(approved.result, dict)
        action = ToolCall(
            "commit",
            "commit_memory",
            {
                "entry_id": "memory_one",
                "expected_revision": 1,
                "approval_sha256": approved.result["approval_sha256"],
            },
        )
        expected_status = MemoryStatus.APPROVED
    (tmp_path / "private" / "result.txt").write_text("Changed after validation.\n")
    refused = await runtime.execute(action, "maintenance", 3)
    assert refused.status is ToolStatus.FAILED
    assert refused.error is not None
    assert refused.error.code == "memory_evidence_changed"
    stored = await store.get("project_one", "memory_one")
    assert stored is not None
    assert stored.status is expected_status
    await store.close()


@pytest.mark.parametrize("stage", ["validate", "approve", "commit"])
@pytest.mark.asyncio
async def test_conflicting_validated_topic_blocks_each_authorizing_transition(
    tmp_path: Path, stage: str
) -> None:
    store, _service, runtime, proposal = await _maintenance_fixture(tmp_path)
    validate = ToolCall(
        "validate",
        "validate_memory",
        {"entry_id": "memory_one", "expected_revision": 1, "evidence_id": "verified_fixture"},
    )
    approve = ToolCall(
        "approve",
        "approve_memory",
        {
            "entry_id": "memory_one",
            "expected_revision": 1,
            "content_sha256": proposal.content_sha256,
            "approved_at": "2026-10-29T00:00:00+00:00",
        },
    )
    action = validate
    if stage in {"approve", "commit"}:
        assert (await runtime.execute(validate, "maintenance", 1)).status is ToolStatus.SUCCEEDED
        action = approve
    if stage == "commit":
        approved = await runtime.execute(approve, "maintenance", 2)
        assert approved.status is ToolStatus.SUCCEEDED
        assert isinstance(approved.result, dict)
        action = ToolCall(
            "commit",
            "commit_memory",
            {
                "entry_id": "memory_one",
                "expected_revision": 1,
                "approval_sha256": approved.result["approval_sha256"],
            },
        )
    before = await store.get("project_one", "memory_one")
    # Simulate a conflicting existing record at the persistence boundary.
    other = replace(proposal, entry_id="conflicting_memory", content="A contradictory diagnosis.")
    await store.create(other)
    bundle_sha = hashlib.sha256((tmp_path / "private" / "bundle.json").read_bytes()).hexdigest()
    conflicting = other.validate(
        MemoryEvidence(
            bundle_sha,
            "project_one",
            "v1",
            MemoryEvidenceKind.INDEPENDENT_ACCEPTANCE,
            "run_one",
            accepted=True,
            source_exists=True,
        )
    )
    # A trusted fixture injects a legacy inconsistent database. New Store writes
    # now reject this state atomically, while service checks must still handle it.
    with sqlite3.connect(tmp_path / "private" / "memory.sqlite3") as connection:
        connection.execute(
            "UPDATE entries SET payload=? WHERE project_id=? AND entry_id=?",
            (json.dumps(asdict(conflicting)), "project_one", other.entry_id),
        )
    result = await runtime.execute(action, "maintenance", 3)
    assert result.status is ToolStatus.FAILED
    assert result.error is not None
    assert result.error.code == "memory_topic_conflict"
    assert await store.get("project_one", "memory_one") == before
    await store.close()


@pytest.mark.asyncio
async def test_operator_edit_and_rejection_remove_old_authority_through_the_tool_pipeline(
    tmp_path: Path,
) -> None:
    store, _service, runtime, proposal = await _maintenance_fixture(tmp_path)
    validate_args: dict[str, JsonValue] = {
        "entry_id": "memory_one",
        "expected_revision": 1,
        "evidence_id": "verified_fixture",
    }
    assert (
        await runtime.execute(
            ToolCall("validate", "validate_memory", validate_args), "maintenance", 1
        )
    ).status is ToolStatus.SUCCEEDED
    approved = await runtime.execute(
        ToolCall(
            "approve",
            "approve_memory",
            {
                "entry_id": "memory_one",
                "expected_revision": 1,
                "content_sha256": proposal.content_sha256,
                "approved_at": "2026-10-29T00:00:00+00:00",
            },
        ),
        "maintenance",
        2,
    )
    assert approved.status is ToolStatus.SUCCEEDED
    assert isinstance(approved.result, dict)
    revised = await runtime.execute(
        ToolCall(
            "revise",
            "revise_memory",
            {"entry_id": "memory_one", "expected_revision": 1, "content": "A revised diagnosis."},
        ),
        "maintenance",
        3,
    )
    assert revised.status is ToolStatus.SUCCEEDED
    assert isinstance(revised.result, dict)
    assert revised.result["revision"] == 2
    assert revised.result["status"] == "proposed"
    assert revised.result["evidence_sha256"] is None
    assert revised.result["approved_by"] is None
    assert revised.result["approval_sha256"] is None
    stale = await runtime.execute(
        ToolCall(
            "old_commit",
            "commit_memory",
            {
                "entry_id": "memory_one",
                "expected_revision": 1,
                "approval_sha256": approved.result["approval_sha256"],
            },
        ),
        "maintenance",
        4,
    )
    assert stale.status is ToolStatus.FAILED
    assert stale.error is not None
    assert stale.error.code == "memory_revision_stale"
    validate_args["expected_revision"] = 2
    assert (
        await runtime.execute(
            ToolCall("validate2", "validate_memory", validate_args), "maintenance", 5
        )
    ).status is ToolStatus.SUCCEEDED
    reject = ToolCall("reject", "reject_memory", {"entry_id": "memory_one", "expected_revision": 2})
    assert (await runtime.execute(reject, "maintenance", 6)).status is ToolStatus.SUCCEEDED
    assert (await runtime.execute(reject, "maintenance", 7)).status is ToolStatus.SUCCEEDED
    stored = await store.get("project_one", "memory_one")
    assert stored is not None
    assert stored.status is MemoryStatus.REJECTED
    assert stored.revision == 2
    assert stored.evidence_sha256 is None
    assert stored.validation_sha256 is None
    assert stored.approval_sha256 is None
    await store.close()


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("review_memory", {"entry_id": "memory_one"}),
        (
            "validate_memory",
            {"entry_id": "memory_one", "expected_revision": 1, "evidence_id": "verified_fixture"},
        ),
        (
            "approve_memory",
            {
                "entry_id": "memory_one",
                "expected_revision": 1,
                "content_sha256": "a" * 64,
                "approved_at": "2026-10-29T00:00:00+00:00",
            },
        ),
        (
            "commit_memory",
            {"entry_id": "memory_one", "expected_revision": 1, "approval_sha256": "a" * 64},
        ),
        (
            "revise_memory",
            {"entry_id": "memory_one", "expected_revision": 1, "content": "A revised diagnosis."},
        ),
        ("reject_memory", {"entry_id": "memory_one", "expected_revision": 1}),
    ],
)
@pytest.mark.asyncio
async def test_every_maintenance_tool_is_unavailable_throughout_an_active_run(
    tmp_path: Path, name: str, arguments: dict[str, JsonValue]
) -> None:
    store, service, runtime, proposal = await _maintenance_fixture(tmp_path)
    service.begin_run("run_one")
    result = await runtime.execute(ToolCall("active", name, arguments), "maintenance", 1)
    assert result.status is ToolStatus.FAILED
    assert result.error is not None
    assert result.error.code == "memory_run_active"
    assert await store.get("project_one", "memory_one") == proposal
    service.end_run("run_one")
    await store.close()


@pytest.mark.asyncio
async def test_operator_identity_cannot_be_overridden_in_approval_arguments(tmp_path: Path) -> None:
    store, _service, runtime, proposal = await _maintenance_fixture(tmp_path)
    result = await runtime.execute(
        ToolCall(
            "impersonate",
            "approve_memory",
            {
                "entry_id": "memory_one",
                "expected_revision": 1,
                "content_sha256": proposal.content_sha256,
                "approved_at": "2026-10-29T00:00:00+00:00",
                "approver": "impostor_fixture",
            },
        ),
        "maintenance",
        1,
    )
    assert result.status is ToolStatus.INVALID
    assert "impostor_fixture" not in str(result.error)
    assert await store.get("project_one", "memory_one") == proposal
    await store.close()


@pytest.mark.asyncio
async def test_cancelled_evidence_io_releases_maintenance_without_starting_a_run_mid_validation(
    tmp_path: Path,
) -> None:
    store, _service, _runtime, proposal = await _maintenance_fixture(tmp_path)
    private = tmp_path / "private"
    grant = MemoryEvidenceGrant(
        "verified_fixture",
        MemoryEvidenceFile(
            "bundle.json", hashlib.sha256((private / "bundle.json").read_bytes()).hexdigest()
        ),
    )
    source = JsonMemoryEvidenceSource(private, workspace=tmp_path / "workspace", grants=(grant,))
    entered, release = asyncio.Event(), asyncio.Event()

    class PausedEvidenceIO:
        async def resolve(self, evidence_id: str) -> MemoryEvidence:
            entered.set()
            await release.wait()
            return await source.resolve(evidence_id)

        async def resolve_sha256(self, sha256: str) -> MemoryEvidence:
            return await source.resolve_sha256(sha256)

    service = MemoryService(
        store, project_id="project_one", applicable_version="v1", evidence=PausedEvidenceIO()
    )
    pending = asyncio.create_task(
        service.validate("memory_one", expected_revision=1, evidence_id="verified_fixture")
    )
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(ToolError, match="memory_maintenance_active"):
            service.begin_run("run_one")
        with pytest.raises(ToolError, match="memory_maintenance_active"):
            await service.review("memory_one")
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    finally:
        release.set()
    assert await store.get("project_one", "memory_one") == proposal
    service.begin_run("run_one")
    service.end_run("run_one")
    await store.close()
