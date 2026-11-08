"""Account transitions must be irreversible references, never silent spending resets."""

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest
from minidsh_rl.calibrated_budget import CalibratedBudget


def fixture(root: Path) -> Path:
    parent = root / "old.json"
    parent.write_text("{}")
    bill = root / "bill.json"
    bill.write_text('{"scope":"test-only"}')
    carry = root / "carry.json"
    carry.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "limit": "50",
                "spent": "14",
                "pending": {},
                "settled": ["old_rpc"],
            }
        )
    )
    quote = root / "quote.json"
    quote.write_text("{}")

    def digest(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()

    audit = root / "audit.json"
    audit.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "policy": "per_category_milli_upper_no_cache_v1",
                "limit_cny": "50",
                "uncertainty_cny": "2",
                "parents": {str(parent): digest(parent)},
                "quote": str(quote),
                "quote_hash": digest(quote),
                "carryover": str(carry),
                "carryover_hash": digest(carry),
                "carried_cny": "14",
                "billing_observations": {str(bill): digest(bill)},
            }
        )
    )
    return audit


def test_prior_operations_stay_spent_and_restart_preserves_new_holds(tmp_path: Path) -> None:
    audit = fixture(tmp_path)
    with CalibratedBudget(tmp_path / "budget.json", audit=audit) as store:
        assert store.available == Decimal("34")
        with pytest.raises(ValueError, match="historical"):
            store.reserve("old_rpc", Decimal(".01"))
        store.reserve("new_rpc", Decimal(".01"))
    with CalibratedBudget(tmp_path / "budget.json", audit=audit) as store:
        assert store.available == Decimal("33.99")
        with pytest.raises(ValueError, match="duplicate"):
            store.reserve("new_rpc", Decimal(".01"))


def test_changed_parent_or_evidence_rejected(tmp_path: Path) -> None:
    audit = fixture(tmp_path)
    with CalibratedBudget(tmp_path / "budget.json", audit=audit) as store:
        (tmp_path / "old.json").write_text('{"unexpected":true}')
        with pytest.raises(ValueError, match="evidence changed"):
            store.reserve("new", Decimal(".01"))
    with pytest.raises(ValueError, match="evidence changed"):
        CalibratedBudget(tmp_path / "budget.json", audit=audit)
