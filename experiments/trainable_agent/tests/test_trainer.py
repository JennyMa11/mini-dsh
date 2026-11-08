"""Measured reduction compensation and no replay of ambiguous optimizer calls."""

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import pytest
from minidsh_rl.budget import BudgetStore
from minidsh_rl.contracts import Turn
from minidsh_rl.datum import build_batch
from minidsh_rl.trainer import Trainer, normalized_batch
from minidsh_rl.transaction import UpdateJournal


@dataclass
class Tensor:
    data: tuple[float, ...] = (0.0, -0.1)


@dataclass
class Response:
    metrics: dict[str, float] = field(default_factory=lambda: {"loss_sum": 0.0})
    loss_fn_outputs: list[dict[str, Tensor]] = field(
        default_factory=lambda: [{"logprobs": Tensor()}]
    )

    def model_dump_json(self, *, indent: int) -> str:
        assert indent == 2
        return '{"metrics":{"loss_sum":0.0}}'


@dataclass
class Future:
    fail: bool = False

    def result(self) -> Response:
        if self.fail:
            raise TimeoutError("unknown optimizer outcome")
        return Response()


@dataclass
class Client:
    fail: bool = False
    calls: int = 0

    def forward_backward(self, _data: list[object], **_kwargs: object) -> Future:
        self.calls += 1
        return Future()

    def optim_step(self, _parameters: object) -> Future:
        self.calls += 1
        return Future(self.fail)


def test_sample_mean_compensation_is_exact_and_does_not_mutate_raw_batch() -> None:
    turn = Turn((1, 2), (3, 4), (-0.1, -0.2))
    batch = build_batch(((turn, turn),), (1.0,))
    normalized = normalized_batch(batch, "sample_mean")
    assert normalized.datums[0].loss_fn_inputs["advantages"].data == [0.0, 0.5, 0.5]
    assert batch.datums[0].loss_fn_inputs["advantages"].data == [0.0, 1.0, 1.0]


def test_update_timeout_retains_budget_and_blocks_duplicate(tmp_path: Path) -> None:
    batch = build_batch(((Turn((1, 2), (3,), (-0.1,)),),), (1.0,))
    with BudgetStore(tmp_path / "budget.json", Decimal("50")) as budget:
        client = Client(fail=True)
        trainer = Trainer(client, budget, tmp_path, "sample_mean")
        with pytest.raises(TimeoutError, match="unknown optimizer"):
            trainer.update(0, batch)
        assert UpdateJournal.load(tmp_path / "update_000.json").state == "optim_submitted"
        assert len(budget.ledger.pending) == 1
        with pytest.raises(FileExistsError):
            trainer.update(0, batch)
        assert client.calls == 2


def test_zero_group_does_not_submit_any_compute(tmp_path: Path) -> None:
    batch = build_batch(((Turn((1, 2), (3,), (-0.1,)),),), (0.0,))
    with BudgetStore(tmp_path / "budget.json", Decimal("50")) as budget:
        client = Client()
        result = Trainer(client, budget, tmp_path, "sample_mean").update(0, batch)
        assert result["updated"] is False
        assert client.calls == 0
        assert budget.ledger.spent == 0


def test_clipped_share_measures_only_unmasked_sampled_actions() -> None:
    from minidsh_rl.trainer import policy_diagnostics  # noqa: PLC0415

    batch = build_batch(((Turn((1, 2), (3,), (-0.1,)),),), (1.0,))
    report = policy_diagnostics(batch, ((-100.0, -0.1),))
    assert report["clipped_share"] == 0
    report = policy_diagnostics(batch, ((-100.0, -2.0),))
    assert report["clipped_share"] == 1
