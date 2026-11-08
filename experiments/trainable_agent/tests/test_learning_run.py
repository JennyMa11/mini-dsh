"""Config/source/numerical admission precedes any provider factory or preflight."""

import json
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pytest
from minidsh_rl import learning_run
from minidsh_rl.cost_store import CostStore
from minidsh_rl.learning_components import ComponentEvidence
from minidsh_rl.learning_execution import Checkpoint
from minidsh_rl.learning_rollout import LearningRollout
from minidsh_rl.learning_run import guarded_run, inspect_run, prepare_run
from minidsh_rl.learning_schedule import FundingPolicy


def prepared(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    funding_policy: FundingPolicy = "full_remaining_v1",
) -> Path:
    tokenizer = tmp_path / "tokenizer.json"
    tokenizer.write_text(json.dumps({"vocabulary_hash": "a" * 64, "template_hash": "b" * 64}))
    quote = tmp_path / "quote.json"
    quote.write_text(
        json.dumps(
            {
                "prices": {
                    "currency": "CNY",
                    "token_unit": "per_1m_tokens",
                    "items": [
                        {
                            "display_name": "Qwen/Qwen3.5-4B",
                            "prices": {
                                k: {"unit_price": v, "billing_unit": "per_1m_tokens"}
                                for k, v in [("prefill", 149), ("sample", 454), ("train", 454)]
                            },
                        }
                    ],
                }
            }
        )
    )
    components = tmp_path / "components.json"
    components.write_text("{}")
    monkeypatch.setattr(
        learning_run,
        "verify_components",
        lambda _p: ComponentEvidence(kernels={}, inputs={}, metrics={}),
    )
    directory = tmp_path / "study"
    prepare_run(
        directory,
        tokenizer=tokenizer,
        quote=quote,
        components=components,
        kind="contract_fixture",
        funding_policy=funding_policy,
    )
    return directory


def store(tmp_path: Path, historical: str) -> CostStore:
    old = tmp_path / "old.json"
    old.write_text(
        json.dumps(
            {"schema_version": 1, "limit": "50", "spent": historical, "pending": {}, "settled": []}
        )
    )
    return CostStore(
        tmp_path / "budget.json",
        legacy=old,
        quote=tmp_path / "quote.json",
        limit=Decimal("50"),
        uncertainty=Decimal(0),
    )


async def test_shortfall_refuses_before_any_preflight_or_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = prepared(tmp_path, monkeypatch)
    calls = []

    async def hook(*_args: object) -> None:
        calls.append("preflight")

    def factory() -> NeverRuntime:
        calls.append("factory")
        return NeverRuntime()

    with store(tmp_path, "42.667") as budget:
        before = budget.path.read_bytes()
        with pytest.raises(ValueError, match="budget"):
            await guarded_run(directory, budget, factory=factory, preflight=hook)
        report = inspect_run(directory, budget.path)
        assert report["execution_admitted"] is False
        assert report["shortfall_cny"] == "12.969"
        assert budget.path.read_bytes() == before
    assert calls == []


class NeverRuntime:
    execution_kind: Literal["contract_fixture"] = "contract_fixture"

    async def initialize(self, _directory: Path) -> Checkpoint:
        raise RuntimeError("unit stop at initialization")

    async def restore(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("unused")

    async def rollout(self, *_args: object, **_kwargs: object) -> LearningRollout:
        raise AssertionError("unused")

    async def feedback(self, *_args: object, **_kwargs: object) -> bool:
        raise AssertionError("unused")

    async def update(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("unused")


async def test_funded_contract_entry_checks_preflight_before_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = prepared(tmp_path, monkeypatch)
    calls = []

    async def hook(*_args: object) -> None:
        calls.append("preflight")

    def factory() -> NeverRuntime:
        calls.append("factory")
        return NeverRuntime()

    with store(tmp_path, "0") as budget, pytest.raises(RuntimeError, match="unit stop"):
        await guarded_run(directory, budget, factory=factory, preflight=hook)
    assert calls == ["preflight", "factory"]


@pytest.mark.parametrize("target", ["execution-config.json", "tokenizer.json", "components.json"])
def test_changed_config_or_evidence_refuses_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    directory = prepared(tmp_path, monkeypatch)
    path = directory / target if target == "execution-config.json" else tmp_path / target
    with path.open("a") as stream:
        stream.write(" ")
    with store(tmp_path, "0") as budget, pytest.raises(ValueError, match="changed"):
        inspect_run(directory, budget.path)


def test_funding_mode_cannot_be_changed_by_editing_only_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = prepared(tmp_path, monkeypatch)
    plan = directory / "plan.json"
    data = json.loads(plan.read_text())
    data["funding_policy"] = "confirmed_stage_v1"
    plan.write_text(json.dumps(data))
    with store(tmp_path, "42.667") as budget, pytest.raises(ValueError, match="funding"):
        inspect_run(directory, budget.path)


async def test_staged_guarded_entry_uses_next_stage_but_never_claims_full_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = prepared(tmp_path, monkeypatch, "confirmed_stage_v1")
    calls = []

    async def hook(*_args: object) -> None:
        calls.append("preflight")

    def factory() -> NeverRuntime:
        calls.append("factory")
        return NeverRuntime()

    with store(tmp_path, "42.667") as budget:
        report = inspect_run(directory, budget.path)
        assert report["financial_capacity"] is False
        assert report["next_stage_financial_capacity"] is True
        assert report["execution_admitted"] is False
        with pytest.raises(RuntimeError, match="unit stop"):
            await guarded_run(directory, budget, factory=factory, preflight=hook)
    assert calls == ["preflight", "factory"]
