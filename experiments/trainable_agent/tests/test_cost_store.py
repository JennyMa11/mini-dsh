"""Durable accounting must survive uncertain submissions and refuse changed inputs."""

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest
from minidsh_rl.cost_store import CostStore


def inputs(tmp_path: Path) -> tuple[Path, Path]:
    legacy = tmp_path / "legacy.json"
    legacy.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "limit": "50",
                "spent": "31.38",
                "pending": {},
                "settled": ["past"],
            }
        )
    )
    quote = tmp_path / "quote.json"
    quote.write_text('{"version":"test"}')
    return legacy, quote


def open_store(tmp_path: Path) -> CostStore:
    legacy, quote = tmp_path / "legacy.json", tmp_path / "quote.json"
    return CostStore(
        tmp_path / "budget-v2.json",
        legacy=legacy,
        quote=quote,
        limit=Decimal("50"),
        uncertainty=Decimal("2"),
    )


def test_restart_retains_pending_and_never_edits_history(tmp_path: Path) -> None:
    legacy, _ = inputs(tmp_path)
    before = hashlib.sha256(legacy.read_bytes()).hexdigest()
    with open_store(tmp_path) as store:
        store.reserve("unknown", Decimal(".02"))
    with open_store(tmp_path) as store:
        assert store.available == Decimal("16.60")
        with pytest.raises(ValueError, match="duplicate"):
            store.reserve("unknown", Decimal(".02"))
        store.settle("unknown", Decimal(".00000603"))
    with open_store(tmp_path) as store:
        assert store.available == Decimal("16.61999397")
    assert before == hashlib.sha256(legacy.read_bytes()).hexdigest()


def test_second_writer_and_changed_quote_or_limit_are_rejected(tmp_path: Path) -> None:
    _, quote = inputs(tmp_path)
    with open_store(tmp_path), pytest.raises(ValueError, match="busy"):
        open_store(tmp_path)
    with pytest.raises(ValueError, match="configuration"):
        CostStore(
            tmp_path / "budget-v2.json",
            legacy=tmp_path / "legacy.json",
            quote=quote,
            limit=Decimal("100"),
            uncertainty=Decimal("2"),
        )
    quote.write_text('{"changed":true}')
    with pytest.raises(ValueError, match="configuration"):
        open_store(tmp_path)


def test_pending_history_and_corrupted_status_rejected(tmp_path: Path) -> None:
    legacy, _ = inputs(tmp_path)
    old = json.loads(legacy.read_text())
    old["pending"] = {"unknown": ".01"}
    legacy.write_text(json.dumps(old))
    with pytest.raises(ValueError, match="pending"):
        open_store(tmp_path)
    old["pending"] = {}
    legacy.write_text(json.dumps(old))
    with open_store(tmp_path) as store:
        store.reserve("rpc", Decimal(".01"))
    path = tmp_path / "budget-v2.json"
    state = json.loads(path.read_text())
    state["charges"]["rpc"]["status"] = "billed"
    path.write_text(json.dumps(state))
    with pytest.raises(ValueError, match="bill"):
        open_store(tmp_path)


def test_write_failure_poisoned_controller_cannot_send_next_rpc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs(tmp_path)
    with open_store(tmp_path) as store:

        def fail() -> None:
            raise OSError("disk unavailable")

        monkeypatch.setattr(store, "_save", fail)
        with pytest.raises(OSError, match="disk unavailable"):
            store.reserve("rpc", Decimal(".02"))
        with pytest.raises(ValueError, match="poisoned"):
            store.reserve("next", Decimal(".02"))


def test_bill_not_counted_twice_after_restart(tmp_path: Path) -> None:
    inputs(tmp_path)
    evidence = tmp_path / "bill.json"
    evidence.write_text('{"auditor":"explicit-test-coverage"}')
    with open_store(tmp_path) as store:
        store.reserve("rpc", Decimal(".02"))
        store.settle("rpc", Decimal(".01"))
        store.reconcile("bill", ("rpc",), Decimal(".003"), evidence)
    with open_store(tmp_path) as store:
        assert store.committed == Decimal("33.383")
        with pytest.raises(ValueError, match="duplicate"):
            store.reconcile("bill", ("rpc",), Decimal(".003"), evidence)


def test_legacy_writer_cannot_race_new_ledger(tmp_path: Path) -> None:
    from minidsh_rl.budget import BudgetStore  # noqa: PLC0415 - test legacy compatibility

    legacy, _ = inputs(tmp_path)
    with open_store(tmp_path), pytest.raises(ValueError, match="busy"):
        BudgetStore(legacy, Decimal("50"))
