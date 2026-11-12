"""Manual consolidation keeps real stored knowledge pending human authorization."""

import asyncio
import hashlib
import json
import threading
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from minidsh.adapters import ScriptedModel
from minidsh.adapters.memory import SQLiteMemoryStore
from minidsh.adapters.memory_evidence import JsonMemoryEvidenceSource
from minidsh.adapters.trace import JsonlTraceWriter
from minidsh.domain import (
    JsonValue,
    ModelCallError,
    ModelRequest,
    ModelResponse,
    TokenUsage,
    ToolCall,
    ToolStatus,
)
from minidsh.domain.memory import (
    MemoryEntry,
    MemoryEvidenceFile,
    MemoryEvidenceGrant,
    MemoryKind,
    MemoryStatus,
)
from minidsh.domain.memory_consolidation import ConsolidationFact, ConsolidationInput
from minidsh.domain.memory_notes import MemoryNoteSource, parse_memory_note
from minidsh.memory_consolidation import ConsolidateMemoryTool, ReviewConsolidationTool
from minidsh.runtime import EventBus
from minidsh.runtime.memory import MemoryService
from minidsh.tools import PermissionPolicy, ToolRegistry, ToolRuntime

pytestmark = pytest.mark.integration


async def _setup(
    root: Path,
    observer: Callable[[str], None] | None = None,
    forbidden: tuple[str, ...] = (),
) -> tuple[MemoryService, SQLiteMemoryStore, list[MemoryEntry]]:
    workspace, private = root / "workspace", root / "private"
    workspace.mkdir()
    private.mkdir()
    (workspace / "actor_trace.jsonl").write_text("original actor facts\n")
    (private / "config.toml").write_text("use_uv=true\n")
    grants = []
    for name in ("old_a", "old_b"):
        data = json.dumps(
            {
                "schema_version": "1.0",
                "kind": "operator_configuration",
                "project_id": "alpha",
                "applicable_version": "v1",
                "source_run_id": name,
                "configuration": {
                    "path": "config.toml",
                    "sha256": hashlib.sha256((private / "config.toml").read_bytes()).hexdigest(),
                },
                "confirmed_by": "fixture_operator",
                "confirmed_at": "2026-01-01T00:00:00+00:00",
            },
            sort_keys=True,
        ).encode()
        (private / (name + ".json")).write_bytes(data)
        grants.append(
            MemoryEvidenceGrant(
                name, MemoryEvidenceFile(name + ".json", hashlib.sha256(data).hexdigest())
            )
        )
    store = SQLiteMemoryStore(
        private / "memory.sqlite3",
        allowed_root=private,
        workspace=workspace,
        transaction_observer=observer,
    )
    service = MemoryService(
        store,
        project_id="alpha",
        applicable_version="v1",
        evidence=JsonMemoryEvidenceSource(private, workspace=workspace, grants=tuple(grants)),
        forbidden_content_sha256=forbidden,
    )
    await store.migrate_history()
    entries = []
    for name in ("old_a", "old_b"):
        entry = MemoryEntry.propose(
            name,
            "alpha",
            MemoryKind.ENVIRONMENT,
            topic=name,
            content="Use uv.",
            source_run_id=name,
            applicable_version="v1",
        )
        await store.create(entry)
        await service.validate(name, expected_revision=1, evidence_id=name)
        approved = await service.approve(
            name,
            expected_revision=1,
            content_sha256=entry.content_sha256,
            approver="fixture_operator",
            approved_at="2026-01-01T00:00:00+00:00",
        )
        entries.append(
            await service.commit(
                name, expected_revision=1, approval_sha256=approved.approval_sha256 or ""
            )
        )
    return service, store, entries


def _arguments(entries: list[MemoryEntry]) -> dict[str, JsonValue]:
    content = "Command checks completed without a local error."
    return {
        "materials_authorized": True,
        "redacted": True,
        "sources": [asdict(MemoryNoteSource.from_entry(e)) for e in entries],
        "facts": [
            {
                "fact_id": "fact_a",
                "source_run_id": "actor_a",
                "content": content,
                "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
            }
        ],
    }


def _proposal(**changes: JsonValue) -> ToolCall:
    return ToolCall(
        "draft",
        "propose_consolidated_note",
        {
            "kind": "environment",
            "topic": "combined",
            "trigger": "For project commands.",
            "rule": "Use uv.",
            "verification": "Run explicit project checks.",
            **changes,
        },
    )


def _runtime(
    root: Path,
    service: MemoryService,
    model: ScriptedModel,
    *,
    allowed: bool = True,
    denied: frozenset[str] = frozenset(),
) -> ToolRuntime:
    bus = EventBus()
    bus.subscribe("*", JsonlTraceWriter(root / "memory_operator_trace.jsonl"))
    tool = ConsolidateMemoryTool(
        service,
        model_factory=lambda: model,
        events=bus,
        operator_id="fixture_operator",
        denied_tools=denied,
    )
    registry = ToolRegistry()
    registry.register(tool)
    return ToolRuntime(
        registry,
        PermissionPolicy(
            frozenset(tool.definition.permissions) if allowed else frozenset(), denied
        ),
        bus,
        max_output_chars=32000,
    )


@pytest.mark.parametrize("value", [1, "true", None])
@pytest.mark.asyncio
async def test_c02_authorization_requires_actual_boolean_true(
    tmp_path: Path, value: JsonValue
) -> None:
    service, store, entries = await _setup(tmp_path)
    args = _arguments(entries)
    args["materials_authorized"] = value
    model = ScriptedModel([ModelResponse("must not be called")])
    try:
        result = await _runtime(tmp_path, service, model).execute(
            ToolCall("manual", "consolidate_memory", args), "operator", 1
        )
        assert result.status is ToolStatus.INVALID
        assert model.requests == []
    finally:
        await store.close()


@pytest.mark.parametrize(
    "tool", ["consolidate_memory", "load_consolidation_inputs", "propose_consolidated_note"]
)
@pytest.mark.asyncio
async def test_c03_deny_rules_apply_to_outer_load_and_proposal(tmp_path: Path, tool: str) -> None:
    service, store, entries = await _setup(tmp_path)
    model = ScriptedModel([ModelResponse(tool_calls=(_proposal(),)), ModelResponse("done")])
    before = (tmp_path / "private/memory.sqlite3").read_bytes()
    try:
        result = await _runtime(tmp_path, service, model, denied=frozenset({tool})).execute(
            ToolCall("manual", "consolidate_memory", _arguments(entries)), "operator", 1
        )
        assert (tmp_path / "private/memory.sqlite3").read_bytes() == before
        if tool != "propose_consolidated_note":
            assert model.requests == []
        else:
            assert result.status is ToolStatus.SUCCEEDED
            assert isinstance(result.result, dict)
            assert result.result["candidates"] == []
    finally:
        await store.close()


@pytest.mark.parametrize("change", ["revision", "fingerprint", "evidence"])
@pytest.mark.asyncio
async def test_c04_current_source_is_rechecked_before_exposure(tmp_path: Path, change: str) -> None:
    service, store, entries = await _setup(tmp_path)
    args = _arguments(entries)
    if change == "revision":
        await service.revise(entries[0].entry_id, expected_revision=1, content="Use a new command.")
    elif change == "evidence":
        (tmp_path / "private/config.toml").write_text("changed=true\n")
    else:
        sources = args["sources"]
        assert isinstance(sources, list)
        assert isinstance(sources[0], dict)
        sources[0]["proposal_sha256"] = "0" * 64
    before = (tmp_path / "private/memory.sqlite3").read_bytes()
    model = ScriptedModel([ModelResponse("not exposed")])
    try:
        result = await _runtime(tmp_path, service, model).execute(
            ToolCall("manual", "consolidate_memory", args), "operator", 1
        )
        assert result.status is ToolStatus.SUCCEEDED
        assert isinstance(result.result, dict)
        assert result.result["status"] == "failed"
        assert result.result["candidates"] == []
        assert model.requests == []
        assert (tmp_path / "private/memory.sqlite3").read_bytes() == before
        service.begin_run("next_actor")
        service.end_run("next_actor")
    finally:
        await store.close()


@pytest.mark.parametrize("budget", ["tools", "steps", "tokens", "unknown"])
@pytest.mark.asyncio
async def test_c05_budget_uses_existing_loop_and_prevents_followup_writes(
    tmp_path: Path, budget: str
) -> None:
    service, store, entries = await _setup(tmp_path)
    args = _arguments(entries)
    if budget == "tools":
        args["max_tool_calls"] = 1
    if budget == "steps":
        args["max_steps"] = 1
    if budget == "tokens":
        args["max_tokens"] = 10
    response = ModelResponse(
        tool_calls=(_proposal(),),
        usage=TokenUsage(input_tokens=20, output_tokens=2),
        usage_known=budget != "unknown",
    )
    model = ScriptedModel([response, ModelResponse("done")])
    try:
        result = await _runtime(tmp_path, service, model).execute(
            ToolCall("manual", "consolidate_memory", args), "operator", 1
        )
        assert result.status is ToolStatus.SUCCEEDED
        assert isinstance(result.result, dict)
        assert result.result["status"] == "budget_exhausted"
        assert len(model.requests) == (0 if budget == "tools" else 1)
        if budget == "steps":
            pending = [
                e for e in await store.snapshot("alpha") if e.status is MemoryStatus.PROPOSED
            ]
            assert len(pending) == 1
        else:
            assert await store.snapshot("alpha") == tuple(entries)
        if budget == "tokens":
            assert result.result["token_overrun"] == 12
        if budget == "unknown":
            assert result.result["unknown_usage_calls"] == 1
        for old in entries:
            assert await store.get("alpha", old.entry_id) == old
    finally:
        await store.close()


@pytest.mark.parametrize(
    "attempt", ["raw_fact", "source", "accepted", "maintenance", "hidden_loader", "shell", "file"]
)
@pytest.mark.asyncio
async def test_c07_model_cannot_escalate_authority_or_store_raw_facts(
    tmp_path: Path, attempt: str
) -> None:
    service, store, entries = await _setup(tmp_path)
    args = _arguments(entries)
    if attempt == "raw_fact":
        facts = args["facts"]
        assert isinstance(facts, list)
        assert isinstance(facts[0], dict)
        call = _proposal(rule=facts[0]["content"])
    elif attempt == "source":
        call = _proposal(sources=[asdict(MemoryNoteSource.from_entry(entries[0]))])
    elif attempt == "accepted":
        call = _proposal(accepted=True)
    else:
        call = ToolCall(
            "escalate",
            {
                "maintenance": "commit_memory",
                "hidden_loader": "load_consolidation_inputs",
                "shell": "shell",
                "file": "read_file",
            }[attempt],
            {},
        )
    model = ScriptedModel([ModelResponse(tool_calls=(call,)), ModelResponse("done")])
    before = (tmp_path / "private/memory.sqlite3").read_bytes()
    try:
        result = await _runtime(tmp_path, service, model).execute(
            ToolCall("manual", "consolidate_memory", args), "operator", 1
        )
        assert result.status is ToolStatus.SUCCEEDED
        assert isinstance(result.result, dict)
        assert result.result["candidates"] == []
        assert (tmp_path / "private/memory.sqlite3").read_bytes() == before
        tool_results = [m for r in model.requests for m in r.messages if m.tool_call_id]
        assert tool_results
        assert any(json.loads(m.content)["status"] in ("invalid", "failed") for m in tool_results)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_c06_cancelled_model_keeps_completed_proposal_and_releases_guard(
    tmp_path: Path,
) -> None:
    service, store, entries = await _setup(tmp_path)
    started = asyncio.Event()

    class Paused(ScriptedModel):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            if not self.requests:
                return await super().complete(request)
            self.requests.append(request)
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    model = Paused([ModelResponse(tool_calls=(_proposal(),))])
    pending = asyncio.create_task(
        _runtime(tmp_path, service, model).execute(
            ToolCall("manual", "consolidate_memory", _arguments(entries)), "operator", 1
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 5)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        snapshot = await store.snapshot("alpha")
        drafts = [e for e in snapshot if e.status is MemoryStatus.PROPOSED]
        assert len(drafts) == 1
        assert drafts[0].approval_sha256 is None
        for old in entries:
            assert await store.get("alpha", old.entry_id) == old
        assert (await store.history("alpha", drafts[0].entry_id)).records[0].after == drafts[0]
        service.begin_run("next_actor")
        service.end_run("next_actor")
    finally:
        if not pending.done():
            pending.cancel()
        await store.close()


@pytest.mark.asyncio
async def test_c01_actual_loop_binds_inputs_and_only_creates_proposed(tmp_path: Path) -> None:
    service, store, entries = await _setup(tmp_path)
    model = ScriptedModel(
        [
            ModelResponse(tool_calls=(_proposal(),)),
            ModelResponse("Draft ready for operator review."),
        ]
    )
    try:
        result = await _runtime(tmp_path, service, model).execute(
            ToolCall("manual", "consolidate_memory", _arguments(entries)), "operator", 1
        )
        assert result.status is ToolStatus.SUCCEEDED
        assert isinstance(result.result, dict)
        assert result.result["status"] == "finished"
        assert isinstance(result.result["input_sha256"], str)
        snapshot = await store.snapshot("alpha")
        pending = [e for e in snapshot if e.status is MemoryStatus.PROPOSED]
        assert len(pending) == 1
        draft = pending[0]
        note = parse_memory_note(draft.content)
        assert note is not None
        assert note.rule == "Use uv."
        assert note.sources == tuple(MemoryNoteSource.from_entry(e) for e in entries)
        assert draft.source_run_id == result.result["run_id"]
        assert draft.approval_sha256 is None
        assert draft.evidence_sha256 is None
        for original in entries:
            assert await store.get("alpha", original.entry_id) == original
        assert (tmp_path / "workspace/actor_trace.jsonl").read_text() == "original actor facts\n"
        assert len(model.requests) == 2
        assert {t.name for t in model.requests[0].tools} == {"propose_consolidated_note"}
        assert any("Use uv." in m.content for m in model.requests[0].messages)
        (tmp_path / "memory_requests.json").write_text(
            json.dumps([asdict(r) for r in model.requests])
        )
        (tmp_path / "memory_effects.json").write_text(json.dumps([asdict(e) for e in snapshot]))
    finally:
        await store.close()


@pytest.mark.parametrize("allowed", [True, False])
@pytest.mark.asyncio
async def test_c03_permission_or_active_actor_rejects_before_model(
    tmp_path: Path, *, allowed: bool
) -> None:
    service, store, entries = await _setup(tmp_path)
    model = ScriptedModel([ModelResponse("must not be called")])
    if allowed:
        service.begin_run("actor")
    before = (tmp_path / "private/memory.sqlite3").read_bytes()
    try:
        result = await _runtime(tmp_path, service, model, allowed=allowed).execute(
            ToolCall("manual", "consolidate_memory", _arguments(entries)), "operator", 1
        )
        assert result.status is (ToolStatus.FAILED if allowed else ToolStatus.DENIED)
        assert model.requests == []
        assert (tmp_path / "private/memory.sqlite3").read_bytes() == before
    finally:
        if allowed:
            service.end_run("actor")
        await store.close()


@pytest.mark.parametrize("change", ["authorization", "redacted", "hash", "extra", "empty"])
@pytest.mark.asyncio
async def test_c02_invalid_materials_do_not_reach_model_or_database(
    tmp_path: Path, change: str
) -> None:
    service, store, entries = await _setup(tmp_path)
    args = _arguments(entries)
    if change == "authorization":
        args["materials_authorized"] = False
    elif change == "redacted":
        args["redacted"] = False
    elif change == "extra":
        args["accepted"] = True
    elif change == "empty":
        args.update(sources=[], facts=[])
    else:
        facts = args["facts"]
        assert isinstance(facts, list)
        assert isinstance(facts[0], dict)
        facts[0]["content_sha256"] = "0" * 64
    before = (tmp_path / "private/memory.sqlite3").read_bytes()
    await store.close()
    model = ScriptedModel([ModelResponse("must not be called")])
    result = await _runtime(tmp_path, service, model).execute(
        ToolCall("manual", "consolidate_memory", args), "operator", 1
    )
    assert result.status is ToolStatus.INVALID
    assert model.requests == []
    assert (tmp_path / "private/memory.sqlite3").read_bytes() == before


@pytest.mark.parametrize("stage", ["before_storage_publish", "after_commit"])
@pytest.mark.asyncio
async def test_c06_real_storage_fault_does_not_retry_and_readback_is_truthful(
    tmp_path: Path, stage: str
) -> None:
    armed = False
    visits = 0

    def fault(actual: str) -> None:
        nonlocal visits
        if armed and actual == stage:
            visits += 1
            raise OSError("fixed storage fault")

    service, store, entries = await _setup(tmp_path, fault)
    model = ScriptedModel(
        [ModelResponse(tool_calls=(_proposal(),)), ModelResponse("Review any published candidate.")]
    )
    armed = True
    try:
        result = await _runtime(tmp_path, service, model).execute(
            ToolCall("manual", "consolidate_memory", _arguments(entries)), "operator", 1
        )
        assert result.status is ToolStatus.SUCCEEDED
        assert isinstance(result.result, dict)
        assert visits == 1
        pending = [e for e in await store.snapshot("alpha") if e.status is MemoryStatus.PROPOSED]
        assert len(pending) == (1 if stage == "after_commit" else 0)
        candidates = result.result["candidates"]
        assert isinstance(candidates, list)
        assert len(candidates) == len(pending)
        for old in entries:
            assert await store.get("alpha", old.entry_id) == old
        for entry in pending:
            assert entry.source_run_id == result.result["run_id"]
            assert (await store.history("alpha", entry.entry_id)).records[0].after == entry
    finally:
        await store.close()


@pytest.mark.parametrize("stage", ["before_storage_publish", "after_commit"])
@pytest.mark.asyncio
async def test_c06_cancelled_publication_settles_worker_and_operator_can_review_by_run(
    tmp_path: Path, stage: str
) -> None:
    armed = False
    reached, release = threading.Event(), threading.Event()

    def pause(actual: str) -> None:
        if armed and actual == stage:
            reached.set()
            if not release.wait(5):
                raise TimeoutError("test storage pause not released")

    service, store, entries = await _setup(tmp_path, pause)
    model = ScriptedModel([ModelResponse(tool_calls=(_proposal(),))])
    runtime = _runtime(tmp_path, service, model)
    armed = True
    pending = asyncio.create_task(
        runtime.execute(
            ToolCall("manual", "consolidate_memory", _arguments(entries)), "operator", 1
        )
    )
    try:
        assert await asyncio.to_thread(reached.wait, 3)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        snapshot = await store.snapshot("alpha")
        drafts = [e for e in snapshot if e.status is MemoryStatus.PROPOSED]
        assert len(drafts) == (1 if stage == "after_commit" else 0)
        for old in entries:
            assert await store.get("alpha", old.entry_id) == old
        trace = [
            json.loads(line)
            for line in (tmp_path / "memory_operator_trace.jsonl").read_text().splitlines()
        ]
        start = next(row for row in trace if row["name"] == "memory_consolidation_started")
        registry = ToolRegistry()
        registry.register(ReviewConsolidationTool(service))
        review = await ToolRuntime(
            registry, PermissionPolicy(frozenset({"memory.maintain"})), EventBus()
        ).execute(
            ToolCall("review", "review_memory_consolidation", {"source_run_id": start["run_id"]}),
            "operator",
            1,
        )
        assert review.status is ToolStatus.SUCCEEDED
        assert isinstance(review.result, dict)
        candidates = review.result["candidates"]
        assert isinstance(candidates, list)
        assert len(candidates) == len(drafts)
        assert start["data"]["input_sha256"]
        service.begin_run("next_actor")
        service.end_run("next_actor")
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await store.close()


@pytest.mark.asyncio
async def test_c04_source_changes_between_model_exposure_and_write_prevent_draft(
    tmp_path: Path,
) -> None:
    service, store, entries = await _setup(tmp_path)

    class SourceChanged(ScriptedModel):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            response = await super().complete(request)
            await store.replace(
                entries[0], entries[0].revise(content="Use a revised command.", expected_revision=1)
            )
            return response

    model = SourceChanged([ModelResponse(tool_calls=(_proposal(),))])
    try:
        result = await _runtime(tmp_path, service, model).execute(
            ToolCall("manual", "consolidate_memory", _arguments(entries)), "operator", 1
        )
        assert result.status is ToolStatus.SUCCEEDED
        assert isinstance(result.result, dict)
        assert result.result["status"] == "failed"
        assert result.result["candidates"] == []
        assert len(model.requests) == 1
        assert not any(
            e.source_run_id == result.result["run_id"] for e in await store.snapshot("alpha")
        )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_c03_05_wall_timeout_and_concurrent_run_leave_no_writes(tmp_path: Path) -> None:
    service, store, entries = await _setup(tmp_path)
    reached = asyncio.Event()

    class WaitModel(ScriptedModel):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            self.requests.append(request)
            reached.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    model = WaitModel([])
    args = _arguments(entries)
    args["max_duration_seconds"] = 0.2
    runtime = _runtime(tmp_path, service, model)
    pending = asyncio.create_task(
        runtime.execute(ToolCall("manual", "consolidate_memory", args), "operator", 1)
    )
    try:
        await asyncio.wait_for(reached.wait(), 3)
        second = await runtime.execute(
            ToolCall("concurrent", "consolidate_memory", args), "operator", 2
        )
        assert second.status is ToolStatus.FAILED
        result = await pending
        assert result.status is ToolStatus.SUCCEEDED
        assert isinstance(result.result, dict)
        assert result.result["status"] == "budget_exhausted"
        assert result.result["candidates"] == []
        assert await store.snapshot("alpha") == tuple(entries)
        service.begin_run("next_actor")
        service.end_run("next_actor")
    finally:
        if not pending.done():
            pending.cancel()
        await store.close()


@pytest.mark.parametrize("input_material", [False, True])
@pytest.mark.asyncio
async def test_c02_07_registered_material_is_rejected_before_persistence_and_keeps_usage(
    tmp_path: Path, *, input_material: bool
) -> None:
    canary = "Private acceptance label: fixed hidden criterion 7391."
    service, store, entries = await _setup(
        tmp_path, forbidden=(hashlib.sha256(canary.encode()).hexdigest(),)
    )
    args = _arguments(entries)
    if input_material:
        facts = args["facts"]
        assert isinstance(facts, list)
        assert isinstance(facts[0], dict)
        facts[0].update(content=canary, content_sha256=hashlib.sha256(canary.encode()).hexdigest())
    model = ScriptedModel(
        [
            ModelResponse(
                tool_calls=(_proposal(rule=canary),),
                usage=TokenUsage(input_tokens=20, output_tokens=4),
            ),
            ModelResponse("No candidate."),
        ]
    )
    before = (tmp_path / "private/memory.sqlite3").read_bytes()
    try:
        result = await _runtime(tmp_path, service, model).execute(
            ToolCall("manual", "consolidate_memory", args), "operator", 1
        )
        assert (tmp_path / "private/memory.sqlite3").read_bytes() == before
        assert canary not in (tmp_path / "memory_operator_trace.jsonl").read_text()
        if input_material:
            assert result.status is ToolStatus.INVALID
            assert model.requests == []
        else:
            assert result.status is ToolStatus.SUCCEEDED
            assert isinstance(result.result, dict)
            assert result.result["candidates"] == []
            assert result.result["input_tokens"] == 20
            assert result.result["output_tokens"] == 4
    finally:
        await store.close()


@pytest.mark.parametrize(
    "change",
    [
        "secret",
        "duplicate_sources",
        "duplicate_facts",
        "many_sources",
        "many_facts",
        "fact_bytes",
        "fact_total",
    ],
)
@pytest.mark.asyncio
async def test_c02_input_bounds_reject_before_closed_store(tmp_path: Path, change: str) -> None:
    service, store, entries = await _setup(tmp_path)
    args = _arguments(entries)
    facts = args["facts"]
    assert isinstance(facts, list)
    sources = args["sources"]
    assert isinstance(sources, list)
    assert isinstance(facts[0], dict)
    if change == "secret":
        content = "password=private_canary_7391"
        facts[0].update(
            content=content, content_sha256=hashlib.sha256(content.encode()).hexdigest()
        )
    elif change == "duplicate_sources":
        sources.append(sources[0])
    elif change == "duplicate_facts":
        facts.append(facts[0])
    elif change == "many_sources":
        args["sources"] = sources * 3
    elif change == "many_facts":
        args["facts"] = facts * 6
    elif change == "fact_total":
        content = "x" * 1800
        args["facts"] = [
            {
                "fact_id": "fact_" + str(i),
                "source_run_id": "actor",
                "content": content,
                "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
            }
            for i in range(5)
        ]
    else:
        content = "非" * 800
        facts[0].update(
            content=content, content_sha256=hashlib.sha256(content.encode()).hexdigest()
        )
    await store.close()
    model = ScriptedModel([ModelResponse("must not be called")])
    result = await _runtime(tmp_path, service, model).execute(
        ToolCall("manual", "consolidate_memory", args), "operator", 1
    )
    assert result.status is ToolStatus.INVALID
    assert model.requests == []


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.asyncio
async def test_c04_no_history_port_or_unmigrated_store_cannot_reach_model(
    tmp_path: Path, *, legacy: bool
) -> None:
    service, store, entries = await _setup(tmp_path)

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

    if legacy:
        service = MemoryService(LegacyStore(), project_id="alpha", applicable_version="v1")
        await store.close()
    else:
        other = SQLiteMemoryStore(
            tmp_path / "private/old.sqlite3",
            allowed_root=tmp_path / "private",
            workspace=tmp_path / "workspace",
        )
        await other.create(
            MemoryEntry.propose(
                "old_pending",
                "alpha",
                MemoryKind.ENVIRONMENT,
                topic="old_pending",
                content="Use configured commands.",
                source_run_id="old_source",
                applicable_version="v1",
            )
        )
        service = MemoryService(other, project_id="alpha", applicable_version="v1")
    model = ScriptedModel([ModelResponse("no private input")])
    args = _arguments(entries)
    try:
        result = await _runtime(tmp_path, service, model).execute(
            ToolCall("manual", "consolidate_memory", args), "operator", 1
        )
        assert result.status is ToolStatus.SUCCEEDED
        assert isinstance(result.result, dict)
        assert result.result["status"] == "failed"
        assert result.result["candidates"] == []
        assert model.requests == []
    finally:
        await store.close()
        if not legacy:
            await other.close()


@pytest.mark.parametrize("scope", ["project", "version"])
@pytest.mark.asyncio
async def test_c04_scope_is_bound_to_the_service_not_supplied_by_model(
    tmp_path: Path, scope: str
) -> None:
    _, store, entries = await _setup(tmp_path)
    service = MemoryService(
        store,
        project_id="beta" if scope == "project" else "alpha",
        applicable_version="v1" if scope == "project" else "v0",
    )
    model = ScriptedModel([ModelResponse("unavailable")])
    before = (tmp_path / "private/memory.sqlite3").read_bytes()
    try:
        result = await _runtime(tmp_path, service, model).execute(
            ToolCall("manual", "consolidate_memory", _arguments(entries)), "operator", 1
        )
        assert result.status is ToolStatus.SUCCEEDED
        assert isinstance(result.result, dict)
        assert result.result["status"] == "failed"
        assert model.requests == []
        assert (tmp_path / "private/memory.sqlite3").read_bytes() == before
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_c07_candidate_attempt_cap_preserves_old_knowledge(tmp_path: Path) -> None:
    service, store, entries = await _setup(tmp_path)
    calls = tuple(
        ToolCall(
            "draft_" + str(i),
            "propose_consolidated_note",
            _proposal(topic="combined_" + str(i), supersede_sources=True).arguments,
        )
        for i in range(6)
    )
    model = ScriptedModel([ModelResponse(tool_calls=calls), ModelResponse("done")])
    try:
        result = await _runtime(tmp_path, service, model).execute(
            ToolCall("manual", "consolidate_memory", _arguments(entries)), "operator", 1
        )
        assert result.status is ToolStatus.SUCCEEDED
        assert isinstance(result.result, dict)
        candidates = result.result["candidates"]
        assert isinstance(candidates, list)
        assert len(candidates) == 5
        pending = [e for e in await store.snapshot("alpha") if e.status is MemoryStatus.PROPOSED]
        assert len(pending) == 5
        for old in entries:
            assert await store.get("alpha", old.entry_id) == old
        for draft in pending:
            note = parse_memory_note(draft.content)
            assert note is not None
            assert note.supersedes == tuple(MemoryNoteSource.from_entry(e) for e in entries)
            assert draft.approval_sha256 is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_c06_model_failure_records_usage_and_releases_run(tmp_path: Path) -> None:
    service, store, entries = await _setup(tmp_path)

    class Failed(ScriptedModel):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            self.requests.append(request)
            raise ModelCallError(
                "fixed model failure", usage=TokenUsage(input_tokens=20, output_tokens=3)
            )

    model = Failed([])
    before = (tmp_path / "private/memory.sqlite3").read_bytes()
    try:
        result = await _runtime(tmp_path, service, model).execute(
            ToolCall("manual", "consolidate_memory", _arguments(entries)), "operator", 1
        )
        assert result.status is ToolStatus.SUCCEEDED
        assert isinstance(result.result, dict)
        assert result.result["status"] == "failed"
        assert result.result["input_tokens"] == 20
        assert result.result["output_tokens"] == 3
        assert result.result["candidates"] == []
        assert len(model.requests) == 1
        assert (tmp_path / "private/memory.sqlite3").read_bytes() == before
        service.begin_run("next_actor")
        service.end_run("next_actor")
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_c03_real_pending_maintenance_blocks_consolidation(tmp_path: Path) -> None:
    armed = False
    reached, release = threading.Event(), threading.Event()

    def pause(stage: str) -> None:
        if armed and stage == "before_storage_read":
            reached.set()
            if not release.wait(5):
                raise TimeoutError("test maintenance not released")

    service, store, entries = await _setup(tmp_path, pause)
    armed = True
    maintenance = asyncio.create_task(service.review(entries[0].entry_id))
    model = ScriptedModel([ModelResponse("must not be called")])
    try:
        assert await asyncio.to_thread(reached.wait, 3)
        result = await _runtime(tmp_path, service, model).execute(
            ToolCall("manual", "consolidate_memory", _arguments(entries)), "operator", 1
        )
        assert result.status is ToolStatus.FAILED
        assert result.error is not None
        assert result.error.code == "memory_maintenance_active"
        assert model.requests == []
        armed = False
        release.set()
        assert await maintenance == entries[0]
        service.begin_run("next_actor")
        service.end_run("next_actor")
    finally:
        release.set()
        await maintenance
        await store.close()


def test_c01_input_hash_binds_materials_and_operator_identity() -> None:
    content = "An explicitly authorized redacted observation."
    fact = ConsolidationFact(
        "fact_a", "actor_source", content, hashlib.sha256(content.encode()).hexdigest()
    )
    source = MemoryNoteSource("source_note", 1, "a" * 64, "b" * 64, "original_source")
    inputs = ConsolidationInput("operator_a", (source,), (fact,))
    assert inputs.input_sha256 == ConsolidationInput("operator_a", (source,), (fact,)).input_sha256
    changed = "A revised authorized observation."
    variants = (
        replace(inputs, operator_id="operator_b"),
        replace(inputs, sources=(replace(source, revision=2),)),
        replace(
            inputs,
            facts=(
                replace(
                    fact,
                    content=changed,
                    content_sha256=hashlib.sha256(changed.encode()).hexdigest(),
                ),
            ),
        ),
    )
    assert all(v.input_sha256 != inputs.input_sha256 for v in variants)
    canonical = {"operator_id": "operator_a", "sources": [asdict(source)], "facts": [asdict(fact)]}
    expected = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert inputs.input_sha256 == expected


@pytest.mark.asyncio
async def test_p505_trusted_operator_task_is_visible_and_hash_bound(tmp_path: Path) -> None:
    service, store, entries = await _setup(tmp_path)
    instruction = (
        "Propose one environment policy and explicitly request "
        "replacement of the authorized sources."
    )
    model = ScriptedModel([ModelResponse("Review the authorized project material.")])
    bus = EventBus()
    bus.subscribe("*", JsonlTraceWriter(tmp_path / "memory_operator_trace.jsonl"))
    try:
        tool = ConsolidateMemoryTool(
            service,
            model_factory=lambda: model,
            events=bus,
            operator_id="fixture_operator",
            task_instruction=instruction,
        )
        registry = ToolRegistry()
        registry.register(tool)
        result = await ToolRuntime(
            registry, PermissionPolicy(frozenset(tool.definition.permissions)), bus
        ).execute(ToolCall("manual", "consolidate_memory", _arguments(entries)), "operator", 1)
        assert result.status is ToolStatus.SUCCEEDED
        assert isinstance(result.result, dict)
        assert instruction in model.requests[0].messages[1].content
        base = _arguments(entries)
        facts = base["facts"]
        assert isinstance(facts, list)
        assert isinstance(facts[0], dict)
        fact = ConsolidationFact(
            str(facts[0]["fact_id"]),
            str(facts[0]["source_run_id"]),
            str(facts[0]["content"]),
            str(facts[0]["content_sha256"]),
        )
        inputs = ConsolidationInput(
            "fixture_operator",
            tuple(MemoryNoteSource.from_entry(e) for e in entries),
            (fact,),
            task_instruction=instruction,
        )
        assert result.result["input_sha256"] == inputs.input_sha256
        assert inputs.input_sha256 != replace(inputs, task_instruction=None).input_sha256
        assert await store.snapshot("alpha") == tuple(entries)
    finally:
        await store.close()
