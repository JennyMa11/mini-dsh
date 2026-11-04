"""Project Memory tools use trusted run scope and the shared permission pipeline."""

import hashlib
from pathlib import Path

import pytest

from minidsh.adapters.memory import SQLiteMemoryStore
from minidsh.adapters.trace import JsonlTraceWriter
from minidsh.domain import JsonValue, Message, Role, ToolCall, ToolStatus
from minidsh.domain.memory import MemoryStatus, MemoryValidationError
from minidsh.runtime import EventBus
from minidsh.runtime.memory import MemoryService, ProposeMemoryTool
from minidsh.tools import PermissionPolicy, ToolRegistry, ToolRuntime
from minidsh.tools.runtime import ToolError

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_unreadable_observation_closes_the_proposal_guard_for_the_run(tmp_path: Path) -> None:
    workspace, private = tmp_path / "workspace", tmp_path / "private"
    workspace.mkdir()
    private.mkdir()
    store = SQLiteMemoryStore(private / "memory.sqlite3", allowed_root=private, workspace=workspace)
    service = MemoryService(store, project_id="project_one", applicable_version="v1")
    service.begin_run("actor_one")
    try:
        with pytest.raises(MemoryValidationError, match="memory_content_invalid"):
            service.observe_context("actor_one", (Message(Role.TOOL, "\ud800"),))
        registry = ToolRegistry()
        registry.register(ProposeMemoryTool(service))
        runtime = ToolRuntime(registry, PermissionPolicy(frozenset({"memory.propose"})), EventBus())
        result = await runtime.execute(
            ToolCall(
                "propose",
                "propose_memory",
                {"kind": "diagnosis", "topic": "summary", "content": "A reusable summary."},
            ),
            "actor_one",
            1,
        )
        assert result.status is ToolStatus.FAILED
        assert result.error is not None
        assert result.error.code == "memory_content_invalid"
        assert await store.snapshot("project_one") == ()
    finally:
        service.end_run("actor_one")
        await store.close()


@pytest.mark.asyncio
async def test_permission_precedes_proposal_io_and_only_a_scoped_proposal_is_persisted(
    tmp_path: Path,
) -> None:
    workspace, private = tmp_path / "workspace", tmp_path / "private"
    workspace.mkdir()
    private.mkdir()
    database = private / "memory.sqlite3"
    store = SQLiteMemoryStore(database, allowed_root=private, workspace=workspace)
    service = MemoryService(store, project_id="project_one", applicable_version="v1")
    service.begin_run("run_one")
    registry = ToolRegistry()
    registry.register(ProposeMemoryTool(service))
    call = ToolCall(
        "proposal",
        "propose_memory",
        {
            "kind": "diagnosis",
            "topic": "assertion_diagnosis",
            "content": "Inspect assertion inputs first.",
            "paths": ["src/value.py"],
            "error_type": "AssertionError",
        },
    )
    denied = ToolRuntime(registry, PermissionPolicy(), EventBus())
    assert (await denied.execute(call, "run_one", 1)).status is ToolStatus.DENIED
    assert not database.exists()
    runtime = ToolRuntime(registry, PermissionPolicy(frozenset({"memory.propose"})), EventBus())
    result = await runtime.execute(call, "run_one", 1)
    assert result.status is ToolStatus.SUCCEEDED
    assert isinstance(result.result, dict)
    entry_id = result.result["entry_id"]
    assert isinstance(entry_id, str)
    assert "content" not in result.result
    assert str(private) not in str(result.result)
    stored = await store.get("project_one", entry_id)
    assert stored is not None
    assert stored.source_run_id == "run_one"
    assert stored.applicable_version == "v1"
    assert stored.status is MemoryStatus.PROPOSED
    assert stored.approved_by is None
    assert stored.evidence_sha256 is None
    assert stored.content == "Inspect assertion inputs first."
    assert await store.get("project_two", entry_id) is None
    unknown = await runtime.execute(ToolCall("approve", "approve_memory", {}), "run_one", 2)
    assert unknown.status is ToolStatus.INVALID
    assert await store.get("project_one", entry_id) == stored
    service.end_run("run_one")
    await store.close()


@pytest.mark.parametrize(
    "forged_field",
    [
        "project_id",
        "applicable_version",
        "source_run_id",
        "approved_by",
        "evidence_sha256",
        "status",
    ],
)
@pytest.mark.asyncio
async def test_model_cannot_choose_project_evidence_or_approval(
    tmp_path: Path, forged_field: str
) -> None:
    workspace, private = tmp_path / "workspace", tmp_path / "private"
    workspace.mkdir()
    private.mkdir()
    database = private / "memory.sqlite3"
    store = SQLiteMemoryStore(database, allowed_root=private, workspace=workspace)
    service = MemoryService(store, project_id="project_one", applicable_version="v1")
    service.begin_run("run_one")
    registry = ToolRegistry()
    registry.register(ProposeMemoryTool(service))
    runtime = ToolRuntime(registry, PermissionPolicy(frozenset({"memory.propose"})), EventBus())
    result = await runtime.execute(
        ToolCall(
            "forged",
            "propose_memory",
            {
                "kind": "repair",
                "topic": "assertion_diagnosis",
                "content": "Inspect assertion inputs first.",
                forged_field: "forged_authority",
            },
        ),
        "run_one",
        1,
    )
    assert result.status is ToolStatus.INVALID
    assert not database.exists()
    service.end_run("run_one")
    await store.close()


@pytest.mark.asyncio
async def test_inactive_or_mismatched_runs_cannot_propose_or_impersonate_execution(
    tmp_path: Path,
) -> None:
    workspace, private = tmp_path / "workspace", tmp_path / "private"
    workspace.mkdir()
    private.mkdir()
    database = private / "memory.sqlite3"
    store = SQLiteMemoryStore(database, allowed_root=private, workspace=workspace)
    service = MemoryService(store, project_id="project_one", applicable_version="v1")
    tool = ProposeMemoryTool(service)
    registry = ToolRegistry()
    registry.register(tool)
    runtime = ToolRuntime(registry, PermissionPolicy(frozenset({"memory.propose"})), EventBus())
    call = ToolCall(
        "proposal",
        "propose_memory",
        {
            "kind": "diagnosis",
            "topic": "assertion_diagnosis",
            "content": "Inspect assertion inputs.",
        },
    )
    before = await runtime.execute(call, "run_one", 1)
    assert before.status is ToolStatus.FAILED
    assert before.error is not None
    assert before.error.code == "memory_run_mismatch"
    service.begin_run("run_one")
    with pytest.raises(ToolError, match="memory_run_active"):
        service.begin_run("another_run")
    wrong = await runtime.execute(call, "another_run", 1)
    assert wrong.status is ToolStatus.FAILED
    with pytest.raises(ToolError, match="memory_run_mismatch"):
        service.end_run("another_run")
    service.end_run("run_one")
    after = await runtime.execute(call, "run_one", 2)
    assert after.status is ToolStatus.FAILED
    with pytest.raises(ToolError, match="memory_run_required"):
        await tool.execute(tool.input_model.model_validate(call.arguments))
    assert not database.exists()
    await store.close()


@pytest.mark.parametrize("field", ["content", "paths", "error_type"])
@pytest.mark.parametrize("encoding", ["assignment", "json", "escaped_json_key"])
@pytest.mark.asyncio
async def test_sensitive_summary_or_metadata_never_reaches_storage_or_trace(
    tmp_path: Path, field: str, encoding: str
) -> None:
    workspace, private = tmp_path / "workspace", tmp_path / "private"
    workspace.mkdir()
    private.mkdir()
    database = private / "memory.sqlite3"
    store = SQLiteMemoryStore(database, allowed_root=private, workspace=workspace)
    service = MemoryService(store, project_id="project_one", applicable_version="v1")
    service.begin_run("run_one")
    registry = ToolRegistry()
    registry.register(ProposeMemoryTool(service))
    events = EventBus()
    trace = private / "probe.jsonl"
    events.subscribe("*", JsonlTraceWriter(trace))
    runtime = ToolRuntime(registry, PermissionPolicy(frozenset({"memory.propose"})), events)
    arguments: dict[str, JsonValue] = {
        "kind": "diagnosis",
        "topic": "assertion_diagnosis",
        "content": "Inspect assertion inputs first.",
    }
    canary = "fixture_memory_canary_67463"
    sensitive = "password=" + canary
    if encoding == "json":
        sensitive = '{"password":"' + canary + '"}'
    elif encoding == "escaped_json_key":
        sensitive = r'{"pass\u0077ord":"' + canary + '"}'
    arguments[field] = ["src/" + sensitive + ".py"] if field == "paths" else sensitive
    result = await runtime.execute(ToolCall("secret", "propose_memory", arguments), "run_one", 1)
    assert result.status is ToolStatus.FAILED
    assert result.error is not None
    assert result.error.code in {"memory_sensitive_content", "memory_path_invalid"}
    assert canary not in str(result.error)
    assert canary not in trace.read_text()
    assert not database.exists()
    service.end_run("run_one")
    await store.close()


@pytest.mark.parametrize(
    "material", ["task", "tool_output", "named_tool_output", "hidden_label", "summary"]
)
@pytest.mark.asyncio
async def test_proposals_reject_known_raw_material_before_storage_and_allow_a_summary(
    tmp_path: Path, material: str
) -> None:
    workspace, private = tmp_path / "workspace", tmp_path / "private"
    workspace.mkdir()
    private.mkdir()
    database = private / "memory.sqlite3"
    store = SQLiteMemoryStore(database, allowed_root=private, workspace=workspace)
    hidden = "fixture_hidden_label_14821"
    service = MemoryService(
        store,
        project_id="project_one",
        applicable_version="v1",
        forbidden_content_sha256=(hashlib.sha256(hidden.encode()).hexdigest(),),
    )
    service.begin_run("run_one", task="Repair the example module.")
    service.observe_context(
        "run_one",
        (
            Message(Role.USER, "Repair the example module."),
            Message(
                Role.TOOL,
                '{"status":"succeeded","result":{"content":"Complete fixture output."}}',
                name="project_memory" if material == "named_tool_output" else "read_file",
                tool_call_id="fixture_read",
            ),
        ),
    )
    content = {
        "task": "Repair the example module.",
        "tool_output": "Complete fixture output.",
        "named_tool_output": "Complete fixture output.",
        "hidden_label": hidden,
        "summary": "Inspect assertion inputs before editing the implementation.",
    }[material]
    registry = ToolRegistry()
    registry.register(ProposeMemoryTool(service))
    runtime = ToolRuntime(registry, PermissionPolicy(frozenset({"memory.propose"})), EventBus())
    result = await runtime.execute(
        ToolCall(
            "material",
            "propose_memory",
            {
                "kind": "diagnosis",
                "topic": "assertion_diagnosis",
                "content": content,
            },
        ),
        "run_one",
        1,
    )
    if material == "summary":
        assert result.status is ToolStatus.SUCCEEDED
        assert database.exists()
    else:
        assert result.status is ToolStatus.FAILED
        assert result.error is not None
        assert result.error.code == (
            "memory_forbidden_content" if material == "hidden_label" else "memory_raw_material"
        )
        assert not database.exists()
        assert content not in str(result.error)
    service.end_run("run_one")
    await store.close()


@pytest.mark.asyncio
async def test_invalid_source_text_does_not_leave_a_half_started_memory_run(tmp_path: Path) -> None:
    workspace, private = tmp_path / "workspace", tmp_path / "private"
    workspace.mkdir()
    private.mkdir()
    store = SQLiteMemoryStore(private / "memory.sqlite3", allowed_root=private, workspace=workspace)
    service = MemoryService(store, project_id="project_one", applicable_version="v1")
    with pytest.raises(MemoryValidationError, match="memory_content_invalid"):
        service.begin_run("invalid", task="\ud800")
    service.begin_run("valid", task="Repair the example module.")
    service.end_run("valid")
    await store.close()
