"""Memory through the actual composition root and Agent Loop."""

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from minidsh.adapters import ScriptedModel
from minidsh.adapters.file_backend import LocalFileBackend
from minidsh.adapters.memory import SQLiteMemoryStore
from minidsh.adapters.memory_evidence import JsonMemoryEvidenceSource
from minidsh.composition import (
    RuntimeComponents,
    RuntimeOptions,
    build_local_runtime,
    plugin_runtime,
)
from minidsh.configured_runtime import build_configured_runtime
from minidsh.domain import AgentBudget, ModelResponse, RunStatus, TokenUsage, ToolCall, ToolStatus
from minidsh.domain.memory import (
    MemoryConfig,
    MemoryEntry,
    MemoryEvidenceFile,
    MemoryEvidenceGrant,
    MemoryKind,
    MemoryStatus,
)
from minidsh.domain.verification import VerificationConfig
from minidsh.runtime.hooks import HookRegistry
from minidsh.tools import PermissionPolicy

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_file_only_runtime_prepares_memory_and_persists_only_an_unapproved_proposal(
    tmp_path: Path,
) -> None:
    workspace, private = tmp_path / "workspace", tmp_path / "private"
    workspace.mkdir()
    private.mkdir()
    database = private / "memory.sqlite3"
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        "propose",
                        "propose_memory",
                        {
                            "kind": "diagnosis",
                            "topic": "assertion_diagnosis",
                            "content": "Inspect assertion inputs before changing code.",
                        },
                    ),
                )
            ),
            ModelResponse("done"),
        ]
    )
    runtime = build_local_runtime(
        workspace=workspace,
        model=model,
        options=RuntimeOptions(
            memory=MemoryConfig("project_one", "v1", str(database), str(private)),
            enabled_tools=frozenset({"read_file", "propose_memory"}),
        ),
    )
    result = await runtime.agent.run("Repair the example module.", run_id="actor_one")
    assert result.status is RunStatus.FINISHED
    assert result.tool_calls == 2  # hidden preparation plus the model's explicit proposal
    assert {tool.name for tool in model.requests[0].tools} == {"read_file", "propose_memory"}
    reply = next(message for message in result.history if message.tool_call_id == "propose")
    payload = json.loads(reply.content)
    entry_id = payload["result"]["entry_id"]
    assert payload["result"]["status"] == "proposed"
    assert all(
        message.name != "project_memory"
        for request in model.requests
        for message in request.messages
    )
    assert runtime.memory_maintenance is not None
    operator = runtime.memory_maintenance("fixture_operator")
    reviewed = await operator.execute(
        ToolCall("review", "review_memory", {"entry_id": entry_id}), "operator_one", 1
    )
    assert reviewed.status is ToolStatus.SUCCEEDED
    assert isinstance(reviewed.result, dict)
    assert reviewed.result["status"] == "proposed"
    assert reviewed.result["approved_by"] is None
    if runtime.close is not None:
        await runtime.close()
    store = SQLiteMemoryStore(database, allowed_root=private, workspace=workspace)
    records = await store.snapshot("project_one")
    assert len(records) == 1
    assert records[0].status is MemoryStatus.PROPOSED
    assert records[0].source_run_id == "actor_one"
    assert records[0].evidence_sha256 is None
    await store.close()


@pytest.mark.parametrize("route", ["configured", "plugin"])
@pytest.mark.asyncio
async def test_managed_routes_expose_the_same_memory_tools_and_operator_boundary(
    tmp_path: Path, route: str
) -> None:
    workspace, private = tmp_path / "workspace", tmp_path / "private"
    workspace.mkdir()
    private.mkdir()
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        "propose",
                        "propose_memory",
                        {
                            "kind": "diagnosis",
                            "topic": "assertion_diagnosis",
                            "content": "Inspect assertion inputs before changing code.",
                        },
                    ),
                )
            ),
            ModelResponse("done"),
        ]
    )
    options = RuntimeOptions(
        memory=MemoryConfig("project_one", "v1", str(private / "memory.sqlite3"), str(private)),
        enabled_tools=frozenset({"read_file", "propose_memory"}),
    )
    if route == "configured":
        runtime = build_configured_runtime(
            workspace=workspace, model=model, options=options, budget=AgentBudget()
        )
        assert "propose_memory" in {tool.name for tool in runtime.tools.definitions()}
        assert runtime.memory_maintenance is not None
        operator = runtime.memory_maintenance("fixture_operator")
        assert {tool.name for tool in operator.definitions()} == {
            "review_memory",
            "validate_memory",
            "approve_memory",
            "commit_memory",
            "revise_memory",
            "reject_memory",
        }
        result = await runtime.agent.run("Repair the example module.", run_id="actor_one")
        assert runtime.close is not None
        await runtime.close()
    else:
        async with plugin_runtime(
            workspace=workspace, model=model, options=options, plugins=()
        ) as runtime:
            result = await runtime.agent.run("Repair the example module.", run_id="actor_one")
            assert runtime.memory_maintenance is not None
    assert result.status is RunStatus.FINISHED
    assert result.tool_calls == 2
    assert "retrieve_memory" not in {tool.name for tool in model.requests[0].tools}
    assert "approve_memory" not in {tool.name for tool in model.requests[0].tools}


async def _seed_configuration_memory(tmp_path: Path) -> tuple[Path, MemoryConfig, MemoryEntry]:
    """Synthetic operator-confirmed policy; no claim of real benchmark acceptance."""
    workspace, private = tmp_path / "workspace", tmp_path / "private"
    workspace.mkdir()
    private.mkdir()
    evidence_root = private / "evidence"
    evidence_root.mkdir()
    body = "CURATED_POLICY: use the project test runner before finishing."
    configuration = json.dumps({"policy": body}).encode()
    (evidence_root / "project.json").write_bytes(configuration)
    document = json.dumps(
        {
            "schema_version": "1.0",
            "kind": "operator_configuration",
            "project_id": "project_one",
            "applicable_version": "v1",
            "source_run_id": "fixture_configuration",
            "configuration": {
                "path": "project.json",
                "sha256": hashlib.sha256(configuration).hexdigest(),
            },
            "confirmed_by": "fixture_operator",
            "confirmed_at": "2026-10-29T00:00:00+00:00",
        }
    ).encode()
    (evidence_root / "confirmation.json").write_bytes(document)
    grant = MemoryEvidenceGrant(
        "confirmed_configuration",
        MemoryEvidenceFile("confirmation.json", hashlib.sha256(document).hexdigest()),
    )
    config = MemoryConfig(
        "project_one",
        "v1",
        str(private / "memory.sqlite3"),
        str(private),
        str(evidence_root),
        (grant,),
    )
    reader = JsonMemoryEvidenceSource(evidence_root, workspace=workspace, grants=(grant,))
    proposed = MemoryEntry.propose(
        "seed_policy",
        "project_one",
        MemoryKind.ENVIRONMENT,
        topic="project_policy",
        content=body,
        source_run_id="fixture_configuration",
        applicable_version="v1",
    )
    validated = proposed.validate(await reader.resolve("confirmed_configuration"))
    approved = validated.approve(
        expected_revision=1,
        content_sha256=validated.content_sha256,
        approver="fixture_operator",
        approved_at="2026-10-29T00:00:00+00:00",
    )
    committed = approved.commit(expected_revision=1, approval_sha256=approved.approval_sha256)
    store = SQLiteMemoryStore(Path(config.database), allowed_root=private, workspace=workspace)
    await store.create(proposed)
    for before, after in [(proposed, validated), (validated, approved), (approved, committed)]:
        await store.replace(before, after)
    await store.close()
    return workspace, config, committed


@pytest.mark.parametrize(
    "strategy", ["full", "sliding", "summary", "retrieval", "retrieval_summary", "symbol", "budget"]
)
@pytest.mark.asyncio
async def test_all_context_strategies_consume_real_private_evidence_as_user_data(
    tmp_path: Path, strategy: str
) -> None:
    workspace, config, seed = await _seed_configuration_memory(tmp_path)
    model = ScriptedModel([ModelResponse("done"), ModelResponse("done again")])
    runtime = build_local_runtime(
        workspace=workspace,
        model=model,
        options=RuntimeOptions(
            memory=config, context_strategy=strategy, enabled_tools=frozenset({"read_file"})
        ),
    )
    first = await runtime.agent.run("Follow the project policy.", run_id="actor_one")
    assert first.status is RunStatus.FINISHED
    request = model.requests[0]
    messages = [message for message in request.messages if message.name == "project_memory"]
    assert len(messages) == 1
    assert messages[0].role.value == "user"
    assert seed.content in messages[0].content
    assert seed.evidence_sha256 is not None
    assert seed.evidence_sha256 in messages[0].content
    assert "confirmation.json" not in str(request.messages)
    assert "project.json" not in str(request.messages)
    assert all("CURATED_POLICY" not in message.content for message in first.history)
    second = await runtime.agent.run("Follow the project policy.", run_id="actor_two")
    assert second.status is RunStatus.FINISHED
    assert second.tool_calls == first.tool_calls
    assert any(seed.content in message.content for message in model.requests[1].messages)
    if runtime.close is not None:
        await runtime.close()


@pytest.mark.parametrize("stop", ["tool_budget", "input_budget", "permission"])
@pytest.mark.asyncio
async def test_memory_preparation_obeys_global_limits_before_model_and_releases_the_run(
    tmp_path: Path, stop: str
) -> None:
    workspace, config, _seed = await _seed_configuration_memory(tmp_path)
    before = hashlib.sha256(await asyncio.to_thread(Path(config.database).read_bytes)).hexdigest()
    model = ScriptedModel([ModelResponse("must not be called")])
    runtime = build_local_runtime(
        workspace=workspace,
        model=model,
        options=RuntimeOptions(
            memory=config,
            enabled_tools=frozenset({"read_file"}),
            context_input_budget=1 if stop == "input_budget" else 8192,
        ),
        budget=AgentBudget(
            max_tool_calls=1 if stop == "tool_budget" else 10,
        ),
        components=RuntimeComponents(
            permissions=PermissionPolicy() if stop == "permission" else None
        ),
    )
    try:
        expected = RunStatus.BUDGET_EXHAUSTED if stop == "tool_budget" else RunStatus.FAILED
        for run_id in ("first_attempt", "second_attempt"):
            result = await runtime.agent.run("Follow the project policy.", run_id=run_id)
            assert result.status is expected
            assert result.tool_calls == 1
            assert result.token_usage.total_tokens == 0
        assert model.requests == []
        assert (
            hashlib.sha256(await asyncio.to_thread(Path(config.database).read_bytes)).hexdigest()
            == before
        )
    finally:
        if runtime.close is not None:
            await runtime.close()


@pytest.mark.parametrize("usage", ["overrun", "unknown"])
@pytest.mark.asyncio
async def test_memory_cannot_write_after_model_usage_exhaustion(tmp_path: Path, usage: str) -> None:
    workspace, config, _seed = await _seed_configuration_memory(tmp_path)
    before = hashlib.sha256(await asyncio.to_thread(Path(config.database).read_bytes)).hexdigest()
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        "unexecuted_proposal",
                        "propose_memory",
                        {
                            "kind": "diagnosis",
                            "topic": "must_not_persist",
                            "content": "New summary.",
                        },
                    ),
                ),
                usage=TokenUsage(100, 20) if usage == "overrun" else TokenUsage(),
                usage_known=usage != "unknown",
            ),
            ModelResponse("must not be called"),
        ]
    )
    runtime = build_local_runtime(
        workspace=workspace,
        model=model,
        options=RuntimeOptions(
            memory=config, enabled_tools=frozenset({"read_file", "propose_memory"})
        ),
        budget=AgentBudget(max_tokens=100),
    )
    try:
        result = await runtime.agent.run("Propose a reusable summary.", run_id="actor_one")
        assert result.status is RunStatus.BUDGET_EXHAUSTED
        assert len(model.requests) == 1
        assert result.tool_calls == 1  # The prior hidden retrieval stays in the same budget.
        assert result.token_usage.total_tokens == (120 if usage == "overrun" else 0)
        assert result.unknown_usage_calls == (1 if usage == "unknown" else 0)
        assert (
            hashlib.sha256(await asyncio.to_thread(Path(config.database).read_bytes)).hexdigest()
            == before
        )
        assert runtime.memory_maintenance is not None
        review = await runtime.memory_maintenance("fixture_operator").execute(
            ToolCall("review", "review_memory", {"entry_id": "seed_policy"}), "operator_one", 1
        )
        assert review.status is ToolStatus.SUCCEEDED
    finally:
        if runtime.close is not None:
            await runtime.close()


@pytest.mark.parametrize("capability", ["default_git", "shell", "fixed_verification"])
def test_memory_refuses_unisolated_process_capabilities_before_any_run(
    tmp_path: Path, capability: str
) -> None:
    workspace, private = tmp_path / "workspace", tmp_path / "private"
    workspace.mkdir()
    private.mkdir()
    database = private / "memory.sqlite3"
    memory = MemoryConfig("project_one", "v1", str(database), str(private))
    options = RuntimeOptions(memory=memory)
    if capability == "shell":
        options = RuntimeOptions(
            memory=memory, allow_shell=True, enabled_tools=frozenset({"shell", "propose_memory"})
        )
    elif capability == "fixed_verification":
        options = RuntimeOptions(
            memory=memory,
            verification=VerificationConfig(("python", "check.py"), "visible_v1"),
            enabled_tools=frozenset({"test", "propose_memory"}),
        )
    model = ScriptedModel([ModelResponse("must not run")])
    with pytest.raises(ValueError, match="memory_requires_isolated_execution"):
        build_local_runtime(workspace=workspace, model=model, options=options)
    assert model.requests == []
    assert not database.exists()


def test_unattested_file_adapters_cannot_implicitly_claim_memory_isolation(tmp_path: Path) -> None:
    workspace, private = tmp_path / "workspace", tmp_path / "private"
    workspace.mkdir()
    private.mkdir()

    class UnknownFiles(LocalFileBackend):
        pass

    with pytest.raises(ValueError, match="memory_requires_isolated_execution"):
        build_local_runtime(
            workspace=workspace,
            model=ScriptedModel([]),
            options=RuntimeOptions(
                memory=MemoryConfig(
                    "project_one", "v1", str(private / "memory.sqlite3"), str(private)
                ),
                enabled_tools=frozenset({"read_file"}),
            ),
            components=RuntimeComponents(files=UnknownFiles((), workspace=workspace)),
        )


@pytest.mark.asyncio
async def test_operator_boundary_covers_start_end_hooks_and_releases_after_the_run(
    tmp_path: Path,
) -> None:
    workspace, config, _seed = await _seed_configuration_memory(tmp_path)
    started, release_start = asyncio.Event(), asyncio.Event()
    ended, release_end = asyncio.Event(), asyncio.Event()
    hooks = HookRegistry()

    async def on_start(_view: object) -> None:
        started.set()
        await release_start.wait()

    async def on_end(_result: object) -> None:
        ended.set()
        await release_end.wait()

    hooks.register_v2("boundary_observer", "1.0", on_run_start=on_start, on_run_end=on_end)
    runtime = build_local_runtime(
        workspace=workspace,
        model=ScriptedModel([ModelResponse("done")]),
        options=RuntimeOptions(memory=config, enabled_tools=frozenset({"read_file"})),
        components=RuntimeComponents(hooks=hooks),
    )
    assert runtime.memory_maintenance is not None
    operator = runtime.memory_maintenance("fixture_operator")
    review = ToolCall("review", "review_memory", {"entry_id": "seed_policy"})
    running = asyncio.create_task(
        runtime.agent.run("Follow the project policy.", run_id="actor_one")
    )
    try:
        await asyncio.wait_for(started.wait(), 1)
        before = await operator.execute(review, "operator_one", 1)
        assert before.error is not None
        assert before.error.code == "memory_run_active"
        release_start.set()
        await asyncio.wait_for(ended.wait(), 1)
        finishing = await operator.execute(review, "operator_one", 2)
        assert finishing.error is not None
        assert finishing.error.code == "memory_run_active"
    finally:
        release_start.set()
        release_end.set()
    result = await running
    assert result.status is RunStatus.FINISHED
    assert (await operator.execute(review, "operator_one", 3)).status is ToolStatus.SUCCEEDED
    if runtime.close is not None:
        await runtime.close()


@pytest.mark.parametrize("material", ["secret", "forbidden_label"])
@pytest.mark.asyncio
async def test_rejected_memory_arguments_are_not_echoed_into_the_next_model_request(
    tmp_path: Path, material: str
) -> None:
    workspace, private = tmp_path / "workspace", tmp_path / "private"
    workspace.mkdir()
    private.mkdir()
    canary = "fixture_private_value_961372"
    body = '{"password":"' + canary + '"}' if material == "secret" else canary
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        "propose",
                        "propose_memory",
                        {
                            "kind": "diagnosis",
                            "topic": "example_diagnosis",
                            "content": body,
                        },
                    ),
                )
            ),
            ModelResponse("done"),
        ]
    )
    runtime = build_local_runtime(
        workspace=workspace,
        model=model,
        options=RuntimeOptions(
            memory=MemoryConfig(
                "project_one",
                "v1",
                str(private / "memory.sqlite3"),
                str(private),
                forbidden_content_sha256=(hashlib.sha256(canary.encode()).hexdigest(),),
            ),
            enabled_tools=frozenset({"read_file", "propose_memory"}),
        ),
    )
    result = await runtime.agent.run("Propose a reusable project summary.", run_id="actor_one")
    assert result.status is RunStatus.FINISHED
    assert len(model.requests) == 2
    assert canary not in str(model.requests[1].messages)
    assert not (private / "memory.sqlite3").exists()
    second = model.requests[1].messages
    calls = [call for message in second for call in message.tool_calls]
    assert [(call.call_id, call.name) for call in calls] == [("propose", "propose_memory")]
    assert [message.tool_call_id for message in second if message.tool_call_id] == ["propose"]
    if runtime.close is not None:
        await runtime.close()


@pytest.mark.asyncio
async def test_raw_file_output_cannot_be_proposed_later_in_the_same_response(
    tmp_path: Path,
) -> None:
    workspace, private = tmp_path / "workspace", tmp_path / "private"
    workspace.mkdir()
    private.mkdir()
    raw = "A complete raw file output that must not become project knowledge."
    (workspace / "notes.txt").write_text(raw)
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall("read", "read_file", {"path": "notes.txt"}),
                    ToolCall(
                        "copy",
                        "propose_memory",
                        {"kind": "diagnosis", "topic": "raw_copy", "content": raw},
                    ),
                )
            ),
            ModelResponse("done"),
        ]
    )
    runtime = build_local_runtime(
        workspace=workspace,
        model=model,
        options=RuntimeOptions(
            memory=MemoryConfig("project_one", "v1", str(private / "memory.sqlite3"), str(private)),
            enabled_tools=frozenset({"read_file", "propose_memory"}),
        ),
    )
    try:
        result = await runtime.agent.run("Inspect notes and propose a summary.", run_id="actor_one")
        assert result.status is RunStatus.FINISHED
        reply = next(message for message in result.history if message.tool_call_id == "copy")
        assert json.loads(reply.content)["error"]["code"] == "memory_raw_material"
        assert not (private / "memory.sqlite3").exists()
    finally:
        if runtime.close is not None:
            await runtime.close()


@pytest.mark.asyncio
async def test_explicit_memory_store_is_selected_even_when_it_is_falsey(tmp_path: Path) -> None:
    workspace, private = tmp_path / "workspace", tmp_path / "private"
    workspace.mkdir()
    private.mkdir()

    class FalseyStore(SQLiteMemoryStore):
        def __bool__(self) -> bool:
            return False

    selected = private / "selected.sqlite3"
    fallback = private / "fallback.sqlite3"
    store = FalseyStore(selected, allowed_root=private, workspace=workspace)
    runtime = build_local_runtime(
        workspace=workspace,
        model=ScriptedModel(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "propose",
                            "propose_memory",
                            {
                                "kind": "diagnosis",
                                "topic": "selected_store",
                                "content": "Check function inputs before editing behavior.",
                            },
                        ),
                    )
                ),
                ModelResponse("done"),
            ]
        ),
        options=RuntimeOptions(
            memory=MemoryConfig("project_one", "v1", str(fallback), str(private)),
            enabled_tools=frozenset({"read_file", "propose_memory"}),
        ),
        components=RuntimeComponents(memory_store=store),
    )
    try:
        result = await runtime.agent.run("Propose a reusable summary.", run_id="actor_one")
        assert result.status is RunStatus.FINISHED
        assert len(await store.snapshot("project_one")) == 1
        assert selected.exists()
        assert not fallback.exists()
    finally:
        if runtime.close is not None:
            await runtime.close()
