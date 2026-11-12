"""Explicit private SQLite capability; no import-time storage or implicit home paths."""

import asyncio
import hashlib
import importlib
import os
import re
import sqlite3
import stat
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager, suppress
from dataclasses import asdict
from pathlib import Path
from threading import Event
from typing import TypeVar

from pydantic import BaseModel, ConfigDict, ValidationError

from minidsh.domain.memory import (
    MAX_PROJECT_ENTRIES,
    MemoryEntry,
    MemoryKind,
    MemoryStatus,
    MemoryValidationError,
)
from minidsh.domain.memory_history import (
    MAX_HISTORY_PAGE,
    MemoryHistoryPage,
    MemoryHistoryRecord,
    MemoryMigration,
)
from minidsh.domain.memory_notes import (
    check_supersession,
    parse_memory_note,
    permits_pending_supersession,
)
from minidsh.ports.memory import MemoryStoreError

_APPLICATION_ID = 0x4D44534D
_SCHEMA_VERSION = 1
_HISTORY_SCHEMA = 2
_VERIFIED = frozenset({MemoryStatus.VALIDATED, MemoryStatus.APPROVED, MemoryStatus.COMMITTED})
_T = TypeVar("_T")
_DATABASE_LIMIT = 8 * 1024 * 1024


def _signature(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


class _StoredEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    entry_id: str
    project_id: str
    kind: MemoryKind
    topic: str
    content: str
    source_run_id: str
    applicable_version: str
    paths: tuple[str, ...]
    error_type: str | None
    schema_version: str
    revision: int
    status: MemoryStatus
    evidence_sha256: str | None
    validation_sha256: str | None
    approved_by: str | None
    approved_at: str | None
    approved_revision: int | None
    approval_sha256: str | None


def _encode(entry: MemoryEntry) -> str:
    return _StoredEntry.model_validate(asdict(entry)).model_dump_json()


def _decode(raw: object) -> MemoryEntry:
    if not isinstance(raw, str):
        raise MemoryStoreError("memory_storage_corrupt")
    try:
        return MemoryEntry(**_StoredEntry.model_validate_json(raw).model_dump())
    except (ValidationError, MemoryValidationError):
        raise MemoryStoreError("memory_storage_corrupt") from None


class SQLiteMemoryStore:
    """The embedding application grants one private file outside its agent workspace.

    This adapter is not an OS sandbox. Runtime integration must also isolate process
    capabilities before exposing memory to an agent. The model can only propose records.
    The optional transaction observer is a synchronous storage fault-injection boundary;
    it receives only a fixed stage name, never entry contents or private paths.
    """

    def __init__(
        self,
        database: Path,
        *,
        allowed_root: Path,
        workspace: Path,
        transaction_observer: Callable[[str], None] | None = None,
    ) -> None:
        try:
            if (
                os.name != "posix"
                or not {os.open, os.stat, os.unlink, os.link, os.rename} <= os.supports_dir_fd
                or not hasattr(os, "O_NOFOLLOW")
                or not hasattr(os, "O_DIRECTORY")
                or not hasattr(sqlite3.Connection, "serialize")
                or not hasattr(sqlite3.Connection, "deserialize")
            ):
                raise ValueError
            self._root = allowed_root.resolve(strict=True)
            self._workspace = workspace.resolve(strict=True)
            if not self._root.is_dir() or database.parent.resolve(strict=True) != self._root:
                raise ValueError
            self._database = self._root / database.name
            root_info = self._root.stat()
            self._root_identity = (root_info.st_dev, root_info.st_ino)
            self._closed = False
            self._lock = asyncio.Lock()
            self._transaction_observer = transaction_observer
            self._directory: int | None = None
            self._file_identity: tuple[int, int, int, int, int] | None = None
            self._checked_path()
        except (OSError, ValueError, RuntimeError):
            raise MemoryStoreError("memory_storage_denied") from None

    def _checked_path(self) -> None:
        root_info = self._root.stat()
        if (root_info.st_dev, root_info.st_ino) != self._root_identity:
            raise MemoryStoreError("memory_storage_denied")
        if self._database.is_symlink() or self._database.resolve().is_relative_to(self._workspace):
            raise MemoryStoreError("memory_storage_denied")
        if any(parent.samefile(self._workspace) for parent in self._database.parents):
            raise MemoryStoreError("memory_storage_denied")
        if self._database.parent.resolve(strict=True) != self._root:
            raise MemoryStoreError("memory_storage_denied")
        if self._database.exists() and (
            not self._database.is_file() or self._database.stat().st_nlink != 1
        ):
            raise MemoryStoreError("memory_storage_denied")

    def _check_open(self) -> None:
        if self._closed:
            raise MemoryStoreError("memory_storage_closed")
        try:
            self._checked_path()
        except MemoryStoreError:
            raise
        except (OSError, ValueError, RuntimeError):
            raise MemoryStoreError("memory_storage_denied") from None

    @staticmethod
    def _check_schema(connection: sqlite3.Connection) -> None:
        application = connection.execute("PRAGMA application_id").fetchone()[0]
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if application != _APPLICATION_ID or version not in {_SCHEMA_VERSION, _HISTORY_SCHEMA}:
            raise MemoryStoreError("memory_schema_unsupported")
        if version == _HISTORY_SCHEMA:
            try:
                connection.execute(
                    "SELECT sequence,project_id,entry_id,revision,proposal_sha256,status,"
                    "operation,before_payload,after_payload FROM memory_history LIMIT 0"
                )
            except sqlite3.Error:
                raise MemoryStoreError("memory_storage_corrupt") from None

    def _initialize(self, cancelled: Event) -> None:
        if self._read_database() is not None:
            return
        with closing(sqlite3.connect(":memory:")) as connection, connection:
            connection.execute("PRAGMA temp_store = MEMORY")
            connection.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            connection.execute(
                "CREATE TABLE entries (project_id TEXT NOT NULL, entry_id TEXT NOT NULL, "
                "payload TEXT NOT NULL, PRIMARY KEY(project_id, entry_id))"
            )
            connection.commit()
            self._before_commit(cancelled, stage="before_initialize_publish")
            self._publish(connection.serialize(), cancelled=cancelled, exclusive=True)

    def _observe(self, stage: str) -> None:
        if self._transaction_observer is not None:
            try:
                self._transaction_observer(stage)
            except Exception:
                raise MemoryStoreError("memory_transaction_failed") from None

    @contextmanager
    def _directory_lease(self, cancelled: Event) -> Iterator[None]:
        descriptor = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if (info.st_dev, info.st_ino) != self._root_identity:
                raise MemoryStoreError("memory_storage_denied")
            locking = importlib.import_module("fcntl")
            deadline = time.monotonic() + 0.1
            while True:
                try:
                    locking.flock(descriptor, locking.LOCK_EX | locking.LOCK_NB)
                    break
                except BlockingIOError:
                    if cancelled.is_set():
                        raise MemoryStoreError("memory_write_cancelled") from None
                    if time.monotonic() >= deadline:
                        raise MemoryStoreError("memory_storage_busy") from None
                    time.sleep(0.002)
            self._directory = descriptor
            self._file_identity = None
            self._checked_path()
            yield
        finally:
            self._directory = None
            self._file_identity = None
            os.close(descriptor)

    def _read_database(self) -> bytes | None:
        directory = self._directory
        if directory is None:
            raise MemoryStoreError("memory_storage_denied")
        for suffix in ("-journal", "-wal", "-shm"):
            try:
                os.stat(self._database.name + suffix, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise MemoryStoreError("memory_storage_busy")
        # This fault boundary immediately precedes the anchored O_NOFOLLOW open;
        # no second pathname lookup is handed to SQLite after it.
        self._observe("before_storage_read")
        try:
            descriptor = os.open(
                self._database.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory,
            )
        except FileNotFoundError:
            self._file_identity = None
            self._checked_path()
            return None
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise MemoryStoreError("memory_storage_denied")
            if before.st_size > _DATABASE_LIMIT:
                raise MemoryStoreError("memory_storage_limit")
            data = stream.read(_DATABASE_LIMIT + 1)
            after = os.fstat(stream.fileno())
            if len(data) > _DATABASE_LIMIT:
                raise MemoryStoreError("memory_storage_limit")
            current = os.stat(self._database.name, dir_fd=directory, follow_symlinks=False)
            if _signature(before) != _signature(after) or _signature(after) != _signature(current):
                raise MemoryStoreError("memory_storage_denied")
        self._file_identity = _signature(after)
        self._checked_path()
        return data

    def _connection(self) -> sqlite3.Connection:
        data = self._read_database()
        if data is None:
            raise MemoryStoreError("memory_storage_denied")
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("PRAGMA temp_store = MEMORY")
            if data:
                connection.deserialize(data)
            return connection
        except BaseException:
            connection.close()
            raise

    def _publish(self, data: bytes, *, cancelled: Event, exclusive: bool = False) -> None:
        directory = self._directory
        if directory is None:
            raise MemoryStoreError("memory_storage_denied")
        if len(data) > _DATABASE_LIMIT:
            raise MemoryStoreError("memory_storage_limit")
        self._checked_path()
        try:
            current = os.stat(self._database.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            identity = None
        else:
            identity = _signature(current)
        if identity != self._file_identity:
            raise MemoryStoreError("memory_storage_denied")
        name = ".memory-write-" + uuid.uuid4().hex
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self._checked_path()
            self._observe("before_storage_publish")
            if cancelled.is_set():
                raise MemoryStoreError("memory_write_cancelled")
            if exclusive:
                os.link(
                    name,
                    self._database.name,
                    src_dir_fd=directory,
                    dst_dir_fd=directory,
                    follow_symlinks=False,
                )
            else:
                os.replace(name, self._database.name, src_dir_fd=directory, dst_dir_fd=directory)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(name, dir_fd=directory)
        try:
            self._checked_path()
        except (MemoryStoreError, OSError, ValueError, RuntimeError):
            # Publication already targeted the granted physical directory. A
            # lost path binding cannot be reported as a transaction rollback.
            raise MemoryStoreError("memory_committed_notification_failed") from None

    def _guarded(self, operation: Callable[[Event], _T], cancelled: Event) -> _T:
        with self._directory_lease(cancelled):
            return operation(cancelled)

    async def create(self, entry: MemoryEntry) -> MemoryEntry:
        if entry.status is not MemoryStatus.PROPOSED:
            raise MemoryStoreError("memory_transition_invalid")
        return await self._run(lambda cancelled: self._create(entry, cancelled))

    async def _run(self, operation: Callable[[Event], _T]) -> _T:
        async with self._lock:
            self._check_open()
            cancelled = Event()
            worker = asyncio.create_task(asyncio.to_thread(self._guarded, operation, cancelled))
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                cancelled.set()
                # A thread cannot be cancelled. Settle it before releasing the lock,
                # including when the caller receives repeated cancellation requests.
                while True:
                    try:
                        await asyncio.shield(worker)
                        break
                    except asyncio.CancelledError:
                        if worker.cancelled():
                            break
                        continue
                    except Exception:
                        break
                raise
            except OSError:
                raise MemoryStoreError("memory_storage_denied") from None

    def _before_commit(self, cancelled: Event, *, stage: str = "before_commit") -> None:
        if self._transaction_observer is not None:
            try:
                self._transaction_observer(stage)
            except Exception:
                raise MemoryStoreError("memory_transaction_failed") from None
        self._checked_path()
        if cancelled.is_set():
            raise MemoryStoreError("memory_write_cancelled")

    def _after_commit(self) -> None:
        if self._transaction_observer is not None:
            try:
                self._transaction_observer("after_commit")
            except Exception:
                # The transaction is already durable. The caller must reconcile
                # by scoped ID; this error must not imply that it was rolled back.
                raise MemoryStoreError("memory_committed_notification_failed") from None

    def _create(self, entry: MemoryEntry, cancelled: Event) -> MemoryEntry:
        try:
            self._checked_path()
            note = parse_memory_note(entry.content)
            if note is not None and not self._database.exists():
                raise MemoryStoreError("memory_history_required")
            self._initialize(cancelled)
            with closing(self._connection()) as connection, connection:
                self._check_schema(connection)
                if note is not None:
                    self._require_history(connection)
                connection.execute("BEGIN IMMEDIATE")
                count = connection.execute(
                    "SELECT count(*) FROM entries WHERE project_id=?", (entry.project_id,)
                ).fetchone()[0]
                if count >= MAX_PROJECT_ENTRIES:
                    raise MemoryStoreError("memory_project_limit")
                connection.execute(
                    "INSERT INTO entries VALUES (?, ?, ?)",
                    (
                        entry.project_id,
                        entry.entry_id,
                        _encode(entry),
                    ),
                )
                self._append_history(connection, None, entry)
                self._before_commit(cancelled)
                connection.commit()
                self._publish(connection.serialize(), cancelled=cancelled)
            self._after_commit()
            return entry
        except sqlite3.IntegrityError:
            raise MemoryStoreError("memory_duplicate") from None
        except sqlite3.Error:
            raise MemoryStoreError("memory_transaction_failed") from None

    @staticmethod
    def _create_history_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE TABLE memory_history (sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
            "project_id TEXT NOT NULL, entry_id TEXT NOT NULL, revision INTEGER NOT NULL, "
            "proposal_sha256 TEXT NOT NULL, status TEXT NOT NULL, operation TEXT NOT NULL, "
            "before_payload TEXT, after_payload TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE INDEX memory_history_entry ON memory_history(project_id,entry_id,sequence)"
        )
        connection.execute(
            "CREATE INDEX memory_history_revision ON "
            "memory_history(project_id,entry_id,revision,proposal_sha256,status)"
        )

    @staticmethod
    def _require_history(connection: sqlite3.Connection) -> None:
        SQLiteMemoryStore._check_schema(connection)
        if connection.execute("PRAGMA user_version").fetchone()[0] != _HISTORY_SCHEMA:
            raise MemoryStoreError("memory_history_required")

    @staticmethod
    def _append_history(
        connection: sqlite3.Connection,
        before: MemoryEntry | None,
        after: MemoryEntry,
        *,
        operation: str | None = None,
    ) -> None:
        if connection.execute("PRAGMA user_version").fetchone()[0] != _HISTORY_SCHEMA:
            return
        if operation is None:
            operation = (
                "created"
                if before is None
                else "revised"
                if before.revision != after.revision
                else after.status.value
            )
        connection.execute(
            "INSERT INTO memory_history(project_id,entry_id,revision,proposal_sha256,status,"
            "operation,before_payload,after_payload) VALUES(?,?,?,?,?,?,?,?)",
            (
                after.project_id,
                after.entry_id,
                after.revision,
                after.proposal_sha256,
                after.status.value,
                operation,
                _encode(before) if before is not None else None,
                _encode(after),
            ),
        )

    def _backup(self, data: bytes) -> tuple[str, str]:
        directory = self._directory
        if directory is None:
            raise MemoryStoreError("memory_storage_denied")
        digest = hashlib.sha256(data).hexdigest()
        target = self._database.name + ".pre-history-" + digest + ".sqlite3"
        name = ".memory-write-" + uuid.uuid4().hex
        descriptor = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self._checked_path()
            try:
                os.link(
                    name, target, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False
                )
            except FileExistsError:
                self._verify_backup(target, data)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(name, dir_fd=directory)
        return target, digest

    def _verify_backup(self, name: str, data: bytes) -> None:
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self._directory
        )
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_size != len(data)
            ):
                raise MemoryStoreError("memory_backup_conflict")
            found = stream.read(_DATABASE_LIMIT + 1)
            after = os.fstat(stream.fileno())
            current = os.stat(name, dir_fd=self._directory, follow_symlinks=False)
            if (
                found != data
                or _signature(before) != _signature(after)
                or _signature(after) != _signature(current)
            ):
                raise MemoryStoreError("memory_backup_conflict")

    async def migrate_history(self) -> MemoryMigration:
        return await self._run(self._migrate_history)

    def _migrate_history(self, cancelled: Event) -> MemoryMigration:
        data = self._read_database()
        try:
            with closing(sqlite3.connect(":memory:")) as connection, connection:
                connection.execute("PRAGMA temp_store=MEMORY")
                version: int | None = None
                if data is not None:
                    connection.deserialize(data)
                    self._check_schema(connection)
                    version = connection.execute("PRAGMA user_version").fetchone()[0]
                    if version == _HISTORY_SCHEMA:
                        return MemoryMigration(
                            changed=False, from_version=version, to_version=_HISTORY_SCHEMA
                        )
                else:
                    connection.execute(f"PRAGMA application_id={_APPLICATION_ID}")
                    connection.execute(
                        "CREATE TABLE entries (project_id TEXT NOT NULL, "
                        "entry_id TEXT NOT NULL, payload TEXT NOT NULL, "
                        "PRIMARY KEY(project_id,entry_id))"
                    )
                rows = connection.execute(
                    "SELECT project_id,entry_id,payload FROM entries ORDER BY project_id,entry_id"
                ).fetchall()
                counts: dict[str, int] = {}
                entries: list[MemoryEntry] = []
                for project_id, entry_id, payload in rows:
                    entry = _decode(payload)
                    if (entry.project_id, entry.entry_id) != (project_id, entry_id):
                        raise MemoryStoreError("memory_storage_corrupt")
                    counts[project_id] = counts.get(project_id, 0) + 1
                    if counts[project_id] > MAX_PROJECT_ENTRIES:
                        raise MemoryStoreError("memory_project_limit")
                    entries.append(entry)
                backup_name, digest = self._backup(data) if data is not None else (None, None)
                connection.execute(f"PRAGMA user_version={_HISTORY_SCHEMA}")
                self._create_history_schema(connection)
                for entry in entries:
                    self._append_history(connection, None, entry, operation="migration_snapshot")
                self._before_commit(cancelled, stage="before_migration_publish")
                connection.commit()
                self._publish(connection.serialize(), cancelled=cancelled, exclusive=data is None)
            self._after_commit()
            return MemoryMigration(
                changed=True,
                from_version=version,
                to_version=_HISTORY_SCHEMA,
                backup_name=backup_name,
                backup_sha256=digest,
            )
        except sqlite3.Error:
            raise MemoryStoreError("memory_transaction_failed") from None

    async def history_enabled(self) -> bool:
        return await self._run(lambda _: self._history_enabled())

    def _history_enabled(self) -> bool:
        if not self._database.exists():
            return False
        try:
            with closing(self._connection()) as connection:
                self._check_schema(connection)
                return bool(
                    connection.execute("PRAGMA user_version").fetchone()[0] == _HISTORY_SCHEMA
                )
        except sqlite3.Error:
            raise MemoryStoreError("memory_storage_corrupt") from None

    async def history(
        self, project_id: str, entry_id: str, *, after_sequence: int = 0, limit: int = 20
    ) -> MemoryHistoryPage:
        if (
            type(limit) is not int
            or not 1 <= limit <= MAX_HISTORY_PAGE
            or type(after_sequence) is not int
            or after_sequence < 0
        ):
            raise MemoryStoreError("memory_history_query_invalid")
        return await self._run(lambda _: self._history(project_id, entry_id, after_sequence, limit))

    def _history(
        self, project_id: str, entry_id: str, after_sequence: int, limit: int
    ) -> MemoryHistoryPage:
        try:
            with closing(self._connection()) as connection:
                self._require_history(connection)
                rows = connection.execute(
                    "SELECT sequence,operation,before_payload,after_payload FROM memory_history "
                    "WHERE project_id=? AND entry_id=? AND sequence>? ORDER BY sequence LIMIT ?",
                    (project_id, entry_id, after_sequence, limit + 1),
                ).fetchall()
            records: list[MemoryHistoryRecord] = []
            for sequence, operation, before, after in rows[:limit]:
                entry = _decode(after)
                previous = _decode(before) if before is not None else None
                if (entry.project_id, entry.entry_id) != (project_id, entry_id) or (
                    previous is not None
                    and (previous.project_id, previous.entry_id) != (project_id, entry_id)
                ):
                    raise MemoryStoreError("memory_storage_corrupt")
                records.append(MemoryHistoryRecord(sequence, operation, previous, entry))
            return MemoryHistoryPage(
                tuple(records), records[-1].sequence if len(rows) > limit else None
            )
        except sqlite3.Error:
            raise MemoryStoreError("memory_storage_corrupt") from None

    async def revision(
        self, project_id: str, entry_id: str, revision: int, proposal_sha256: str
    ) -> MemoryEntry | None:
        if (
            type(revision) is not int
            or revision < 1
            or re.fullmatch(r"[a-f0-9]{64}", proposal_sha256) is None
        ):
            raise MemoryStoreError("memory_history_query_invalid")
        return await self._run(
            lambda _: self._revision(project_id, entry_id, revision, proposal_sha256)
        )

    def _revision(
        self, project_id: str, entry_id: str, revision: int, proposal_sha256: str
    ) -> MemoryEntry | None:
        try:
            with closing(self._connection()) as connection:
                self._require_history(connection)
                row = connection.execute(
                    "SELECT after_payload FROM memory_history WHERE project_id=? AND entry_id=? "
                    "AND revision=? AND proposal_sha256=? AND status='committed' "
                    "ORDER BY sequence DESC LIMIT 1",
                    (project_id, entry_id, revision, proposal_sha256),
                ).fetchone()
            if row is None:
                return None
            entry = _decode(row[0])
            if (entry.project_id, entry.entry_id, entry.revision, entry.proposal_sha256) != (
                project_id,
                entry_id,
                revision,
                proposal_sha256,
            ) or entry.status is not MemoryStatus.COMMITTED:
                raise MemoryStoreError("memory_storage_corrupt")
            return entry
        except sqlite3.Error:
            raise MemoryStoreError("memory_storage_corrupt") from None

    async def get(self, project_id: str, entry_id: str) -> MemoryEntry | None:
        return await self._run(lambda _: self._get(project_id, entry_id))

    def _get(self, project_id: str, entry_id: str) -> MemoryEntry | None:
        self._checked_path()
        if not self._database.exists():
            return None
        try:
            with closing(self._connection()) as connection:
                self._check_schema(connection)
                row = connection.execute(
                    "SELECT payload FROM entries WHERE project_id=? AND entry_id=?",
                    (project_id, entry_id),
                ).fetchone()
            value = _decode(row[0]) if row else None
            self._checked_path()
            if value is not None and (value.project_id != project_id or value.entry_id != entry_id):
                raise MemoryStoreError("memory_storage_corrupt")
            return value
        except sqlite3.Error:
            raise MemoryStoreError("memory_storage_corrupt") from None

    async def close(self) -> None:
        self._closed = True
        async with self._lock:
            pass

    async def snapshot(self, project_id: str) -> tuple[MemoryEntry, ...]:
        return await self._run(lambda _: self._snapshot(project_id))

    def _snapshot(self, project_id: str) -> tuple[MemoryEntry, ...]:
        self._checked_path()
        if not self._database.exists():
            return ()
        try:
            with closing(self._connection()) as connection:
                self._check_schema(connection)
                rows = connection.execute(
                    "SELECT entry_id, payload FROM entries WHERE project_id=? "
                    "ORDER BY entry_id LIMIT ?",
                    (project_id, MAX_PROJECT_ENTRIES + 1),
                ).fetchall()
            if len(rows) > MAX_PROJECT_ENTRIES:
                raise MemoryStoreError("memory_project_limit")
            entries = tuple(_decode(row[1]) for row in rows)
            self._checked_path()
            if any(
                entry.project_id != project_id or entry.entry_id != row[0]
                for entry, row in zip(entries, rows, strict=True)
            ):
                raise MemoryStoreError("memory_storage_corrupt")
            return entries
        except sqlite3.Error:
            raise MemoryStoreError("memory_storage_corrupt") from None

    async def replace(self, expected: MemoryEntry, updated: MemoryEntry) -> MemoryEntry:
        expected.check_replacement(updated)
        note = parse_memory_note(updated.content)
        if updated.status is MemoryStatus.SUPERSEDED or (
            updated.status is MemoryStatus.COMMITTED and note is not None and note.supersedes
        ):
            raise MemoryStoreError("memory_atomic_replacement_required")
        return await self._run(lambda cancelled: self._replace(expected, updated, cancelled))

    def _replace(
        self, expected: MemoryEntry, updated: MemoryEntry, cancelled: Event
    ) -> MemoryEntry:
        self._checked_path()
        if not self._database.exists():
            raise MemoryStoreError("memory_not_found")
        try:
            with closing(self._connection()) as connection, connection:
                self._check_schema(connection)
                if parse_memory_note(updated.content) is not None:
                    self._require_history(connection)
                connection.execute("BEGIN IMMEDIATE")
                if updated.status in _VERIFIED:
                    # The service's preliminary check is useful feedback, but
                    # the directory lease serializes snapshots and publication
                    # across separate instances and processes.
                    rows = connection.execute(
                        "SELECT payload FROM entries WHERE project_id=? AND entry_id<>? LIMIT ?",
                        (updated.project_id, updated.entry_id, MAX_PROJECT_ENTRIES + 1),
                    ).fetchall()
                    if len(rows) > MAX_PROJECT_ENTRIES:
                        raise MemoryStoreError("memory_project_limit")
                    for row in rows:
                        other = _decode(row[0])
                        if (
                            other.applicable_version == updated.applicable_version
                            and other.topic == updated.topic
                            and other.status in _VERIFIED
                            and other.content_sha256 != updated.content_sha256
                            and not permits_pending_supersession(updated, other)
                        ):
                            raise MemoryStoreError("memory_topic_conflict")
                changed = connection.execute(
                    "UPDATE entries SET payload=? WHERE project_id=? AND entry_id=? AND payload=?",
                    (_encode(updated), expected.project_id, expected.entry_id, _encode(expected)),
                )
                if changed.rowcount != 1:
                    current = connection.execute(
                        "SELECT payload FROM entries WHERE project_id=? AND entry_id=?",
                        (expected.project_id, expected.entry_id),
                    ).fetchone()
                    if not (
                        expected.status is MemoryStatus.APPROVED
                        and updated.status is MemoryStatus.COMMITTED
                        and current is not None
                        and current[0] == _encode(updated)
                    ):
                        raise MemoryStoreError("memory_revision_stale")
                if changed.rowcount == 1 and expected != updated:
                    self._append_history(connection, expected, updated)
                self._before_commit(cancelled)
                connection.commit()
                self._publish(connection.serialize(), cancelled=cancelled)
            self._after_commit()
            return updated
        except sqlite3.Error:
            raise MemoryStoreError("memory_transaction_failed") from None

    async def replace_many(
        self, changes: tuple[tuple[MemoryEntry, MemoryEntry], ...]
    ) -> tuple[MemoryEntry, ...]:
        try:
            check_supersession(changes)
        except MemoryValidationError as exc:
            raise MemoryStoreError(exc.code) from None
        return await self._run(lambda cancelled: self._replace_many(changes, cancelled))

    def _replace_many(
        self, changes: tuple[tuple[MemoryEntry, MemoryEntry], ...], cancelled: Event
    ) -> tuple[MemoryEntry, ...]:
        try:
            with closing(self._connection()) as connection, connection:
                self._require_history(connection)
                connection.execute("BEGIN IMMEDIATE")
                current = [
                    connection.execute(
                        "SELECT payload FROM entries WHERE project_id=? AND entry_id=?",
                        (before.project_id, before.entry_id),
                    ).fetchone()
                    for before, _ in changes
                ]
                if all(
                    row is not None and row[0] == _encode(after)
                    for row, (_, after) in zip(current, changes, strict=True)
                ):
                    return tuple(after for _, after in changes)
                if not all(
                    row is not None and row[0] == _encode(before)
                    for row, (before, _) in zip(current, changes, strict=True)
                ):
                    raise MemoryStoreError("memory_revision_stale")
                for before, after in changes:
                    connection.execute(
                        "UPDATE entries SET payload=? WHERE project_id=? AND entry_id=?",
                        (_encode(after), before.project_id, before.entry_id),
                    )
                    self._append_history(connection, before, after)
                final = tuple(after for _, after in changes)
                committed = next(entry for entry in final if entry.status is MemoryStatus.COMMITTED)
                rows = connection.execute(
                    "SELECT payload FROM entries WHERE project_id=? LIMIT ?",
                    (committed.project_id, MAX_PROJECT_ENTRIES + 1),
                ).fetchall()
                if len(rows) > MAX_PROJECT_ENTRIES:
                    raise MemoryStoreError("memory_project_limit")
                if any(
                    other.entry_id != committed.entry_id
                    and other.applicable_version == committed.applicable_version
                    and other.topic == committed.topic
                    and other.status in _VERIFIED
                    and other.content_sha256 != committed.content_sha256
                    for other in (_decode(row[0]) for row in rows)
                ):
                    raise MemoryStoreError("memory_topic_conflict")
                self._before_commit(cancelled)
                connection.commit()
                self._publish(connection.serialize(), cancelled=cancelled)
            self._after_commit()
            return final
        except sqlite3.Error:
            raise MemoryStoreError("memory_transaction_failed") from None
