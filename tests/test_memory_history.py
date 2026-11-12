"""P3 storage contracts against real private SQLite and atomic file publication."""

import asyncio
import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable
from contextlib import closing
from dataclasses import replace
from itertools import pairwise
from pathlib import Path

import pytest

from minidsh.adapters.memory import SQLiteMemoryStore
from minidsh.domain.memory import (
    MemoryEntry,
    MemoryEvidence,
    MemoryEvidenceKind,
    MemoryKind,
)
from minidsh.domain.memory_history import MemoryHistoryRecord
from minidsh.domain.memory_notes import MemoryNote, MemoryNoteSource
from minidsh.ports.memory import MemoryStoreError

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_n11_history_schema_rejects_entry_101_without_eviction(tmp_path: Path) -> None:
    store, database = _store(tmp_path)
    try:
        await store.migrate_history()
        for index in range(100):
            await store.create(_chain(f"note_{index}")[0])
        before = database.read_bytes()
        with pytest.raises(MemoryStoreError, match="memory_project_limit"):
            await store.create(_chain("extra")[0])
        assert database.read_bytes() == before
        assert len(await store.snapshot("alpha")) == 100
        assert len((await store.history("alpha", "note_0")).records) == 1
        await store.create(_chain("other", "beta")[0])
        assert len(await store.snapshot("beta")) == 1
    finally:
        await store.close()


def _store(
    root: Path, observer: Callable[[str], None] | None = None
) -> tuple[SQLiteMemoryStore, Path]:
    workspace, private = root / "workspace", root / "private"
    workspace.mkdir(exist_ok=True)
    private.mkdir(exist_ok=True)
    database = private / "memory.sqlite3"
    return SQLiteMemoryStore(
        database, allowed_root=private, workspace=workspace, transaction_observer=observer
    ), database


def _chain(entry_id: str, project: str = "alpha") -> list[MemoryEntry]:
    proposed = MemoryEntry.propose(
        entry_id,
        project,
        MemoryKind.ENVIRONMENT,
        topic=entry_id,
        content="Use uv for project commands.",
        source_run_id="source_one",
        applicable_version="v1",
    )
    validated = proposed.validate(
        MemoryEvidence(
            "a" * 64,
            project,
            "v1",
            MemoryEvidenceKind.OPERATOR_CONFIGURATION,
            "source_one",
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
    revised = committed.revise(
        content="Use uv and wait for readback.", expected_revision=1, source_run_id="source_two"
    )
    return [proposed, validated, approved, committed, revised, revised.reject(expected_revision=2)]


def _inspect(database: Path) -> tuple[int, list[tuple[str, str, str]]]:
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.deserialize(database.read_bytes())
        return (
            connection.execute("PRAGMA user_version").fetchone()[0],
            connection.execute(
                "SELECT project_id,entry_id,payload FROM entries ORDER BY project_id,entry_id"
            ).fetchall(),
        )


@pytest.mark.asyncio
async def test_n05_explicit_migration_preserves_current_bytes_and_never_invents_past_states(
    tmp_path: Path,
) -> None:
    store, database = _store(tmp_path)
    try:
        for index in range(4):
            chain = _chain(f"entry_{index}")[: index + 1]
            await store.create(chain[0])
            for previous, updated in pairwise(chain):
                await store.replace(previous, updated)
        rejected = _chain("rejected")[0]
        await store.create(rejected)
        await store.replace(rejected, rejected.reject(expected_revision=1))
        await store.create(_chain("foreign", "beta")[0])
        before = database.read_bytes()
        original_rows = _inspect(database)[1]
        assert not await store.history_enabled()
        await store.snapshot("alpha")
        assert database.read_bytes() == before
        receipt = await store.migrate_history()
        assert receipt.changed
        assert (receipt.from_version, receipt.to_version) == (1, 2)
        assert receipt.backup_sha256 == hashlib.sha256(before).hexdigest()
        assert receipt.backup_name is not None
        assert (database.parent / receipt.backup_name).read_bytes() == before
        assert _inspect(database) == (2, original_rows)
        (tmp_path / "memory_effects.json").write_text(
            json.dumps(
                {
                    "scope": "explicit migration preserves raw current rows and backup",
                    "before_sha256": hashlib.sha256(before).hexdigest(),
                    "after_sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
                    "backup_sha256": receipt.backup_sha256,
                    "current_rows_unchanged": True,
                }
            )
        )
        for project_id, entry_id, _ in original_rows:
            page = await store.history(project_id, entry_id)
            assert len(page.records) == 1
            assert page.records[0].operation == "migration_snapshot"
            assert page.records[0].before is None
            assert page.records[0].after == await store.get(project_id, entry_id)
        assert (await store.history("alpha", "foreign")).records == ()
        after = database.read_bytes()
        again = await store.migrate_history()
        assert not again.changed
        assert database.read_bytes() == after
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_n06_version_two_without_history_is_corrupt_not_an_idempotent_migration(
    tmp_path: Path,
) -> None:
    store, database = _store(tmp_path)
    await store.migrate_history()
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.deserialize(database.read_bytes())
        connection.execute("DROP TABLE memory_history")
        connection.commit()
        database.write_bytes(connection.serialize())
    before, names = database.read_bytes(), set(database.parent.iterdir())
    try:
        with pytest.raises(MemoryStoreError):
            await store.migrate_history()
        assert database.read_bytes() == before
        assert set(database.parent.iterdir()) == names
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_n06_post_publish_notification_failure_requires_readback_not_rollback(
    tmp_path: Path,
) -> None:
    armed = False

    def observer(stage: str) -> None:
        if armed and stage == "after_commit":
            raise OSError("fixture notification fault")

    store, database = _store(tmp_path, observer)
    await store.create(_chain("note")[0])
    original_rows = _inspect(database)[1]
    before = database.read_bytes()
    armed = True
    try:
        with pytest.raises(MemoryStoreError, match="memory_committed_notification_failed"):
            await store.migrate_history()
        assert _inspect(database) == (2, original_rows)
        assert database.read_bytes() != before
        assert await store.history_enabled()
        assert len((await store.history("alpha", "note")).records) == 1
        after = database.read_bytes()
        assert not (await store.migrate_history()).changed
        assert database.read_bytes() == after
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_n06_cancel_after_publication_leaves_a_readable_committed_migration(
    tmp_path: Path,
) -> None:
    entered, release = threading.Event(), threading.Event()
    armed = False

    def observer(stage: str) -> None:
        if armed and stage == "after_commit":
            entered.set()
            if not release.wait(5):
                raise OSError("fixture release deadline")

    store, database = _store(tmp_path, observer)
    await store.create(_chain("note")[0])
    original_rows = _inspect(database)[1]
    armed = True
    pending = asyncio.create_task(store.migrate_history())
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        pending.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert _inspect(database) == (2, original_rows)
        assert await store.history_enabled()
        assert not (await store.migrate_history()).changed
    finally:
        release.set()
        if not pending.done():
            await pending
        await store.close()


@pytest.mark.asyncio
async def test_n07_fresh_history_records_every_real_transition_and_pages_without_cross_project_leak(
    tmp_path: Path,
) -> None:
    store, database = _store(tmp_path)
    chain = _chain("note")
    try:
        receipt = await store.migrate_history()
        assert receipt.changed
        assert receipt.from_version is None
        assert receipt.backup_name is None
        assert await store.history_enabled()
        await store.create(chain[0])
        for previous, updated in pairwise(chain):
            await store.replace(previous, updated)
        records: list[MemoryHistoryRecord] = []
        cursor = 0
        while True:
            page = await store.history("alpha", "note", after_sequence=cursor, limit=2)
            records.extend(page.records)
            if page.next_after is None:
                break
            assert page.next_after > cursor
            cursor = page.next_after
        assert [row.after for row in records] == chain
        assert [row.before for row in records] == [None, *chain[:-1]]
        assert [row.operation for row in records] == [
            "created",
            "validated",
            "approved",
            "committed",
            "revised",
            "rejected",
        ]
        assert [row.sequence for row in records] == sorted({row.sequence for row in records})
        assert (await store.history("beta", "note")).records == ()
        assert await store.revision("alpha", "note", 1, chain[3].proposal_sha256) == chain[3]
        assert await store.revision("beta", "note", 1, chain[3].proposal_sha256) is None
        assert await store.get("alpha", "note") == chain[-1]
        before = database.read_bytes()
        await store.history("alpha", "note")
        assert database.read_bytes() == before
    finally:
        await store.close()


@pytest.mark.parametrize("version", [9, 0])
@pytest.mark.asyncio
async def test_n06_unknown_versions_are_not_rebuilt_or_backed_up(
    tmp_path: Path, version: int
) -> None:
    store, database = _store(tmp_path)
    await store.create(_chain("note")[0])
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.deserialize(database.read_bytes())
        connection.execute(f"PRAGMA user_version={version}")
        database.write_bytes(connection.serialize())
    before, names = database.read_bytes(), set(database.parent.iterdir())
    try:
        with pytest.raises(MemoryStoreError, match="memory_schema_unsupported"):
            await store.migrate_history()
        assert database.read_bytes() == before
        assert set(database.parent.iterdir()) == names
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_n06_corrupt_database_is_preserved_without_migration_or_backup(
    tmp_path: Path,
) -> None:
    store, database = _store(tmp_path)
    database.write_bytes(b"not a sqlite database")
    before, names = database.read_bytes(), set(database.parent.iterdir())
    try:
        with pytest.raises(MemoryStoreError):
            await store.migrate_history()
        assert database.read_bytes() == before
        assert set(database.parent.iterdir()) == names
    finally:
        await store.close()


@pytest.mark.parametrize("fault", ["before_migration_publish", "before_storage_publish"])
@pytest.mark.asyncio
async def test_n06_migration_failure_leaves_original_database_and_only_a_valid_backup(
    tmp_path: Path, fault: str
) -> None:
    armed = False

    def observer(stage: str) -> None:
        if armed and stage == fault:
            raise OSError("fixture migration fault")

    store, database = _store(tmp_path, observer)
    await store.create(_chain("note")[0])
    before = database.read_bytes()
    armed = True
    try:
        with pytest.raises(MemoryStoreError):
            await store.migrate_history()
        assert database.read_bytes() == before
        assert _inspect(database)[0] == 1
        backups = list(database.parent.glob("*.pre-history-*.sqlite3"))
        assert len(backups) == 1
        assert backups[0].read_bytes() == before
        assert not list(database.parent.glob(".memory-write-*"))
        armed = False
        assert (await store.migrate_history()).changed
    finally:
        await store.close()


@pytest.mark.parametrize("checkpoint", ["before_migration_publish", "before_storage_publish"])
@pytest.mark.asyncio
async def test_n06_cancelled_migration_settles_thread_before_releasing_store(
    tmp_path: Path, checkpoint: str
) -> None:
    entered, release = threading.Event(), threading.Event()
    armed = False

    def observer(stage: str) -> None:
        if armed and stage == checkpoint:
            entered.set()
            if not release.wait(5):
                raise OSError("fixture release deadline")

    store, database = _store(tmp_path, observer)
    await store.create(_chain("note")[0])
    before = database.read_bytes()
    armed = True
    pending = asyncio.create_task(store.migrate_history())
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert database.read_bytes() == before
        assert not await store.history_enabled()
    finally:
        release.set()
        if not pending.done():
            await pending
        await store.close()


@pytest.mark.parametrize("limit", [0, 101, True])
@pytest.mark.asyncio
async def test_n11_history_query_bounds_precede_storage_io(tmp_path: Path, limit: int) -> None:
    store, _ = _store(tmp_path)
    await store.close()
    with pytest.raises(MemoryStoreError, match="memory_history_query_invalid"):
        await store.history("alpha", "note", limit=limit)


@pytest.mark.asyncio
async def test_n07_history_write_and_current_record_roll_back_together(tmp_path: Path) -> None:
    armed = False

    def observer(stage: str) -> None:
        if armed and stage == "before_storage_publish":
            raise OSError("fixture publish fault")

    store, database = _store(tmp_path, observer)
    chain = _chain("note")
    await store.migrate_history()
    await store.create(chain[0])
    before = database.read_bytes()
    armed = True
    try:
        with pytest.raises(MemoryStoreError):
            await store.replace(chain[0], chain[1])
        assert database.read_bytes() == before
        assert await store.get("alpha", "note") == chain[0]
        assert [r.after for r in (await store.history("alpha", "note")).records] == [chain[0]]
    finally:
        await store.close()


async def _replacement(
    store: SQLiteMemoryStore,
) -> tuple[tuple[MemoryEntry, MemoryEntry], ...]:
    originals: list[MemoryEntry] = []
    for name in ("old_a", "old_b"):
        chain = _chain(name)[:4]
        await store.create(chain[0])
        for previous, updated in pairwise(chain):
            await store.replace(previous, updated)
        originals.append(chain[-1])
    sources = tuple(MemoryNoteSource.from_entry(entry) for entry in originals)
    note = MemoryNote(
        "On project commands.",
        "Use uv and check completion.",
        "Run the contract.",
        sources=sources,
        supersedes=sources,
    )
    proposal = MemoryEntry.propose(
        "replacement",
        "alpha",
        MemoryKind.ENVIRONMENT,
        topic="old_a",
        content=note.to_content(),
        source_run_id="new_source",
        applicable_version="v1",
    )
    validated = proposal.validate(
        MemoryEvidence(
            "c" * 64,
            "alpha",
            "v1",
            MemoryEvidenceKind.OPERATOR_CONFIGURATION,
            "new_source",
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
    await store.create(proposal)
    await store.replace(proposal, validated)
    await store.replace(validated, approved)
    return (
        (approved, approved.commit(expected_revision=1, approval_sha256=approved.approval_sha256)),
        *((entry, entry.supersede(expected_revision=1)) for entry in originals),
    )


@pytest.mark.asyncio
async def test_n09_two_old_notes_and_new_commit_change_atomically_and_reconcile_replay(
    tmp_path: Path,
) -> None:
    store, database = _store(tmp_path)
    try:
        await store.migrate_history()
        changes = await _replacement(store)
        assert await store.replace_many(changes) == tuple(updated for _, updated in changes)
        for previous, updated in changes:
            assert await store.get("alpha", updated.entry_id) == updated
            history = await store.history("alpha", updated.entry_id)
            assert history.records[-1].before == previous
            assert history.records[-1].after == updated
        before = database.read_bytes()
        assert await store.replace_many(changes) == tuple(updated for _, updated in changes)
        assert database.read_bytes() == before
    finally:
        await store.close()


@pytest.mark.parametrize("fault", ["stale", "publish"])
@pytest.mark.asyncio
async def test_n09_stale_reference_or_publish_failure_cannot_partially_supersede(
    tmp_path: Path, fault: str
) -> None:
    armed = False

    def observer(stage: str) -> None:
        if armed and stage == "before_storage_publish":
            raise OSError("fixture replacement publication fault")

    store, database = _store(tmp_path, observer)
    try:
        await store.migrate_history()
        changes = await _replacement(store)
        if fault == "stale":
            old = changes[-1][0]
            await store.replace(old, old.revise(content="A changed rule.", expected_revision=1))
        else:
            armed = True
        before = database.read_bytes()
        histories = [await store.history("alpha", previous.entry_id) for previous, _ in changes]
        with pytest.raises(MemoryStoreError):
            await store.replace_many(changes)
        assert database.read_bytes() == before
        assert [
            await store.history("alpha", previous.entry_id) for previous, _ in changes
        ] == histories
        assert await store.get("alpha", changes[0][0].entry_id) == changes[0][0]
    finally:
        await store.close()


@pytest.mark.parametrize("invalid", ["omit", "cross_project", "duplicate", "unapproved"])
@pytest.mark.asyncio
async def test_n10_replacement_must_exactly_match_the_approved_supersedes_relation(
    tmp_path: Path, invalid: str
) -> None:
    store, database = _store(tmp_path)
    try:
        await store.migrate_history()
        changes = await _replacement(store)
        if invalid == "omit":
            changes = changes[:-1]
        elif invalid == "cross_project":
            old = _chain("foreign", "beta")[3]
            changes = (*changes[:2], (old, old.supersede(expected_revision=1)))
        elif invalid == "duplicate":
            changes = (*changes, changes[-1])
        else:
            changes = (
                (changes[0][0].revise(content="unapproved", expected_revision=1), changes[0][1]),
                *changes[1:],
            )
        before = database.read_bytes()
        with pytest.raises((MemoryStoreError, ValueError)):
            await store.replace_many(changes)
        assert database.read_bytes() == before
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_n09_individual_replace_cannot_create_an_orphaned_supersession(
    tmp_path: Path,
) -> None:
    store, database = _store(tmp_path)
    try:
        await store.migrate_history()
        changes = await _replacement(store)
        before = database.read_bytes()
        for previous, updated in changes:
            with pytest.raises(MemoryStoreError, match="memory_atomic_replacement_required"):
                await store.replace(previous, updated)
        assert database.read_bytes() == before
    finally:
        await store.close()


@pytest.mark.parametrize("collision", ["different_bytes", "symlink", "hardlink"])
@pytest.mark.asyncio
async def test_n06_existing_backup_capability_cannot_be_overwritten_or_redirected(
    tmp_path: Path, collision: str
) -> None:
    store, database = _store(tmp_path)
    await store.create(_chain("note")[0])
    before = database.read_bytes()
    target = database.parent / (
        database.name + ".pre-history-" + hashlib.sha256(before).hexdigest() + ".sqlite3"
    )
    outside = tmp_path / "outside"
    outside.write_text("outside sentinel")
    if collision == "different_bytes":
        target.write_text("different backup")
    elif collision == "symlink":
        target.symlink_to(outside)
    else:
        target.hardlink_to(outside)
    names = set(database.parent.iterdir())
    try:
        with pytest.raises(MemoryStoreError):
            await store.migrate_history()
        assert database.read_bytes() == before
        assert outside.read_text() == "outside sentinel"
        assert set(database.parent.iterdir()) == names
    finally:
        await store.close()


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.asyncio
async def test_n08_structured_store_writes_require_explicit_history_migration(
    tmp_path: Path, *, existing: bool
) -> None:
    store, database = _store(tmp_path)
    note = MemoryNote("On commands.", "Use uv.", "Run a check.")
    proposed = replace(_chain("note")[0], content=note.to_content())
    if existing:
        await store.create(_chain("old")[0])
    before = database.read_bytes() if existing else None
    names = set(database.parent.iterdir())
    try:
        with pytest.raises(MemoryStoreError, match="memory_history_required"):
            await store.create(proposed)
        assert (database.read_bytes() if database.exists() else None) == before
        assert set(database.parent.iterdir()) == names
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_n11_real_history_growth_reaches_eight_mib_without_eviction_or_partial_publish(
    tmp_path: Path,
) -> None:
    store, database = _store(tmp_path)
    expected: dict[str, MemoryEntry] = {}
    rejected = False
    failure_code: str | None = None
    try:
        await store.migrate_history()
        for index in range(100):
            entry = MemoryEntry.propose(
                f"large_{index}",
                "alpha",
                MemoryKind.ENVIRONMENT,
                topic=f"large_{index}",
                content="x" * 2048,
                source_run_id="source_one",
                applicable_version="v1",
                paths=tuple("src/" + str(i).zfill(2) + "_" + "p" * 505 for i in range(32)),
            )
            evidence = MemoryEvidence(
                "a" * 64,
                "alpha",
                "v1",
                MemoryEvidenceKind.OPERATOR_CONFIGURATION,
                "source_one",
                accepted=True,
                source_exists=True,
                confirmed_by="fixture_operator",
                confirmed_at="2026-01-01T00:00:00+00:00",
            )
            verified = entry.validate(evidence)
            approved = verified.approve(
                expected_revision=1,
                content_sha256=verified.content_sha256,
                approver="fixture_operator",
                approved_at="2026-01-01T00:00:00+00:00",
            )
            states = (
                entry,
                verified,
                approved,
                approved.commit(expected_revision=1, approval_sha256=approved.approval_sha256),
            )
            for position, state in enumerate(states):
                before = database.read_bytes()
                try:
                    if position == 0:
                        await store.create(state)
                    else:
                        await store.replace(states[position - 1], state)
                except MemoryStoreError as error:
                    failure_code = error.code
                    assert database.read_bytes() == before
                    rejected = True
                    break
                expected[state.entry_id] = state
            if rejected:
                break
        assert rejected
        assert failure_code == "memory_storage_limit"
        assert database.stat().st_size <= 8 * 1024 * 1024
        assert database.stat().st_size > 7 * 1024 * 1024
        assert {entry.entry_id: entry for entry in await store.snapshot("alpha")} == expected
        assert len(expected) < 100
        for entry_id, state in expected.items():
            page = await store.history("alpha", entry_id)
            assert page.records[-1].after == state
        assert not list(database.parent.glob(".memory-write-*"))
    finally:
        await store.close()
