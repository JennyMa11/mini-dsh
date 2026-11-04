"""Continuous conversation orchestration over injectable storage and loop factories."""

import uuid
from collections.abc import Callable
from dataclasses import replace

from minidsh.domain import Message, RunResult
from minidsh.domain.harness import SessionSummary
from minidsh.ports import AgentRunner, SessionJournal, SessionLog, SessionStore
from minidsh.ports.conversations import ConversationRepository, ConversationTurn


class _BoundJournal:
    def __init__(self, log: SessionLog) -> None:
        self.log = log

    async def create(self, run_id: str) -> SessionLog:
        del run_id
        return self.log

    async def open(self, run_id: str) -> SessionLog:
        del run_id
        return self.log


class _BoundSnapshots:
    def __init__(self, store: SessionStore, turn: ConversationTurn, session_id: str) -> None:
        self.store, self.turn, self.session_id = store, turn, session_id

    async def save(self, result: RunResult) -> None:
        bound = replace(result, session_id=self.session_id)
        await self.turn.finish(bound)
        await self.store.save(bound)

    async def load(self, run_id: str) -> RunResult | None:
        return await self.store.load(run_id)


class ConversationService:
    def __init__(
        self,
        repository: ConversationRepository,
        snapshots: SessionStore,
        factory: Callable[[SessionJournal, SessionStore], AgentRunner],
    ) -> None:
        self.repository, self._snapshots, self._factory = repository, snapshots, factory

    async def run_turn(
        self,
        task: str,
        *,
        session_id: str | None = None,
        run_id: str | None = None,
        seed: tuple[Message, ...] = (),
    ) -> RunResult:
        """Append a turn under one writer lease; cancellation retains committed facts."""
        identifier = session_id or await self.repository.create(seed)
        actual_run = run_id or uuid.uuid4().hex
        turn = await self.repository.begin(identifier, actual_run)
        try:
            snapshots = _BoundSnapshots(self._snapshots, turn, identifier)
            agent = self._factory(_BoundJournal(turn.log), snapshots)
            result = await agent.run(task, run_id=actual_run, prior_history=turn.log.messages())
            return replace(result, session_id=identifier)
        finally:
            turn.close()

    async def list(self) -> tuple[SessionSummary, ...]:
        return await self.repository.list()

    async def history(self, session_id: str) -> tuple[Message, ...]:
        return await self.repository.history(session_id)

    async def fork(self, run_id: str) -> str:
        if await self.repository.find_run(run_id) is None:
            await self.resume_run(run_id)
        return await self.repository.fork(run_id)

    async def resume_run(self, run_id: str) -> str:
        """Resolve a stable session, or import an immutable legacy run once."""
        identifier = await self.repository.find_run(run_id)
        if identifier is not None:
            summaries = await self.repository.list()
            summary = next(item for item in summaries if item.session_id == identifier)
            if summary.latest_run_id != run_id:
                return await self.repository.fork(run_id)
            return identifier
        result = await self._snapshots.load(run_id)
        if result is None:
            raise ValueError("no saved run")
        return await self.repository.import_run(result)
