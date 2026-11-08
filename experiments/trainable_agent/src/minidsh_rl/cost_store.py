"""Durable expansion spending, bound to an immutable legacy ledger and quote."""

import hashlib
import os
from decimal import Decimal
from pathlib import Path
from tempfile import NamedTemporaryFile
from types import TracebackType
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict

from minidsh.infrastructure.locks import FileLease
from minidsh_rl.cost_model import Charge, CostAccount, ReconciledBill

_SHA256_LENGTH = 64


class _Legacy(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema_version: Literal[1]
    limit: Decimal
    spent: Decimal
    pending: dict[str, Decimal]
    settled: list[str]


class _Charge(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    amount: Decimal
    status: Literal["pending", "estimated", "billed"]


class _Bill(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    operations: tuple[str, ...]
    amount: Decimal
    evidence_sha256: str


class _State(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema_version: Literal[2] = 2
    limit: Decimal
    historical: Decimal
    uncertainty: Decimal
    legacy_hash: str
    quote_hash: str
    charges: dict[str, _Charge]
    bills: dict[str, _Bill]


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _restore(state: _State) -> CostAccount:
    account = CostAccount(state.limit, state.historical, state.uncertainty)
    covered: set[str] = set()
    for key, bill in state.bills.items():
        if (
            not key
            or not bill.operations
            or len(set(bill.operations)) != len(bill.operations)
            or covered.intersection(bill.operations)
            or bill.amount < 0
            or len(bill.evidence_sha256) != _SHA256_LENGTH
            or any(c not in "0123456789abcdef" for c in bill.evidence_sha256)
        ):
            raise ValueError("invalid bill coverage")
        covered.update(bill.operations)
    billed = {key for key, charge in state.charges.items() if charge.status == "billed"}
    if covered != billed:
        raise ValueError("bill coverage does not match charges")
    for key, charge in state.charges.items():
        if not key or charge.amount < 0 or (charge.status == "billed" and charge.amount != 0):
            raise ValueError("invalid bill or charge amount")
        if charge.status == "pending" and charge.amount == 0:
            raise ValueError("invalid pending reservation")
        account._charges[key] = Charge(charge.amount, charge.status)
    account._bills.update(
        {
            key: ReconciledBill(b.operations, b.amount, b.evidence_sha256)
            for key, b in state.bills.items()
        }
    )
    return account


class CostStore:
    """Hold both leases; persist admission before RPC and fail closed after disk errors.

    This store never edits or reduces the historical ledger. A future audited
    historical reconciliation must use a new explicit protocol, not reset files.
    """

    def __init__(
        self, path: Path, *, legacy: Path, quote: Path, limit: Decimal, uncertainty: Decimal
    ) -> None:
        if path.resolve() in (legacy.resolve(), quote.resolve()):
            raise ValueError("ledger must not overwrite evidence")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lease = FileLease(path.with_suffix(".lock"))
        self._legacy_lease: FileLease | None = None
        self._closed = self._poisoned = False
        self.path, self._legacy, self._quote = path, legacy, quote
        try:
            self._legacy_lease = FileLease(legacy.with_suffix(".lock"))
            old = _Legacy.model_validate_json(legacy.read_bytes())
            if old.pending:
                raise ValueError("legacy has pending operations")
            if old.limit != limit or old.spent < 0:
                raise ValueError("legacy budget configuration changed")
            self._legacy_hash, self._quote_hash = _hash(legacy), _hash(quote)
            if path.exists():
                state = _State.model_validate_json(path.read_bytes())
                if (
                    state.limit != limit
                    or state.historical != old.spent
                    or state.uncertainty != uncertainty
                    or state.legacy_hash != self._legacy_hash
                    or state.quote_hash != self._quote_hash
                ):
                    raise ValueError("budget configuration changed")
                self._account = _restore(state)
            else:
                self._account = CostAccount(limit, old.spent, uncertainty)
                self._save()
        except BaseException:
            self.close()
            raise

    @property
    def available(self) -> Decimal:
        """Retain pending amounts and observed overruns across controller restarts."""
        self._ready()
        return self._account.available

    @property
    def committed(self) -> Decimal:
        """Total legacy, new charges and uncertainty reservation."""
        self._ready()
        return self._account.committed

    def _ready(self) -> None:
        if self._closed or self._poisoned:
            raise ValueError("cost store closed or poisoned; audit and reopen")
        if _hash(self._legacy) != self._legacy_hash or _hash(self._quote) != self._quote_hash:
            raise ValueError("budget configuration evidence changed")

    def _save(self) -> None:
        state = _State(
            limit=self._account.limit,
            historical=self._account.historical,
            uncertainty=self._account.uncertainty,
            legacy_hash=self._legacy_hash,
            quote_hash=self._quote_hash,
            charges={
                key: _Charge(amount=c.amount, status=c.status)
                for key, c in self._account.charges.items()
            },
            bills={
                key: _Bill(
                    operations=b.operations, amount=b.amount, evidence_sha256=b.evidence_sha256
                )
                for key, b in self._account.bills.items()
            },
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

    def _persist(self) -> None:
        try:
            self._save()
        except BaseException:
            self._poisoned = True
            raise

    def reserve(self, operation: str, maximum: Decimal) -> None:
        """Durable pre-RPC admission; the caller must not submit when this raises."""
        self._ready()
        self._account.reserve(operation, maximum)
        self._persist()

    def settle(self, operation: str, estimate: Decimal) -> None:
        """Persist known usage; an unknown response must never call this method."""
        self._ready()
        self._account.settle(operation, estimate)
        self._persist()

    def reconcile(
        self, bill_id: str, operations: tuple[str, ...], amount: Decimal, evidence: Path
    ) -> None:
        """Only an external auditor may attest operation coverage before this call."""
        self._ready()
        self._account.reconcile(bill_id, operations, amount, _hash(evidence))
        self._persist()

    def close(self) -> None:
        """Release leases without cancelling or forgetting pending operations."""
        self._closed = True
        if self._legacy_lease is not None:
            self._legacy_lease.close()
        self._lease.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
