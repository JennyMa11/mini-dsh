"""Real temporary SQLite storage through its public project-scoped interface."""

import asyncio
import hashlib
import multiprocessing
import threading
from multiprocessing.connection import Connection
from pathlib import Path

import pytest

from minidsh.adapters.memory import SQLiteMemoryStore
from minidsh.domain.memory import (
    MemoryEntry,
    MemoryEvidence,
    MemoryEvidenceKind,
    MemoryKind,
    MemoryStatus,
    MemoryValidationError,
    select_memory,
)
from minidsh.ports.memory import MemoryStoreError

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("replacement", ["directory", "symlink"])
@pytest.mark.asyncio
async def test_replacement_at_atomic_publish_cannot_redirect_the_write(
    tmp_path: Path, replacement: str
) -> None:
    workspace, private, outside = (tmp_path / name for name in ("workspace", "private", "outside"))
    for directory in (workspace, private, outside):
        directory.mkdir()
    old = MemoryEntry.propose(
        "memory_one",
        "project_one",
        MemoryKind.DIAGNOSIS,
        topic="atomic_publish",
        content="Inspect inputs before publishing.",
        source_run_id="run_one",
        applicable_version="v1",
    )
    external = SQLiteMemoryStore(
        outside / "memory.sqlite3", allowed_root=outside, workspace=workspace
    )
    await external.create(old)
    await external.close()
    outside_before = hashlib.sha256((outside / "memory.sqlite3").read_bytes()).hexdigest()
    armed = False
    reached = False

    def replace_path(stage: str) -> None:
        nonlocal reached
        if armed and stage == "before_storage_publish" and not reached:
            reached = True
            if replacement == "directory":
                private.rename(tmp_path / "original_private")
                private.symlink_to(outside, target_is_directory=True)
            else:
                (private / "memory.sqlite3").rename(private / "original.sqlite3")
                (private / "memory.sqlite3").symlink_to(outside / "memory.sqlite3")

    store = SQLiteMemoryStore(
        private / "memory.sqlite3",
        allowed_root=private,
        workspace=workspace,
        transaction_observer=replace_path,
    )
    await store.create(old)
    changed = old.revise(
        content="Inspect the final artifact before publishing.", expected_revision=1
    )
    armed = True
    try:
        if replacement == "directory":
            with pytest.raises(MemoryStoreError, match="memory_committed_notification_failed"):
                await store.replace(old, changed)
        else:
            assert await store.replace(old, changed) == changed
        assert reached
        assert (
            hashlib.sha256((outside / "memory.sqlite3").read_bytes()).hexdigest() == outside_before
        )
    finally:
        await store.close()
    location = tmp_path / "original_private" if replacement == "directory" else private
    reopened = SQLiteMemoryStore(
        location / "memory.sqlite3", allowed_root=location, workspace=workspace
    )
    try:
        assert await reopened.get("project_one", "memory_one") == changed
        assert not list(location.glob(".memory-*"))
    finally:
        await reopened.close()


def _held_process_write(private: Path, workspace: Path, channel: Connection) -> None:
    def pause(stage: str) -> None:
        if stage == "before_commit":
            channel.send("commit_reached")
            if not channel.poll(5) or channel.recv() != "release":
                raise TimeoutError("fixture process write was not released")

    async def write() -> None:
        store = SQLiteMemoryStore(
            private / "memory.sqlite3",
            allowed_root=private,
            workspace=workspace,
            transaction_observer=pause,
        )
        try:
            await store.create(
                MemoryEntry.propose(
                    "child_entry",
                    "project_one",
                    MemoryKind.DIAGNOSIS,
                    topic="child_diagnosis",
                    content="Inspect the child operation first.",
                    source_run_id="child_run",
                    applicable_version="v1",
                )
            )
        finally:
            await store.close()

    try:
        asyncio.run(write())
    finally:
        channel.close()


@pytest.mark.asyncio
async def test_process_writers_do_not_lose_a_committed_update(tmp_path: Path) -> None:
    workspace, private = tmp_path / "workspace", tmp_path / "private"
    workspace.mkdir()
    private.mkdir()
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_held_process_write, args=(private, workspace, child))
    process.start()
    child.close()
    store = SQLiteMemoryStore(private / "memory.sqlite3", allowed_root=private, workspace=workspace)
    proposal = MemoryEntry.propose(
        "parent_entry",
        "project_one",
        MemoryKind.DIAGNOSIS,
        topic="parent_diagnosis",
        content="Inspect the parent operation first.",
        source_run_id="parent_run",
        applicable_version="v1",
    )
    try:
        assert await asyncio.to_thread(parent.poll, 3)
        assert parent.recv() == "commit_reached"
        with pytest.raises(MemoryStoreError, match="memory_storage_busy"):
            await store.create(proposal)
        parent.send("release")
        await asyncio.to_thread(process.join, 3)
        assert not process.is_alive()
        assert process.exitcode == 0
        assert await store.create(proposal) == proposal
        assert {entry.entry_id for entry in await store.snapshot("project_one")} == {
            "child_entry",
            "parent_entry",
        }
    finally:
        if process.is_alive():
            process.terminate()
            await asyncio.to_thread(process.join, 3)
        process.close()
        parent.close()
        await store.close()


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
@pytest.mark.asyncio
async def test_unfinished_external_sqlite_sidecars_are_not_silently_adopted(
    tmp_path: Path, suffix: str
) -> None:
    workspace, private = tmp_path / "workspace", tmp_path / "private"
    workspace.mkdir()
    private.mkdir()
    database = private / "memory.sqlite3"
    database.write_bytes(b"outside fixture bytes")
    sidecar = private / (database.name + suffix)
    sidecar.write_bytes(b"unfinished external SQLite transaction")
    store = SQLiteMemoryStore(database, allowed_root=private, workspace=workspace)
    try:
        with pytest.raises(MemoryStoreError, match="memory_storage_busy"):
            await store.snapshot("project_one")
        assert database.read_bytes() == b"outside fixture bytes"
        assert sidecar.read_bytes() == b"unfinished external SQLite transaction"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_oversized_database_is_rejected_without_reading_it_into_sqlite(
    tmp_path: Path,
) -> None:
    workspace, private = tmp_path / "workspace", tmp_path / "private"
    workspace.mkdir()
    private.mkdir()
    database = private / "memory.sqlite3"
    with database.open("wb") as stream:
        stream.truncate(8 * 1024 * 1024 + 1)
    store = SQLiteMemoryStore(database, allowed_root=private, workspace=workspace)
    try:
        with pytest.raises(MemoryStoreError, match="memory_storage_limit"):
            await store.snapshot("project_one")
        assert database.stat().st_size == 8 * 1024 * 1024 + 1
        assert sorted(path.name for path in private.iterdir()) == ["memory.sqlite3"]
    finally:
        await store.close()


@pytest.mark.parametrize("replacement", ["directory", "symlink"])
@pytest.mark.parametrize("operation", ["get", "snapshot", "create", "replace"])
@pytest.mark.asyncio
async def test_path_replacement_at_storage_read_never_reads_or_changes_the_outside_database(
    tmp_path: Path, replacement: str, operation: str
) -> None:
    workspace, private, outside = (tmp_path / name for name in ("workspace", "private", "outside"))
    for directory in (workspace, private, outside):
        directory.mkdir()
    entry = MemoryEntry.propose(
        "memory_one",
        "project_one",
        MemoryKind.DIAGNOSIS,
        topic="assertion_inputs",
        content="Inspect assertion inputs first.",
        source_run_id="run_one",
        applicable_version="v1",
    )
    external = SQLiteMemoryStore(
        outside / "memory.sqlite3", allowed_root=outside, workspace=workspace
    )
    external_entry = entry.revise(
        content="Outside database must remain private.", expected_revision=1
    )
    await external.create(external_entry)
    await external.close()
    outside_before = hashlib.sha256((outside / "memory.sqlite3").read_bytes()).hexdigest()
    armed = False
    reached = False

    def replace_path(stage: str) -> None:
        nonlocal reached
        if armed and stage == "before_storage_read" and not reached:
            reached = True
            if replacement == "directory":
                private.rename(tmp_path / "original_private")
                private.symlink_to(outside, target_is_directory=True)
            else:
                (private / "memory.sqlite3").rename(private / "original.sqlite3")
                (private / "memory.sqlite3").symlink_to(outside / "memory.sqlite3")

    store = SQLiteMemoryStore(
        private / "memory.sqlite3",
        allowed_root=private,
        workspace=workspace,
        transaction_observer=replace_path,
    )
    await store.create(entry)
    original_before = hashlib.sha256((private / "memory.sqlite3").read_bytes()).hexdigest()
    armed = True
    try:

        async def exercise() -> None:
            if operation == "get":
                await store.get("project_one", "memory_one")
            elif operation == "snapshot":
                await store.snapshot("project_one")
            elif operation == "create":
                await store.create(
                    MemoryEntry.propose(
                        "memory_two",
                        "project_one",
                        MemoryKind.DIAGNOSIS,
                        topic="other_diagnosis",
                        content="Inspect failure details before editing.",
                        source_run_id="run_two",
                        applicable_version="v1",
                    )
                )
            else:
                await store.replace(
                    entry, entry.revise(content="Check inputs again.", expected_revision=1)
                )

        with pytest.raises(MemoryStoreError, match="memory_storage_denied"):
            await exercise()
        assert reached
        assert (
            hashlib.sha256((outside / "memory.sqlite3").read_bytes()).hexdigest() == outside_before
        )
        original = (
            tmp_path / "original_private/memory.sqlite3"
            if replacement == "directory"
            else private / "original.sqlite3"
        )
        assert hashlib.sha256(original.read_bytes()).hexdigest() == original_before
        assert not list(original.parent.glob(".memory-*"))
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_failed_initialization_leaves_no_database_and_reopen_can_create(
    tmp_path: Path,
) -> None:
    workspace, private = tmp_path / "workspace", tmp_path / "private"
    workspace.mkdir()
    private.mkdir()
    database = private / "memory.sqlite3"

    def fail_initialization(stage: str) -> None:
        if stage == "before_initialize_publish":
            raise OSError("fixture_private_initialization_diagnostic")

    store = SQLiteMemoryStore(
        database,
        allowed_root=private,
        workspace=workspace,
        transaction_observer=fail_initialization,
    )
    proposal = MemoryEntry.propose(
        "memory_one",
        "project_one",
        MemoryKind.DIAGNOSIS,
        topic="initialization",
        content="Inspect the failure before editing.",
        source_run_id="run_one",
        applicable_version="v1",
    )
    with pytest.raises(MemoryStoreError, match=r"^memory_transaction_failed$"):
        await store.create(proposal)
    assert not database.exists()
    assert list(private.iterdir()) == []
    await store.close()
    reopened = SQLiteMemoryStore(database, allowed_root=private, workspace=workspace)
    try:
        assert await reopened.snapshot("project_one") == ()
        assert await reopened.create(proposal) == proposal
        assert await reopened.get("project_one", "memory_one") == proposal
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_conflicting_topic_validation_is_atomic_across_store_instances(
    tmp_path: Path,
) -> None:
    workspace, private = tmp_path / "workspace", tmp_path / "private"
    workspace.mkdir()
    private.mkdir()
    stores = [
        SQLiteMemoryStore(private / "memory.sqlite3", allowed_root=private, workspace=workspace)
        for _ in range(2)
    ]
    proposals = [
        MemoryEntry.propose(
            f"memory_{index}",
            "project_one",
            MemoryKind.DIAGNOSIS,
            topic="test_runner",
            content=content,
            source_run_id="run_one",
            applicable_version="v1",
        )
        for index, content in enumerate(("Use the project runner.", "Use a different runner."))
    ]
    proof = MemoryEvidence(
        "a" * 64,
        "project_one",
        "v1",
        MemoryEvidenceKind.INDEPENDENT_ACCEPTANCE,
        source_run_id="run_one",
        accepted=True,
        source_exists=True,
    )
    try:
        for store, entry in zip(stores, proposals, strict=True):
            await store.create(entry)
        outcomes = await asyncio.gather(
            *(
                store.replace(entry, entry.validate(proof))
                for store, entry in zip(stores, proposals, strict=True)
            ),
            return_exceptions=True,
        )
        assert sum(isinstance(result, MemoryEntry) for result in outcomes) == 1
        errors = [result for result in outcomes if isinstance(result, MemoryStoreError)]
        assert [error.code for error in errors] == ["memory_topic_conflict"]
        entries = await stores[0].snapshot("project_one")
        assert sorted(entry.status.value for entry in entries) == ["proposed", "validated"]
    finally:
        for store in stores:
            await store.close()


@pytest.mark.asyncio
async def test_proposal_survives_reopen_and_is_scoped_to_its_project(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    private = tmp_path / "private"
    workspace.mkdir()
    private.mkdir()
    database = private / "memory.sqlite3"
    store = SQLiteMemoryStore(database, allowed_root=private, workspace=workspace)
    entry = MemoryEntry.propose(
        "memory_one",
        "project_one",
        MemoryKind.REPAIR,
        topic="repair_assertion",
        content="Inspect the failing assertion first.",
        source_run_id="run_one",
        applicable_version="v1",
        paths=("src/value.py",),
    )
    assert await store.create(entry) == entry
    assert await store.get("project_one", "memory_one") == entry
    assert await store.get("other_project", "memory_one") is None
    assert await store.snapshot("project_one") == (entry,)
    assert await store.snapshot("other_project") == ()
    await store.close()
    reopened = SQLiteMemoryStore(database, allowed_root=private, workspace=workspace)
    loaded = await reopened.get("project_one", "memory_one")
    assert loaded == entry
    assert loaded is not None
    assert loaded.status is MemoryStatus.PROPOSED
    assert loaded.approved_by is None
    assert loaded.evidence_sha256 is None
    assert await reopened.snapshot("project_one") == (entry,)
    assert not (workspace / "memory.sqlite3").exists()
    await reopened.close()


@pytest.mark.asyncio
async def test_validated_approved_and_committed_states_are_atomically_read_back(
    tmp_path: Path,
) -> None:
    workspace, private = tmp_path / "workspace", tmp_path / "private"
    workspace.mkdir()
    private.mkdir()
    store = SQLiteMemoryStore(private / "memory.sqlite3", allowed_root=private, workspace=workspace)
    proposed = MemoryEntry.propose(
        "memory_one",
        "project_one",
        MemoryKind.REPAIR,
        topic="repair_assertion",
        content="Inspect the failing assertion first.",
        source_run_id="run_one",
        applicable_version="v1",
    )
    validated = proposed.validate(
        MemoryEvidence(
            "a" * 64,
            "project_one",
            "v1",
            MemoryEvidenceKind.INDEPENDENT_ACCEPTANCE,
            source_run_id="run_one",
            accepted=True,
            source_exists=True,
        )
    )
    approved = validated.approve(
        expected_revision=1,
        content_sha256=validated.content_sha256,
        approver="fixture_operator",
        approved_at="2026-10-29T00:00:00+00:00",
    )
    committed = approved.commit(expected_revision=1, approval_sha256=approved.approval_sha256)
    await store.create(proposed)
    for before, after in [(proposed, validated), (validated, approved), (approved, committed)]:
        assert await store.replace(before, after) == after
        assert await store.get("project_one", "memory_one") == after
    # Retry the same commit after its original receipt was lost.
    assert await store.replace(approved, committed) == committed
    assert await store.replace(committed, committed) == committed
    await store.close()
    reopened = SQLiteMemoryStore(
        private / "memory.sqlite3", allowed_root=private, workspace=workspace
    )
    assert await reopened.get("project_one", "memory_one") == committed
    assert select_memory(
        await reopened.snapshot("project_one"),
        project_id="project_one",
        applicable_version="v1",
    ).entries == (committed,)
    rejected = committed.reject(expected_revision=1)
    assert await reopened.replace(committed, rejected) == rejected
    with pytest.raises(MemoryStoreError, match="memory_revision_stale"):
        await reopened.replace(approved, committed)
    assert await reopened.get("project_one", "memory_one") == rejected
    assert (
        select_memory(
            await reopened.snapshot("project_one"),
            project_id="project_one",
            applicable_version="v1",
        ).entries
        == ()
    )
    await reopened.close()


@pytest.mark.asyncio
async def test_stale_updates_and_skipped_states_leave_the_stored_record_intact(
    tmp_path: Path,
) -> None:
    workspace, private = tmp_path / "workspace", tmp_path / "private"
    workspace.mkdir()
    private.mkdir()
    store = SQLiteMemoryStore(private / "memory.sqlite3", allowed_root=private, workspace=workspace)
    proposed = MemoryEntry.propose(
        "memory_one",
        "project_one",
        MemoryKind.REPAIR,
        topic="repair_assertion",
        content="Inspect the failing assertion.",
        source_run_id="run_one",
        applicable_version="v1",
    )
    validated = proposed.validate(
        MemoryEvidence(
            "a" * 64,
            "project_one",
            "v1",
            MemoryEvidenceKind.INDEPENDENT_ACCEPTANCE,
            source_run_id="run_one",
            accepted=True,
            source_exists=True,
        )
    )
    approved = validated.approve(
        expected_revision=1,
        content_sha256=validated.content_sha256,
        approver="fixture_operator",
        approved_at="2026-10-29T00:00:00+00:00",
    )
    committed = approved.commit(expected_revision=1, approval_sha256=approved.approval_sha256)
    await store.create(proposed)
    with pytest.raises(MemoryValidationError):
        await store.replace(proposed, committed)
    assert await store.get("project_one", "memory_one") == proposed
    await store.replace(proposed, validated)
    with pytest.raises(MemoryStoreError, match="memory_revision_stale"):
        await store.replace(proposed, proposed.revise(content="Stale edit.", expected_revision=1))
    assert await store.get("project_one", "memory_one") == validated
    await store.close()


def test_case_alias_cannot_hide_storage_inside_the_agent_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    private = workspace / "private"
    private.mkdir()
    alias = tmp_path / "WORKSPACE" / "private"
    if not alias.is_dir():
        pytest.skip("requires a case-insensitive filesystem")
    with pytest.raises(MemoryStoreError, match="memory_storage_denied"):
        SQLiteMemoryStore(alias / "memory.sqlite3", allowed_root=alias, workspace=workspace)
    assert not (private / "memory.sqlite3").exists()


@pytest.mark.asyncio
async def test_cancelling_a_pending_write_settles_and_rolls_back_before_returning(
    tmp_path: Path,
) -> None:
    workspace, private = tmp_path / "workspace", tmp_path / "private"
    workspace.mkdir()
    private.mkdir()
    reached_commit, release = threading.Event(), threading.Event()

    def pause_before_commit(stage: str) -> None:
        if stage == "before_commit":
            reached_commit.set()
            if not release.wait(timeout=5):
                raise TimeoutError("test transaction boundary was not released")

    database = private / "memory.sqlite3"
    store = SQLiteMemoryStore(
        database,
        allowed_root=private,
        workspace=workspace,
        transaction_observer=pause_before_commit,
    )
    entry = MemoryEntry.propose(
        "cancelled_proposal",
        "project_one",
        MemoryKind.REPAIR,
        topic="repair_assertion",
        content="Inspect the failing assertion first.",
        source_run_id="run_one",
        applicable_version="v1",
    )
    write = asyncio.create_task(store.create(entry))
    try:
        assert await asyncio.to_thread(reached_commit.wait, 3)
        write.cancel()
        await asyncio.sleep(0)
        write.cancel()
        await asyncio.sleep(0)
        assert not write.done(), "cancellation must settle the transaction worker"
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await write
    assert await store.get("project_one", "cancelled_proposal") is None
    await store.close()
    reopened = SQLiteMemoryStore(database, allowed_root=private, workspace=workspace)
    assert await reopened.get("project_one", "cancelled_proposal") is None
    await reopened.close()


@pytest.mark.asyncio
async def test_cancellation_after_commit_keeps_one_complete_record_and_close_waits(
    tmp_path: Path,
) -> None:
    workspace, private = tmp_path / "workspace", tmp_path / "private"
    workspace.mkdir()
    private.mkdir()
    committed, release = threading.Event(), threading.Event()

    def pause_after_commit(stage: str) -> None:
        if stage == "after_commit":
            committed.set()
            if not release.wait(timeout=5):
                raise TimeoutError("test transaction boundary was not released")

    database = private / "memory.sqlite3"
    store = SQLiteMemoryStore(
        database,
        allowed_root=private,
        workspace=workspace,
        transaction_observer=pause_after_commit,
    )
    entry = MemoryEntry.propose(
        "completed_proposal",
        "project_one",
        MemoryKind.REPAIR,
        topic="repair_assertion",
        content="Inspect the failing assertion first.",
        source_run_id="run_one",
        applicable_version="v1",
    )
    write = asyncio.create_task(store.create(entry))
    close: asyncio.Task[None] | None = None
    try:
        assert await asyncio.to_thread(committed.wait, 3)
        write.cancel()
        close = asyncio.create_task(store.close())
        await asyncio.sleep(0)
        assert not close.done(), "close must wait for the write worker"
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await write
    assert close is not None
    await close
    with pytest.raises(MemoryStoreError, match="memory_storage_closed"):
        await store.get("project_one", "completed_proposal")
    reopened = SQLiteMemoryStore(database, allowed_root=private, workspace=workspace)
    assert await reopened.get("project_one", "completed_proposal") == entry
    await reopened.close()


@pytest.mark.parametrize("stage", ["before_commit", "after_commit"])
@pytest.mark.parametrize("operation", ["create", "replace"])
@pytest.mark.asyncio
async def test_transaction_fault_has_an_explicit_reopenable_outcome(
    tmp_path: Path, stage: str, operation: str
) -> None:
    workspace, private = tmp_path / "workspace", tmp_path / "private"
    workspace.mkdir()
    private.mkdir()
    armed = False

    def fail_at_transaction_boundary(observed: str) -> None:
        if armed and observed == stage:
            raise OSError("fixture_private_diagnostic_must_not_escape")

    database = private / "memory.sqlite3"
    store = SQLiteMemoryStore(
        database,
        allowed_root=private,
        workspace=workspace,
        transaction_observer=fail_at_transaction_boundary,
    )
    proposed = MemoryEntry.propose(
        "memory_one",
        "project_one",
        MemoryKind.DIAGNOSIS,
        topic="repair_assertion",
        content="Inspect the failing assertion first.",
        source_run_id="run_one",
        applicable_version="v1",
    )
    before = None
    after = proposed
    if operation == "replace":
        await store.create(proposed)
        before = proposed
        after = proposed.revise(content="Check the assertion inputs.", expected_revision=1)
    armed = True
    write = store.create(after) if operation == "create" else store.replace(proposed, after)
    with pytest.raises(MemoryStoreError) as caught:
        await write
    expected_code = (
        "memory_transaction_failed"
        if stage == "before_commit"
        else "memory_committed_notification_failed"
    )
    assert str(caught.value) == expected_code
    await store.close()
    reopened = SQLiteMemoryStore(database, allowed_root=private, workspace=workspace)
    assert await reopened.get("project_one", "memory_one") == (
        before if stage == "before_commit" else after
    )
    await reopened.close()


@pytest.mark.asyncio
async def test_project_quota_rejects_entry_101_without_eviction_or_cross_project_leak(
    tmp_path: Path,
) -> None:
    workspace, private = tmp_path / "workspace", tmp_path / "private"
    workspace.mkdir()
    private.mkdir()
    store = SQLiteMemoryStore(private / "memory.sqlite3", allowed_root=private, workspace=workspace)
    for index in range(100):
        await store.create(
            MemoryEntry.propose(
                f"entry_{index}",
                "project_one",
                MemoryKind.DIAGNOSIS,
                topic="assertion_diagnosis",
                content="Inspect assertion inputs before changing the implementation.",
                source_run_id="run_one",
                applicable_version="v1",
            )
        )
    excess = MemoryEntry.propose(
        "entry_100",
        "project_one",
        MemoryKind.DIAGNOSIS,
        topic="assertion_diagnosis",
        content="Inspect assertion inputs before changing the implementation.",
        source_run_id="run_one",
        applicable_version="v1",
    )
    with pytest.raises(MemoryStoreError, match="memory_project_limit"):
        await store.create(excess)
    assert await store.get("project_one", "entry_100") is None
    for index in range(100):
        assert await store.get("project_one", f"entry_{index}") is not None
    other = MemoryEntry.propose(
        "entry_100",
        "project_two",
        MemoryKind.DIAGNOSIS,
        topic="assertion_diagnosis",
        content="Inspect assertion inputs before changing the implementation.",
        source_run_id="run_two",
        applicable_version="v1",
    )
    assert await store.create(other) == other
    assert await store.get("project_two", "entry_0") is None
    await store.close()


@pytest.mark.asyncio
async def test_replacing_the_granted_directory_invalidates_the_open_store(tmp_path: Path) -> None:
    workspace, private = tmp_path / "workspace", tmp_path / "private"
    workspace.mkdir()
    private.mkdir()
    database = private / "memory.sqlite3"
    store = SQLiteMemoryStore(database, allowed_root=private, workspace=workspace)
    original_directory = tmp_path / "original_private"
    private.rename(original_directory)
    private.mkdir()
    with pytest.raises(MemoryStoreError, match="memory_storage_denied"):
        await store.get("project_one", "memory_one")
    assert not database.exists()
    await store.close()
