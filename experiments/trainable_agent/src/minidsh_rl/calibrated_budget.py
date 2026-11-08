"""New accounting epoch with immutable, auditable predecessor balances."""

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from minidsh.infrastructure.locks import FileLease
from minidsh_rl.cost_store import CostStore


class _Audit(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    schema_version: Literal[1]
    policy: Literal["per_category_milli_upper_no_cache_v1"]
    limit_cny: Decimal
    uncertainty_cny: Decimal
    parents: dict[str, str]
    quote: str
    quote_hash: str
    carryover: str
    carryover_hash: str
    carried_cny: Decimal
    billing_observations: dict[str, str]


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class CalibratedBudget(CostStore):
    """A fixed new ledger; keep all predecessor locks while any new RPC is possible."""

    def __init__(self, path: Path, *, audit: Path) -> None:
        self._parent_leases: list[FileLease] = []
        self._bindings: dict[Path, str] = {audit: _hash(audit)}
        record = _Audit.model_validate_json(audit.read_bytes())
        if record.limit_cny != Decimal("50") or record.uncertainty_cny != Decimal("2"):
            raise ValueError("budget configuration changed")
        if not record.parents or not record.billing_observations:
            raise ValueError("missing predecessor/billing evidence")
        self._historical_ids: set[str] = set()
        try:
            for parent, digest in record.parents.items():
                p = Path(parent)
                self._parent_leases.append(FileLease(p.with_suffix(".lock")))
                self._bindings[p] = digest
            self._bindings.update({Path(p): h for p, h in record.billing_observations.items()})
            for p, digest in self._bindings.items():
                if _hash(p) != digest:
                    raise ValueError("predecessor evidence changed")
            carry = Path(record.carryover)
            if _hash(carry) != record.carryover_hash:
                raise ValueError("carryover evidence changed")
            prior = json.loads(carry.read_text())
            if Decimal(prior["spent"]) != record.carried_cny or prior["pending"]:
                raise ValueError("carryover amount or pending changed")
            self._historical_ids = set(prior["settled"])
            if _hash(Path(record.quote)) != record.quote_hash:
                raise ValueError("quote evidence changed")
            super().__init__(
                path,
                legacy=carry,
                quote=Path(record.quote),
                limit=record.limit_cny,
                uncertainty=record.uncertainty_cny,
            )
            if self._quote_hash != record.quote_hash:
                raise ValueError("quote evidence changed")
        except BaseException:
            self.close()
            raise

    def _ready(self) -> None:
        super()._ready()
        if any(_hash(p) != digest for p, digest in self._bindings.items()):
            raise ValueError("predecessor evidence changed")

    def reserve(self, operation: str, maximum: Decimal) -> None:
        """A migration cannot make a previously submitted operation eligible again."""
        if operation in self._historical_ids:
            raise ValueError("duplicate historical operation")
        super().reserve(operation, maximum)

    def close(self) -> None:
        if hasattr(self, "_lease"):
            super().close()
        for lease in self._parent_leases:
            lease.close()
