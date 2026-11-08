"""Single-writer durable spending reservations, including ambiguous RPC outcomes."""

import os
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from tempfile import NamedTemporaryFile
from types import TracebackType

from pydantic import BaseModel, ConfigDict

from minidsh.infrastructure.locks import FileLease
from minidsh_rl.contracts import Ledger


class _State(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = 1
    limit: Decimal
    spent: Decimal
    pending: dict[str, Decimal]
    settled: list[str]


def cost_upper_bound(inputs: int, outputs: int, training: int) -> Decimal:
    """Round each category up to a cent, conservatively ignoring cache savings."""
    if min(inputs, outputs, training) < 0:
        raise ValueError("negative token count")
    return sum(
        (
            (Decimal(count) * price / Decimal(1000000)).quantize(Decimal(".01"), ROUND_CEILING)
            for count, price in (
                (inputs, Decimal("1.49")),
                (outputs, Decimal("4.54")),
                (training, Decimal("4.54")),
            )
        ),
        Decimal("0"),
    )


class BudgetStore:
    """Hold the OS lease for the entire controller lifetime; save before RPC."""

    def __init__(self, path: Path, limit: Decimal) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lease = FileLease(path.with_suffix(".lock"))
        try:
            if path.exists():
                state = _State.model_validate_json(path.read_text())
                if state.schema_version != 1 or state.limit != limit:
                    raise ValueError("budget limit changed or unsupported schema")
                values = (state.limit, state.spent, *state.pending.values())
                if not all(value.is_finite() and value >= 0 for value in values):
                    raise ValueError("invalid persisted budget")
                if set(state.pending) & set(state.settled):
                    raise ValueError("ambiguous duplicate budget operations")
                self.ledger = Ledger(limit, state.spent, state.pending, set(state.settled))
            else:
                if not limit.is_finite() or limit <= 0:
                    raise ValueError("invalid budget limit")
                self.ledger = Ledger(limit)
                self._save()
        except BaseException:
            self._lease.close()
            raise

    def _save(self) -> None:
        state = _State(
            limit=self.ledger.limit,
            spent=self.ledger.spent,
            pending=self.ledger.pending,
            settled=sorted(self.ledger.settled),
        )
        with NamedTemporaryFile(mode="w", dir=self.path.parent, delete=False) as file:
            temporary = Path(file.name)
            try:
                file.write(state.model_dump_json(indent=2) + "\n")
                file.flush()
                os.fsync(file.fileno())
                temporary.replace(self.path)
            finally:
                temporary.unlink(missing_ok=True)

    def reserve(self, operation: str, maximum: Decimal) -> None:
        """Durably reserve before sending; do not release on timeout/cancellation."""
        self.ledger.reserve(operation, maximum)
        self._save()

    def settle(self, operation: str, actual: Decimal) -> None:
        """Record known consumption; overruns remain visible and block new work."""
        self.ledger.settle(operation, actual)
        self._save()

    def __enter__(self) -> "BudgetStore":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._lease.close()
