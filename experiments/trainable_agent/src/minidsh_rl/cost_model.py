"""Precise offline cost contracts; not yet an authoritative RPC spending store.

Historical v1 safety accounting stays untouched. Reconciliation requires an
external auditor to establish operation-to-bill coverage; a hash alone cannot
establish that a workspace bill covers a particular operation.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType
from typing import Literal

_MILLION = Decimal(1000000)
_DIGEST_LENGTH = 64


def _amount(value: Decimal) -> None:
    if not value.is_finite() or value < 0:
        raise ValueError("invalid cost")


@dataclass(frozen=True, slots=True)
class PriceCard:
    """CNY per million tokens; actual provider minimum fees must be added separately."""

    input_per_million: Decimal
    output_per_million: Decimal
    training_per_million: Decimal

    def __post_init__(self) -> None:
        for value in (self.input_per_million, self.output_per_million, self.training_per_million):
            _amount(value)

    def estimate(self, *, inputs: int = 0, outputs: int = 0, training: int = 0) -> Decimal:
        """Retain sub-cent precision; never infer cached usage from placeholder zeros."""
        counts = (inputs, outputs, training)
        if any(type(count) is not int or count < 0 for count in counts):
            raise ValueError("invalid token count")
        return (
            inputs * self.input_per_million
            + outputs * self.output_per_million
            + training * self.training_per_million
        ) / _MILLION


@dataclass(frozen=True, slots=True)
class Charge:
    """Pending upper bound or known estimated usage; billed operations carry zero here."""

    amount: Decimal
    status: Literal["pending", "estimated", "billed"]


@dataclass(frozen=True, slots=True)
class ReconciledBill:
    """Auditor-supplied mapping, retained so repeated reconciliation cannot hide spend."""

    operations: tuple[str, ...]
    amount: Decimal
    evidence_sha256: str


@dataclass(slots=True)
class CostAccount:
    """Single-controller accounting contract; durable storage is a separate stage gate."""

    limit: Decimal
    historical: Decimal
    uncertainty: Decimal
    _charges: dict[str, Charge] = field(default_factory=dict, init=False)
    _bills: dict[str, ReconciledBill] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        for value in (self.limit, self.historical, self.uncertainty):
            _amount(value)
        if not self.limit:
            raise ValueError("zero budget")

    @property
    def charges(self) -> Mapping[str, Charge]:
        """Expose immutable values without allowing callers to release reservations."""
        return MappingProxyType(self._charges)

    @property
    def bills(self) -> Mapping[str, ReconciledBill]:
        """Return auditable reconciliation records."""
        return MappingProxyType(self._bills)

    @property
    def committed(self) -> Decimal:
        """Count historical, unsettled, billed and uncertainty exactly once."""
        return (
            self.historical
            + self.uncertainty
            + sum((charge.amount for charge in self._charges.values()), Decimal(0))
            + sum((bill.amount for bill in self._bills.values()), Decimal(0))
        )

    @property
    def available(self) -> Decimal:
        """Negative availability preserves observed overruns rather than clamping them."""
        return self.limit - self.committed

    def reserve(self, operation: str, maximum: Decimal) -> None:
        """Check admission before sending an RPC; unknown operations stay pending."""
        _amount(maximum)
        if not operation or not maximum:
            raise ValueError("invalid reservation")
        if operation in self._charges:
            raise ValueError("duplicate operation")
        if maximum > self.available:
            raise ValueError("budget exhausted")
        self._charges[operation] = Charge(maximum, "pending")

    def settle(self, operation: str, estimate: Decimal) -> None:
        """Record returned usage even when it exceeded the reservation."""
        _amount(estimate)
        if operation not in self._charges or self._charges[operation].status != "pending":
            raise ValueError("not a pending operation")
        self._charges[operation] = Charge(estimate, "estimated")

    def reconcile(
        self, bill_id: str, operations: tuple[str, ...], amount: Decimal, evidence_sha256: str
    ) -> None:
        """Replace only explicitly mapped estimates, atomically after all validation.

        Callers must verify bill ownership, time range, model and full coverage.
        Unattributed workspace bills cannot invoke this method. This contract
        deliberately has no API to release historical liability.
        """
        _amount(amount)
        if not bill_id or bill_id in self._bills:
            raise ValueError("invalid or duplicate bill")
        if not operations or len(set(operations)) != len(operations):
            raise ValueError("invalid operation coverage")
        if len(evidence_sha256) != _DIGEST_LENGTH or any(
            c not in "0123456789abcdef" for c in evidence_sha256
        ):
            raise ValueError("invalid evidence hash")
        for operation in operations:
            if operation not in self._charges:
                raise ValueError("unknown operation")
            if self._charges[operation].status == "pending":
                raise ValueError("pending operation not reconciled")
            if self._charges[operation].status == "billed":
                raise ValueError("operation already reconciled")
        self._bills[bill_id] = ReconciledBill(operations, amount, evidence_sha256)
        for operation in operations:
            self._charges[operation] = Charge(Decimal(0), "billed")
