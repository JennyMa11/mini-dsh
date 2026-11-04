"""Versioned append-only stable conversations with single-writer platform leases."""

import json
import os
import uuid
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from minidsh.adapters.session_codec import StoredMessage, message_to_json
from minidsh.domain import Message, Role, RunResult, SessionMessageEvent, ToolCall, redact_value
from minidsh.domain.harness import SessionSummary
from minidsh.infrastructure import WorkspaceSandbox
from minidsh.infrastructure.locks import FileLease
from minidsh.ports import SessionLog


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"] = "1.0"
    session_id: str
    sequence: int
    kind: Literal["created", "message", "turn_started", "turn_finished"]
    run_id: str = ""
    step_id: int = 0
    occurred_at: datetime
    message: StoredMessage | None = None
    status: str = ""
    parent_session_id: str | None = None
    parent_run_id: str | None = None


def _message(stored: StoredMessage) -> Message:
    return Message(
        Role(stored.role),
        stored.content,
        stored.name,
        stored.tool_call_id,
        tuple(ToolCall(call.call_id, call.name, call.arguments) for call in stored.tool_calls),
        stored.reasoning_content,
    )


def _read(path: Path, identifier: str) -> list[_Record]:
    contents = path.read_text(encoding="utf-8")
    if not contents or not contents.endswith("\n"):
        raise ValueError("incomplete conversation log")
    try:
        records = [_Record.model_validate_json(line) for line in contents.splitlines()]
    except ValidationError:
        raise ValueError("invalid or unsupported conversation format") from None
    if records[0].kind != "created":
        raise ValueError("conversation header missing")
    for index, record in enumerate(records, 1):
        if record.session_id != identifier or record.sequence != index:
            raise ValueError("invalid conversation identity or sequence")
    return records


class ConversationLog:
    def __init__(self, path: Path, identifier: str, run_id: str, records: list[_Record]) -> None:
        self.path, self.session_id, self.run_id, self.records = path, identifier, run_id, records

    def record(
        self,
        kind: Literal["created", "message", "turn_started", "turn_finished"],
        *,
        message: Message | None = None,
        step_id: int = 0,
        status: str = "",
    ) -> _Record:
        payload = _Record(
            session_id=self.session_id,
            sequence=len(self.records) + 1,
            kind=kind,
            run_id=self.run_id,
            step_id=step_id,
            occurred_at=datetime.now(UTC),
            message=StoredMessage.model_validate(message_to_json(message)) if message else None,
            status=status,
        )
        encoded = (
            json.dumps(
                redact_value(payload.model_dump(mode="json")), ensure_ascii=False, sort_keys=True
            )
            + "\n"
        )
        committed = _Record.model_validate_json(encoded)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        self.records.append(committed)
        return committed

    async def append(self, step_id: int, message: Message) -> SessionMessageEvent:
        record = self.record("message", message=message, step_id=step_id)
        assert record.message is not None
        return SessionMessageEvent(
            self.run_id, record.sequence, step_id, _message(record.message), record.occurred_at
        )

    def messages(self) -> tuple[Message, ...]:
        result: list[Message] = []
        for record in self.records:
            if record.message is not None:
                message = _message(record.message)
                if message.name == "system_prompt_updated" and result:
                    result[0] = Message(Role.SYSTEM, message.content)
                else:
                    result.append(message)
        return tuple(result)

    def events(self) -> tuple[SessionMessageEvent, ...]:
        return tuple(
            SessionMessageEvent(
                record.run_id,
                record.sequence,
                record.step_id,
                _message(record.message),
                record.occurred_at,
            )
            for record in self.records
            if record.message is not None
        )


class _Turn:
    def __init__(self, log: ConversationLog, lease: FileLease) -> None:
        self.log: SessionLog = log
        self._conversation_log, self._lease = log, lease
        self._finished = False

    async def finish(self, result: RunResult) -> None:
        if not self._finished:
            self._conversation_log.record("turn_finished", status=result.status.value)
            self._finished = True

    def close(self) -> None:
        self._lease.close()


class JsonConversationRepository:
    def __init__(self, workspace: Path) -> None:
        self._sandbox = WorkspaceSandbox(workspace)

    def _root(self) -> Path:
        return self._sandbox.resolve(".minidsh/conversations")

    def _path(self, identifier: str) -> Path:
        if not identifier or not identifier.replace("-", "").replace("_", "").isalnum():
            raise ValueError("invalid session identity")
        return self._sandbox.resolve(f".minidsh/conversations/{identifier}/events.v1.jsonl")

    async def create(self, seed: tuple[Message, ...]) -> str:
        return self._create(seed)

    async def import_run(self, result: RunResult) -> str:
        identifier = self._create((), parent_run=result.run_id)
        turn = await self.begin(identifier, result.run_id)
        try:
            for message in result.history:
                await turn.log.append(0, message)
            await turn.finish(result)
        finally:
            turn.close()
        return identifier

    def _create(
        self,
        seed: tuple[Message, ...],
        *,
        parent_session: str | None = None,
        parent_run: str | None = None,
    ) -> str:
        identifier = uuid.uuid4().hex
        path = self._path(identifier)
        path.parent.mkdir(parents=True, exist_ok=False)
        header = _Record(
            session_id=identifier,
            sequence=1,
            kind="created",
            occurred_at=datetime.now(UTC),
            parent_session_id=parent_session,
            parent_run_id=parent_run,
        )
        with path.open("x", encoding="utf-8") as handle:
            handle.write(header.model_dump_json() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        log = ConversationLog(path, identifier, "", [header])
        for message in seed:
            log.record("message", message=message)
        return identifier

    async def begin(self, session_id: str, run_id: str) -> _Turn:
        path = self._path(session_id)
        lease = FileLease(self._sandbox.resolve(f".minidsh/conversations/{session_id}/writer.lock"))
        try:
            records = _read(path, session_id)
            if any(record.kind == "turn_started" and record.run_id == run_id for record in records):
                raise ValueError("run already exists in session")
            log = ConversationLog(path, session_id, run_id, records)
            starts = [record.run_id for record in records if record.kind == "turn_started"]
            ends = {record.run_id for record in records if record.kind == "turn_finished"}
            for pending in starts:
                if pending not in ends:
                    log.run_id = pending
                    log.record("turn_finished", status="cancelled")
            log.run_id = run_id
            log.record("turn_started")
            return _Turn(log, lease)
        except BaseException:
            lease.close()
            raise

    async def history(self, session_id: str) -> tuple[Message, ...]:
        path = self._path(session_id)
        return ConversationLog(path, session_id, "", _read(path, session_id)).messages()

    async def list(self) -> tuple[SessionSummary, ...]:
        root = self._root()
        if not root.exists():
            return ()
        summaries = []
        for path in sorted(root.glob("*/events.v1.jsonl")):
            identifier = path.parent.name
            records = _read(self._path(identifier), identifier)
            tasks = [
                record.message.content
                for record in records
                if record.message is not None and record.message.role == "user"
            ]
            turns = [record for record in records if record.kind == "turn_finished"]
            summaries.append(
                SessionSummary(
                    identifier,
                    tasks[0][:80] if tasks else "New session",
                    turns[-1].run_id if turns else None,
                    len(turns),
                    turns[-1].status if turns else "idle",
                    records[0].parent_session_id,
                )
            )
        return tuple(summaries)

    async def find_run(self, run_id: str) -> str | None:
        for summary in await self.list():
            records = _read(self._path(summary.session_id), summary.session_id)
            if any(
                record.run_id == run_id and record.kind == "turn_finished" for record in records
            ):
                return summary.session_id
        return None

    async def fork(self, run_id: str) -> str:
        identifier = await self.find_run(run_id)
        if identifier is None:
            raise ValueError("fork requires a completed conversation turn")
        path = self._path(identifier)
        records = _read(path, identifier)
        stop = next(
            index
            for index, record in enumerate(records)
            if record.run_id == run_id and record.kind == "turn_finished"
        )
        seed = ConversationLog(path, identifier, "", records[: stop + 1]).messages()
        return self._create(deepcopy(seed), parent_session=identifier, parent_run=run_id)
