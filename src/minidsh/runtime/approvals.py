"""Run-scoped approvals that can only restrict an already authorized action."""

import asyncio
from collections.abc import Awaitable, Callable

from minidsh.domain.harness import ApprovalChoice, ApprovalRequest


class DenyApproval:
    async def approve(self, request: ApprovalRequest) -> ApprovalChoice:
        del request
        return ApprovalChoice.DENY


class ApprovalBroker:
    """Non-blocking GUI approval rendezvous, with exact run/target remembered grants."""

    def __init__(self, *, timeout_seconds: float = 120.0) -> None:
        self.timeout_seconds = timeout_seconds
        self._pending: dict[str, tuple[ApprovalRequest, asyncio.Future[ApprovalChoice]]] = {}
        self._remembered: set[tuple[str, str]] = set()
        self.on_change: Callable[[], None] | None = None

    @property
    def pending(self) -> tuple[ApprovalRequest, ...]:
        return tuple(request for request, _ in self._pending.values())

    async def approve(self, request: ApprovalRequest) -> ApprovalChoice:
        if (request.run_id, request.scope) in self._remembered:
            return ApprovalChoice.ALLOW
        future: asyncio.Future[ApprovalChoice] = asyncio.get_running_loop().create_future()
        self._pending[request.request_id] = (request, future)
        if self.on_change is not None:
            self.on_change()
        try:
            async with asyncio.timeout(self.timeout_seconds):
                choice = await future
            if choice is ApprovalChoice.REMEMBER:
                self._remembered.add((request.run_id, request.scope))
            return choice
        except TimeoutError:
            return ApprovalChoice.DENY
        finally:
            self._pending.pop(request.request_id, None)
            if self.on_change is not None:
                self.on_change()

    def respond(self, request_id: str, run_id: str, choice: ApprovalChoice) -> None:
        pending = self._pending.get(request_id)
        if pending is None or pending[0].run_id != run_id or pending[1].done():
            raise ValueError("approval is missing, expired, or belongs to another run")
        pending[1].set_result(choice)

    def release(self, run_id: str) -> None:
        """Forget grants and reject any outstanding request at a run boundary."""
        self._remembered = {grant for grant in self._remembered if grant[0] != run_id}
        for request, future in self._pending.values():
            if request.run_id == run_id and not future.done():
                future.set_result(ApprovalChoice.DENY)


class CallbackApproval:
    """Terminal/embedded approval with the same run-scoped matching as the GUI."""

    def __init__(self, callback: Callable[[ApprovalRequest], Awaitable[ApprovalChoice]]) -> None:
        self._callback = callback
        self._remembered: set[tuple[str, str]] = set()

    async def approve(self, request: ApprovalRequest) -> ApprovalChoice:
        grant = (request.run_id, request.scope)
        if grant in self._remembered:
            return ApprovalChoice.ALLOW
        choice = await self._callback(request)
        if choice is ApprovalChoice.REMEMBER:
            self._remembered.add(grant)
        return choice

    def release(self, run_id: str) -> None:
        self._remembered = {grant for grant in self._remembered if grant[0] != run_id}
