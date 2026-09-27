"""Pure in-memory session facts for embedded and deterministic runs."""

from minidsh.domain.session import MemorySessionLog
from minidsh.ports import SessionLog


class MemorySessionJournal:
    """Keep independent run logs with exclusive creation."""

    def __init__(self) -> None:
        self._logs: dict[str, MemorySessionLog] = {}

    async def create(self, run_id: str) -> SessionLog:
        """Create a log without erasing any previous run."""
        if run_id in self._logs:
            raise ValueError("session log already exists")
        log = MemorySessionLog(run_id)
        self._logs[run_id] = log
        return log

    async def open(self, run_id: str) -> SessionLog:
        """Read facts without replaying side effects."""
        return self._logs[run_id]
