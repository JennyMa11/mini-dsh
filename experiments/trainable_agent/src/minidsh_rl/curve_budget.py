"""Explicit cumulative-150/new-stage-100 epoch; immutable prior evidence and IDs."""

import json
from decimal import Decimal
from pathlib import Path
from typing import Literal

from minidsh.infrastructure.locks import FileLease
from minidsh_rl.calibrated_budget import CalibratedBudget
from minidsh_rl.cost_store import CostStore, _Legacy, _restore, _State
from minidsh_rl.learning_schedule import FrozenRecord, atomic_record, digest

TOTAL_LIMIT = Decimal("150")
STAGE_LIMIT = Decimal("100")
UNCERTAINTY = Decimal("2")


class Migration(FrozenRecord):
    schema_version: Literal[1] = 1
    grant: Literal["curve_100_over_50_v1"] = "curve_100_over_50_v1"
    parent: str
    parent_legacy: str
    bindings: dict[str, str]
    carry_sha256: str
    quote: str
    quote_sha256: str
    baseline_committed: Decimal
    historical_ids: tuple[str, ...]


def prepare_migration(parent: Path, audit: Path, directory: Path) -> Migration:
    """Snapshot a fully closed old epoch; never rewrite/raise its original limit."""
    if parent.with_suffix(".curve-150.json").exists():
        raise ValueError("parent epoch already migrated")
    with CalibratedBudget(parent, audit=audit) as previous:
        state = _State.model_validate_json(parent.read_bytes())
        if any(charge.status == "pending" for charge in state.charges.values()):
            raise ValueError("parent has pending operations")
        ids = tuple(sorted(previous._historical_ids | set(state.charges)))
        bindings = {str(path): value for path, value in previous._bindings.items()}
        bindings.update(
            {str(parent): digest(parent), str(previous._legacy): digest(previous._legacy)}
        )
        quote = previous._quote
        baseline = previous.committed
        carry = _Legacy(
            schema_version=1,
            limit=TOTAL_LIMIT,
            spent=baseline - UNCERTAINTY,
            pending={},
            settled=list(ids),
        )
        directory.mkdir(parents=True, exist_ok=False)
        atomic_record(directory / "carry.json", carry, create=True)
        record = Migration(
            parent=str(parent),
            parent_legacy=str(previous._legacy),
            bindings=bindings,
            carry_sha256=digest(directory / "carry.json"),
            quote=str(quote),
            quote_sha256=digest(quote),
            baseline_committed=baseline,
            historical_ids=ids,
        )
        atomic_record(directory / "migration.json", record, create=True)
        _claim(record, directory)
    return record


def _claim(record: Migration, directory: Path) -> None:
    marker = Path(record.parent).with_suffix(".curve-150.json")
    with marker.open("x") as stream:
        json.dump(
            {
                "directory": str(directory.resolve()),
                "migration_sha256": digest(directory / "migration.json"),
            },
            stream,
        )


def claim_existing_migration(directory: Path) -> None:
    """Seal the already confirmed v2 epoch after its original execution, without RPC."""
    record = Migration.model_validate_json((directory / "migration.json").read_bytes())
    with_lease = FileLease(Path(record.parent).with_suffix(".lock"))
    try:
        if any(digest(Path(p)) != h for p, h in record.bindings.items()):
            raise ValueError("migration evidence changed")
        parent = _State.model_validate_json(Path(record.parent).read_bytes())
        current = _State.model_validate_json((directory / "budget.json").read_bytes())
        if (
            _restore(parent).committed != record.baseline_committed
            or current.limit != TOTAL_LIMIT
            or current.historical != record.baseline_committed - UNCERTAINTY
            or any(c.status == "pending" for c in current.charges.values())
        ):
            raise ValueError("cannot seal unknown or mismatched epoch")
        _claim(record, directory)
    finally:
        with_lease.close()


class CurveBudget(CostStore):
    """Hold all predecessor leases and enforce cumulative and incremental limits."""

    def __init__(self, directory: Path) -> None:
        record = Migration.model_validate_json((directory / "migration.json").read_bytes())
        self._bindings = {Path(p): h for p, h in record.bindings.items()}
        self._bindings[directory / "migration.json"] = digest(directory / "migration.json")
        self._bindings[directory / "carry.json"] = record.carry_sha256
        self._bindings[Path(record.quote)] = record.quote_sha256
        parent = _State.model_validate_json(Path(record.parent).read_bytes())
        old_carry = _Legacy.model_validate_json(Path(record.parent_legacy).read_bytes())
        if (
            parent.limit != Decimal("50")
            or parent.uncertainty != UNCERTAINTY
            or old_carry.limit != Decimal("50")
            or old_carry.spent != parent.historical
            or parent.legacy_hash != digest(Path(record.parent_legacy))
            or parent.quote_hash != record.quote_sha256
            or _restore(parent).committed != record.baseline_committed
            or set(record.historical_ids) != set(old_carry.settled) | set(parent.charges)
            or any(c.status == "pending" for c in parent.charges.values())
        ):
            raise ValueError("migration baseline/operation history changed")
        marker = Path(record.parent).with_suffix(".curve-150.json")
        claim = json.loads(marker.read_text())
        if claim["directory"] != str(directory.resolve()) or claim["migration_sha256"] != digest(
            directory / "migration.json"
        ):
            raise ValueError("migration claim evidence changed")
        self._bindings[marker] = digest(marker)
        self._baseline = record.baseline_committed
        self._historical_ids = set(record.historical_ids)
        self._parent_leases: list[FileLease] = []
        try:
            for path in sorted({Path(record.parent), *(Path(p) for p in record.bindings)}):
                self._parent_leases.append(FileLease(path.with_suffix(".lock")))
            self._check_bindings()
            carry = _Legacy.model_validate_json((directory / "carry.json").read_bytes())
            if (
                carry.spent + UNCERTAINTY != self._baseline
                or carry.limit != TOTAL_LIMIT
                or set(carry.settled) != self._historical_ids
                or carry.pending
            ):
                raise ValueError("migration carryover changed")
            super().__init__(
                directory / "budget.json",
                legacy=directory / "carry.json",
                quote=Path(record.quote),
                limit=TOTAL_LIMIT,
                uncertainty=UNCERTAINTY,
            )
        except BaseException:
            self.close()
            raise

    def _check_bindings(self) -> None:
        if any(p.is_symlink() or digest(p) != h for p, h in self._bindings.items()):
            raise ValueError("migration evidence changed")

    def _ready(self) -> None:
        super()._ready()
        self._check_bindings()

    @property
    def available(self) -> Decimal:
        return min(super().available, STAGE_LIMIT - (self.committed - self._baseline))

    def reserve(self, operation: str, maximum: Decimal) -> None:
        if operation in self._historical_ids:
            raise ValueError("duplicate historical operation")
        if maximum > self.available:
            raise ValueError("new-stage allocation exceeded")
        super().reserve(operation, maximum)

    def close(self) -> None:
        if hasattr(self, "_lease"):
            super().close()
        for lease in self._parent_leases:
            lease.close()
